"""Planted optimizer bugs ("mutants") for evaluating the fuzzer.

Each bug is a small, realistic mistake in ONE optimization pass or in the
register allocator, the kind that real compilers have shipped: a wrong
algebraic shortcut, constant folding that uses the wrong arithmetic, a
data-flow analysis that forgets a case or uses the wrong meet, a cache of
facts that is not invalidated, a call treated as pure, a missing
interference edge, a scratch register used twice.

A bug is switched on only inside a `with planted(name):` block and is
always switched off again afterwards, even if an exception occurs. The
reference interpreter, the VM and the register machine are never changed:
every bug lives in the compiler alone, so the reference still defines the
correct answer.

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

from . import ops, opt, regalloc
from .cfg import CFG, EXIT
from .tac import Instr


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
# constprop and calls
# ----------------------------------------------------------------------
def _call_result_undef():
    original = opt._transfer_const

    def wrong(ins, env):
        if ins.kind == "call" and ins.dst is not None:
            opt._call_clobbers(env)
            env.pop(ins.dst, None)         # "no value yet" (UNDEF) instead of NAC: a constant
            return                         # from another path then wins at the next join
        original(ins, env)
    return _patch(opt, "_transfer_const", wrong)


def _call_keeps_globals():
    return _patch(opt, "_call_clobbers", lambda env: None)   # a global's constant survives a call


# ----------------------------------------------------------------------
# copyprop (available copies)
# ----------------------------------------------------------------------
def _copyprop_no_kill():
    def wrong(ins, facts):
        if ins.kind == "call":
            opt._copy_call_kill(facts)
        d = ins.defs()
        if d is None:
            return
        for f in [f for f in facts if f[0] == d]:   # forgets to drop x -> y when y is reassigned
            facts.discard(f)
        if ins.kind == "copy" and isinstance(ins.a, str) and ins.a != d:
            facts.add((d, ins.a))
    return _patch(opt, "_copy_transfer", wrong)


def _union(sources):
    return frozenset().union(*sources)     # "may" meet where a "must" meet is needed


def _copyprop_meet_union():
    return _patch(opt, "_copy_join", _union)


def _copyprop_call_keeps_globals():
    return _patch(opt, "_copy_call_kill", lambda facts: None)   # x = @g survives a call that may change @g


# ----------------------------------------------------------------------
# cse (available expressions)
# ----------------------------------------------------------------------
def _cse_no_kill():
    def wrong(ins, facts):
        opt._cse_effects(ins, facts)
        d = ins.defs()
        if d is None:
            return
        for f in [f for f in facts if f[1] == d]:   # forgets that a ? b is stale once a or b changes
            facts.discard(f)
        key = opt._expr_key(ins)
        if key is not None and d not in key[1:]:
            facts.add((key, d))
    return _patch(opt, "_cse_transfer", wrong)


def _cse_sub_commutes():
    opt.COMMUTATIVE.add("-")               # treats a - b as equal to b - a
    return lambda: opt.COMMUTATIVE.discard("-")


def _cse_meet_union():
    return _patch(opt, "_cse_join", _union)


def _cse_load_ignores_store():
    return _patch(opt, "_cse_store_kills", lambda facts, name: None)   # a[i] reused after a store to a


# ----------------------------------------------------------------------
# dce (liveness)
# ----------------------------------------------------------------------
def _liveness_one_sweep():
    def wrong(cfg: CFG) -> Dict[int, Set[str]]:
        gset = opt.unit_globals(cfg)
        by_id = cfg.by_id()
        live_in: Dict[int, Set[str]] = {b.id: set() for b in cfg.blocks}
        live_out: Dict[int, Set[str]] = {b.id: set() for b in cfg.blocks}
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
                live.update(opt.live_uses(ins, cfg, gset))
            live_out[bid], live_in[bid] = out, live
        return live_out
    return wrong


def _liveness_ignores_branch():
    def wrong_uses(ins):
        if ins.kind == "cbr":
            return []                      # forgets that a branch reads its condition
        return ins.uses()
    return _patch(opt, "_uses", wrong_uses)


def _liveness_no_backedge():
    return _patch(opt, "liveness", _liveness_one_sweep())


def _liveness_return_ignores_globals():
    return _patch(opt, "_ret_reads_globals", lambda gset: set())   # the caller's later reads are forgotten


def _dce_deletes_call():
    original = Instr.has_effect

    def wrong(self):
        if self.kind == "call":
            return False                   # treats a call like arithmetic: deletable if unused
        return original(self)
    return _patch(Instr, "has_effect", wrong)


# ----------------------------------------------------------------------
# regalloc
# ----------------------------------------------------------------------
def _regalloc_dead_def():
    original = regalloc._def_edges

    def wrong(ins, d, live_after):
        if d not in live_after:
            return set()                   # a result nobody reads gets no interference edges,
        return original(ins, d, live_after)   # so writing it may clobber a live register
    return _patch(regalloc, "_def_edges", wrong)


def _regalloc_entry_params():
    def wrong(params, live_entry):
        return {}                          # forgets that the parameters are all written on entry
    return _patch(regalloc, "_param_edges", wrong)


def _regalloc_scratch_shared():
    return _patch(regalloc, "scratch_for", lambda position: regalloc.SCRATCH[0])   # both spilled operands in t0


BUGS: Dict[str, Bug] = {b.name: b for b in [
    Bug("div-self-one", "constprop", "algebraic shortcut x / x -> 1 (wrong when x is 0)", _div_self_one),
    Bug("fold-div-floor", "constprop", "constant division rounds down instead of toward zero", _fold_div_floor),
    Bug("fold-shift-unmasked", "constprop", "constant shifts by 32 or more give 0 instead of using the low 5 bits", _fold_shift_unmasked),
    Bug("fold-unsigned-compare", "constprop", "constant comparisons treat negative numbers as large unsigned ones", _fold_unsigned_compare),
    Bug("meet-keep-first", "constprop", "at a join, two different constants are merged into the first one instead of NAC", _meet_keep_first),
    Bug("unreachable-false-branch", "unreachable", "reachability ignores the false edge of a conditional branch", _unreachable_false_branch),
    Bug("call-result-undef", "constprop", "a call's result is treated as 'no value yet' instead of NAC", _call_result_undef),
    Bug("copyprop-no-kill", "copyprop", "after x = y, a later assignment to y does not cancel the copy", _copyprop_no_kill),
    Bug("copyprop-meet-union", "copyprop", "a copy available on ANY incoming path is used after a join (union, not intersection)", _copyprop_meet_union),
    Bug("cse-no-kill", "cse", "a remembered a ? b is reused after a or b has been reassigned", _cse_no_kill),
    Bug("cse-sub-commutes", "cse", "a - b is treated as the same expression as b - a", _cse_sub_commutes),
    Bug("cse-meet-union", "cse", "an expression available on ANY incoming path is reused after a join", _cse_meet_union),
    Bug("cse-load-ignores-store", "cse", "a remembered array read a[i] is reused after a store to a", _cse_load_ignores_store),
    Bug("call-keeps-globals", "constprop", "a global's known constant value survives a call that may change it", _call_keeps_globals),
    Bug("copyprop-call-keeps-globals", "copyprop", "a copy x = @g survives a call that may change @g", _copyprop_call_keeps_globals),
    Bug("liveness-ignores-branch", "dce", "liveness forgets that a conditional branch reads its condition", _liveness_ignores_branch),
    Bug("liveness-no-backedge", "dce", "liveness makes one backward sweep instead of iterating to a fixed point", _liveness_no_backedge),
    Bug("liveness-return-ignores-globals", "dce", "liveness forgets that a function's caller may read globals after it returns", _liveness_return_ignores_globals),
    Bug("dce-deletes-call", "dce", "a call whose result is unused is deleted, with its prints", _dce_deletes_call),
    Bug("regalloc-dead-def", "regalloc", "a result that is never read gets no interference edges", _regalloc_dead_def),
    Bug("regalloc-entry-params", "regalloc", "parameters get no interference edges at the function's entry", _regalloc_entry_params),
    Bug("regalloc-scratch-shared", "regalloc", "two spilled operands of one instruction are both loaded into t0", _regalloc_scratch_shared),
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
