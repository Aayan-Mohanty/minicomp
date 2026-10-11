"""A virtual machine that executes three-address code.

This is the compiler's "target machine". Unoptimized and optimized code
both run here, and their output is compared against the reference
interpreter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from . import ops
from .interp import DEFAULT_FUEL, OutOfFuel
from .tac import Instr, Operand


@dataclass
class RunResult:
    output: List[int]
    steps: int                 # instructions executed (labels not counted)


def run(code: List[Instr], fuel: int = DEFAULT_FUEL) -> RunResult:
    labels: Dict[str, int] = {ins.target: i for i, ins in enumerate(code) if ins.kind == "label"}
    env: Dict[str, int] = {}
    output: List[int] = []
    pc, steps = 0, 0

    def val(x: Operand) -> int:
        return x if isinstance(x, int) else env[x]

    while pc < len(code):
        ins = code[pc]
        pc += 1
        k = ins.kind
        if k == "label":
            continue
        steps += 1
        if steps > fuel:
            raise OutOfFuel(output)
        if k == "copy":
            env[ins.dst] = val(ins.a)
        elif k == "binop":
            env[ins.dst] = ops.binary(ins.op, val(ins.a), val(ins.b))
        elif k == "unop":
            env[ins.dst] = ops.unary(ins.op, val(ins.a))
        elif k == "print":
            output.append(val(ins.a))
        elif k == "jump":
            pc = labels[ins.target]
        elif k == "cbr":
            pc = labels[ins.target] if val(ins.a) != 0 else labels[ins.target2]
        else:
            raise ValueError(f"unknown instruction kind {k!r}")
    return RunResult(output, steps)
