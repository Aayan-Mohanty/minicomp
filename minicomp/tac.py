"""Three-address code (TAC): the compiler's intermediate representation.

Every instruction has at most one operator and at most three addresses:

    x = a               copy      (a is a variable or an integer constant)
    x = op a            unop      op in  - ! ~
    x = a op b          binop     op is any binary operator
    print a             print
    L3:                 label
    goto L3             jump
    if a goto L1 else L2   cbr    (conditional branch: a != 0 -> L1, else L2)

An operand is either a variable name (str) or a constant (int).
Names produced by the compiler:
    x, x#1, x#2   source variables; each `var` declaration gets a unique
                  name, so shadowed variables never clash
    %1, %2        temporaries
    L1, L2        labels
'#' and '%' cannot appear in Mini identifiers, so these never collide
with names the programmer writes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List, Optional, Union

Operand = Union[str, int]

DEF_KINDS = ("copy", "unop", "binop")


@dataclass(frozen=True)
class Instr:
    kind: str                       # copy unop binop print label jump cbr
    dst: Optional[str] = None
    op: Optional[str] = None
    a: Optional[Operand] = None
    b: Optional[Operand] = None
    target: Optional[str] = None    # label name (label, jump) / true target (cbr)
    target2: Optional[str] = None   # false target (cbr)

    def defs(self) -> Optional[str]:
        """The variable this instruction assigns, if any."""
        return self.dst if self.kind in DEF_KINDS else None

    def uses(self) -> List[str]:
        """Variable names this instruction reads."""
        return [x for x in (self.a, self.b) if isinstance(x, str)]

    def with_operands(self, a, b) -> "Instr":
        return replace(self, a=a, b=b)

    def __str__(self) -> str:
        k = self.kind
        if k == "copy":
            return f"{self.dst} = {self.a}"
        if k == "unop":
            return f"{self.dst} = {self.op}{self.a}"
        if k == "binop":
            return f"{self.dst} = {self.a} {self.op} {self.b}"
        if k == "print":
            return f"print {self.a}"
        if k == "label":
            return f"{self.target}:"
        if k == "jump":
            return f"goto {self.target}"
        if k == "cbr":
            return f"if {self.a} goto {self.target} else {self.target2}"
        return f"<{k}>"


def copy(dst: str, a: Operand) -> Instr:
    return Instr("copy", dst=dst, a=a)


def unop(dst: str, op: str, a: Operand) -> Instr:
    return Instr("unop", dst=dst, op=op, a=a)


def binop(dst: str, op: str, a: Operand, b: Operand) -> Instr:
    return Instr("binop", dst=dst, op=op, a=a, b=b)


def format_code(code: List[Instr]) -> str:
    lines = []
    for ins in code:
        lines.append(str(ins) if ins.kind == "label" else f"    {ins}")
    return "\n".join(lines)
