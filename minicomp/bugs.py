"""Planted optimizer bugs ("mutants") for evaluating the fuzzer.

Each bug is a small, realistic mistake in ONE optimization pass, the kind
that real compilers have shipped: a wrong algebraic shortcut, constant
folding that uses the wrong arithmetic, a data-flow analysis that forgets
a case, a cache of facts that is not invalidated.

A bug is switched on only inside a `with planted(name):` block and is
always switched off again afterwards, even if an exception occurs. The
reference interpreter and the VM are never changed: every bug lives in
the optimizer alone, so the reference still defines the correct answer.

    from minicomp.bugs import BUGS, planted
    with planted("fold-div-floor"):
        ...  # the optimizer now folds division with floor rounding

Not thread-safe: use planted bugs from one thread at a time (the CLI and
the evaluation do; the web server never plants bugs).
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Dict, List, Set

from . import ops, opt
from .cfg import CFG, EXIT
from .tac import Instr

_original_uses = Instr.uses


@dataclass(frozen=True)
class Bug:
    name: str
    home: str               # the pass that contains the mistake
    summary: str            # one line: what is wrong
    install: Callable[[], Callable[[], None]]   # switches the bug on, returns the undo function


def _patch(obj, attr: str, value) -> Callable[[], None]:
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    return lambda: setattr(obj, attr, old)


def _patch_pass(name: str, func) -> Callable[[], None]:
    old = opt.PASS_FUNCS[name]
    opt.PASS_FUNCS[name] = func
    return lambda: opt.PASS_FUNCS.__setitem__(name, old)


# ----------------------------------------------------------------------
# constprop: folding and algebra
# ----------------------------------------------------------------------
def _div_self_one():
    original = opt._identity

    def wrong(op, a, b):
        if op == "/" and isinstance(a, str) and a == b:
            return 1                       # false when x == 0: in Mini 0 / 0 == 0
        return original(op, a, b)
    return _patch(opt, "_identity", wrong)


def _fold_div_floor():
    def wrong(op, a, b):
        if op == "/" and b != 0:
            return ops.wrap(a // b)        # rounds down; Mini rounds toward zero
        return ops.binary(op, a, b)
    return _patch(opt, "fold_binary", wrong)


def _fold_shift_unmasked():
    def wrong(op, a, b):
        if op == "<<" and not 0 <= b < 32:
            return 0                       # "all bits shifted out"; Mini uses b & 31
        if op == ">>" and not 0 <= b < 32:
            return -1 if a < 0 else 0
        return ops.binary(op, a, b)
    return _patch(opt, "fold_binary", wrong)


def _fold_unsigned_compare():
    def wrong(op, a, b):
        if op in ("<", "<=", ">", ">="):
            return ops.binary(op, a & 0xFFFFFFFF, b & 0xFFFFFFFF)   # Mini compares signed values
        return ops.binary(op, a, b)
    return _patch(opt, "fold_binary", wrong)


def _meet_keep_first():
    def wrong(envs):
        out = {}
        for env in envs:
            for var, val in env.items():
                if var not in out:
                    out[var] = val         # a conflicting value at a join is ignored, not NAC
        return out
    return _patch(opt, "_meet", wrong)


# ----------------------------------------------------------------------
# unreachable
# ----------------------------------------------------------------------
def _unreachable_false_branch():
    def wrong_reachable(cfg: CFG) -> set:
        if not cfg.blocks:
            return set()
        by_id = cfg.by_id()
        seen, stack = set(), [cfg.blocks[0].id]
        while stack:
            bid = stack.pop()
            if bid in seen or bid == EXIT:
                continue
            seen.add(bid)
            block = by_id[bid]
            last = block.instrs[-1] if block.instrs else None
            succ = block.succ[:1] if last is not None and last.kind == "cbr" else block.succ
            stack.extend(succ)             # forgets the "else" edge of a branch
        return seen

    def wrong_pass(cfg: CFG) -> None:
        live = wrong_reachable(cfg)
        cfg.blocks = [b for b in cfg.blocks if b.id in live]
        opt.connect(cfg.blocks)
    return _patch_pass("unreachable", wrong_pass)


# ----------------------------------------------------------------------
# copyprop
# ----------------------------------------------------------------------
def _copyprop_no_kill():
    def wrong(cfg: CFG) -> None:
        for block in cfg.blocks:
            copies: Dict[str, str] = {}
            new = []
            for ins in block.instrs:
                a = copies.get(ins.a, ins.a) if isinstance(ins.a, str) else ins.a
                b = copies.get(ins.b, ins.b) if isinstance(ins.b, str) else ins.b
                ins = ins.with_operands(a, b)
                d = ins.defs()
                if d is not None:
                    # forgets to drop x -> y when y itself is reassigned
                    copies = {x: y for x, y in copies.items() if x != d}
                    if ins.kind == "copy" and isinstance(ins.a, str) and ins.a != d:
                        copies[d] = ins.a
                new.append(ins)
            block.instrs = new
    return _patch_pass("copyprop", wrong)


# ----------------------------------------------------------------------
# cse
# ----------------------------------------------------------------------
def _cse_no_kill():
    def wrong(cfg: CFG) -> None:
        for block in cfg.blocks:
            available: Dict[tuple, str] = {}
            new = []
            for ins in block.instrs:
                key = opt._expr_key(ins)
                if key is not None and key in available and available[key] != ins.dst:
                    ins = opt.copy(ins.dst, available[key])
                d = ins.defs()
                if d is not None:
                    # forgets that a ? b is stale once a or b is reassigned
                    available = {k: v for k, v in available.items() if v != d}
                    key = opt._expr_key(ins)
                    if key is not None and d not in key[1:]:
                        available[key] = d
                new.append(ins)
            block.instrs = new
    return _patch_pass("cse", wrong)


def _cse_sub_commutes():
    opt.COMMUTATIVE.add("-")               # treats a - b as equal to b - a
    return lambda: opt.COMMUTATIVE.discard("-")


# ----------------------------------------------------------------------
# dce (liveness)
# ----------------------------------------------------------------------
def _liveness_ignores_branch():
    def wrong_uses(self):
        if self.kind == "cbr":
            return []                      # forgets that a branch reads its condition
        return _original_uses(self)
    return _patch(Instr, "uses", wrong_uses)


def _liveness_no_backedge():
    def wrong(cfg: CFG) -> Dict[int, Set[str]]:
        by_id = cfg.by_id()
        live_in: Dict[int, Set[str]] = {b.id: set() for b in cfg.blocks}
        live_out: Dict[int, Set[str]] = {}
        for bid in reversed([b.id for b in cfg.blocks]):   # ONE backward sweep, no fixed point:
            block = by_id[bid]                               # facts carried by loop back edges are lost
            out: Set[str] = set()
            for s in block.succ:
                if s != EXIT:
                    out |= live_in[s]
            live = set(out)
            for ins in reversed(block.instrs):
                d = ins.defs()
                if d is not None:
                    live.discard(d)
                live.update(ins.uses())
            live_out[bid], live_in[bid] = out, live
        return live_out
    return _patch(opt, "liveness", wrong)


BUGS: Dict[str, Bug] = {b.name: b for b in [
    Bug("div-self-one", "constprop", "algebraic shortcut x / x -> 1 (wrong when x is 0)", _div_self_one),
    Bug("fold-div-floor", "constprop", "constant division rounds down instead of toward zero", _fold_div_floor),
    Bug("fold-shift-unmasked", "constprop", "constant shifts by 32 or more give 0 instead of using the low 5 bits", _fold_shift_unmasked),
    Bug("fold-unsigned-compare", "constprop", "constant comparisons treat negative numbers as large unsigned ones", _fold_unsigned_compare),
    Bug("meet-keep-first", "constprop", "at a join, two different constants are merged into the first one instead of NAC", _meet_keep_first),
    Bug("unreachable-false-branch", "unreachable", "reachability ignores the false edge of a conditional branch", _unreachable_false_branch),
    Bug("copyprop-no-kill", "copyprop", "after x = y, a later assignment to y does not cancel the copy", _copyprop_no_kill),
    Bug("cse-no-kill", "cse", "a remembered a ? b is reused after a or b has been reassigned", _cse_no_kill),
    Bug("cse-sub-commutes", "cse", "a - b is treated as the same expression as b - a", _cse_sub_commutes),
    Bug("liveness-ignores-branch", "dce", "liveness forgets that a conditional branch reads its condition", _liveness_ignores_branch),
    Bug("liveness-no-backedge", "dce", "liveness makes one backward sweep instead of iterating to a fixed point", _liveness_no_backedge),
]}


@contextmanager
def planted(name: str):
    """Switch one bug on for the duration of the `with` block."""
    if name not in BUGS:
        raise ValueError(f"unknown bug '{name}'; known: {', '.join(BUGS)}")
    undo = BUGS[name].install()
    try:
        yield BUGS[name]
    finally:
        undo()


def bug_names() -> List[str]:
    return list(BUGS)
