"""Three-address code (TAC): the compiler's intermediate representation.

Every instruction has at most one operator and at most three addresses:

    x = a               copy      (a is a variable or an integer constant)
    x = op a            unop      op in  - ! ~
    x = a op b          binop     op is any binary operator
    print a             print
    L3:                 label
    goto L3             jump
    if a goto L1 else L2   cbr    (conditional branch: a != 0 -> L1, else L2)
    x = call f(a, b)    call      (or `call f(a, b)` when the result is unused)
    return a            ret
    fun f(p, q):        func      (header: starts the code of function f)
    array a[8]          array     (creates array a with 8 elements, all 0)
    x = a[i]            load      (element i mod 8; the remainder is in 0..7)
    a[i] = v            store

A program is one list: the top-level code ("main") first, then each
function, starting at its `fun` header. Main ends where the first header
begins. Every function is compiled, optimized and allocated separately:
`split_units` cuts the list into units and `join_units` glues them back.

An operand is either a variable name (str) or a constant (int).
Names produced by the compiler:
    x, x#1, x#2   source variables; each `var` declaration gets a unique
                  name, so shadowed variables never clash
    %1, %2        temporaries
    L1, L2        labels
    @x            a global: a top-level variable or array that some
                  function uses. It lives in memory shared by main and
                  every function; all other variables belong to one unit.
'#', '%' and '@' cannot appear in Mini identifiers, so these never collide
with names the programmer writes. Each function has its own variables,
so `x` in main and `x` in a function are different variables (unless x
is a global, @x).

An array name is not an operand: it is kept in `target`, so it never
appears among the variables an instruction reads or writes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, List, NamedTuple, Optional, Tuple, Union

Operand = Union[str, int]

DEF_KINDS = ("copy", "unop", "binop", "load")


def is_global(x) -> bool:
    """True for a global variable or array name (@x)."""
    return isinstance(x, str) and x.startswith("@")


@dataclass(frozen=True)
class Instr:
    kind: str                       # copy unop binop load store array print label jump cbr call ret func
    dst: Optional[str] = None
    op: Optional[str] = None
    a: Optional[Operand] = None
    b: Optional[Operand] = None
    target: Optional[str] = None    # label name (label, jump) / true target (cbr) / function (call, func)
                                    # / array name (array, load, store)
    target2: Optional[str] = None   # false target (cbr)
    args: Tuple[Operand, ...] = ()  # call arguments; func: parameter names

    def defs(self) -> Optional[str]:
        """The variable this instruction assigns, if any."""
        if self.kind in DEF_KINDS or self.kind == "call":
            return self.dst
        return None

    def uses(self) -> List[str]:
        """Variable names this instruction reads."""
        if self.kind == "func":
            return []
        return [x for x in (self.a, self.b) + tuple(self.args) if isinstance(x, str)]

    def has_effect(self) -> bool:
        """True if the instruction does more than assign its destination
        (so it must not be deleted even when the destination is never read)."""
        return self.kind not in DEF_KINDS

    def with_operands(self, a, b) -> "Instr":
        return replace(self, a=a, b=b)

    def map_operands(self, fn: Callable[[Operand], Operand]) -> "Instr":
        """Apply fn to every operand that is read (a, b and call arguments)."""
        if self.kind in ("label", "jump", "func"):
            return self
        a = fn(self.a) if self.a is not None else None
        b = fn(self.b) if self.b is not None else None
        args = tuple(fn(x) for x in self.args) if self.kind == "call" else self.args
        if a == self.a and b == self.b and args == self.args:
            return self
        return replace(self, a=a, b=b, args=args)

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
        if k == "call":
            text = f"call {self.target}({', '.join(map(str, self.args))})"
            return f"{self.dst} = {text}" if self.dst else text
        if k == "ret":
            return f"return {self.a}"
        if k == "func":
            return f"fun {self.target}({', '.join(self.args)}):"
        if k == "array":
            return f"array {self.target}[{self.a}]"
        if k == "load":
            return f"{self.dst} = {self.target}[{self.a}]"
        if k == "store":
            return f"{self.target}[{self.a}] = {self.b}"
        return f"<{k}>"


def copy(dst: str, a: Operand) -> Instr:
    return Instr("copy", dst=dst, a=a)


def unop(dst: str, op: str, a: Operand) -> Instr:
    return Instr("unop", dst=dst, op=op, a=a)


def binop(dst: str, op: str, a: Operand, b: Operand) -> Instr:
    return Instr("binop", dst=dst, op=op, a=a, b=b)


def globals_in(code: List[Instr]) -> set:
    """Every global (@x) that a piece of code reads, writes or indexes."""
    out = set()
    for ins in code:
        for x in (ins.dst, ins.a, ins.b, ins.target) + tuple(ins.args):
            if is_global(x):
                out.add(x)
    return out


class Unit(NamedTuple):
    header: Optional[Instr]          # None for main
    body: List[Instr]

    @property
    def name(self) -> str:
        return self.header.target if self.header is not None else "main"

    @property
    def params(self) -> Tuple[str, ...]:
        return tuple(self.header.args) if self.header is not None else ()


def split_units(code: List[Instr]) -> List[Unit]:
    """Main first, then one unit per function header."""
    units = [Unit(None, [])]
    for ins in code:
        if ins.kind == "func":
            units.append(Unit(ins, []))
        else:
            units[-1].body.append(ins)
    return units


def join_units(units: List[Unit]) -> List[Instr]:
    code: List[Instr] = []
    for u in units:
        if u.header is not None:
            code.append(u.header)
        code.extend(u.body)
    return code


def instruction_count(code: List[Instr]) -> int:
    """Instructions in a TAC program, not counting labels and function headers."""
    return sum(1 for ins in code if ins.kind not in ("label", "func"))


def format_code(code: List[Instr]) -> str:
    lines = []
    for ins in code:
        if ins.kind == "func":
            lines.append("")
            lines.append(str(ins))
        else:
            lines.append(str(ins) if ins.kind == "label" else f"    {ins}")
    return "\n".join(lines)
