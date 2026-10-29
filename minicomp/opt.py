"""Optimizer: classic machine-independent optimizations on the CFG.

Every unit (main and each function) is optimized on its own. Passes, run
repeatedly until the code stops changing:

  constprop   Global constant propagation (forward data-flow analysis over
              the CFG, Kildall's lattice UNDEF > constant > NAC), followed by
              constant folding and algebraic simplification of each
              instruction, and turning branches on constants into gotos.
              A function's parameters, every call result, every array
              element read and every global on entry are NAC.
  unreachable Removes blocks that can no longer be reached from the entry.
  copyprop    Global copy propagation: after `x = y`, a use of x reads y
              instead, wherever `x = y` reaches on EVERY path with neither x
              nor y reassigned since (available-copies analysis: forward,
              intersection at joins).
  cse         Global common-subexpression elimination: `d = a op b` (or an
              array read `d = a[i]`) becomes `d = h` where h holds the same
              value on EVERY path, with its operands and h unchanged since
              and, for an array read, no store to that array in between
              (available-expressions analysis: forward, intersection at
              joins; the holder must be the same variable on all paths).
  dce         Dead-code elimination using global liveness analysis
              (backward data-flow): an assignment or array read whose result
              is never read again is deleted. Calls, prints, branches,
              array stores and array creations are never deleted (a call
              may print), even when a call's result is never read.

Every Mini operation is total (no traps, no undefined behaviour; array
indexes wrap), so any assignment can be folded, moved or deleted without
changing what a program prints. A call is not an operation: it may print,
so it is never folded, merged with another call or deleted.

What a call can change in the caller: its own result, and every global
(@x, a top-level variable or array that functions use). It cannot change
the caller's other variables or local arrays. So at a call, facts about
globals are dropped (constants become NAC, copies and expressions that
mention a global are forgotten, remembered global array reads are
forgotten), and a call counts as reading every global (the callee may read
them). In a function, `return` also counts as reading every global, since
the caller may read them afterwards.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from . import ops
from .cfg import CFG, EXIT, build_cfg, connect, linearize, reachable
from .tac import Instr, Operand, Unit, copy, globals_in, is_global, join_units, split_units

# The optimizer evaluates constant expressions through these two hooks
# rather than calling ops directly. Normally they ARE the functions in ops;
# the bug-injection module (bugs.py) can replace them to plant a folding bug
# in the optimizer without touching the reference interpreter or the VM.
fold_binary = ops.binary
fold_unary = ops.unary

NAC = "NAC"          # "not a constant": the value may differ between executions
COMMUTATIVE = {"+", "*", "&", "|", "^", "==", "!=", "&&", "||"}
ALL_PASSES = ["constprop", "unreachable", "copyprop", "cse", "dce"]
MAX_ROUNDS = 25


# ======================================================================
# Constant folding and algebraic simplification (one instruction)
# ======================================================================
def fold(ins: Instr) -> Instr:
    """Simplify one instruction using only its own operands."""
    a, b = ins.a, ins.b
    if ins.kind == "unop" and isinstance(a, int):
        return copy(ins.dst, fold_unary(ins.op, a))
    if ins.kind == "binop":
        if isinstance(a, int) and isinstance(b, int):
            return copy(ins.dst, fold_binary(ins.op, a, b))
        simplified = _identity(ins.op, a, b)
        if simplified is not None:
            return copy(ins.dst, simplified)
    if ins.kind == "cbr":
        if isinstance(a, int):
            return Instr("jump", target=ins.target if a != 0 else ins.target2)
        if ins.target == ins.target2:
            return Instr("jump", target=ins.target)
    return ins


def _identity(op: str, a: Operand, b: Operand) -> Optional[Operand]:
    """Identities that hold for EVERY 32-bit value of the variable operand.

    Deliberately not included, because it is false in Mini:
      x / x -> 1   (fails for x = 0, since 0 / 0 == 0)
    """
    if op == "+" and b == 0: return a
    if op == "+" and a == 0: return b
    if op == "-" and b == 0: return a
    if op == "*" and (a == 0 or b == 0): return 0
    if op == "*" and b == 1: return a
    if op == "*" and a == 1: return b
    if op == "/" and b == 1: return a
    if op == "|" and b == 0: return a
    if op == "|" and a == 0: return b
    if op == "^" and b == 0: return a
    if op == "^" and a == 0: return b
    if op == "&" and (a == 0 or b == 0): return 0
    if op in ("<<", ">>") and b == 0: return a
    if op == "-" and isinstance(a, str) and a == b: return 0     # x - x == 0 even with wrap-around
    if op == "^" and isinstance(a, str) and a == b: return 0
    return None


# ======================================================================
# Global constant propagation (forward data-flow)
# ======================================================================
Env = Dict[str, object]        # var -> int constant or NAC; missing = UNDEF (no value yet)


def _meet(envs: Sequence[Env]) -> Env:
    out: Env = {}
    for env in envs:
        for var, val in env.items():
            if var not in out:
                out[var] = val
            elif out[var] != val:
                out[var] = NAC
    return out


def _value(x: Operand, env: Env):
    if isinstance(x, int):
        return x
    return env.get(x)          # None = UNDEF


def _call_clobbers(env: Env) -> None:
    """After a call, any global may hold a value the callee stored."""
    for v in env:
        if is_global(v):
            env[v] = NAC


def _transfer_const(ins: Instr, env: Env) -> None:
    if ins.kind == "call":
        _call_clobbers(env)
    d = ins.defs()
    if d is None:
        return
    if ins.kind in ("call", "load"):
        env[d] = NAC                             # whatever the function returns / the array holds
        return
    vals = [_value(x, env) for x in (ins.a, ins.b) if x is not None]
    if any(v == NAC for v in vals):
        env[d] = NAC
    elif any(v is None for v in vals):
        if is_global(d):
            env[d] = NAC                         # globals stay known to the analysis (see _call_clobbers)
        else:
            env.pop(d, None)                     # still UNDEF
    elif ins.kind == "copy":
        env[d] = vals[0]
    elif ins.kind == "unop":
        env[d] = fold_unary(ins.op, vals[0])
    else:
        env[d] = fold_binary(ins.op, vals[0], vals[1])


def constant_states(cfg: CFG) -> Dict[int, Env]:
    """IN set (constant environment) of every block, by worklist iteration."""
    by_id = cfg.by_id()
    entry = cfg.blocks[0].id if cfg.blocks else None
    on_entry: Env = {p: NAC for p in cfg.params}  # set by the caller: unknown here
    for g in globals_in([ins for b in cfg.blocks for ins in b.instrs]):
        on_entry[g] = NAC                        # globals: set by main's prologue or by a caller
    out_env: Dict[int, Optional[Env]] = {b.id: None for b in cfg.blocks}   # None = not yet computed
    in_env: Dict[int, Env] = {}
    work = deque(b.id for b in cfg.blocks)
    queued = set(work)
    while work:
        bid = work.popleft()
        queued.discard(bid)
        block = by_id[bid]
        sources = [out_env[p] for p in block.pred if out_env[p] is not None]
        if bid == entry:
            sources.append(on_entry)
        env = _meet(sources)
        in_env[bid] = dict(env)
        for ins in block.instrs:
            _transfer_const(ins, env)
        if env != out_env[bid]:
            out_env[bid] = env
            for s in block.succ:
                if s != EXIT and s not in queued:
                    work.append(s)
                    queued.add(s)
    return in_env


def constprop(cfg: CFG) -> None:
    states = constant_states(cfg)
    for block in cfg.blocks:
        env = dict(states.get(block.id, {}))
        new = []
        for ins in block.instrs:
            simplified = fold(ins.map_operands(lambda x: _substitute(x, env)))
            new.append(simplified)
            _transfer_const(ins, env)            # state update uses the original meaning
        block.instrs = new
    connect(cfg.blocks)


def _substitute(x: Optional[Operand], env: Env) -> Optional[Operand]:
    if isinstance(x, str):
        v = env.get(x)
        if isinstance(v, int):
            return v
    return x


# ======================================================================
# Unreachable-block removal
# ======================================================================
def remove_unreachable(cfg: CFG) -> None:
    live = reachable(cfg)
    cfg.blocks = [b for b in cfg.blocks if b.id in live]
    connect(cfg.blocks)


# ======================================================================
# Shared forward "must" analysis (intersection at joins)
# ======================================================================
Facts = FrozenSet[tuple]


def must_forward(cfg: CFG, transfer: Callable[[Instr, Set[tuple]], None],
                 join: Callable[[Sequence[Facts]], Facts]) -> Dict[int, Facts]:
    """IN facts of every block for a forward analysis whose facts must hold
    on every path: the entry starts with none, other blocks start at "all
    facts" (top) and shrink to the greatest fixed point."""
    by_id = cfg.by_id()
    entry = cfg.blocks[0].id if cfg.blocks else None
    out: Dict[int, Optional[Facts]] = {b.id: None for b in cfg.blocks}   # None = top (not yet reached)
    ins_: Dict[int, Facts] = {}
    work = deque(b.id for b in cfg.blocks)
    queued = set(work)
    while work:
        bid = work.popleft()
        queued.discard(bid)
        block = by_id[bid]
        sources = [out[p] for p in block.pred if out[p] is not None]
        if bid == entry:
            sources.append(frozenset())
        if not sources:
            continue                              # not reached yet
        facts = set(join(sources))
        ins_[bid] = frozenset(facts)
        for ins in block.instrs:
            transfer(ins, facts)
        new = frozenset(facts)
        if new != out[bid]:
            out[bid] = new
            for s in block.succ:
                if s != EXIT and s not in queued:
                    work.append(s)
                    queued.add(s)
    return ins_


def join_facts(sources: Sequence[Facts]) -> Facts:
    """Meet of a must analysis: a fact holds after a join only if it holds on every incoming path."""
    return frozenset.intersection(*sources)


# Each pass looks its meet up through its own name, so that bugs.py can
# plant a mistake in one pass without touching the other.
_copy_join = join_facts
_cse_join = join_facts


# ======================================================================
# Global copy propagation (available copies)
# ======================================================================
def _copy_call_kill(facts: Set[tuple]) -> None:
    """A call may change any global: forget copies that mention one."""
    for f in [f for f in facts if is_global(f[0]) or is_global(f[1])]:
        facts.discard(f)


def _copy_transfer(ins: Instr, facts: Set[tuple]) -> None:
    if ins.kind == "call":
        _copy_call_kill(facts)
    d = ins.defs()
    if d is None:
        return
    for f in [f for f in facts if d in f]:       # (x, y): x or y changed
        facts.discard(f)
    if ins.kind == "copy" and isinstance(ins.a, str) and ins.a != d:
        facts.add((d, ins.a))


def copyprop(cfg: CFG) -> None:
    states = must_forward(cfg, _copy_transfer, _copy_join)
    for block in cfg.blocks:
        facts = set(states.get(block.id, frozenset()))
        new = []
        for ins in block.instrs:
            copies = dict(facts)                  # x -> y; each x has at most one y
            new.append(ins.map_operands(lambda x: copies.get(x, x) if isinstance(x, str) else x))
            _copy_transfer(ins, facts)
        block.instrs = new


# ======================================================================
# Global common-subexpression elimination (available expressions)
# ======================================================================
def _expr_key(ins: Instr) -> Optional[tuple]:
    if ins.kind == "load":
        return ("[]", ins.a, ("array", ins.target))
    if ins.kind == "binop":
        a, b = ins.a, ins.b
        if ins.op in COMMUTATIVE and repr(a) > repr(b):
            a, b = b, a
        return (ins.op, a, b)
    if ins.kind == "unop":
        return (ins.op, ins.a, None)
    return None


def _reads_array(fact: tuple, name: str) -> bool:
    key = fact[0]
    return key[0] == "[]" and key[2] == ("array", name)


def _cse_store_kills(facts: Set[tuple], name: str) -> None:
    """A store to (or re-creation of) array `name` makes its remembered reads stale."""
    for f in [f for f in facts if _reads_array(f, name)]:
        facts.discard(f)


def _cse_call_kill(facts: Set[tuple]) -> None:
    """A call may change any global variable or global array."""
    for f in list(facts):
        key, holder = f
        if (is_global(holder) or any(is_global(x) for x in key[1:])
                or (key[0] == "[]" and is_global(key[2][1]))):
            facts.discard(f)


def _cse_effects(ins: Instr, facts: Set[tuple]) -> None:
    """What an instruction does to remembered expressions besides defining a variable."""
    if ins.kind in ("store", "array"):
        _cse_store_kills(facts, ins.target)
    elif ins.kind == "call":
        _cse_call_kill(facts)


def _cse_transfer(ins: Instr, facts: Set[tuple]) -> None:
    _cse_effects(ins, facts)
    d = ins.defs()
    if d is None:
        return
    # (key, holder): anything that read or held d is no longer valid
    for f in [f for f in facts if f[1] == d or d in f[0][1:]]:
        facts.discard(f)
    key = _expr_key(ins)
    if key is not None and d not in key[1:]:
        facts.add((key, d))


def cse(cfg: CFG) -> None:
    states = must_forward(cfg, _cse_transfer, _cse_join)
    for block in cfg.blocks:
        facts = set(states.get(block.id, frozenset()))
        new = []
        for ins in block.instrs:
            key = _expr_key(ins)
            if key is not None:
                holders = sorted(h for k, h in facts if k == key and h != ins.dst)
                if holders:
                    new.append(copy(ins.dst, holders[0]))
                    _cse_transfer(ins, facts)
                    continue
            new.append(ins)
            _cse_transfer(ins, facts)
        block.instrs = new


# ======================================================================
# Dead-code elimination with liveness analysis (backward data-flow)
# ======================================================================
def _uses(ins: Instr) -> List[str]:
    """Variables an instruction reads, as DCE sees them (a hook for bugs.py)."""
    return ins.uses()


def _ret_reads_globals(gset: Set[str]) -> Set[str]:
    """Globals that a function's caller may read after `return`: all of them."""
    return gset


def live_uses(ins: Instr, cfg: CFG, gset: Set[str]) -> List[str]:
    """Variables an instruction may read: its operands, plus every global
    for a call (the callee may read them) and for a function's return."""
    out = list(_uses(ins))
    if ins.kind == "call":
        out.extend(gset)
    elif ins.kind == "ret" and cfg.is_function:
        out.extend(_ret_reads_globals(gset))
    return out


def unit_globals(cfg: CFG) -> Set[str]:
    return {g for g in globals_in([ins for b in cfg.blocks for ins in b.instrs])}


def liveness(cfg: CFG) -> Dict[int, Set[str]]:
    """LIVE-OUT set of every block."""
    gset = unit_globals(cfg)
    by_id = cfg.by_id()
    live_in: Dict[int, Set[str]] = {b.id: set() for b in cfg.blocks}
    live_out: Dict[int, Set[str]] = {b.id: set() for b in cfg.blocks}
    work = deque(reversed([b.id for b in cfg.blocks]))
    queued = set(work)
    while work:
        bid = work.popleft()
        queued.discard(bid)
        block = by_id[bid]
        out: Set[str] = set()
        for s in block.succ:
            if s != EXIT:
                out |= live_in[s]
        live = set(out)
        for ins in reversed(block.instrs):
            d = ins.defs()
            if d is not None:
                live.discard(d)
            live.update(live_uses(ins, cfg, gset))
        live_out[bid] = out
        if live != live_in[bid]:
            live_in[bid] = live
            for p in block.pred:
                if p not in queued:
                    work.append(p)
                    queued.add(p)
    return live_out


def dce(cfg: CFG) -> None:
    outs = liveness(cfg)
    gset = unit_globals(cfg)
    for block in cfg.blocks:
        live = set(outs[block.id])
        kept = []
        for ins in reversed(block.instrs):
            d = ins.defs()
            if d is not None and d not in live and not ins.has_effect():
                continue                          # result never read: delete
            if d is not None:
                live.discard(d)
            live.update(live_uses(ins, cfg, gset))
            kept.append(ins)
        block.instrs = list(reversed(kept))


# ======================================================================
# Driver
# ======================================================================
PASS_FUNCS: Dict[str, Callable[[CFG], None]] = {
    "constprop": constprop,
    "unreachable": remove_unreachable,
    "copyprop": copyprop,
    "cse": cse,
    "dce": dce,
}


def check_passes(passes: Sequence[str]) -> None:
    unknown = [p for p in passes if p not in PASS_FUNCS]
    if unknown:
        raise ValueError(f"unknown pass(es): {', '.join(unknown)}")


def optimize_unit(body: List[Instr], params: Tuple[str, ...], passes: Sequence[str],
                  is_function: bool = False) -> List[Instr]:
    """Optimize one unit's code until it stops changing."""
    code = body
    for _ in range(MAX_ROUNDS):
        cfg = build_cfg(code)
        cfg.params = params
        cfg.is_function = is_function
        for name in passes:
            PASS_FUNCS[name](cfg)
            connect(cfg.blocks)
        new_code = linearize(cfg)
        if new_code == code:
            break
        code = new_code
    return code


def optimize(code: List[Instr], passes: Sequence[str] = ALL_PASSES) -> List[Instr]:
    """Run the given passes on every unit (main and each function)."""
    check_passes(passes)
    return join_units([Unit(u.header, optimize_unit(u.body, u.params, passes, u.header is not None))
                       for u in split_units(code)])
