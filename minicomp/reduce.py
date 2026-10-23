"""Test-case reducer: shrink a failing program to a minimal one that still fails.

A program found by the fuzzer is often dozens of lines long, while the
compiler bug it exposes usually depends on just a few of them. The reducer
removes everything else automatically, in the style of delta debugging
(Zeller and Hildebrandt, 2002) and C-Reduce (Regehr et al., 2012).

A candidate change is KEPT only if the program is still "interesting":
  1. it still passes semantic analysis (no undeclared variables),
  2. the reference interpreter still finishes (no infinite loop created),
  3. exactly the same compiled versions still fail, in the same way
     (mismatch stays mismatch, crash stays crash).
Rule 3 stops the reducer from drifting onto a different bug, and keeps the
blame (the suspect passes) the same as in the original failure.

Changes are tried in three families, repeated until none succeeds (a fixed
point) or the test budget runs out:
  statements   delete chunks of each statement list: half the list, then
               quarters, ... down to single statements
  structure    replace an if / while / block by its body; drop an else
  expressions  replace an expression by 0, by 1, or by one of its operands

Each change is made in place and undone if it is not interesting, so the
reducer never holds more than one copy of the program.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, fields, is_dataclass
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

from .harness import Verdict, check_program
from .nodes import (Assign, Binary, Block, If, IntLit, Print, Program, Unary,
                    Var, VarDecl, While)
from .opt import ALL_PASSES
from .printer import to_source
from .semantic import SemanticErrors, check


class NotAFailure(ValueError):
    """The program given to the reducer does not fail, so there is nothing to keep."""


class _OutOfBudget(Exception):
    pass


@dataclass
class ReduceResult:
    original_source: str
    source: str                   # the reduced program
    program: Program
    verdict: Verdict              # full 8-way verdict of the reduced program
    tests: int                    # candidate programs checked
    seconds: float
    before: dict                  # {"lines", "statements", "nodes"}
    after: dict
    complete: bool                # True = reached a fixed point; False = budget ran out


def measure(program: Program) -> dict:
    return {"lines": to_source(program).count("\n"),
            "statements": _count(program.body, statements_only=True),
            "nodes": _count(program.body)}


def _count(node, statements_only: bool = False) -> int:
    if isinstance(node, list):
        return sum(_count(n, statements_only) for n in node)
    if not is_dataclass(node):
        return 0
    stmt_types = (VarDecl, Assign, Print, If, While, Block)
    own = 1 if (not statements_only or isinstance(node, stmt_types)) else 0
    return own + sum(_count(getattr(node, f.name), statements_only) for f in fields(node) if f.compare)


# ----------------------------------------------------------------------
# Walking the tree
# ----------------------------------------------------------------------
def statement_lists(program: Program) -> List[list]:
    """Every list of statements in the program, outermost first."""
    out = [program.body]

    def walk(stmts):
        for s in stmts:
            if isinstance(s, Block):
                out.append(s.body)
                walk(s.body)
            elif isinstance(s, If):
                out.append(s.then.body)
                walk(s.then.body)
                if isinstance(s.orelse, Block):
                    out.append(s.orelse.body)
                    walk(s.orelse.body)
                elif isinstance(s.orelse, If):
                    walk([s.orelse])
            elif isinstance(s, While):
                out.append(s.body.body)
                walk(s.body.body)
    walk(program.body)
    return out


def expression_slots(program: Program) -> List[Tuple[object, str]]:
    """Every place an expression sits, as (owner node, attribute name), in source order."""
    slots: List[Tuple[object, str]] = []

    def expr(owner, attr):
        slots.append((owner, attr))
        e = getattr(owner, attr)
        if isinstance(e, Unary):
            expr(e, "operand")
        elif isinstance(e, Binary):
            expr(e, "left")
            expr(e, "right")

    def stmts(lst):
        for s in lst:
            if isinstance(s, VarDecl):
                expr(s, "init")
            elif isinstance(s, (Assign, Print)):
                expr(s, "value")
            elif isinstance(s, If):
                _if(s)
            elif isinstance(s, While):
                expr(s, "cond")
                stmts(s.body.body)
            elif isinstance(s, Block):
                stmts(s.body)

    def _if(s):
        expr(s, "cond")
        stmts(s.then.body)
        if isinstance(s.orelse, Block):
            stmts(s.orelse.body)
        elif isinstance(s.orelse, If):
            _if(s.orelse)

    stmts(program.body)
    return slots


def _simpler(e) -> List[object]:
    """Replacements for an expression, simplest first."""
    if isinstance(e, IntLit):
        return [IntLit(v) for v in (0, 1) if v != e.value] if e.value not in (0, 1) else []
    if isinstance(e, Var):
        return [IntLit(0), IntLit(1)]
    if isinstance(e, Unary):
        return [IntLit(0), IntLit(1), e.operand]
    if isinstance(e, Binary):
        return [IntLit(0), IntLit(1), e.left, e.right]
    return []


# ----------------------------------------------------------------------
# The reducer
# ----------------------------------------------------------------------
class Reducer:
    def __init__(self, program: Program, verdict: Verdict, passes: Sequence[str] = ALL_PASSES,
                 max_tests: int = 4000, time_limit: float = 90.0) -> None:
        if verdict.status not in ("mismatch", "crash"):
            raise NotAFailure(f"the program does not fail (status: {verdict.status})")
        self.program = copy.deepcopy(program)
        self.status = verdict.status
        self.failing = sorted(verdict.failing)
        self.passes = list(passes)
        self.max_tests, self.deadline = max_tests, time.perf_counter() + time_limit
        self.tests = 0

    def interesting(self) -> bool:
        if self.tests >= self.max_tests or time.perf_counter() > self.deadline:
            raise _OutOfBudget
        self.tests += 1
        try:
            check(self.program)
        except SemanticErrors:
            return False
        try:
            v = check_program(self.program, self.passes, only=self.failing)
        except Exception:
            return False
        return v.status == self.status and sorted(v.failing) == self.failing

    # -- family 1: delete chunks of statements ----------------------------
    def drop_statements(self) -> bool:
        changed = False
        for lst in statement_lists(self.program):
            if not any(lst is attached for attached in statement_lists(self.program)):
                continue                     # removed together with its parent earlier
            chunk = max(1, len(lst) // 2)
            while True:
                i = 0
                while i < len(lst):
                    removed = lst[i:i + chunk]
                    del lst[i:i + chunk]
                    if self.interesting():
                        changed = True       # keep; the next chunk slid into place i
                    else:
                        lst[i:i] = removed
                        i += chunk
                if chunk == 1:
                    break
                chunk //= 2
        return changed

    # -- family 2: flatten structure ---------------------------------------
    def _structure_options(self, lst: list, i: int) -> Iterator[Tuple[Callable[[], None], Callable[[], None]]]:
        s = lst[i]

        def splice(new_stmts):
            return (lambda: lst.__setitem__(slice(i, i + 1), list(new_stmts)),
                    lambda: lst.__setitem__(slice(i, i + len(new_stmts)), [s]))

        if isinstance(s, If):
            yield splice(s.then.body)
            if isinstance(s.orelse, Block):
                yield splice(s.orelse.body)
            elif isinstance(s.orelse, If):
                yield splice([s.orelse])
            if s.orelse is not None:
                old = s.orelse
                yield (lambda: setattr(s, "orelse", None), lambda: setattr(s, "orelse", old))
        elif isinstance(s, While):
            yield splice(s.body.body)
        elif isinstance(s, Block):
            yield splice(s.body)

    def flatten(self) -> bool:
        changed = False
        for lst in statement_lists(self.program):
            if not any(lst is attached for attached in statement_lists(self.program)):
                continue
            i = 0
            while i < len(lst):
                accepted = False
                for apply, undo in self._structure_options(lst, i):
                    apply()
                    if self.interesting():
                        accepted = changed = True
                        break
                    undo()
                if not accepted:
                    i += 1                   # on success, look at what now stands at i
        return changed

    # -- family 3: simplify expressions -------------------------------------
    def simplify_expressions(self) -> bool:
        changed = False
        index = 0
        while True:
            slots = expression_slots(self.program)    # recomputed: the tree changes
            if index >= len(slots):
                return changed
            owner, attr = slots[index]
            original = getattr(owner, attr)
            for candidate in _simpler(original):
                setattr(owner, attr, candidate)
                if self.interesting():
                    changed = True
                    break
                setattr(owner, attr, original)
            index += 1

    # -- driver ---------------------------------------------------------------
    def run(self) -> bool:
        """Reduce in place. Returns True if a fixed point was reached."""
        try:
            while True:
                progress = self.drop_statements()
                progress |= self.flatten()
                progress |= self.simplify_expressions()
                if not progress:
                    return True
        except _OutOfBudget:
            return False


def reduce_program(program: Program, verdict: Optional[Verdict] = None,
                   passes: Sequence[str] = ALL_PASSES, max_tests: int = 4000,
                   time_limit: float = 90.0) -> ReduceResult:
    """Shrink a failing program. Raises NotAFailure if it does not fail."""
    start = time.perf_counter()
    check(program)
    if verdict is None:
        verdict = check_program(program, passes)
    r = Reducer(program, verdict, passes, max_tests, time_limit)
    before = measure(program)
    complete = r.run()
    final = check_program(r.program, passes)
    return ReduceResult(
        original_source=to_source(program), source=to_source(r.program), program=r.program,
        verdict=final, tests=r.tests, seconds=round(time.perf_counter() - start, 2),
        before=before, after=measure(r.program), complete=complete)
