"""Evaluation: does the fuzzer find real optimizer bugs, how fast, and how well?

For every planted bug (see bugs.py) and every generator setting, run a
campaign until the first failure (or until `n` programs pass) and record:

  detected     was the bug found at all within n programs?
  programs     how many programs it took (1 = the very first one)
  status       mismatch (wrong output) or crash (compiler/VM failure or hang)
  blamed       did a single-pass run point at the pass that holds the bug?
  reduced      size of the failing program before and after reduction

A baseline campaign on the CORRECT compiler for each generator setting
measures false alarms; there should be none, because every Mini program
has exactly one correct output.

Generator settings compared (each turns ONE feature of the default off):
  default          the generator as used everywhere else in the project
  no-swap          reused subexpressions are never written with their
                   operands swapped
  no-functions     programs without functions or calls (so also without
                   globals used by functions, and without recursion)
  no-arrays        no array declarations, reads or writes
  no-globals       functions use no top-level variables or arrays
  no-recursion     no function calls itself
  no-loop-control  no `for` loops, `break` or `continue`
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .bugs import BUGS, planted
from .generator import GenConfig, generate
from .harness import campaign
from .reduce import reduce_program

SETTINGS: Dict[str, GenConfig] = {
    "default": GenConfig(),
    "no-swap": GenConfig(swap_reuse=0.0),
    "no-functions": GenConfig(max_functions=0),
    "no-arrays": GenConfig(arrays=False),
    "no-globals": GenConfig(globals=False),
    "no-recursion": GenConfig(recursion=False),
    "no-loop-control": GenConfig(loop_control=False),
}


@dataclass
class Detection:
    detected: bool
    programs: Optional[int] = None      # programs run up to and including the first failure
    seed: Optional[int] = None
    status: Optional[str] = None
    suspects: List[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class BugResult:
    name: str
    home: str
    summary: str
    by_setting: Dict[str, Detection]
    blamed: Optional[bool] = None       # home pass among the suspects (first detection)
    reduced_from: Optional[int] = None  # lines
    reduced_to: Optional[int] = None
    reduced_source: Optional[str] = None
    reduce_tests: Optional[int] = None
    reduce_seconds: Optional[float] = None
    reduce_complete: Optional[bool] = None


@dataclass
class EvalReport:
    n: int
    seed: int
    baseline: Dict[str, dict]           # setting -> {"programs", "failures", "timeouts"}
    bugs: List[BugResult]
    seconds: float

    # -- summary numbers -----------------------------------------------------
    def detected(self, setting: Optional[str] = None) -> int:
        if setting is None:
            return sum(any(d.detected for d in b.by_setting.values()) for b in self.bugs)
        return sum(b.by_setting[setting].detected for b in self.bugs)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def markdown(self) -> str:
        settings = list(SETTINGS)
        lines = [f"Planted-bug evaluation: up to {self.n} programs per bug and setting, seeds {self.seed}-{self.seed + self.n - 1}.", ""]
        head = "| Bug | Pass | " + " | ".join(f"Found ({s})" for s in settings) + " | Status | Blamed | Reduced |"
        lines += [head, "|" + "---|" * (5 + len(settings))]
        for b in self.bugs:
            cells = []
            for s in settings:
                d = b.by_setting[s]
                cells.append(f"program {d.programs}" if d.detected else f"no (0/{self.n})")
            first = next((b.by_setting[s] for s in settings if b.by_setting[s].detected), None)
            status = first.status if first else "-"
            blamed = "-" if b.blamed is None else ("yes" if b.blamed else "no (passes together)")
            red = f"{b.reduced_from} -> {_lines(b.reduced_to)}" if b.reduced_to is not None else "-"
            lines.append(f"| `{b.name}` | {b.home} | " + " | ".join(cells) + f" | {status} | {blamed} | {red} |")
        lines.append("")
        total = len(self.bugs)
        for s in settings:
            found = [b.by_setting[s].programs for b in self.bugs if b.by_setting[s].detected]
            med = _num(statistics.median(found)) if found else "-"
            lines.append(f"- {s}: {len(found)}/{total} bugs found; median programs to first detection: {med}")
        lines.append(f"- any setting: {self.detected()}/{total} bugs found")
        blamed = [b.blamed for b in self.bugs if b.blamed is not None]
        lines.append(f"- correct pass blamed: {sum(blamed)}/{len(blamed)} detected bugs")
        red = [(b.reduced_from, b.reduced_to) for b in self.bugs if b.reduced_to is not None]
        if red:
            lines.append(f"- reduction: median {_num(statistics.median(f for f, _ in red))} -> "
                         f"{_num(statistics.median(t for _, t in red))} lines")
        for s, base in self.baseline.items():
            lines.append(f"- false alarms on the correct compiler ({s}): {base['failures']}/{base['programs']} programs")
        lines.append(f"- total time: {self.seconds:.0f} s")
        return "\n".join(lines)


def _num(x) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _lines(k: int) -> str:
    return f"{k} line" if k == 1 else f"{k} lines"


def evaluate(n: int = 400, seed: int = 0, bugs: Optional[Sequence[str]] = None,
             reduce: bool = True, log: Callable[[str], None] = lambda s: None) -> EvalReport:
    start = time.perf_counter()
    names = list(bugs) if bugs else list(BUGS)

    baseline = {}
    for s, cfg in SETTINGS.items():
        r = campaign(n, seed=seed, config=cfg)
        baseline[s] = {"programs": r.programs, "failures": len(r.failures), "timeouts": r.timeouts}
        log(f"baseline ({s}): {len(r.failures)} failures in {r.programs} programs")

    results: List[BugResult] = []
    for name in names:
        bug = BUGS[name]
        by_setting: Dict[str, Detection] = {}
        first_failure = None
        for s, cfg in SETTINGS.items():
            t0 = time.perf_counter()
            with planted(name):
                r = campaign(n, seed=seed, config=cfg, stop_after=1)
            if r.failures:
                f = r.failures[0]
                by_setting[s] = Detection(True, f.seed - seed + 1, f.seed, f.verdict.status,
                                          f.verdict.suspects, round(time.perf_counter() - t0, 2))
                if first_failure is None:
                    first_failure = (cfg, f)
            else:
                by_setting[s] = Detection(False, seconds=round(time.perf_counter() - t0, 2))
        res = BugResult(name, bug.home, bug.summary, by_setting)
        if first_failure is not None:
            cfg, f = first_failure
            res.blamed = bug.home in f.verdict.suspects
            if reduce:
                with planted(name):
                    red = reduce_program(generate(f.seed, cfg), f.verdict)
                res.reduced_from, res.reduced_to = red.before["lines"], red.after["lines"]
                res.reduced_source, res.reduce_tests = red.source, red.tests
                res.reduce_seconds, res.reduce_complete = red.seconds, red.complete
        found = ", ".join(f"{s}: program {d.programs}" if d.detected else f"{s}: not found"
                          for s, d in by_setting.items())
        log(f"{name}: {found}" + (f"; reduced {res.reduced_from} -> {res.reduced_to} lines" if res.reduced_to else ""))
        results.append(res)
    return EvalReport(n, seed, baseline, results, round(time.perf_counter() - start, 1))
