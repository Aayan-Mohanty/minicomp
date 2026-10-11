"""Abstract syntax tree (AST) node types.

Positions (line, col) are excluded from == comparison, so two trees with
the same structure compare equal even if they came from differently
formatted source. The round-trip test (parse -> print -> parse) relies on this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union


def _pos():
    return field(default=0, compare=False, repr=False)


# ---------------------------------------------------------------- expressions
@dataclass
class IntLit:
    value: int
    line: int = _pos()
    col: int = _pos()


@dataclass
class Var:
    name: str
    line: int = _pos()
    col: int = _pos()


@dataclass
class Unary:
    op: str                 # "-", "!", "~"
    operand: "Expr"
    line: int = _pos()
    col: int = _pos()


@dataclass
class Binary:
    op: str                 # "+", "-", "*", "/", "%", "<<", ">>", "&", "|", "^",
                            # "==", "!=", "<", "<=", ">", ">=", "&&", "||"
    left: "Expr"
    right: "Expr"
    line: int = _pos()
    col: int = _pos()


Expr = Union[IntLit, Var, Unary, Binary]


# ---------------------------------------------------------------- statements
@dataclass
class VarDecl:
    name: str
    init: Expr
    line: int = _pos()
    col: int = _pos()


@dataclass
class Assign:
    name: str
    value: Expr
    line: int = _pos()
    col: int = _pos()


@dataclass
class Print:
    value: Expr
    line: int = _pos()
    col: int = _pos()


@dataclass
class Block:
    body: List["Stmt"]
    line: int = _pos()
    col: int = _pos()


@dataclass
class If:
    cond: Expr
    then: Block
    orelse: Optional[Union[Block, "If"]] = None     # `else if` is an If here
    line: int = _pos()
    col: int = _pos()


@dataclass
class While:
    cond: Expr
    body: Block
    line: int = _pos()
    col: int = _pos()


Stmt = Union[VarDecl, Assign, Print, Block, If, While]


@dataclass
class Program:
    body: List[Stmt]
