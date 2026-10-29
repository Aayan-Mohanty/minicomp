"""Intermediate code generation: AST -> three-address code.

Translation scheme (the standard one for structured statements):

    if (c) S1 else S2        while (c) S                for (I; c; U) S
    ------------------       ------------------         ------------------
        t = <c>              Lhead:                         <I>
        if t goto L1 else L2     t = <c>                Lhead:
    L1: <S1>                     if t goto Lbody else Lend  t = <c>
        goto Lend            Lbody:                         if t goto Lbody else Lend
    L2: <S2>                     <S>                    Lbody:
    Lend:                        goto Lhead                 <S>
                             Lend:                      Lnext:
                                                            <U>
                                                            goto Lhead
                                                        Lend:

`break` jumps to the innermost loop's Lend; `continue` jumps to Lhead in a
`while` loop and to Lnext (the update) in a `for` loop.

`&&` and `||` are emitted as ordinary binary operators when their right
operand is a constant or a variable: evaluating it then costs nothing and
can never fail. Otherwise short-circuit jumps are emitted, so the compiled
code evaluates the right operand exactly when the reference does:

    t = a && <e>             t = a || <e>
    ---------------------    ---------------------
        %1 = <a>                 %1 = <a>
        t' = 0                   t' = 1
        if %1 goto L1 else L2    if %1 goto L2 else L1
    L1: %2 = <e>             L1: %2 = <e>
        t' = %2 != 0             t' = %2 != 0
    L2: t = t'               L2: t = t'

Arrays: `var a[8];` becomes `array a[8]`, `a[i]` a `load` into a
temporary and `a[i] = v;` a `store`.

Functions: main's code comes first; each function follows as
`fun f(p, q):` + body + `return 0` (reached only if the body can fall
off its end). Each function has its own variable names.

Globals: a top-level variable or array that some function uses is named
@x everywhere (main and functions). Main starts with a prologue that sets
every such variable to 0 and creates every such array, because a function
may run before the declaration does (the declaration then sets the value
again). A global read as the left operand of an operator, or as a call
argument, is first copied into a temporary when a call is evaluated later
in the same expression, since that call may change it: the operand must
have the value it had when the reference read it, left to right.

The input must already have passed semantic analysis.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from .nodes import (ArrayDecl, Assign, Binary, Block, Break, Call, CallStmt, Continue, For,
                    If, Index, IndexAssign, IntLit, Print, Program, Return, Unary, Var, VarDecl,
                    While)
from .tac import Instr, Operand, binop, copy, is_global, unop


def has_call(e) -> bool:
    if isinstance(e, Call):
        return True
    if isinstance(e, Unary):
        return has_call(e.operand)
    if isinstance(e, Binary):
        return has_call(e.left) or has_call(e.right)
    if isinstance(e, Index):
        return has_call(e.index)
    return False


def escaping_globals(program: Program) -> Set[str]:
    """Top-level names that some function uses (they become @name)."""
    from .semantic import check
    used = check(program).globals_used
    return set().union(*used.values()) if used else set()


def lower(program: Program) -> List[Instr]:
    return _Lowerer(escaping_globals(program)).run(program)


class _Lowerer:
    def __init__(self, globals_: Set[str]) -> None:
        self.code: List[Instr] = []
        self.ntemp = 0
        self.nlabel = 0
        self.scopes: List[Dict[str, str]] = [{}]    # source name -> unique TAC name
        self.declared: Dict[str, int] = {}          # how many times each name was declared
        self.globals = globals_                      # top-level names used by functions
        self.in_function = False
        self.loops: List[Tuple[str, str]] = []      # (break label, continue label), innermost last

    # -- names -----------------------------------------------------------
    def temp(self) -> str:
        self.ntemp += 1
        return f"%{self.ntemp}"

    def label(self) -> str:
        self.nlabel += 1
        return f"L{self.nlabel}"

    def declare(self, name: str) -> str:
        if not self.in_function and len(self.scopes) == 1 and name in self.globals:
            self.scopes[0][name] = "@" + name        # a top-level global
            return "@" + name
        n = self.declared.get(name, 0)
        self.declared[name] = n + 1
        unique = name if n == 0 else f"{name}#{n}"
        self.scopes[-1][name] = unique
        return unique

    def resolve(self, name: str) -> str:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        if self.in_function:
            return "@" + name                        # not local: a global (semantic analysis checked it)
        raise NameError(name)                        # unreachable after semantic checking

    def emit(self, ins: Instr) -> None:
        self.code.append(ins)

    # -- statements ------------------------------------------------------
    def run(self, program: Program) -> List[Instr]:
        for name, d in program.top_level_names().items():       # prologue: globals start at 0
            if name in self.globals:
                if isinstance(d, ArrayDecl):
                    self.emit(Instr("array", target="@" + name, a=d.size))
                else:
                    self.emit(copy("@" + name, 0))
        for s in program.main():
            self.stmt(s)
        self.in_function = True
        for f in program.functions():
            self.scopes, self.declared = [{}], {}       # a function has its own variables
            self.emit(Instr("func", target=f.name, args=tuple(self.declare(p) for p in f.params)))
            for s in f.body.body:
                self.stmt(s)
            self.emit(Instr("ret", a=0))
        return self.code

    def stmt(self, s) -> None:
        if isinstance(s, VarDecl):
            value = self.expr(s.init)                 # before the new name exists
            self.emit(copy(self.declare(s.name), value))
        elif isinstance(s, ArrayDecl):
            self.emit(Instr("array", target=self.declare(s.name), a=s.size))
        elif isinstance(s, Assign):
            self.expr(s.value, dst=self.resolve(s.name))
        elif isinstance(s, IndexAssign):
            index = self.expr(s.index, protect=has_call(s.value))
            value = self.expr(s.value)
            self.emit(Instr("store", target=self.resolve(s.name), a=index, b=value))
        elif isinstance(s, Print):
            self.emit(Instr("print", a=self.expr(s.value)))
        elif isinstance(s, CallStmt):
            self.call(s.call, None)
        elif isinstance(s, Return):
            self.emit(Instr("ret", a=self.expr(s.value)))
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
            self.loops.append((l_end, l_head))
            self.stmt(s.body)
            self.loops.pop()
            self.emit(Instr("jump", target=l_head))
            self.emit(Instr("label", target=l_end))
        elif isinstance(s, For):
            self.scopes.append({})                    # the loop variable's scope
            if s.init is not None:
                self.stmt(s.init)
            l_head, l_body, l_next, l_end = self.label(), self.label(), self.label(), self.label()
            self.emit(Instr("label", target=l_head))
            cond = self.expr(s.cond)
            self.emit(Instr("cbr", a=cond, target=l_body, target2=l_end))
            self.emit(Instr("label", target=l_body))
            self.loops.append((l_end, l_next))
            self.stmt(s.body)
            self.loops.pop()
            self.emit(Instr("label", target=l_next))
            if s.update is not None:
                self.stmt(s.update)
            self.emit(Instr("jump", target=l_head))
            self.emit(Instr("label", target=l_end))
            self.scopes.pop()
        elif isinstance(s, Break):
            self.emit(Instr("jump", target=self.loops[-1][0]))
        elif isinstance(s, Continue):
            self.emit(Instr("jump", target=self.loops[-1][1]))
        else:
            raise TypeError(f"unknown statement {type(s).__name__}")

    # -- expressions -----------------------------------------------------
    def expr(self, e, dst: Optional[str] = None, protect: bool = False) -> Operand:
        """Emit code for `e`. If `dst` is given, the result is stored there.

        protect: a call is evaluated after this operand is read and before
        it is used, so a global read here is copied into a temporary first.
        """
        if isinstance(e, (IntLit, Var)):
            value: Operand = e.value if isinstance(e, IntLit) else self.resolve(e.name)
            if dst is not None:
                self.emit(copy(dst, value))
                return dst
            if protect and is_global(value):
                t = self.temp()
                self.emit(copy(t, value))
                return t
            return value
        if isinstance(e, Index):
            index = self.expr(e.index)
            out = dst or self.temp()
            self.emit(Instr("load", dst=out, target=self.resolve(e.name), a=index))
            return out
        if isinstance(e, Unary):
            a = self.expr(e.operand)
            out = dst or self.temp()
            self.emit(unop(out, e.op, a))
            return out
        if isinstance(e, Binary):
            if e.op in ("&&", "||") and not isinstance(e.right, (IntLit, Var)):
                return self.short_circuit(e, dst)
            a = self.expr(e.left, protect=has_call(e.right))
            b = self.expr(e.right)
            out = dst or self.temp()
            self.emit(binop(out, e.op, a, b))
            return out
        if isinstance(e, Call):
            out = dst or self.temp()
            self.call(e, out)
            return out
        raise TypeError(f"unknown expression {type(e).__name__}")

    def call(self, e: Call, dst: Optional[str]) -> None:
        args = tuple(self.expr(a, protect=any(has_call(b) for b in e.args[i + 1:]))
                     for i, a in enumerate(e.args))      # left to right
        self.emit(Instr("call", dst=dst, target=e.name, args=args))

    def short_circuit(self, e: Binary, dst: Optional[str]) -> Operand:
        a = self.expr(e.left)
        t = self.temp()                                  # not dst: the right side may read it
        l_right, l_end = self.label(), self.label()
        if e.op == "&&":
            self.emit(copy(t, 0))
            self.emit(Instr("cbr", a=a, target=l_right, target2=l_end))
        else:
            self.emit(copy(t, 1))
            self.emit(Instr("cbr", a=a, target=l_end, target2=l_right))
        self.emit(Instr("label", target=l_right))
        b = self.expr(e.right)
        self.emit(binop(t, "!=", b, 0))
        self.emit(Instr("label", target=l_end))
        if dst is not None:
            self.emit(copy(dst, t))
            return dst
        return t
