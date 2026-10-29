"""A virtual machine that executes three-address code.

This is the compiler's "target machine" for TAC. Unoptimized and optimized
code both run here, and their output is compared against the reference
interpreter. (regalloc.py adds a lower-level target: a register machine.)

Calls: `x = call f(a, b)` evaluates the arguments in the caller, starts f
with a fresh set of variables and arrays holding its parameters, and
stores f's `return` value in x. Main ends where the first function header
begins. Calls nested more than MAX_CALL_DEPTH deep stop the program
(StackOverflow), as in the reference interpreter.

Globals (@x) live in one memory shared by main and every function. Arrays
are lists; `a[i]` uses element ops.element(i, n).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import ops
from .interp import DEFAULT_FUEL, MAX_CALL_DEPTH, OutOfFuel, StackOverflow
from .tac import Instr, Operand, is_global


@dataclass
class RunResult:
    output: List[int]
    steps: int                 # instructions executed (labels and headers not counted)


def run(code: List[Instr], fuel: int = DEFAULT_FUEL) -> RunResult:
    labels: Dict[str, int] = {ins.target: i for i, ins in enumerate(code) if ins.kind == "label"}
    funcs: Dict[str, Tuple[int, Tuple[str, ...]]] = {
        ins.target: (i + 1, tuple(ins.args)) for i, ins in enumerate(code) if ins.kind == "func"}
    env: Dict[str, object] = {}                 # this unit's variables and arrays
    memory: Dict[str, object] = {}              # globals
    frames: List[Tuple[Dict[str, object], int, Optional[str]]] = []   # (caller env, return pc, result variable)
    output: List[int] = []
    pc, steps = 0, 0

    def val(x: Operand) -> int:
        if isinstance(x, int):
            return x
        return memory[x] if is_global(x) else env[x]

    def put(d: str, v: int) -> None:
        if is_global(d):
            memory[d] = v
        else:
            env[d] = v

    def array(name: str) -> list:
        return memory[name] if is_global(name) else env[name]

    while True:
        if pc >= len(code) or code[pc].kind == "func":
            if not frames:
                break                                   # end of main
            result = 0                                  # a function body fell off its end
            env, pc, dst = frames.pop()
            if dst is not None:
                put(dst, result)
            continue
        ins = code[pc]
        pc += 1
        k = ins.kind
        if k == "label":
            continue
        steps += 1
        if steps > fuel:
            raise OutOfFuel(output)
        if k == "copy":
            put(ins.dst, val(ins.a))
        elif k == "binop":
            put(ins.dst, ops.binary(ins.op, val(ins.a), val(ins.b)))
        elif k == "unop":
            put(ins.dst, ops.unary(ins.op, val(ins.a)))
        elif k == "load":
            arr = array(ins.target)
            put(ins.dst, arr[ops.element(val(ins.a), len(arr))])
        elif k == "store":
            arr = array(ins.target)
            arr[ops.element(val(ins.a), len(arr))] = val(ins.b)
        elif k == "array":
            if is_global(ins.target):
                memory[ins.target] = [0] * ins.a
            else:
                env[ins.target] = [0] * ins.a
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
            frames.append((env, pc, ins.dst))
            env = dict(zip(params, args))
            pc = start
        elif k == "ret":
            result = val(ins.a)
            if not frames:
                break                                   # (semantic analysis forbids return in main)
            env, pc, dst = frames.pop()
            if dst is not None:
                put(dst, result)
        else:
            raise ValueError(f"unknown instruction kind {k!r}")
    return RunResult(output, steps)
