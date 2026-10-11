"""Optimizer: classic machine-independent optimizations on the CFG.

Passes (run repeatedly until the code stops changing):

  constprop   Global constant propagation (forward data-flow analysis over
              the CFG, Kildall's lattice UNDEF > constant > NAC), followed by
              constant folding and algebraic simplification of each
              instruction, and turning branches on constants into gotos.
  unreachable Removes blocks that can no longer be reached from the entry.
  copyprop    Local copy propagation: after `x = y`, later uses of x in the
              same block read y instead.
  cse         Local common-subexpression elimination: a repeated `a op b`
              in a block reuses the earlier result.
  dce         Dead-code elimination using global liveness analysis
              (backward data-flow): an assignment whose result is never
              read again is deleted.

Every Mini operation is total (no traps, no undefined behaviour), so any
assignment can be folded, moved or deleted without changing what a
program prints.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Dict, List, Optional, Sequence, Set

from . import ops
from .cfg import CFG, EXIT, build_cfg, connect, linearize, reachable
from .tac import Instr, Operand, copy

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
        return copy(ins.dst, ops.unary(ins.op, a))
    if ins.kind == "binop":
        if isinstance(a, int) and isinstance(b, int):
            return copy(ins.dst, ops.binary(ins.op, a, b))
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


def _transfer_const(ins: Instr, env: Env) -> None:
    d = ins.defs()
    if d is None:
        return
    vals = [_value(x, env) for x in (ins.a, ins.b) if x is not None]
    if any(v == NAC for v in vals):
        env[d] = NAC
    elif any(v is None for v in vals):
        env.pop(d, None)                         # still UNDEF
    elif ins.kind == "copy":
        env[d] = vals[0]
    elif ins.kind == "unop":
        env[d] = ops.unary(ins.op, vals[0])
    else:
        env[d] = ops.binary(ins.op, vals[0], vals[1])


def constant_states(cfg: CFG) -> Dict[int, Env]:
    """IN set (constant environment) of every block, by worklist iteration."""
    by_id = cfg.by_id()
    out_env: Dict[int, Optional[Env]] = {b.id: None for b in cfg.blocks}   # None = not yet computed
    in_env: Dict[int, Env] = {}
    work = deque(b.id for b in cfg.blocks)
    queued = set(work)
    while work:
        bid = work.popleft()
        queued.discard(bid)
        block = by_id[bid]
        env = _meet([out_env[p] for p in block.pred if out_env[p] is not None])
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
            a = _substitute(ins.a, env)
            b = _substitute(ins.b, env)
            simplified = fold(ins.with_operands(a, b))
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
# Local copy propagation
# ======================================================================
def copyprop(cfg: CFG) -> None:
    for block in cfg.blocks:
        copies: Dict[str, str] = {}               # x -> y   after `x = y`
        new = []
        for ins in block.instrs:
            a = copies.get(ins.a, ins.a) if isinstance(ins.a, str) else ins.a
            b = copies.get(ins.b, ins.b) if isinstance(ins.b, str) else ins.b
            ins = ins.with_operands(a, b)
            d = ins.defs()
            if d is not None:
                copies = {x: y for x, y in copies.items() if x != d and y != d}
                if ins.kind == "copy" and isinstance(ins.a, str) and ins.a != d:
                    copies[d] = ins.a
            new.append(ins)
        block.instrs = new


# ======================================================================
# Local common-subexpression elimination
# ======================================================================
def cse(cfg: CFG) -> None:
    for block in cfg.blocks:
        available: Dict[tuple, str] = {}         # (op, a, b) -> variable holding the value
        new = []
        for ins in block.instrs:
            key = _expr_key(ins)
            if key is not None and key in available and available[key] != ins.dst:
                ins = copy(ins.dst, available[key])
            d = ins.defs()
            if d is not None:
                # anything that read or held d is no longer valid
                available = {k: v for k, v in available.items() if v != d and d not in k[1:]}
                key = _expr_key(ins)
                if key is not None and d not in key[1:]:
                    available[key] = d
            new.append(ins)
        block.instrs = new


def _expr_key(ins: Instr) -> Optional[tuple]:
    if ins.kind == "binop":
        a, b = ins.a, ins.b
        if ins.op in COMMUTATIVE and repr(a) > repr(b):
            a, b = b, a
        return (ins.op, a, b)
    if ins.kind == "unop":
        return (ins.op, ins.a, None)
    return None


# ======================================================================
# Dead-code elimination with liveness analysis (backward data-flow)
# ======================================================================
def liveness(cfg: CFG) -> Dict[int, Set[str]]:
    """LIVE-OUT set of every block."""
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
            live.update(ins.uses())
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
    for block in cfg.blocks:
        live = set(outs[block.id])
        kept = []
        for ins in reversed(block.instrs):
            d = ins.defs()
            if d is not None and d not in live:
                continue                          # result never read: delete
            if d is not None:
                live.discard(d)
            live.update(ins.uses())
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


def optimize(code: List[Instr], passes: Sequence[str] = ALL_PASSES) -> List[Instr]:
    """Run the given passes, in order, until the code stops changing."""
    unknown = [p for p in passes if p not in PASS_FUNCS]
    if unknown:
        raise ValueError(f"unknown pass(es): {', '.join(unknown)}")
    for _ in range(MAX_ROUNDS):
        cfg = build_cfg(code)
        for name in passes:
            PASS_FUNCS[name](cfg)
            connect(cfg.blocks)
        new_code = linearize(cfg)
        if new_code == code:
            break
        code = new_code
    return code
