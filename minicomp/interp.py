"""Reference interpreter: executes the AST directly.

This is the "ground truth" for differential testing. It is deliberately
simple and has no optimizations, so its output defines what every
correct compilation of a program must print.

Programs that loop forever are stopped after a fixed amount of work
("fuel"), so testing never hangs. The same fuel rule will be used for the
compiled code, and programs that run out of fuel are excluded from
comparisons instead of being reported as bugs.
"""

from __future__ import annotations

from typing import Dict, List

from . import ops
from .nodes import (Assign, Binary, Block, If, IntLit, Print, Program, Unary,
                    Var, VarDecl, While)

DEFAULT_FUEL = 1_000_000


class OutOfFuel(Exception):
    """The program executed more than the allowed number of steps."""

    def __init__(self, output: List[int]) -> None:
        super().__init__("program exceeded its step limit")
        self.output = output          # what it printed before being stopped


def run(program: Program, fuel: int = DEFAULT_FUEL) -> List[int]:
    """Run a semantically checked program and return the values it printed."""
    return _Interpreter(fuel).run(program)


class _Interpreter:
    def __init__(self, fuel: int) -> None:
        self.fuel = fuel
        self.output: List[int] = []
        self.scopes: List[Dict[str, int]] = [{}]

    def tick(self) -> None:
        self.fuel -= 1
        if self.fuel < 0:
            raise OutOfFuel(self.output)

    def run(self, program: Program) -> List[int]:
        for s in program.body:
            self.stmt(s)
        return self.output

    # -- variables -------------------------------------------------------
    def find(self, name: str) -> Dict[str, int]:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope
        raise NameError(name)       # unreachable after semantic checking

    # -- statements ------------------------------------------------------
    def stmt(self, s) -> None:
        self.tick()
        if isinstance(s, VarDecl):
            value = self.expr(s.init)               # evaluated before declaring
            self.scopes[-1][s.name] = value
        elif isinstance(s, Assign):
            value = self.expr(s.value)
            self.find(s.name)[s.name] = value
        elif isinstance(s, Print):
            self.output.append(self.expr(s.value))
        elif isinstance(s, Block):
            self.scopes.append({})
            try:
                for inner in s.body:
                    self.stmt(inner)
            finally:
                self.scopes.pop()
        elif isinstance(s, If):
            if self.expr(s.cond) != 0:
                self.stmt(s.then)
            elif s.orelse is not None:
                self.stmt(s.orelse)
        elif isinstance(s, While):
            while self.expr(s.cond) != 0:
                self.tick()
                self.stmt(s.body)
        else:
            raise TypeError(f"unknown statement {type(s).__name__}")

    # -- expressions -----------------------------------------------------
    def expr(self, e) -> int:
        if isinstance(e, IntLit):
            return e.value
        if isinstance(e, Var):
            return self.find(e.name)[e.name]
        if isinstance(e, Unary):
            return ops.unary(e.op, self.expr(e.operand))
        if isinstance(e, Binary):
            left = self.expr(e.left)
            # && and || short-circuit. Mini expressions have no side effects,
            # so this never changes the result; it only saves work.
            if e.op == "&&" and left == 0:
                return 0
            if e.op == "||" and left != 0:
                return 1
            return ops.binary(e.op, left, self.expr(e.right))
        raise TypeError(f"unknown expression {type(e).__name__}")
