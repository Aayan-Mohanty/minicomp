"""Tests for the random program generator and the differential testing harness."""

import json

import minicomp.opt as opt
from minicomp.generator import generate, generate_source
from minicomp.harness import campaign, check_source
from minicomp.interp import run
from minicomp.parser import parse
from minicomp.semantic import check
from minicomp.tac import Instr

SEEDS = range(300)


# ---------------------------------------------------------------- generator
def test_generated_programs_are_valid_and_terminate():
    for s in SEEDS:
        prog = parse(generate_source(s))
        check(prog)                          # raises if a variable is misused
        run(prog, fuel=200_000)              # raises OutOfFuel if it does not finish


def test_same_seed_same_program():
    for s in (0, 1, 99):
        assert generate_source(s) == generate_source(s)
    assert generate_source(1) != generate_source(2)


def test_printed_program_parses_back_to_the_same_tree():
    for s in range(50):
        assert parse(generate_source(s)) == generate(s)


def test_generator_covers_the_features_the_optimizer_needs():
    sources = [generate_source(s) for s in SEEDS]
    joined = "\n".join(sources)
    assert sum("while" in src for src in sources) > 200
    assert sum("fun f0(" in src for src in sources) > 150               # functions ...
    assert "fun f2(p, q, r)" in joined and "fun f1()" in joined        # ... with 0-3 parameters
    assert "f0(" in joined.replace("fun f0(", "")                      # ... that are called
    assert any(line.startswith("        ") and line.strip().startswith("return ")
               for line in joined.splitlines())                         # early returns
    assert "else if" in joined and "} else {" in joined
    assert "2147483647" in joined                        # edge-case constant
    assert any(f"{v} / {v}" in joined for v in "abcdefgh")   # x op x pattern
    # shadowing: some name declared again in a nested block
    assert any(line.startswith("    ") and line.strip().startswith("var a ")
               for line in joined.splitlines())


# ---------------------------------------------------------------- harness, correct compiler
def test_correct_compiler_has_no_failures():
    report = campaign(120, seed=500)
    assert report.failures == [] and report.ok == 120 and report.timeouts == 0
    assert all(n > 0 for n in report.pass_activity.values())   # every pass was exercised
    assert report.size_after < report.size_before


def test_single_program_verdict_lists_every_configuration():
    v = check_source("var a = 6; print a * 7;")
    assert v.status == "ok"
    assert set(v.outputs) == {"reference", "O0", "O1", "only:constprop", "only:unreachable",
                              "only:copyprop", "only:cse", "only:dce", "machine"}
    assert v.code_size["O1"] == 1                         # just `print 42`
    assert v.code_size["machine"] == 1


def test_non_terminating_program_is_skipped_not_blamed():
    v = check_source("var i = 0; while (1) { i = i + 1; }")
    assert v.status == "timeout" and v.failing == []


# ---------------------------------------------------------------- harness, planted bugs
def test_detects_wrong_algebraic_identity_and_blames_constprop():
    original = opt._identity

    def buggy(op, a, b):
        if op == "/" and isinstance(a, str) and a == b:
            return 1                          # wrong: 0 / 0 == 0 in Mini
        return original(op, a, b)

    opt._identity = buggy
    try:
        v = check_source("var k = 0; var x = 0; while (k < 2) { k = k + 1; x = x * k; } print x / x;")
        report = campaign(300, seed=0, stop_after=1)
    finally:
        opt._identity = original
    assert v.status == "mismatch" and v.outputs["reference"] == [0] and v.outputs["O1"] == [1]
    assert v.suspects == ["constprop"]
    assert len(report.failures) == 1 and report.failures[0].verdict.suspects == ["constprop"]


def test_detects_liveness_bug_as_crash_and_blames_dce():
    original = Instr.uses
    Instr.uses = lambda self: [] if self.kind == "cbr" else original(self)
    try:
        report = campaign(50, seed=0, stop_after=1)
    finally:
        Instr.uses = original
    f = report.failures[0]
    assert f.verdict.status == "crash" and "dce" in f.verdict.suspects


def test_failures_are_saved_with_their_seed(tmp_path):
    original = opt._identity
    opt._identity = lambda op, a, b: 1 if (op == "/" and a == b and isinstance(a, str)) \
        else original(op, a, b)
    try:
        report = campaign(300, seed=0, out_dir=tmp_path, stop_after=1)
    finally:
        opt._identity = original
    seed = report.failures[0].seed
    saved = (tmp_path / f"seed_{seed}.mini").read_text()
    meta = json.loads((tmp_path / f"seed_{seed}.json").read_text())
    assert saved == generate_source(seed)                 # reproducible from the seed alone
    assert meta["status"] == "mismatch" and meta["suspects"] == ["constprop"]
