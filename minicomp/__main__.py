"""Command-line interface.

    python3 -m minicomp tokens  FILE    show the token stream (lexer)
    python3 -m minicomp ast     FILE    show the syntax tree (parser)
    python3 -m minicomp symbols FILE    show the symbol table (semantic analysis)
    python3 -m minicomp fmt     FILE    print the program back as formatted source
    python3 -m minicomp run     FILE    check and run with the reference interpreter

Exit status: 0 = success, 1 = compile error, 2 = usage/file error, 3 = step limit hit.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields, is_dataclass

from .errors import CompileError
from .interp import DEFAULT_FUEL, OutOfFuel, run
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="minicomp", description="Mini language compiler")
    ap.add_argument("command", choices=["tokens", "ast", "symbols", "fmt", "run"])
    ap.add_argument("file")
    ap.add_argument("--fuel", type=int, default=DEFAULT_FUEL, help="step limit for run")
    args = ap.parse_args(argv)

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
        for value in run(program, fuel=args.fuel):
            print(value)
        return 0
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
