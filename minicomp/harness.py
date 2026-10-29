"""Differential testing harness.

For one program, the harness runs nine versions:

    reference      the AST interpreter (the ground truth)
    O0             unoptimized TAC on the VM
    O1             fully optimized TAC (all passes) on the VM
    only:<pass>    TAC optimized with a single pass, on the VM (5 runs)
    machine        O1 code after register allocation, on the register machine

If any version prints something different from the reference, or the
compiler or VM crashes on it, the program is a failure. The other runs
narrow the blame: a pass that fails on its own is a suspect, and
"regalloc" is the suspect when only the machine run fails. (A bug that
appears only when passes interact shows up in O1 alone.)

A fuzzing campaign repeats this for many generated programs. Program i
of a campaign with base seed S uses seed S + i, so any failure can be
regenerated exactly with `python3 -m minicomp generate --seed S+i`.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import vm
from .generator import GenConfig, generate
from .regalloc import allocate, run_machine
from .tac import instruction_count
from .interp import OutOfFuel, run_counted
from .lower import lower
from .nodes import Program
from .opt import ALL_PASSES, optimize
from .parser import parse
from .printer import to_source
from .semantic import check

REF_FUEL = 200_000           # reference interpreter step limit

# Compiled code may run at most STEP_FACTOR * (reference steps) + STEP_SLACK
# instructions before it counts as "did not finish". Why this is safe: the
# reference counts a step per statement, loop iteration, call, argument
# passed, operator and array element read; every TAC instruction executed
# is paid for by one of these, at most 4 per step (e.g. a short-circuit
# `&&`: copy, branch, compare, copy; a global copied into a temporary
# before a later call is paid for by the operator or argument it feeds; a
# function's final `return 0` by its call; main's prologue entry for a
# global by that global's declaration, which every finished run executes);
# no optimization adds an executed instruction; and register allocation
# adds at most 3 per TAC instruction (two loads and a store). So correct
# code runs at most 16 * steps instructions; STEP_SLACK is extra margin.
# A tight limit makes miscompiled infinite loops fail fast instead of
# running to VM_FUEL.
STEP_FACTOR = 20
STEP_SLACK = 1_000
VM_FUEL = STEP_FACTOR * REF_FUEL + STEP_SLACK   # the largest limit any program can get


@dataclass
class Verdict:
    status: str                              # "ok", "mismatch", "crash", "timeout"
    outputs: Dict[str, object] = field(default_factory=dict)   # config -> list of ints or error text
    failing: List[str] = field(default_factory=list)           # configs that disagree or crashed
    code_size: Dict[str, int] = field(default_factory=dict)    # config -> instructions (labels not counted)
    pass_changed: Dict[str, bool] = field(default_factory=dict)  # did each pass alter the code?

    @property
    def suspects(self) -> List[str]:
        out = [c.split(":", 1)[1] for c in self.failing if c.startswith("only:")]
        if MACHINE in self.failing and "O1" not in self.failing:
            out.append("regalloc")
        return out


MACHINE = "machine"


def configurations(passes: Sequence[str] = ALL_PASSES) -> Dict[str, Optional[List[str]]]:
    configs: Dict[str, Optional[List[str]]] = {"O0": None, "O1": list(passes)}
    for p in passes:
        configs[f"only:{p}"] = [p]
    configs[MACHINE] = list(passes)          # O1, then register allocation
    return configs


def check_program(program: Program, passes: Sequence[str] = ALL_PASSES,
                  only: Optional[Sequence[str]] = None) -> Verdict:
    """Run every configuration on one (semantically valid) program.

    `only` restricts the run to the named configurations (e.g. ["O1"]);
    the reducer uses it to re-test just the versions that failed.
    """
    v = Verdict("ok")
    try:
        ref, ref_steps = run_counted(program, fuel=REF_FUEL)
    except OutOfFuel:
        v.status = "timeout"                 # cannot judge without a reference result
        return v
    v.outputs["reference"] = ref
    budget = min(VM_FUEL, STEP_FACTOR * ref_steps + STEP_SLACK)

    code0 = lower(program)
    optimized: Dict[tuple, list] = {}
    for name, chosen in configurations(passes).items():
        if only is not None and name not in only:
            continue
        try:
            key = tuple(chosen) if chosen is not None else None
            if key is None:
                code = code0
            else:
                if key not in optimized:
                    optimized[key] = optimize(code0, chosen)
                code = optimized[key]
            if name.startswith("only:"):
                v.pass_changed[name[5:]] = code != code0
            if name == MACHINE:
                machine = allocate(code)
                v.code_size[name] = sum(1 for i in machine.code if i.kind not in ("label", "func"))
                out = run_machine(machine, fuel=budget).output
            else:
                v.code_size[name] = instruction_count(code)
                out = vm.run(code, fuel=budget).output
        except OutOfFuel:
            out = "error: did not finish (the reference did)"
        except Exception as e:                # a compiler or VM crash is also a bug
            out = f"error: {type(e).__name__}: {e}"
        v.outputs[name] = out
        if out != ref:
            v.failing.append(name)
    if v.failing:
        crashed = any(isinstance(v.outputs[c], str) for c in v.failing)
        v.status = "crash" if crashed else "mismatch"
    return v


def check_source(source: str, passes: Sequence[str] = ALL_PASSES) -> Verdict:
    program = parse(source)
    check(program)
    return check_program(program, passes)


# ----------------------------------------------------------------------
# Campaigns
# ----------------------------------------------------------------------
@dataclass
class Failure:
    seed: int
    verdict: Verdict
    source: str


@dataclass
class CampaignReport:
    programs: int = 0
    ok: int = 0
    timeouts: int = 0
    failures: List[Failure] = field(default_factory=list)
    seconds: float = 0.0
    pass_activity: Dict[str, int] = field(default_factory=dict)   # programs each pass changed
    size_before: int = 0
    size_after: int = 0

    def summary(self) -> str:
        judged = self.programs - self.timeouts
        lines = [
            f"programs generated   {self.programs}",
            f"  agreed             {self.ok}",
            f"  failures           {len(self.failures)}",
            f"  skipped (timeout)  {self.timeouts}",
            f"time                 {self.seconds:.1f} s "
            f"({self.programs / self.seconds:.0f} programs/s)" if self.seconds else "time 0 s",
        ]
        if judged and self.size_before:
            lines.append(f"code size, O0 -> O1  {self.size_before} -> {self.size_after} instructions "
                         f"({100 * (1 - self.size_after / self.size_before):.0f}% smaller)")
        if judged:
            lines.append("programs where each pass changed the code:")
            for p, n in self.pass_activity.items():
                lines.append(f"  {p:<12} {n:>6}  ({100 * n / judged:.0f}%)")
        for f in self.failures:
            who = ", ".join(f.verdict.suspects) or "only visible with all passes together"
            lines.append(f"FAILURE seed={f.seed} status={f.verdict.status} "
                         f"failing={','.join(f.verdict.failing)} suspects: {who}")
        return "\n".join(lines)


def campaign(count: int, seed: int = 0, passes: Sequence[str] = ALL_PASSES,
             config: Optional[GenConfig] = None, out_dir: Optional[Path] = None,
             stop_after: Optional[int] = None) -> CampaignReport:
    report = CampaignReport(pass_activity={p: 0 for p in passes})
    start = time.perf_counter()
    for i in range(count):
        s = seed + i
        program = generate(s, config)
        report.programs += 1
        try:
            check(program)                                 # a generator bug would show up here
            verdict = check_program(program, passes)
        except Exception:                                  # bug in the harness itself
            verdict = Verdict("crash", {"harness": traceback.format_exc()}, ["harness"])
        if verdict.status == "timeout":
            report.timeouts += 1
            continue
        for p, changed in verdict.pass_changed.items():
            report.pass_activity[p] += int(changed)
        report.size_before += verdict.code_size.get("O0", 0)
        report.size_after += verdict.code_size.get("O1", 0)
        if verdict.status == "ok":
            report.ok += 1
            continue
        failure = Failure(s, verdict, to_source(program))
        report.failures.append(failure)
        if out_dir is not None:
            save_failure(failure, Path(out_dir))
        if stop_after is not None and len(report.failures) >= stop_after:
            break
    report.seconds = time.perf_counter() - start
    return report


def save_failure(failure: Failure, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"seed_{failure.seed}.mini"
    path.write_text(failure.source, encoding="utf-8")
    meta = {"seed": failure.seed, "status": failure.verdict.status,
            "failing": failure.verdict.failing, "suspects": failure.verdict.suspects,
            "outputs": failure.verdict.outputs}
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path
