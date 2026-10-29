"""Register allocation by graph coloring, and a register machine to run the result.

This is the compiler's back end. TAC has unlimited variables; a real
processor has a few registers. The allocator maps every TAC variable of a
unit (main or one function) to one of K registers r0..r{K-1}, or, when
that is impossible, to a memory slot s0, s1, ... in the unit's frame
("spilling"). Two scratch registers t0 and t1, never allocated, carry
spilled values in and out.

Steps (Chaitin's method with Briggs' optimistic coloring):
  1. Liveness: which variables are live after each instruction
     (backward data-flow over the unit's CFG, iterated to a fixed point).
  2. Interference graph: an edge joins two variables that must not share a
     register. At every definition of d, d interferes with every variable
     live after it (for a copy d = s, except s: both hold the same value).
     A function's parameters all interfere with each other: the caller
     writes them all on entry.
  3. Simplify: repeatedly remove a variable with fewer than K neighbours
     left (it can always be coloured later) and push it on a stack. If
     none has, push the best spill candidate (most neighbours per use)
     anyway, optimistically.
  4. Select: pop variables and give each the lowest register not used by
     an already coloured neighbour; a variable with no free register is
     spilled to its own memory slot.
  5. Rewrite: TAC becomes machine code. A spilled operand is loaded into a
     scratch register first (t0 for the first operand, t1 for the second);
     a spilled result is computed into t0 and stored. Call arguments and
     results may name slots directly.

Globals (@x) are never given registers: they live in memory shared by all
units (a callee may change them), so they are handled like spilled
variables whose location is @x. Arrays live in memory too: `array`,
`aload` and `astore` name the array, and only the index and value
operands go through registers.

The register machine runs machine code. Each call gets a fresh register
file, fresh slots and fresh local arrays, and the machine writes the arguments into the
callee's parameter locations (the header lists them), so registers are
saved across calls by the machine itself; this keeps the calling
convention out of the allocator.

Differential testing checks the allocator like any optimization: the
harness also runs every program as "machine" (-O1, allocated, run on this
machine) and compares its output with the reference.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Union

from . import ops
from .cfg import EXIT, build_cfg
from .interp import DEFAULT_FUEL, MAX_CALL_DEPTH, OutOfFuel, StackOverflow
from .tac import Instr, Unit, is_global, split_units

K = 5                                   # allocatable registers
SCRATCH = ("t0", "t1")

MOperand = Union[str, int]              # "r3", "t0", "s2" (slot), "@x" (global) or an immediate


@dataclass(frozen=True)
class MInstr:
    kind: str                           # set unop binop load store aload astore array print label jump cbr call ret func
    dst: Optional[str] = None           # register; store: slot or global; call: register, slot or global
    op: Optional[str] = None
    a: Optional[MOperand] = None
    b: Optional[MOperand] = None
    target: Optional[str] = None
    target2: Optional[str] = None
    args: Tuple[MOperand, ...] = ()     # call: arguments; func: parameter locations

    def __str__(self) -> str:
        k = self.kind
        w = _fmt
        if k == "set":
            return f"{self.dst} = {w(self.a)}"
        if k == "unop":
            return f"{self.dst} = {self.op}{w(self.a)}"
        if k == "binop":
            return f"{self.dst} = {w(self.a)} {self.op} {w(self.b)}"
        if k == "load":
            return f"{self.dst} = [{self.a}]"
        if k == "store":
            return f"[{self.dst}] = {w(self.a)}"
        if k == "print":
            return f"print {w(self.a)}"
        if k == "label":
            return f"{self.target}:"
        if k == "jump":
            return f"goto {self.target}"
        if k == "cbr":
            return f"if {w(self.a)} goto {self.target} else {self.target2}"
        if k == "call":
            text = f"call {self.target}({', '.join(w(x) for x in self.args)})"
            return f"{w(self.dst)} = {text}" if self.dst else text
        if k == "ret":
            return f"return {w(self.a)}"
        if k == "func":
            return f"fun {self.target}({', '.join(w(x) for x in self.args)}):"
        if k == "array":
            return f"array {self.target}[{self.a}]"
        if k == "aload":
            return f"{self.dst} = {self.target}[{w(self.a)}]"
        if k == "astore":
            return f"{self.target}[{w(self.a)}] = {w(self.b)}"
        return f"<{k}>"


def _in_memory(x) -> bool:
    """A slot (s0, s1, ...) or a global (@x): an operand that is not a register."""
    return isinstance(x, str) and (x.startswith("s") or is_global(x))


def _fmt(x: MOperand) -> str:
    return f"[{x}]" if _in_memory(x) else str(x)


@dataclass
class UnitAllocation:
    name: str
    locations: Dict[str, str]           # variable -> "rN" or "sN" (globals are not listed)
    interference: Dict[str, Set[str]]
    registers_used: int
    spilled: List[str]


@dataclass
class MachineProgram:
    code: List[MInstr]
    units: List[UnitAllocation] = field(default_factory=list)

    def text(self) -> List[str]:
        out = []
        for ins in self.code:
            if ins.kind == "func":
                out.append("")
                out.append(str(ins))
            else:
                out.append(str(ins) if ins.kind == "label" else f"    {ins}")
        return out


# ----------------------------------------------------------------------
# 1. Liveness (the allocator's own, independent of the optimizer's DCE)
# ----------------------------------------------------------------------
def local_uses(ins: Instr) -> List[str]:
    """Variables that need a register: what the instruction reads, minus globals."""
    return [v for v in ins.uses() if not is_global(v)]


def local_def(ins: Instr) -> Optional[str]:
    d = ins.defs()
    return None if d is None or is_global(d) else d


def live_out_sets(body: List[Instr]):
    cfg = build_cfg(body)
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
            live.update(local_uses(ins))
        live_out[bid] = out
        if live != live_in[bid]:
            live_in[bid] = live
            for p in block.pred:
                if p not in queued:
                    work.append(p)
                    queued.add(p)
    return cfg, live_in, live_out


# ----------------------------------------------------------------------
# 2. Interference graph
# ----------------------------------------------------------------------
def _def_edges(ins: Instr, d: str, live_after: Set[str]) -> Set[str]:
    """Variables that the result d of `ins` must not share a register with."""
    others = live_after - {d}
    if ins.kind == "copy" and isinstance(ins.a, str):
        others.discard(ins.a)                    # d = s: same value, may share
    return others


def _param_edges(params: Tuple[str, ...], live_entry: Set[str]) -> Dict[str, Set[str]]:
    """All parameters are written together on entry."""
    return {p: (set(params) | live_entry) - {p} for p in params}


def interference(unit: Unit):
    cfg, live_in, live_out = live_out_sets(unit.body)
    graph: Dict[str, Set[str]] = {}

    def node(v: str) -> None:
        graph.setdefault(v, set())

    def edge(u: str, v: str) -> None:
        node(u); node(v)
        if u != v:
            graph[u].add(v)
            graph[v].add(u)

    uses: Dict[str, int] = {}
    for p in unit.params:
        node(p)
    for block in cfg.blocks:
        live = set(live_out[block.id])
        for ins in reversed(block.instrs):
            for v in local_uses(ins):
                node(v)
                uses[v] = uses.get(v, 0) + 1
            d = local_def(ins)
            if d is not None:
                node(d)
                uses[d] = uses.get(d, 0) + 1
                for v in _def_edges(ins, d, live):
                    edge(d, v)
                live.discard(d)
            elif ins.defs() is not None:
                live.discard(ins.defs())
            live.update(local_uses(ins))
    entry = live_in[cfg.blocks[0].id] if cfg.blocks else set()
    for p, others in _param_edges(unit.params, set(entry)).items():
        for v in others:
            edge(p, v)
    return graph, uses


# ----------------------------------------------------------------------
# 3-4. Simplify and select
# ----------------------------------------------------------------------
def color(graph: Dict[str, Set[str]], uses: Dict[str, int], k: int = K) -> Dict[str, Optional[int]]:
    remaining = set(graph)
    degree = {v: len(graph[v]) for v in graph}
    stack: List[str] = []
    while remaining:
        low = [v for v in remaining if degree[v] < k]
        if low:
            v = min(low)
        else:                                    # spill candidate: many neighbours, few uses
            v = max(remaining, key=lambda x: (degree[x] / (uses.get(x, 0) + 1), x))
        stack.append(v)
        remaining.discard(v)
        for n in graph[v]:
            if n in remaining:
                degree[n] -= 1
    colors: Dict[str, Optional[int]] = {}
    while stack:
        v = stack.pop()
        taken = {colors[n] for n in graph[v] if colors.get(n) is not None}
        free = [c for c in range(k) if c not in taken]
        colors[v] = free[0] if free else None    # None = spill
    return colors


# ----------------------------------------------------------------------
# 5. Rewrite into machine code
# ----------------------------------------------------------------------
def scratch_for(position: int) -> str:
    """Scratch register that carries the spilled operand at this position (0 or 1)."""
    return SCRATCH[position]


def allocate_unit(unit: Unit, k: int = K) -> Tuple[List[MInstr], UnitAllocation]:
    graph, uses = interference(unit)
    colors = color(graph, uses, k)
    loc: Dict[str, str] = {}
    spilled: List[str] = []
    for v in sorted(colors):
        if colors[v] is None:
            loc[v] = f"s{len(spilled)}"
            spilled.append(v)
        else:
            loc[v] = f"r{colors[v]}"
    code: List[MInstr] = []

    def place(x: str) -> str:
        return x if is_global(x) else loc[x]

    def read(x, position: int) -> MOperand:
        if isinstance(x, int):
            return x
        where = place(x)
        if _in_memory(where):
            reg = scratch_for(position)
            code.append(MInstr("load", dst=reg, a=where))
            return reg
        return where

    def write(d: str, make) -> None:
        where = place(d)
        if _in_memory(where):
            code.append(make(SCRATCH[0]))
            code.append(MInstr("store", dst=where, a=SCRATCH[0]))
        else:
            code.append(make(where))

    if unit.header is not None:
        code.append(MInstr("func", target=unit.name, args=tuple(loc[p] for p in unit.params)))
    for ins in unit.body:
        k_ = ins.kind
        if k_ == "copy":
            src = read(ins.a, 0)
            dst = place(ins.dst)
            if src != dst:
                if _in_memory(dst):
                    code.append(MInstr("store", dst=dst, a=src))
                else:
                    code.append(MInstr("set", dst=dst, a=src))
        elif k_ == "load":
            index = read(ins.a, 0)
            write(ins.dst, lambda r: MInstr("aload", dst=r, target=ins.target, a=index))
        elif k_ == "store":
            index = read(ins.a, 0)
            value = read(ins.b, 1)
            code.append(MInstr("astore", target=ins.target, a=index, b=value))
        elif k_ == "array":
            code.append(MInstr("array", target=ins.target, a=ins.a))
        elif k_ == "unop":
            a = read(ins.a, 0)
            write(ins.dst, lambda r: MInstr("unop", dst=r, op=ins.op, a=a))
        elif k_ == "binop":
            a = read(ins.a, 0)
            b = read(ins.b, 1)
            write(ins.dst, lambda r: MInstr("binop", dst=r, op=ins.op, a=a, b=b))
        elif k_ == "print":
            code.append(MInstr("print", a=read(ins.a, 0)))
        elif k_ == "cbr":
            code.append(MInstr("cbr", a=read(ins.a, 0), target=ins.target, target2=ins.target2))
        elif k_ in ("label", "jump"):
            code.append(MInstr(k_, target=ins.target))
        elif k_ == "call":
            args = tuple(x if isinstance(x, int) else place(x) for x in ins.args)
            code.append(MInstr("call", dst=place(ins.dst) if ins.dst else None, target=ins.target, args=args))
        elif k_ == "ret":
            code.append(MInstr("ret", a=read(ins.a, 0)))
        else:
            raise ValueError(f"cannot allocate instruction kind {k_!r}")
    used = len({c for c in colors.values() if c is not None})
    return code, UnitAllocation(unit.name, loc, graph, used, spilled)


def allocate(code: List[Instr], k: int = K) -> MachineProgram:
    """Allocate registers for a whole TAC program (normally after -O1)."""
    out = MachineProgram([])
    for unit in split_units(code):
        mcode, info = allocate_unit(unit, k)
        out.code.extend(mcode)
        out.units.append(info)
    return out


# ----------------------------------------------------------------------
# The register machine
# ----------------------------------------------------------------------
@dataclass
class MachineResult:
    output: List[int]
    steps: int
    loads: int
    stores: int


def run_machine(program: MachineProgram, fuel: int = DEFAULT_FUEL) -> MachineResult:
    code = program.code
    labels = {ins.target: i for i, ins in enumerate(code) if ins.kind == "label"}
    funcs = {ins.target: (i + 1, ins.args) for i, ins in enumerate(code) if ins.kind == "func"}
    regs: Dict[str, int] = {}
    slots: Dict[str, int] = {}
    arrays: Dict[str, List[int]] = {}           # this unit's local arrays
    memory: Dict[str, object] = {}              # globals: variables and arrays
    frames: List[tuple] = []                    # (regs, slots, arrays, return pc, result location)
    output: List[int] = []
    pc = steps = loads = stores = 0

    def val(x: MOperand) -> int:
        if isinstance(x, int):
            return x
        if is_global(x):
            return memory[x]
        return slots[x] if x.startswith("s") else regs[x]

    def put(where: str, value: int) -> None:
        if is_global(where):
            memory[where] = value
        elif where.startswith("s"):
            slots[where] = value
        else:
            regs[where] = value

    def array(name: str) -> List[int]:
        return memory[name] if is_global(name) else arrays[name]

    while True:
        if pc >= len(code) or code[pc].kind == "func":
            if not frames:
                break
            regs, slots, arrays, pc, dst = frames.pop()  # fell off the end: returns 0
            if dst is not None:
                put(dst, 0)
            continue
        ins = code[pc]
        pc += 1
        k = ins.kind
        if k == "label":
            continue
        steps += 1
        if steps > fuel:
            raise OutOfFuel(output)
        if k == "set":
            regs[ins.dst] = val(ins.a)
        elif k == "binop":
            regs[ins.dst] = ops.binary(ins.op, val(ins.a), val(ins.b))
        elif k == "unop":
            regs[ins.dst] = ops.unary(ins.op, val(ins.a))
        elif k == "load":
            loads += 1
            regs[ins.dst] = val(ins.a)
        elif k == "store":
            stores += 1
            put(ins.dst, val(ins.a))
        elif k == "aload":
            arr = array(ins.target)
            regs[ins.dst] = arr[ops.element(val(ins.a), len(arr))]
        elif k == "astore":
            arr = array(ins.target)
            arr[ops.element(val(ins.a), len(arr))] = val(ins.b)
        elif k == "array":
            if is_global(ins.target):
                memory[ins.target] = [0] * ins.a
            else:
                arrays[ins.target] = [0] * ins.a
        elif k == "print":
            output.append(val(ins.a))
        elif k == "jump":
            pc = labels[ins.target]
        elif k == "cbr":
            pc = labels[ins.target] if val(ins.a) != 0 else labels[ins.target2]
        elif k == "call":
            if len(frames) >= MAX_CALL_DEPTH:
                raise StackOverflow(output)
            start, params = funcs[ins.target]
            args = [val(x) for x in ins.args]
            frames.append((regs, slots, arrays, pc, ins.dst))
            regs, slots, arrays = {}, {}, {}
            for where, value in zip(params, args):
                put(where, value)
            pc = start
        elif k == "ret":
            result = val(ins.a)
            if not frames:
                break
            regs, slots, arrays, pc, dst = frames.pop()
            if dst is not None:
                put(dst, result)
        else:
            raise ValueError(f"unknown machine instruction {k!r}")
    return MachineResult(output, steps, loads, stores)
