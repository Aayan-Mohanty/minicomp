"""Differential testing harness.

For one program, the harness runs several versions:

    reference      the AST interpreter (the ground truth)
    O0             unoptimized TAC on the VM
    O1             fully optimized TAC (all passes)
    only:<pass>    TAC optimized with a single pass

If any version prints something different from the reference, or the
compiler or VM crashes on it, the program is a failure. The single-pass
versions narrow the blame: a pass that fails on its own is a suspect.
(A bug that appears only when passes interact shows up in O1 alone.)

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
from .interp import OutOfFuel, run as interpret
from .lower import lower
from .nodes import Program
from .opt import ALL_PASSES, optimize
from .parser import parse
from .printer import to_source
from .semantic import check

REF_FUEL = 200_000           # reference interpreter step limit
VM_FUEL = 2_000_000          # generous: compiled code must not hang where the reference finished


@dataclass
class Verdict:
    status: str                              # "ok", "mismatch", "crash", "timeout"
    outputs: Dict[str, object] = field(default_factory=dict)   # config -> list of ints or error text
    failing: List[str] = field(default_factory=list)           # configs that disagree or crashed
    code_size: Dict[str, int] = field(default_factory=dict)    # config -> TAC instructions
    pass_changed: Dict[str, bool] = field(default_factory=dict)  # did each pass alter the code?

    @property
    def suspects(self) -> List[str]:
        return [c.split(":", 1)[1] for c in self.failing if c.startswith("only:")]


def configurations(passes: Sequence[str] = ALL_PASSES) -> Dict[str, Optional[List[str]]]:
    configs: Dict[str, Optional[List[str]]] = {"O0": None, "O1": list(passes)}
    for p in passes:
        configs[f"only:{p}"] = [p]
    return configs


def check_program(program: Program, passes: Sequence[str] = ALL_PASSES,
                  only: Optional[Sequence[str]] = None) -> Verdict:
    """Run every configuration on one (semantically valid) program.

    `only` restricts the run to the named configurations (e.g. ["O1"]);
    the reducer uses it to re-test just the versions that failed.
    """
    v = Verdict("ok")
    try:
        ref = interpret(program, fuel=REF_FUEL)
    except OutOfFuel:
        v.status = "timeout"                 # cannot judge without a reference result
        return v
    v.outputs["reference"] = ref

    code0 = lower(program)
    for name, chosen in configurations(passes).items():
        if only is not None and name not in only:
            continue
        try:
            code = code0 if chosen is None else optimize(code0, chosen)
            v.code_size[name] = len(code)
            if name.startswith("only:"):
                v.pass_changed[name[5:]] = code != code0
            out = vm.run(code, fuel=VM_FUEL).output
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
