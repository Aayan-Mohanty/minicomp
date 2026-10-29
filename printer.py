"""AST -> Mini source code (a "pretty printer").

Needed later by the random program generator (to write programs out) and
by the reducer (to show the shrunk program). Parentheses are added only
where precedence or left-associativity requires them, so
parse(to_source(tree)) == tree for every tree the parser can produce.
"""

from __future__ import annotations

from typing import List

from .nodes import (ArrayDecl, Assign, Binary, Block, Break, Call, CallStmt, Continue, For,
                    FunDecl, If, Index, IndexAssign, IntLit, Print, Program, Return, Unary, Var,
                    VarDecl, While)
from .parser import PRECEDENCE

INDENT = "    "


def to_source(program: Program) -> str:
    lines: List[str] = []
    for s in program.body:
        _stmt(s, 0, lines)
    return "\n".join(lines) + "\n"


def expr_source(e) -> str:
    return _expr(e)


def _stmt(s, depth: int, out: List[str]) -> None:
    pad = INDENT * depth
    if isinstance(s, FunDecl):
        out.append(f"{pad}fun {s.name}({', '.join(s.params)}) {{")
        for inner in s.body.body:
            _stmt(inner, depth + 1, out)
        out.append(f"{pad}}}")
    elif isinstance(s, Return):
        out.append(f"{pad}return {_expr(s.value)};")
    elif isinstance(s, CallStmt):
        out.append(f"{pad}{_expr(s.call)};")
    elif isinstance(s, VarDecl):
        out.append(f"{pad}var {s.name} = {_expr(s.init)};")
    elif isinstance(s, ArrayDecl):
        out.append(f"{pad}var {s.name}[{s.size}];")
    elif isinstance(s, Assign):
        out.append(f"{pad}{s.name} = {_expr(s.value)};")
    elif isinstance(s, IndexAssign):
        out.append(f"{pad}{s.name}[{_expr(s.index)}] = {_expr(s.value)};")
    elif isinstance(s, Break):
        out.append(f"{pad}break;")
    elif isinstance(s, Continue):
        out.append(f"{pad}continue;")
    elif isinstance(s, For):
        init = ""
        if isinstance(s.init, VarDecl):
            init = f"var {s.init.name} = {_expr(s.init.init)}"
        elif isinstance(s.init, Assign):
            init = f"{s.init.name} = {_expr(s.init.value)}"
        update = f"{s.update.name} = {_expr(s.update.value)}" if s.update is not None else ""
        out.append(f"{pad}for ({init}; {_expr(s.cond)}; {update}) {{")
        for inner in s.body.body:
            _stmt(inner, depth + 1, out)
        out.append(f"{pad}}}")
    elif isinstance(s, Print):
        out.append(f"{pad}print {_expr(s.value)};")
    elif isinstance(s, Block):
        out.append(f"{pad}{{")
        for inner in s.body:
            _stmt(inner, depth + 1, out)
        out.append(f"{pad}}}")
    elif isinstance(s, If):
        _if(s, depth, out, pad)
    elif isinstance(s, While):
        out.append(f"{pad}while ({_expr(s.cond)}) {{")
        for inner in s.body.body:
            _stmt(inner, depth + 1, out)
        out.append(f"{pad}}}")
    else:
        raise TypeError(f"unknown statement {type(s).__name__}")


def _if(s: If, depth: int, out: List[str], prefix: str) -> None:
    out.append(f"{prefix}if ({_expr(s.cond)}) {{")
    for inner in s.then.body:
        _stmt(inner, depth + 1, out)
    pad = INDENT * depth
    if s.orelse is None:
        out.append(f"{pad}}}")
    elif isinstance(s.orelse, If):
        _if(s.orelse, depth, out, f"{pad}}} else ")
    else:
        out.append(f"{pad}}} else {{")
        for inner in s.orelse.body:
            _stmt(inner, depth + 1, out)
        out.append(f"{pad}}}")


def _expr(e) -> str:
    if isinstance(e, IntLit):
        return str(e.value)
    if isinstance(e, Var):
        return e.name
    if isinstance(e, Call):
        return f"{e.name}({', '.join(_expr(a) for a in e.args)})"
    if isinstance(e, Index):
        return f"{e.name}[{_expr(e.index)}]"
    if isinstance(e, Unary):
        inner = _expr(e.operand)
        if not isinstance(e.operand, (IntLit, Var, Unary, Call, Index)):
            inner = f"({inner})"
        return f"{e.op}{inner}"
    if isinstance(e, Binary):
        p = PRECEDENCE[e.op]
        left, right = _expr(e.left), _expr(e.right)
        if isinstance(e.left, Binary) and PRECEDENCE[e.left.op] < p:
            left = f"({left})"
        # left-associative: an equal-precedence right child needs parentheses
        if isinstance(e.right, Binary) and PRECEDENCE[e.right.op] <= p:
            right = f"({right})"
        return f"{left} {e.op} {right}"
    raise TypeError(f"unknown expression {type(e).__name__}")
