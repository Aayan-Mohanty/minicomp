"""Random program generator: produces valid, always-terminating Mini programs.

Every generated program is guaranteed to
  1. parse and pass semantic analysis: a variable or array is only used
     where it is visible, and the generator tracks scopes exactly like the
     checker;
  2. terminate: every `while` and `for` loop has its own counter `kN`; a
     `while` counter is declared just before the loop and incremented first
     thing in the body, a `for` counter is declared and incremented in the
     loop header, and no counter is assigned anywhere else, so a loop runs
     at most `max_loop_iters` times (`continue` and `break` only make it
     shorter). A function calls only functions declared before it, and
     itself only if it is recursive: a recursive function's first
     parameter `n` is a depth that is never assigned, the body starts with
     `if (n < 1) { return ...; }`, every recursive call passes `n - 1`,
     and every other call passes a constant from 0 to 3;
  3. finish within `max_steps` steps of the reference interpreter: a
     candidate that takes longer is discarded and the next candidate for
     the same seed is generated instead, so the harness never has to skip
     a generated program for running out of fuel;
  4. have observable results: statements print values as they go, and the
     program ends by printing every top-level variable and every element
     of every top-level array, so a wrong value almost always shows up in
     the output.

Programs start with global variables (g0, g1) and possibly a global array
(ga), then up to `max_functions` functions (f0, f1, ...) with 0-3
parameters; their bodies may print, loop, branch, break, continue, return
early, read and write arrays and globals, and call earlier functions (and
themselves, if recursive). Calls then appear in expressions and as
statements everywhere after.

To give the optimizer something to do, expressions are biased toward
  - edge-case constants (0, 1, 31, 32, 2147483647, powers of two),
  - the same variable on both sides (`x - x`, `x / x`, `x ^ x`),
  - repeating an earlier expression or array read (common subexpressions),
    sometimes with the operands of the repeated expression swapped
    (`b - a` after `a - b`),
  - branch conditions that are constant (dead branches),
  - array indexes outside 0..n-1 (they wrap),
and some declarations shadow an outer variable of the same name.

GenConfig switches each feature off separately (used by the evaluation to
measure what each feature contributes).
"""


from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .interp import OutOfFuel, run as interpret
from .nodes import (ArrayDecl, Assign, Binary, Block, Break, Call, CallStmt, Continue, Expr, For,
                    FunDecl, If, Index, IndexAssign, IntLit, Print, Program, Return, Unary, Var,
                    VarDecl, While)
from .parser import BINARY_LEVELS
from .printer import to_source

BINARY_OPS = [op for level in BINARY_LEVELS for op in level]
UNARY_OPS = ["-", "!", "~"]
NAMES = ["a", "b", "c", "d", "e", "f", "g", "h"]
EDGE_CONSTANTS = [0, 1, 2, 3, 7, 8, 16, 31, 32, 33, 255, 256, 65535, 65536,
                  1000, 2147483647, 1073741824]
ARRAY_SIZES = [1, 2, 3, 4, 5, 8]
DEPTH = "n"                        # a recursive function's depth parameter (not in NAMES)


@dataclass
class GenConfig:
    max_top_stmts: int = 12        # statements at the top level
    max_block_stmts: int = 5       # statements in a nested block
    max_nesting: int = 3           # if/while/for nesting depth
    max_expr_depth: int = 4
    max_loop_iters: int = 6
    max_functions: int = 3
    call_rate: float = 0.08        # chance that an expression is a call (when one is possible)
    # Probability that a reused subexpression is written with its operands
    # swapped (b op a instead of a op b). The first evaluation showed that
    # without it, bugs that confuse a - b with b - a go unnoticed.
    swap_reuse: float = 0.3
    arrays: bool = True            # array declarations, reads and writes
    globals: bool = True           # top-level g0, g1, ga that functions read and write
    recursion: bool = True         # some functions call themselves (bounded depth)
    loop_control: bool = True      # `for` loops, `break` and `continue`
    max_steps: int = 50_000        # reference-interpreter budget for a generated program
    max_attempts: int = 50


@dataclass
class _Scope:
    names: List[str]               # variables declared here, in order
    counters: List[str]            # loop counters / depth declared here (readable, never assigned)
    arrays: List[Tuple[str, int]] = field(default_factory=list)   # (name, size) declared here


class Generator:
    def __init__(self, seed, config: Optional[GenConfig] = None) -> None:
        self.rng = random.Random(seed)
        # A separate stream for the swap, so changing its rate never
        # changes any other choice the generator makes for this seed.
        self.swap_rng = random.Random(f"swap/{seed}")
        self.cfg = config or GenConfig()
        self.scopes: List[_Scope] = [_Scope([], [])]
        self.n_counters = 0
        self.n_arrays = 0
        self.recent: List[Expr] = []      # pool of earlier expressions, for reuse
        self.callable: List[FunDecl] = []  # functions the code being generated may call
        self.recursive: set = set()       # names of recursive functions
        self.current: Optional[str] = None   # the function being generated
        self.in_function = False
        self.loops = 0                    # enclosing loops (for break / continue)

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

    def arrays(self) -> List[Tuple[str, int]]:
        """Visible arrays (array names are unique, so none is ever hidden)."""
        return [a for s in self.scopes for a in s.arrays]

    def visible_names(self) -> set:
        return set(self.readable()) | {a for a, _ in self.arrays()}

    # -- expressions -------------------------------------------------------
    def constant(self) -> Expr:
        r = self.rng.random()
        if r < 0.55:
            return IntLit(self.rng.choice(EDGE_CONSTANTS))
        if r < 0.75:
            return Unary("-", IntLit(self.rng.choice(EDGE_CONSTANTS[1:])))
        return IntLit(self.rng.randint(0, 100))

    def call(self, depth: int) -> Call:
        f = self.rng.choice(self.callable)
        params = f.params
        args: List[Expr] = []
        if f.name in self.recursive:
            if f.name == self.current:
                args.append(Binary("-", Var(DEPTH), IntLit(1)))     # one level shallower
            else:
                args.append(IntLit(self.rng.randint(0, 3)))         # a small starting depth
            params = params[1:]
        args += [self.expr(max(0, depth - 1)) for _ in params]
        return Call(f.name, args)

    def index(self, size: int, depth: int) -> Expr:
        """An index: usually small, sometimes outside 0..size-1 (it wraps)."""
        r = self.rng.random()
        if r < 0.45:
            return IntLit(self.rng.randint(0, size + 2))
        if r < 0.55:
            return Unary("-", IntLit(self.rng.randint(1, size + 1)))
        return self.expr(min(depth, 2))

    def element(self, depth: int) -> Expr:
        name, size = self.rng.choice(self.arrays())
        e = Index(name, self.index(size, depth - 1))
        if self.rng.random() < 0.3:
            self.recent = (self.recent + [e])[-8:]          # array reads are reused too
        return e

    def expr(self, depth: Optional[int] = None) -> Expr:
        depth = self.cfg.max_expr_depth if depth is None else depth
        names = self.readable()
        if self.callable and depth > 0 and self.rng.random() < self.cfg.call_rate:
            return self.call(depth)
        r = self.rng.random()
        if self.arrays() and depth > 0 and self.rng.random() < 0.12:
            return self.element(depth)
        if depth <= 0 or r < 0.2:
            if names and self.rng.random() < 0.65:
                return Var(self.rng.choice(names))
            return self.constant()
        if r < 0.27:
            visible = self.visible_names()
            allowed = {f.name for f in self.callable}
            usable = [e for e in self.recent if _var_names(e) <= visible and _calls(e) <= allowed]
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
    def declare(self):
        if self.cfg.arrays and self.rng.random() < 0.12:
            name = f"a{self.n_arrays}"                         # unique: never hides another array
            self.n_arrays += 1
            size = self.rng.choice(ARRAY_SIZES)
            self.scopes[-1].arrays.append((name, size))
            return ArrayDecl(name, size)
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
        if self.callable and self.rng.random() < 0.06:
            return CallStmt(self.call(self.cfg.max_expr_depth))
        if self.in_function and nesting > 0 and self.rng.random() < 0.05:
            return Return(self.expr(2))                      # early return
        if self.loops and self.cfg.loop_control and self.rng.random() < 0.07:
            jump = Break() if self.rng.random() < 0.5 else Continue()
            if self.rng.random() < 0.75:
                return If(self.condition(), Block([jump]))  # leave the loop or iteration sometimes
            return jump                                      # always: the rest of the block is dead
        if self.arrays() and self.rng.random() < 0.1:
            name, size = self.rng.choice(self.arrays())
            return IndexAssign(name, self.index(size, 2), self.expr())
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
            if self.cfg.loop_control and self.rng.random() < 0.4:
                return self.for_loop(nesting)
            return self.while_loop(nesting)
        return Print(Var(self.rng.choice(self.readable())))

    def block(self, nesting: int, prefix: Optional[List] = None) -> Block:
        self.scopes.append(_Scope([], []))
        body = list(prefix or [])
        body += self.statements(self.rng.randint(0, self.cfg.max_block_stmts), nesting + 1)
        self.scopes.pop()
        return Block(body)

    def loop_body(self, nesting: int, prefix: Optional[List] = None) -> Block:
        self.loops += 1
        body = self.block(nesting, prefix)
        self.loops -= 1
        return body

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

    def counter(self) -> str:
        k = f"k{self.n_counters}"
        self.n_counters += 1
        return k

    def loop_condition(self, k: str) -> Expr:
        cond = Binary("<", Var(k), IntLit(self.rng.randint(0, self.cfg.max_loop_iters)))
        if self.rng.random() < 0.4:                          # extra condition; the counter still bounds it
            cond = Binary("&&", cond, self.condition())
        return cond

    def while_loop(self, nesting: int) -> List:
        k = self.counter()
        decl = VarDecl(k, IntLit(0))
        self.scopes[-1].counters.append(k)
        cond = self.loop_condition(k)
        step = Assign(k, Binary("+", Var(k), IntLit(1)))
        return [decl, While(cond, self.loop_body(nesting, prefix=[step]))]

    def for_loop(self, nesting: int) -> For:
        k = self.counter()
        self.scopes.append(_Scope([], [k]))                  # the header's scope holds the counter
        cond = self.loop_condition(k)
        body = self.loop_body(nesting)
        self.scopes.pop()
        return For(VarDecl(k, IntLit(0)), cond, Assign(k, Binary("+", Var(k), IntLit(1))), body)

    # -- functions ---------------------------------------------------------
    def function(self, index: int, globals_scope: _Scope) -> FunDecl:
        name = f"f{index}"
        recursive = self.cfg.recursion and self.rng.random() < 0.35
        params = ["p", "q", "r"][:self.rng.randint(0, 2 if recursive else 3)]
        saved = (self.scopes, self.recent, self.callable)
        own = _Scope(list(params), [DEPTH] if recursive else [])
        self.scopes, self.recent, self.in_function = [globals_scope, own], [], True
        self.current, self.loops = name, 0
        body: List = []
        decl = FunDecl(name, ([DEPTH] if recursive else []) + params, Block(body))
        if recursive:                                        # base case first, before it can call itself
            body.append(If(Binary("<", Var(DEPTH), IntLit(1)), Block([Return(self.expr(2))])))
            self.recursive.add(name)
            self.callable = self.callable + [decl]
        body += self.statements(self.rng.randint(1, self.cfg.max_block_stmts + 1), 0)
        body.append(Return(self.expr(3)))
        self.scopes, self.recent, self.callable = saved
        self.in_function, self.current = False, None
        return decl

    # -- whole program -----------------------------------------------------
    def program(self) -> Program:
        top: List = []
        if self.cfg.globals:                                 # globals that functions share with main
            for i in range(self.rng.randint(1, 2)):
                top.append(VarDecl(f"g{i}", self.constant()))
                self.scopes[0].names.append(f"g{i}")
            if self.cfg.arrays and self.rng.random() < 0.6:
                size = self.rng.choice(ARRAY_SIZES)
                top.append(ArrayDecl("ga", size))
                self.scopes[0].arrays.append(("ga", size))
        globals_scope = _Scope(list(self.scopes[0].names), [], list(self.scopes[0].arrays))
        functions: List[FunDecl] = []
        for i in range(self.rng.randint(0, self.cfg.max_functions)):
            self.callable = list(functions)                  # only earlier ones (and itself, if recursive)
            functions.append(self.function(i, globals_scope))
        self.callable = list(functions)
        n = self.rng.randint(3, self.cfg.max_top_stmts)
        body = top + self.statements(n, 0)
        for name in self.readable():                       # make every final value visible
            body.append(Print(Var(name)))
        for name, size in self.scopes[0].arrays:
            for i in range(size):
                body.append(Print(Index(name, IntLit(i))))
        return Program(list(functions) + body)


def _var_names(e) -> set:
    if isinstance(e, Var):
        return {e.name}
    if isinstance(e, Index):
        return {e.name} | _var_names(e.index)
    if isinstance(e, Unary):
        return _var_names(e.operand)
    if isinstance(e, Binary):
        return _var_names(e.left) | _var_names(e.right)
    if isinstance(e, Call):
        return set().union(*(_var_names(a) for a in e.args)) if e.args else set()
    return set()


def _calls(e) -> set:
    if isinstance(e, Call):
        return {e.name}.union(*(_calls(a) for a in e.args)) if e.args else {e.name}
    if isinstance(e, Index):
        return _calls(e.index)
    if isinstance(e, Unary):
        return _calls(e.operand)
    if isinstance(e, Binary):
        return _calls(e.left) | _calls(e.right)
    return set()


def generate_with_attempts(seed: int, config: Optional[GenConfig] = None) -> Tuple[Program, int]:
    """(program, attempt): candidate 0 uses the seed itself, candidate i > 0 the
    seed "<seed>/<i>"; the first that finishes within max_steps is returned."""
    cfg = config or GenConfig()
    program = None
    for attempt in range(cfg.max_attempts):
        program = Generator(seed if attempt == 0 else f"{seed}/{attempt}", cfg).program()
        try:
            interpret(program, fuel=cfg.max_steps)
            return program, attempt
        except OutOfFuel:
            continue
    return program, cfg.max_attempts - 1           # practically unreachable


def generate(seed: int, config: Optional[GenConfig] = None) -> Program:
    return generate_with_attempts(seed, config)[0]


def generate_source(seed: int, config: Optional[GenConfig] = None) -> str:
    return to_source(generate(seed, config))
