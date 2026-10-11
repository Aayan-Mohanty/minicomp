"""Intermediate code generation: AST -> three-address code.

Translation scheme (the standard one for structured statements):

    if (c) S1 else S2        while (c) S
    ------------------       ------------------
        t = <c>              Lhead:
        if t goto L1 else L2     t = <c>
    L1: <S1>                     if t goto Lbody else Lend
        goto Lend            Lbody:
    L2: <S2>                     <S>
    Lend:                        goto Lhead
                             Lend:

`&&` and `||` are emitted as ordinary binary operators, without
short-circuit jumps. This is exact for Mini because evaluating an
expression can never fail, loop or have a side effect (division by zero
is defined), so evaluating both sides always gives the same result.

The input must already have passed semantic analysis.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .nodes import (Assign, Binary, Block, If, IntLit, Print, Program, Unary,
                    Var, VarDecl, While)
from .tac import Instr, Operand, binop, copy, unop


def lower(program: Program) -> List[Instr]:
    return _Lowerer().run(program)


class _Lowerer:
    def __init__(self) -> None:
        self.code: List[Instr] = []
        self.ntemp = 0
        self.nlabel = 0
        self.scopes: List[Dict[str, str]] = [{}]    # source name -> unique TAC name
        self.declared: Dict[str, int] = {}          # how many times each name was declared

    # -- names -----------------------------------------------------------
    def temp(self) -> str:
        self.ntemp += 1
        return f"%{self.ntemp}"

    def label(self) -> str:
        self.nlabel += 1
        return f"L{self.nlabel}"

    def declare(self, name: str) -> str:
        n = self.declared.get(name, 0)
        self.declared[name] = n + 1
        unique = name if n == 0 else f"{name}#{n}"
        self.scopes[-1][name] = unique
        return unique

    def resolve(self, name: str) -> str:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise NameError(name)            # unreachable after semantic checking

    def emit(self, ins: Instr) -> None:
        self.code.append(ins)

    # -- statements ------------------------------------------------------
    def run(self, program: Program) -> List[Instr]:
        for s in program.body:
            self.stmt(s)
        return self.code

    def stmt(self, s) -> None:
        if isinstance(s, VarDecl):
            value = self.expr(s.init)                 # before the new name exists
            self.assign(self.declare(s.name), value)
        elif isinstance(s, Assign):
            self.expr(s.value, dst=self.resolve(s.name))
        elif isinstance(s, Print):
            self.emit(Instr("print", a=self.expr(s.value)))
        elif isinstance(s, Block):
            self.scopes.append({})
            for inner in s.body:
                self.stmt(inner)
            self.scopes.pop()
        elif isinstance(s, If):
            cond = self.expr(s.cond)
            l_then, l_end = self.label(), self.label()
            l_else = self.label() if s.orelse is not None else l_end
            self.emit(Instr("cbr", a=cond, target=l_then, target2=l_else))
            self.emit(Instr("label", target=l_then))
            self.stmt(s.then)
            if s.orelse is not None:
                self.emit(Instr("jump", target=l_end))
                self.emit(Instr("label", target=l_else))
                self.stmt(s.orelse)
            self.emit(Instr("label", target=l_end))
        elif isinstance(s, While):
            l_head, l_body, l_end = self.label(), self.label(), self.label()
            self.emit(Instr("label", target=l_head))
            cond = self.expr(s.cond)
            self.emit(Instr("cbr", a=cond, target=l_body, target2=l_end))
            self.emit(Instr("label", target=l_body))
            self.stmt(s.body)
            self.emit(Instr("jump", target=l_head))
            self.emit(Instr("label", target=l_end))
        else:
            raise TypeError(f"unknown statement {type(s).__name__}")

    def assign(self, dst: str, value: Operand) -> None:
        self.emit(copy(dst, value))

    # -- expressions -----------------------------------------------------
    def expr(self, e, dst: Optional[str] = None) -> Operand:
        """Emit code for `e`. If `dst` is given, the result is stored there."""
        if isinstance(e, (IntLit, Var)):
            value: Operand = e.value if isinstance(e, IntLit) else self.resolve(e.name)
            if dst is not None:
                self.assign(dst, value)
                return dst
            return value
        if isinstance(e, Unary):
            a = self.expr(e.operand)
            out = dst or self.temp()
            self.emit(unop(out, e.op, a))
            return out
        if isinstance(e, Binary):
            a = self.expr(e.left)
            b = self.expr(e.right)
            out = dst or self.temp()
            self.emit(binop(out, e.op, a, b))
            return out
        raise TypeError(f"unknown expression {type(e).__name__}")
