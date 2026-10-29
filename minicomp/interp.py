"""Reference interpreter: executes the AST directly.

This is the "ground truth" for differential testing. It is deliberately
simple and has no optimizations, so its output defines what every
correct compilation of a program must print.

It does NOT use ops.py. Its arithmetic is a second, independent
implementation of the language rules (below), written a different way:
32-bit wrap-around through ctypes.c_int32, division from Python's floor
division with a correction, shifts with `% 32`. The compiler (optimizer
and VM) uses ops.py, so a mistake in either implementation shows up as a
difference instead of being copied into both sides. tests/test_frontend.py
also compares the two directly on edge cases and random values.

Calls: arguments are evaluated left to right, then the function body runs
with a fresh set of local variables holding the parameters. `return e;`
ends the function with the value of e; reaching the end of the body
returns 0. `&&` and `||` evaluate their right operand only when needed
(this matters when it calls a function that prints).

Globals: every variable and array declared at the top level (outside any
block) exists, holding 0, from the start of the program; its declaration
then sets its value (an array declaration sets every element to 0).
Functions can read and assign these; a function's own local of the same
name hides the global.

Arrays: `a[i]` is element (i mod n) of an n-element array, where the
remainder is taken in 0..n-1 (so a[-1] is the last element). Reading and
writing outside 0..n-1 therefore never fails. The index is evaluated
before the value in `a[i] = v;`.

Work is counted in steps ("fuel"): one per statement executed, per loop
iteration, per call, per argument passed, per operator evaluated and per
array element read.
Every TAC instruction the compiler emits is tied to one of these, so
compiled code needs at most a fixed multiple of the reference's steps
(the harness relies on this). Programs that loop forever are stopped
after a fixed number of steps, and programs that nest calls more than
MAX_CALL_DEPTH deep are stopped too (StackOverflow); the compiled code
obeys the same limits, and programs stopped this way are excluded from
comparisons instead of being reported as bugs.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Dict, List

from .nodes import (ArrayDecl, Assign, Binary, Block, Break, Call, CallStmt, Continue, For,
                    FunDecl, If, Index, IndexAssign, IntLit, Print, Program, Return, Unary, Var,
                    VarDecl, While)

DEFAULT_FUEL = 1_000_000
MAX_CALL_DEPTH = 200

# Each Mini call uses a few dozen Python frames here; make room once, at import
# (changing the limit around every run would race between web-server threads).
if sys.getrecursionlimit() < 20_000:
    sys.setrecursionlimit(20_000)


class OutOfFuel(Exception):
    """The program executed more than the allowed number of steps."""

    def __init__(self, output: List[int], message: str = "program exceeded its step limit") -> None:
        super().__init__(message)
        self.output = output          # what it printed before being stopped


class StackOverflow(OutOfFuel):
    """Calls were nested more than MAX_CALL_DEPTH deep."""

    def __init__(self, output: List[int]) -> None:
        super().__init__(output, f"calls nested more than {MAX_CALL_DEPTH} deep")


# ----------------------------------------------------------------------
# The language's arithmetic, written independently of ops.py
# ----------------------------------------------------------------------
def i32(x: int) -> int:
    return ctypes.c_int32(x).value                 # keeps the low 32 bits, signed


def _quotient(a: int, b: int) -> int:
    if b == 0:
        return 0
    q = a // b                                     # Python rounds toward minus infinity ...
    if q < 0 and q * b != a:
        q += 1                                     # ... so move inexact negative results toward zero
    return q


BINARY = {
    "+": lambda a, b: i32(a + b),
    "-": lambda a, b: i32(a - b),
    "*": lambda a, b: i32(a * b),
    "/": lambda a, b: i32(_quotient(a, b)),
    "%": lambda a, b: i32(a - b * _quotient(a, b)),
    "&": lambda a, b: i32(a & b),
    "|": lambda a, b: i32(a | b),
    "^": lambda a, b: i32(a ^ b),
    "<<": lambda a, b: i32(a << (b % 32)),
    ">>": lambda a, b: a >> (b % 32),
    "==": lambda a, b: 1 if a == b else 0,
    "!=": lambda a, b: 1 if a != b else 0,
    "<": lambda a, b: 1 if a < b else 0,
    "<=": lambda a, b: 1 if a <= b else 0,
    ">": lambda a, b: 1 if a > b else 0,
    ">=": lambda a, b: 1 if a >= b else 0,
}
UNARY = {
    "-": lambda a: i32(-a),
    "!": lambda a: 1 if a == 0 else 0,
    "~": lambda a: i32(-a - 1),
}


class _Return(Exception):
    def __init__(self, value: int) -> None:
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


def run(program: Program, fuel: int = DEFAULT_FUEL) -> List[int]:
    """Run a semantically checked program and return the values it printed."""
    return _Interpreter(program, fuel).run(program)


def run_counted(program: Program, fuel: int = DEFAULT_FUEL):
    """(printed values, steps used)."""
    it = _Interpreter(program, fuel)
    out = it.run(program)
    return out, fuel - it.fuel


class _Interpreter:
    def __init__(self, program: Program, fuel: int) -> None:
        self.fuel = fuel
        self.output: List[int] = []
        # Top-level variables and arrays exist (as 0) from the start, so a
        # function can use one before its declaration has run.
        self.globals: Dict[str, object] = {
            name: ([0] * d.size if isinstance(d, ArrayDecl) else 0)
            for name, d in program.top_level_names().items()}
        self.scopes: List[Dict[str, object]] = [self.globals]
        self.functions: Dict[str, FunDecl] = {f.name: f for f in program.functions()}
        self.depth = 0

    def tick(self) -> None:
        self.fuel -= 1
        if self.fuel < 0:
            raise OutOfFuel(self.output)

    def run(self, program: Program) -> List[int]:
        for s in program.main():
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
        elif isinstance(s, ArrayDecl):
            self.scopes[-1][s.name] = [0] * s.size
        elif isinstance(s, Assign):
            value = self.expr(s.value)
            self.find(s.name)[s.name] = value
        elif isinstance(s, IndexAssign):
            i = self.expr(s.index)                  # index first, then the value
            value = self.expr(s.value)
            arr = self.find(s.name)[s.name]
            arr[i % len(arr)] = value               # Python's % is already in 0..n-1 for n > 0
        elif isinstance(s, Break):
            raise _Break()
        elif isinstance(s, Continue):
            raise _Continue()
        elif isinstance(s, Print):
            self.output.append(self.expr(s.value))
        elif isinstance(s, CallStmt):
            self.expr(s.call)
        elif isinstance(s, Return):
            raise _Return(self.expr(s.value))
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
                try:
                    self.stmt(s.body)
                except _Break:
                    break
                except _Continue:
                    pass
        elif isinstance(s, For):
            self.scopes.append({})                  # the loop variable's scope
            try:
                if s.init is not None:
                    self.stmt(s.init)
                while self.expr(s.cond) != 0:
                    self.tick()
                    try:
                        self.stmt(s.body)
                    except _Break:
                        break
                    except _Continue:
                        pass
                    if s.update is not None:
                        self.stmt(s.update)
            finally:
                self.scopes.pop()
        else:
            raise TypeError(f"unknown statement {type(s).__name__}")

    # -- expressions -----------------------------------------------------
    def expr(self, e) -> int:
        if isinstance(e, IntLit):
            return e.value
        if isinstance(e, Var):
            return self.find(e.name)[e.name]
        if isinstance(e, Index):
            self.tick()
            i = self.expr(e.index)
            arr = self.find(e.name)[e.name]
            return arr[i % len(arr)]
        if isinstance(e, Unary):
            self.tick()
            return UNARY[e.op](self.expr(e.operand))
        if isinstance(e, Binary):
            self.tick()
            left = self.expr(e.left)
            # && and || evaluate the right side only when it decides the result
            if e.op == "&&":
                return 0 if left == 0 else (1 if self.expr(e.right) != 0 else 0)
            if e.op == "||":
                return 1 if left != 0 else (1 if self.expr(e.right) != 0 else 0)
            return BINARY[e.op](left, self.expr(e.right))
        if isinstance(e, Call):
            return self.call(e)
        raise TypeError(f"unknown expression {type(e).__name__}")

    def call(self, e: Call) -> int:
        args = []
        for a in e.args:                               # left to right, in the caller
            self.tick()                                # one step per argument passed
            args.append(self.expr(a))
        f = self.functions[e.name]
        self.tick()
        if self.depth >= MAX_CALL_DEPTH:
            raise StackOverflow(self.output)
        saved, self.scopes = self.scopes, [self.globals, dict(zip(f.params, args))]
        self.depth += 1
        try:
            for s in f.body.body:
                self.stmt(s)
            return 0                                   # fell off the end
        except _Return as r:
            return r.value
        finally:
            self.scopes = saved
            self.depth -= 1
