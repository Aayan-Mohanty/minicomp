"""Phase 3: semantic analysis with a scoped symbol table.

Rules checked:
  - a variable must be declared (with `var`) before it is read or assigned
  - a name cannot be declared twice in the same block
  - a declaration's initializer is checked BEFORE the new name exists, so
    in `var x = x + 1;` the right-hand `x` must refer to an outer `x`
  - inner blocks may shadow outer names

All errors are collected (not just the first one), in source order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .errors import CompileError
from .nodes import (Assign, Binary, Block, If, IntLit, Print, Program, Unary,
                    Var, VarDecl, While)


@dataclass
class Symbol:
    name: str
    line: int
    col: int
    depth: int              # 0 = top level, 1 = first nested block, ...


@dataclass
class Scope:
    symbols: Dict[str, Symbol] = field(default_factory=dict)


class SymbolTable:
    """A stack of scopes; lookup searches from the innermost outward."""

    def __init__(self) -> None:
        self.scopes: List[Scope] = [Scope()]
        self.all_symbols: List[Symbol] = []       # every declaration, for reporting

    def enter(self) -> None:
        self.scopes.append(Scope())

    def leave(self) -> None:
        self.scopes.pop()

    def declare(self, name: str, line: int, col: int) -> Symbol:
        sym = Symbol(name, line, col, len(self.scopes) - 1)
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

    def error(self, message: str, node) -> None:
        self.errors.append(CompileError("semantic", message, node.line, node.col))

    def run(self, program: Program) -> SymbolTable:
        for stmt in program.body:
            self.stmt(stmt)
        if self.errors:
            raise SemanticErrors(self.errors)
        return self.table

    def stmt(self, s) -> None:
        if isinstance(s, VarDecl):
            self.expr(s.init)                       # before the name exists
            prev = self.table.lookup_current(s.name)
            if prev is not None:
                self.error(f"'{s.name}' is already declared in this block (line {prev.line})", s)
            else:
                self.table.declare(s.name, s.line, s.col)
        elif isinstance(s, Assign):
            if self.table.lookup(s.name) is None:
                self.error(f"assignment to undeclared variable '{s.name}'", s)
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
            self.stmt(s.body)
        else:
            raise TypeError(f"unknown statement {type(s).__name__}")

    def expr(self, e) -> None:
        if isinstance(e, IntLit):
            return
        if isinstance(e, Var):
            if self.table.lookup(e.name) is None:
                self.error(f"use of undeclared variable '{e.name}'", e)
        elif isinstance(e, Unary):
            self.expr(e.operand)
        elif isinstance(e, Binary):
            self.expr(e.left)
            self.expr(e.right)
        else:
            raise TypeError(f"unknown expression {type(e).__name__}")
