"""Command-line interface.

    python3 -m minicomp tokens  FILE    show the token stream (lexer)
    python3 -m minicomp ast     FILE    show the syntax tree (parser)
    python3 -m minicomp symbols FILE    show the symbol table (semantic analysis)
    python3 -m minicomp fmt     FILE    print the program back as formatted source
    python3 -m minicomp run     FILE    check and run with the reference interpreter
    python3 -m minicomp tac     FILE [-O1]   show three-address code (optimized with -O1)
    python3 -m minicomp cfg     FILE [-O1]   show basic blocks and the control-flow graph
    python3 -m minicomp vm      FILE [-O1]   compile and run on the TAC virtual machine
    python3 -m minicomp compare FILE    run reference, -O0, -O1 and each pass alone; report differences
    python3 -m minicomp generate --seed N          print the random program for seed N
    python3 -m minicomp fuzz -n COUNT [--seed N] [--out DIR] [--stop-after K]
                                        differential testing campaign over generated programs

Exit status: 0 = success, 1 = compile error, 2 = usage/file error, 3 = step limit hit,
4 = miscompilation found.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields, is_dataclass

from .errors import CompileError
from . import vm
from .cfg import build_cfg
from .interp import DEFAULT_FUEL, OutOfFuel, run
from .lower import lower
from .opt import ALL_PASSES, optimize
from .tac import format_code
from .generator import generate_source
from .harness import campaign, check_program
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="minicomp", description="Mini language compiler")
    ap.add_argument("command", choices=["tokens", "ast", "symbols", "fmt", "run",
                                        "tac", "cfg", "vm", "compare", "generate", "fuzz"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--seed", type=int, default=0, help="generate/fuzz: (first) seed")
    ap.add_argument("-n", type=int, default=200, help="fuzz: number of programs")
    ap.add_argument("--out", help="fuzz: folder to save failing programs in")
    ap.add_argument("--stop-after", type=int, help="fuzz: stop after this many failures")
    ap.add_argument("-O", dest="opt", type=int, choices=[0, 1], default=0,
                    help="optimization level for tac/cfg/vm (default 0)")
    ap.add_argument("--passes", help=f"comma-separated subset of: {','.join(ALL_PASSES)}")
    ap.add_argument("--fuel", type=int, default=DEFAULT_FUEL, help="step limit for run")
    args = ap.parse_args(argv)
    passes = args.passes.split(",") if args.passes else ALL_PASSES

    if args.command == "generate":
        print(generate_source(args.seed), end="")
        return 0
    if args.command == "fuzz":
        report = campaign(args.n, seed=args.seed, passes=passes,
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
                print(f"{s.name:<12} declared at {s.line}:{s.col}  scope depth {s.depth}")
            return 0
        if args.command == "run":
            for value in run(program, fuel=args.fuel):
                print(value)
            return 0

        if args.command == "compare":
            return compare(program, passes)
        code0 = lower(program)
        code = optimize(code0, passes) if (args.opt == 1 or args.passes) else code0
        if args.command == "tac":
            print(format_code(code))
            print(f"\n; {len(code)} instructions (unoptimized: {len(code0)})")
            return 0
        if args.command == "cfg":
            print(build_cfg(code))
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
