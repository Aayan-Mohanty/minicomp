"""Command-line interface.

    python3 -m minicomp tokens  FILE    show the token stream (lexer)
    python3 -m minicomp ast     FILE    show the syntax tree (parser)
    python3 -m minicomp symbols FILE    show the symbol table (semantic analysis)
    python3 -m minicomp fmt     FILE    print the program back as formatted source
    python3 -m minicomp run     FILE    check and run with the reference interpreter
    python3 -m minicomp tac     FILE [-O1]   show three-address code (optimized with -O1)
    python3 -m minicomp cfg     FILE [-O1]   show basic blocks and the control-flow graph of each unit
    python3 -m minicomp vm      FILE [-O1]   compile and run on the TAC virtual machine
    python3 -m minicomp asm     FILE [--registers K]   -O1, then register allocation: machine code
    python3 -m minicomp compare FILE    run reference, -O0, -O1, each pass alone and the machine
                                        code; report differences
    python3 -m minicomp generate --seed N          print the random program for seed N
    python3 -m minicomp fuzz -n COUNT [--seed N] [--out DIR] [--stop-after K]
                                        differential testing campaign over generated programs
    python3 -m minicomp reduce FILE | --seed N  shrink a failing program to a minimal one
    python3 -m minicomp bugs                    list the planted optimizer bugs
    python3 -m minicomp evaluate [-n N] [--out results.json] [--no-reduce]
                                        run the planted-bug evaluation

Options for every command that compiles: --bug NAME plants one compiler bug
(see `bugs`) for that run only. Generator options (generate, fuzz, reduce
--seed): --no-swap (never write a reused expression with swapped operands),
--no-functions (no functions or calls), --no-arrays, --no-globals (functions
use no top-level variables), --no-recursion, --no-loop-control (no `for`,
`break` or `continue`).

Exit status: 0 = success, 1 = compile error, 2 = usage/file error, 3 = step limit hit,
4 = miscompilation found.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields, is_dataclass

from .errors import CompileError
from . import vm
from .cfg import unit_cfgs
from .interp import DEFAULT_FUEL, OutOfFuel, run
from .lower import lower
from .opt import ALL_PASSES, optimize
from .regalloc import K, allocate, run_machine
from .tac import format_code, instruction_count
from .bugs import BUGS, planted
from .evaluate import evaluate
from .generator import GenConfig, generate, generate_source
from .harness import campaign, check_program
from .reduce import NotAFailure, reduce_program
from pathlib import Path
from .lexer import tokenize
from .parser import parse
from .printer import to_source
from .semantic import SemanticErrors, check


def tree_lines(node, indent: str = "", label: str = ""):
    """Readable indented dump of an AST."""
    prefix = f"{indent}{label + ': ' if label else ''}"
    if isinstance(node, list):
        yield f"{prefix}[{len(node)} item(s)]"
        for item in node:
            yield from tree_lines(item, indent + "  ")
        return
    if not is_dataclass(node):
        yield f"{prefix}{node!r}"
        return
    simple = [f"{f.name}={getattr(node, f.name)!r}" for f in fields(node)
              if f.compare and isinstance(getattr(node, f.name), (int, str))]
    pos = f"  @{node.line}:{node.col}" if hasattr(node, "line") and node.line else ""
    yield f"{prefix}{type(node).__name__}({', '.join(simple)}){pos}"
    for f in fields(node):
        value = getattr(node, f.name)
        if f.compare and not isinstance(value, (int, str)) and value is not None:
            yield from tree_lines(value, indent + "  ", f.name)


def compare(program, passes) -> int:
    """Run every configuration on one program and report whether they agree."""
    v = check_program(program, passes)
    if v.status == "timeout":
        print("skipped: the program did not finish within the step limit")
        return 3
    for name, out in v.outputs.items():
        shown = out if isinstance(out, str) else (" ".join(map(str, out)) or "(no output)")
        mark = "  <-- differs" if name in v.failing else ""
        size = f"[{v.code_size[name]:>4} instr]" if name in v.code_size else " " * 12
        print(f"{name:<18} {size}  {shown[:70]}{'...' if len(shown) > 70 else ''}{mark}")
    if v.status == "ok":
        print("\nresult: all outputs match")
        return 0
    print(f"\nresult: {v.status.upper()}; suspect passes: {', '.join(v.suspects) or 'interaction of passes'}")
    return 4


def reduce_cmd(program, name, passes, args) -> int:
    try:
        res = reduce_program(program, passes=passes, max_tests=args.max_tests)
    except NotAFailure:
        print(f"nothing to reduce: all 9 runs of {name} agree"
              + ("" if args.bug else " (plant a bug with --bug NAME to try the reducer)"), file=sys.stderr)
        return 0
    print(res.source, end="")
    b, a = res.before, res.after
    print(f"; {name}: {b['lines']} -> {a['lines']} lines, {b['statements']} -> {a['statements']} statements, "
          f"{res.tests} candidates tested in {res.seconds} s"
          + ("" if res.complete else " (budget reached before a fixed point)"), file=sys.stderr)
    print(f"; still {res.verdict.status}: failing {', '.join(res.verdict.failing)}; "
          f"suspects {', '.join(res.verdict.suspects) or 'passes together'}", file=sys.stderr)
    if args.out:
        Path(args.out).write_text(res.source, encoding="utf-8")
    return 4


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="minicomp", description="Mini language compiler")
    ap.add_argument("command", choices=["tokens", "ast", "symbols", "fmt", "run", "tac", "cfg", "vm", "asm",
                                        "compare", "generate", "fuzz", "reduce", "bugs", "evaluate"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--seed", type=int, default=0, help="generate/fuzz: (first) seed")
    ap.add_argument("-n", type=int, help="fuzz: number of programs (default 200); evaluate: per bug (default 400)")
    ap.add_argument("--out", help="fuzz: folder for failing programs; reduce: file for the result; "
                                  "evaluate: JSON results file")
    ap.add_argument("--stop-after", type=int, help="fuzz: stop after this many failures")
    ap.add_argument("-O", dest="opt", type=int, choices=[0, 1], default=0,
                    help="optimization level for tac/cfg/vm (default 0)")
    ap.add_argument("--passes", help=f"comma-separated subset of: {','.join(ALL_PASSES)}")
    ap.add_argument("--fuel", type=int, default=DEFAULT_FUEL, help="step limit for run")
    ap.add_argument("--bug", help="plant this optimizer bug for the run (evaluate: only this bug)")
    ap.add_argument("--no-swap", action="store_true",
                    help="generator: never write a reused expression with its operands swapped")
    ap.add_argument("--no-functions", action="store_true", help="generator: no functions or calls")
    ap.add_argument("--no-arrays", action="store_true", help="generator: no arrays")
    ap.add_argument("--no-globals", action="store_true", help="generator: functions use no top-level variables")
    ap.add_argument("--no-recursion", action="store_true", help="generator: no recursive functions")
    ap.add_argument("--no-loop-control", action="store_true", help="generator: no for, break or continue")
    ap.add_argument("--registers", type=int, default=K, help=f"asm: number of registers (default {K})")
    ap.add_argument("--no-reduce", action="store_true", help="evaluate: skip reducing the failures")
    ap.add_argument("--max-tests", type=int, default=4000, help="reduce: candidate budget")
    args = ap.parse_args(argv)
    if args.bug and args.bug not in BUGS:
        ap.error(f"unknown bug '{args.bug}'; run `python3 -m minicomp bugs` for the list")
    if args.command == "evaluate":
        return run_evaluation(args)
    if args.bug:
        with planted(args.bug):
            return _main(args, ap)
    return _main(args, ap)


def run_evaluation(args) -> int:
    n = args.n or 400
    report = evaluate(n=n, seed=args.seed, bugs=[args.bug] if args.bug else None,
                      reduce=not args.no_reduce, log=lambda line: print(line, file=sys.stderr, flush=True))
    print(report.markdown())
    for b in report.bugs:
        if b.reduced_source:
            print(f"\n--- {b.name}: reduced program ({b.reduced_from} -> {b.reduced_to} line{'' if b.reduced_to == 1 else 's'}) ---")
            print(b.reduced_source, end="")
    if args.out:
        Path(args.out).write_text(report.to_json(), encoding="utf-8")
        print(f"\nresults written to {args.out}", file=sys.stderr)
    return 0


def _main(args, ap) -> int:
    passes = args.passes.split(",") if args.passes else ALL_PASSES
    gen_config = None
    if (args.no_swap or args.no_functions or args.no_arrays or args.no_globals
            or args.no_recursion or args.no_loop_control):
        gen_config = GenConfig(swap_reuse=0.0 if args.no_swap else GenConfig.swap_reuse,
                               max_functions=0 if args.no_functions else GenConfig.max_functions,
                               arrays=not args.no_arrays, globals=not args.no_globals,
                               recursion=not args.no_recursion, loop_control=not args.no_loop_control)
    if args.command == "bugs":
        width = max(len(b) for b in BUGS) + 2
        for b in BUGS.values():
            print(f"{b.name:<{width}} {b.home:<12} {b.summary}")
        return 0
    if args.command == "reduce" and args.file is None:
        return reduce_cmd(generate(args.seed, gen_config), f"seed {args.seed}", passes, args)

    if args.command == "generate":
        print(generate_source(args.seed, gen_config), end="")
        return 0
    if args.command == "fuzz":
        report = campaign(args.n or 200, seed=args.seed, passes=passes, config=gen_config,
                          out_dir=Path(args.out) if args.out else None,
                          stop_after=args.stop_after)
        print(report.summary())
        return 4 if report.failures else 0
    if args.file is None:
        ap.error(f"'{args.command}' needs a FILE")

    try:
        with open(args.file, encoding="utf-8") as fh:
            source = fh.read()
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        if args.command == "tokens":
            for t in tokenize(source):
                print(f"{t.line:>4}:{t.col:<4} {t.kind:<8} {t.text}")
            return 0
        program = parse(source)
        if args.command == "ast":
            print("\n".join(tree_lines(program)))
            return 0
        if args.command == "fmt":
            print(to_source(program), end="")
            return 0
        table = check(program)
        if args.command == "symbols":
            for s in table.all_symbols:
                where = f"in {s.owner}()" if s.owner else "top level"
                kind = f"array[{s.size}]" if s.kind == "array" else s.kind
                print(f"{s.name:<12} {kind:<11} declared at {s.line}:{s.col}  {where}, scope depth {s.depth}")
            for f, names in table.globals_used.items():
                if names:
                    print(f"{f}() uses globals: {', '.join(sorted(names))}")
            return 0
        if args.command == "run":
            for value in run(program, fuel=args.fuel):
                print(value)
            return 0

        if args.command == "compare":
            return compare(program, passes)
        if args.command == "reduce":
            return reduce_cmd(program, args.file, passes, args)
        code0 = lower(program)
        if args.command == "asm":
            if args.registers < 1:
                ap.error("--registers must be at least 1")
            machine = allocate(optimize(code0, passes), args.registers)
            print("\n".join(machine.text()).lstrip("\n"))
            print(f"\n; {args.registers} registers r0-r{args.registers - 1}, scratch t0 t1", file=sys.stderr)
            for u in machine.units:
                spilled = f"; spilled to memory: {', '.join(u.spilled)}" if u.spilled else ""
                nv, nr = len(u.locations), u.registers_used
                print(f"; {u.name}: {nv} variable{'' if nv == 1 else 's'} in {nr} register{'' if nr == 1 else 's'}"
                      f"{spilled}", file=sys.stderr)
            return 0
        code = optimize(code0, passes) if (args.opt == 1 or args.passes) else code0
        if args.command == "tac":
            print(format_code(code))
            print(f"\n; {instruction_count(code)} instructions, not counting labels "
                  f"(unoptimized: {instruction_count(code0)})")
            return 0
        if args.command == "cfg":
            parts = []
            for unit, graph in unit_cfgs(code):
                head = "main" if unit.header is None else str(unit.header)
                parts.append(f"== {head}\n{graph}")
            print("\n\n".join(parts))
            return 0
        if args.command == "vm":
            res = vm.run(code, fuel=args.fuel)
            for value in res.output:
                print(value)
            print(f"; {res.steps} instructions executed", file=sys.stderr)
            return 0
        raise AssertionError(args.command)
    except CompileError as e:
        print(f"{args.file}:{e}", file=sys.stderr)
        return 1
    except SemanticErrors as e:
        for err in e.errors:
            print(f"{args.file}:{err}", file=sys.stderr)
        return 1
    except OutOfFuel as e:
        for value in e.output:
            print(value)
        print(f"stopped: more than {args.fuel} steps", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
