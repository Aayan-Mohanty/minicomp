"""Phase 3: semantic analysis with a scoped symbol table.

Rules checked:
  - a variable must be declared (with `var`) before it is read or assigned
  - a name cannot be declared twice in the same block
  - a declaration's initializer is checked BEFORE the new name exists, so
    in `var x = x + 1;` the right-hand `x` must refer to an outer `x`
  - inner blocks may shadow outer names
Arrays:
  - `var a[N];` declares an array of N integers, 1 <= N <= MAX_ARRAY_SIZE
  - an array is used only through its elements: `a[i]` and `a[i] = v;`
    (no `a` on its own, no assignment of a whole array, no array arguments)
  - indexing something that is not an array is an error
Loops:
  - `break` and `continue` are allowed only inside a `while` or `for` loop
  - a variable declared in a `for` header belongs to the loop
Functions:
  - functions are declared at the top level and can be called from anywhere
    in the program, before or after their declaration (also recursively)
  - two functions cannot have the same name; a function's parameters must
    be distinct; functions and variables have separate name spaces
  - a call must name a declared function and pass exactly as many arguments
    as it has parameters
  - a function body sees its own parameters and local variables, and the
    program's top-level variables and arrays (those declared outside any
    block, anywhere in the program): its "globals". A local name hides a
    global of the same name.
  - `return` is allowed only inside a function

All errors are collected (not just the first one), in source order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set

from .errors import CompileError
from .nodes import (ArrayDecl, Assign, Binary, Block, Break, Call, CallStmt, Continue, For,
                    FunDecl, If, Index, IndexAssign, IntLit, Print, Program, Return, Unary, Var,
                    VarDecl, While)

MAX_ARRAY_SIZE = 1024


@dataclass
class Symbol:
    name: str
    line: int
    col: int
    depth: int              # 0 = top level, 1 = first nested block, ...
    kind: str = "var"       # "var", "array", "param" or "function"
    owner: str = ""         # the function it belongs to ("" = top-level code)
    size: int = 0           # arrays: number of elements


@dataclass
class Scope:
    symbols: Dict[str, Symbol] = field(default_factory=dict)


class SymbolTable:
    """A stack of scopes; lookup searches from the innermost outward."""

    def __init__(self) -> None:
        self.scopes: List[Scope] = [Scope()]
        self.all_symbols: List[Symbol] = []       # every declaration, for reporting
        self.functions: Dict[str, FunDecl] = {}
        self.owner = ""
        self.globals_used: Dict[str, Set[str]] = {}   # function -> top-level names it uses

    def enter(self) -> None:
        self.scopes.append(Scope())

    def leave(self) -> None:
        self.scopes.pop()

    def declare(self, name: str, line: int, col: int, kind: str = "var", size: int = 0) -> Symbol:
        sym = Symbol(name, line, col, len(self.scopes) - 1, kind, self.owner, size)
        self.scopes[-1].symbols[name] = sym
        self.all_symbols.append(sym)
        return sym

    def lookup_current(self, name: str):
        return self.scopes[-1].symbols.get(name)

    def lookup(self, name: str):
        for scope in reversed(self.scopes):
            if name in scope.symbols:
                return scope.symbols[name]
        return None


def check(program: Program) -> SymbolTable:
    """Return the symbol table, or raise SemanticErrors listing every problem."""
    return _Checker().run(program)


class SemanticErrors(Exception):
    def __init__(self, errors: List[CompileError]) -> None:
        super().__init__("\n".join(str(e) for e in errors))
        self.errors = errors


class _Checker:
    def __init__(self) -> None:
        self.table = SymbolTable()
        self.errors: List[CompileError] = []
        self.loops = 0                                # enclosing while/for loops
        self.in_function = False
        self.top: Dict[str, Symbol] = {}              # top-level variables and arrays (globals)

    def error(self, message: str, node) -> None:
        self.errors.append(CompileError("semantic", message, node.line, node.col))

    def run(self, program: Program) -> SymbolTable:
        for f in program.functions():                 # every function is visible everywhere
            if f.name in self.table.functions:
                prev = self.table.functions[f.name]
                self.error(f"function '{f.name}' is already declared (line {prev.line})", f)
            else:
                self.table.functions[f.name] = f
                self.table.all_symbols.append(Symbol(f.name, f.line, f.col, 0, "function"))
        for name, d in program.top_level_names().items():
            kind = "array" if isinstance(d, ArrayDecl) else "var"
            self.top[name] = Symbol(name, d.line, d.col, 0, kind, "", getattr(d, "size", 0))
        for stmt in program.body:
            if isinstance(stmt, FunDecl):
                self.function(stmt)
            else:
                self.stmt(stmt)
        if self.errors:
            self.errors.sort(key=lambda e: (e.line, e.col))     # source order
            raise SemanticErrors(self.errors)
        return self.table

    def function(self, f: FunDecl) -> None:
        outer_scopes, self.table.scopes = self.table.scopes, [Scope()]
        self.table.owner, self.in_function, self.loops = f.name, True, 0
        self.table.globals_used.setdefault(f.name, set())
        for p in f.params:
            if self.table.lookup_current(p) is not None:
                self.error(f"parameter '{p}' appears twice in '{f.name}'", f)
            else:
                self.table.declare(p, f.line, f.col, "param")
        for inner in f.body.body:                     # the body shares the parameters' scope
            self.stmt(inner)
        self.table.scopes, self.table.owner, self.in_function = outer_scopes, "", False

    def resolve(self, name: str):
        """The symbol a name refers to here: local first, then (in a function) a global."""
        sym = self.table.lookup(name)
        if sym is None and self.in_function and name in self.top:
            sym = self.top[name]
            self.table.globals_used[self.table.owner].add(name)
        return sym

    def stmt(self, s) -> None:
        if isinstance(s, FunDecl):                    # the parser never puts one here
            self.error("functions can only be declared at the top level", s)
        elif isinstance(s, Return):
            if not self.in_function:
                self.error("'return' outside a function", s)
            self.expr(s.value)
        elif isinstance(s, CallStmt):
            self.expr(s.call)
        elif isinstance(s, VarDecl):
            self.expr(s.init)                       # before the name exists
            self.declare(s, "var")
        elif isinstance(s, ArrayDecl):
            if not 1 <= s.size <= MAX_ARRAY_SIZE:
                self.error(f"array size must be between 1 and {MAX_ARRAY_SIZE}, not {s.size}", s)
            self.declare(s, "array", s.size)
        elif isinstance(s, Assign):
            sym = self.resolve(s.name)
            if sym is None:
                self.error(f"assignment to undeclared variable '{s.name}'", s)
            elif sym.kind == "array":
                self.error(f"cannot assign to the whole array '{s.name}'; assign to an element: "
                           f"{s.name}[i] = ...", s)
            self.expr(s.value)
        elif isinstance(s, IndexAssign):
            self.array_ref(s.name, s)
            self.expr(s.index)
            self.expr(s.value)
        elif isinstance(s, Print):
            self.expr(s.value)
        elif isinstance(s, Block):
            self.table.enter()
            for inner in s.body:
                self.stmt(inner)
            self.table.leave()
        elif isinstance(s, If):
            self.expr(s.cond)
            self.stmt(s.then)
            if s.orelse is not None:
                self.stmt(s.orelse)
        elif isinstance(s, While):
            self.expr(s.cond)
            self.loops += 1
            self.stmt(s.body)
            self.loops -= 1
        elif isinstance(s, For):
            self.table.enter()                      # the loop's own scope, for its variable
            if s.init is not None:
                self.stmt(s.init)
            self.expr(s.cond)
            if s.update is not None:
                self.stmt(s.update)
            self.loops += 1
            self.stmt(s.body)
            self.loops -= 1
            self.table.leave()
        elif isinstance(s, (Break, Continue)):
            if self.loops == 0:
                word = "break" if isinstance(s, Break) else "continue"
                self.error(f"'{word}' outside a loop", s)
        else:
            raise TypeError(f"unknown statement {type(s).__name__}")

    def declare(self, s, kind: str, size: int = 0) -> None:
        prev = self.table.lookup_current(s.name)
        if prev is not None:
            self.error(f"'{s.name}' is already declared in this block (line {prev.line})", s)
        else:
            self.table.declare(s.name, s.line, s.col, kind, size)

    def array_ref(self, name: str, node) -> None:
        sym = self.resolve(name)
        if sym is None:
            self.error(f"use of undeclared array '{name}'", node)
        elif sym.kind != "array":
            self.error(f"'{name}' is not an array", node)

    def expr(self, e) -> None:
        if isinstance(e, IntLit):
            return
        if isinstance(e, Var):
            sym = self.resolve(e.name)
            if sym is None:
                self.error(f"use of undeclared variable '{e.name}'", e)
            elif sym.kind == "array":
                self.error(f"'{e.name}' is an array; read an element with {e.name}[i]", e)
        elif isinstance(e, Index):
            self.array_ref(e.name, e)
            self.expr(e.index)
        elif isinstance(e, Unary):
            self.expr(e.operand)
        elif isinstance(e, Binary):
            self.expr(e.left)
            self.expr(e.right)
        elif isinstance(e, Call):
            f = self.table.functions.get(e.name)
            if f is None:
                self.error(f"call to undeclared function '{e.name}'", e)
            elif len(e.args) != len(f.params):
                self.error(f"'{e.name}' takes {len(f.params)} argument{'' if len(f.params) == 1 else 's'}, "
                           f"but {len(e.args)} {'was' if len(e.args) == 1 else 'were'} given", e)
            for a in e.args:
                self.expr(a)
        else:
            raise TypeError(f"unknown expression {type(e).__name__}")
