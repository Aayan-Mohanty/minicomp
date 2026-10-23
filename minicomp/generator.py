"""Random program generator: produces valid, always-terminating Mini programs.

Every generated program is guaranteed to
  1. parse and pass semantic analysis: a variable is only used where it is
     visible, and the generator tracks scopes exactly like the checker;
  2. terminate: every loop has its own counter `kN`, declared just before
     the loop, incremented first thing in the body, and never assigned
     anywhere else, so a loop runs at most `max_loop_iters` times;
  3. have observable results: statements print values as they go, and the
     program ends by printing every top-level variable, so a wrong value
     almost always shows up in the output.

To give the optimizer something to do, expressions are biased toward
  - edge-case constants (0, 1, 31, 32, 2147483647, powers of two),
  - the same variable on both sides (`x - x`, `x / x`, `x ^ x`),
  - repeating an earlier expression (common subexpressions),
  - branch conditions that are constant (dead branches),
and some declarations shadow an outer variable of the same name.

The same seed always produces the same program, so every failure can be
reproduced from its seed alone.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from typing import List, Optional

from .nodes import (Assign, Binary, Block, Expr, If, IntLit, Print, Program,
                    Unary, Var, VarDecl, While)
from .parser import BINARY_LEVELS
from .printer import to_source

BINARY_OPS = [op for level in BINARY_LEVELS for op in level]
UNARY_OPS = ["-", "!", "~"]
NAMES = ["a", "b", "c", "d", "e", "f", "g", "h"]
EDGE_CONSTANTS = [0, 1, 2, 3, 7, 8, 16, 31, 32, 33, 255, 256, 65535, 65536,
                  1000, 2147483647, 1073741824]


@dataclass
class GenConfig:
    max_top_stmts: int = 12        # statements at the top level
    max_block_stmts: int = 5       # statements in a nested block
    max_nesting: int = 3           # if/while nesting depth
    max_expr_depth: int = 4
    max_loop_iters: int = 6
    # Probability that a reused subexpression is written with its operands
    # swapped (b op a instead of a op b). Off by default so that every seed
    # keeps producing exactly the same program; the evaluation showed that
    # without it, bugs that confuse a - b with b - a go unnoticed.
    swap_reuse: float = 0.0


@dataclass
class _Scope:
    names: List[str]               # declared here, in order
    counters: List[str]            # loop counters declared here (readable, never assigned)


class Generator:
    def __init__(self, seed: int, config: Optional[GenConfig] = None) -> None:
        self.rng = random.Random(seed)
        # A separate stream for the optional swap, so turning it on never
        # changes any other choice the generator makes for this seed.
        self.swap_rng = random.Random(seed * 2654435761 + 97)
        self.cfg = config or GenConfig()
        self.scopes: List[_Scope] = [_Scope([], [])]
        self.n_counters = 0
        self.recent: List[Expr] = []      # pool of earlier expressions, for reuse

    # -- scope tracking (mirrors semantic analysis) -----------------------
    def readable(self) -> List[str]:
        seen, out = set(), []
        for scope in reversed(self.scopes):
            for n in scope.names + scope.counters:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
        return out

    def assignable(self) -> List[str]:
        """Visible variables that are not loop counters.

        A counter name is never shadowed (each is unique), and an ordinary
        name can be shadowed only by another ordinary name, so the name
        resolved here is always the ordinary variable.
        """
        counters = {c for s in self.scopes for c in s.counters}
        return [n for n in self.readable() if n not in counters]

    # -- expressions -------------------------------------------------------
    def constant(self) -> Expr:
        r = self.rng.random()
        if r < 0.55:
            return IntLit(self.rng.choice(EDGE_CONSTANTS))
        if r < 0.75:
            return Unary("-", IntLit(self.rng.choice(EDGE_CONSTANTS[1:])))
        return IntLit(self.rng.randint(0, 100))

    def expr(self, depth: Optional[int] = None) -> Expr:
        depth = self.cfg.max_expr_depth if depth is None else depth
        names = self.readable()
        r = self.rng.random()
        if depth <= 0 or r < 0.2:
            if names and self.rng.random() < 0.65:
                return Var(self.rng.choice(names))
            return self.constant()
        if r < 0.27:
            visible = set(names)
            usable = [e for e in self.recent if _var_names(e) <= visible]
            if usable:                                         # common subexpression
                e = copy.deepcopy(self.rng.choice(usable))
                if (self.cfg.swap_reuse and isinstance(e, Binary)
                        and self.swap_rng.random() < self.cfg.swap_reuse):
                    e = Binary(e.op, e.right, e.left)          # same operands, swapped
                return e
        if r < 0.35 and names:
            v = Var(self.rng.choice(names))                    # x op x
            return Binary(self.rng.choice(BINARY_OPS), v, Var(v.name))
        if r < 0.45:
            return Unary(self.rng.choice(UNARY_OPS), self.expr(depth - 1))
        e = Binary(self.rng.choice(BINARY_OPS), self.expr(depth - 1), self.expr(depth - 1))
        if self.rng.random() < 0.3:
            self.recent = (self.recent + [e])[-8:]
        return e

    def condition(self) -> Expr:
        if self.rng.random() < 0.2:
            return self.constant()                             # constant branch
        op = self.rng.choice(["<", "<=", ">", ">=", "==", "!=", "&&", "||"])
        return Binary(op, self.expr(2), self.expr(2))

    # -- statements --------------------------------------------------------
    def declare(self) -> VarDecl:
        init = self.expr()                                     # before the name exists
        visible = self.assignable()
        if visible and self.rng.random() < 0.25 and len(self.scopes) > 1:
            name = self.rng.choice(visible)                    # shadow an outer name
        else:
            name = self.rng.choice(NAMES)
        if name in self.scopes[-1].names:                      # can't redeclare in this block
            return None
        self.scopes[-1].names.append(name)
        return VarDecl(name, init)

    def statements(self, count: int, nesting: int) -> List:
        out = []
        for _ in range(count):
            s = self.statement(nesting)
            if s is not None:
                out.extend(s if isinstance(s, list) else [s])
        return out

    def statement(self, nesting: int):
        r = self.rng.random()
        targets = self.assignable()
        if r < 0.25 or not targets:
            return self.declare()
        if r < 0.50:
            return Assign(self.rng.choice(targets), self.expr())
        if r < 0.65:
            return Print(self.expr())
        if r < 0.82 and nesting < self.cfg.max_nesting:
            return self.if_statement(nesting)
        if nesting < self.cfg.max_nesting:
            return self.while_loop(nesting)
        return Print(Var(self.rng.choice(self.readable())))

    def block(self, nesting: int, prefix: Optional[List] = None) -> Block:
        self.scopes.append(_Scope([], []))
        body = list(prefix or [])
        body += self.statements(self.rng.randint(0, self.cfg.max_block_stmts), nesting + 1)
        self.scopes.pop()
        return Block(body)

    def if_statement(self, nesting: int) -> If:
        cond = self.condition()
        then = self.block(nesting)
        orelse = None
        r = self.rng.random()
        if r < 0.35:
            orelse = self.block(nesting)
        elif r < 0.5 and nesting + 1 < self.cfg.max_nesting:
            orelse = self.if_statement(nesting + 1)          # else if
        return If(cond, then, orelse)

    def while_loop(self, nesting: int) -> List:
        k = f"k{self.n_counters}"
        self.n_counters += 1
        decl = VarDecl(k, IntLit(0))
        self.scopes[-1].counters.append(k)
        bound = self.rng.randint(0, self.cfg.max_loop_iters)
        cond = Binary("<", Var(k), IntLit(bound))
        if self.rng.random() < 0.4:                          # extra condition; the counter still bounds it
            cond = Binary("&&", cond, self.condition())
        step = Assign(k, Binary("+", Var(k), IntLit(1)))
        return [decl, While(cond, self.block(nesting, prefix=[step]))]

    # -- whole program -----------------------------------------------------
    def program(self) -> Program:
        n = self.rng.randint(3, self.cfg.max_top_stmts)
        body = self.statements(n, 0)
        for name in self.readable():                       # make every final value visible
            body.append(Print(Var(name)))
        return Program(body)


def _var_names(e) -> set:
    if isinstance(e, Var):
        return {e.name}
    if isinstance(e, Unary):
        return _var_names(e.operand)
    if isinstance(e, Binary):
        return _var_names(e.left) | _var_names(e.right)
    return set()


def generate(seed: int, config: Optional[GenConfig] = None) -> Program:
    return Generator(seed, config).program()


def generate_source(seed: int, config: Optional[GenConfig] = None) -> str:
    return to_source(generate(seed, config))
