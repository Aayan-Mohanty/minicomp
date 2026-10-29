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


@dataclass
class Call:
    name: str               # function name
    args: List["Expr"]
    line: int = _pos()
    col: int = _pos()


@dataclass
class Index:
    """An array element read: `a[i]`. The index wraps: element i mod length."""
    name: str               # array name
    index: "Expr"
    line: int = _pos()
    col: int = _pos()


Expr = Union[IntLit, Var, Unary, Binary, Call, Index]


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


@dataclass
class CallStmt:
    """A call used as a statement, for its effect (`log(x);`)."""
    call: Call
    line: int = _pos()
    col: int = _pos()


@dataclass
class Return:
    value: Expr
    line: int = _pos()
    col: int = _pos()


@dataclass
class ArrayDecl:
    """`var a[8];`: an array of `size` integers, all 0."""
    name: str
    size: int
    line: int = _pos()
    col: int = _pos()


@dataclass
class IndexAssign:
    """`a[i] = v;` (the index is evaluated first, then the value)."""
    name: str
    index: Expr
    value: Expr
    line: int = _pos()
    col: int = _pos()


@dataclass
class For:
    """`for (init; cond; update) body`. init is a VarDecl or Assign (or None),
    update an Assign (or None). A variable declared in init belongs to the
    loop: it exists from init to the end of the loop."""
    init: Optional[Union[VarDecl, Assign]]
    cond: Expr
    update: Optional[Assign]
    body: Block
    line: int = _pos()
    col: int = _pos()


@dataclass
class Break:
    line: int = _pos()
    col: int = _pos()


@dataclass
class Continue:
    line: int = _pos()
    col: int = _pos()


Stmt = Union[VarDecl, ArrayDecl, Assign, IndexAssign, Print, Block, If, While, For,
             Break, Continue, CallStmt, Return]


@dataclass
class FunDecl:
    """`fun name(p1, p2) { ... }`: only at the top level of a program."""
    name: str
    params: List[str]
    body: Block
    line: int = _pos()
    col: int = _pos()


@dataclass
class Program:
    body: List[Union[Stmt, FunDecl]]

    def functions(self) -> List[FunDecl]:
        return [s for s in self.body if isinstance(s, FunDecl)]

    def main(self) -> List[Stmt]:
        """The top-level statements (everything except function declarations)."""
        return [s for s in self.body if not isinstance(s, FunDecl)]

    def top_level_names(self) -> dict:
        """Variables and arrays declared at the top level (not inside a block):
        name -> its declaration. Functions can use these (globals)."""
        out = {}
        for s in self.main():
            if isinstance(s, (VarDecl, ArrayDecl)) and s.name not in out:
                out[s.name] = s
        return out
