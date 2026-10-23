"""Tests for planted bugs (bugs.py), the reducer (reduce.py) and the evaluation (evaluate.py)."""

import pytest

from minicomp import opt
from minicomp.bugs import BUGS, planted
from minicomp.evaluate import evaluate
from minicomp.generator import GenConfig, generate, generate_source
from minicomp.harness import check_source
from minicomp.interp import run
from minicomp.parser import parse
from minicomp.reduce import NotAFailure, reduce_program
from minicomp.semantic import check
from minicomp.tac import Instr

# Small programs that expose particular bugs (found by the fuzzer, then reduced).
TRIGGERS = {
    "div-self-one": "var k0 = 0; while (k0 / k0 <= 0) { k0 = 1; } var f = k0; if (f) { } else { print 0; }",
    "fold-div-floor": "var d = ~1; if (1 / d) { } else { print 0; }",
    "copyprop-no-kill": "var a = 1; var b = 0; b = a; a = 0; print b;",
    "meet-keep-first": "var k0 = 0; while (k0 < 1) { k0 = 1; }",
    "liveness-no-backedge": "var k0 = 0; while (k0 < 1) { k0 = 1; }",
}


def optimizer_state():
    return (opt.fold_binary, opt.fold_unary, opt._identity, opt._meet, opt.liveness,
            dict(opt.PASS_FUNCS), frozenset(opt.COMMUTATIVE), Instr.uses)


# ---------------------------------------------------------------- planted bugs
def test_every_bug_is_undone_even_after_an_error():
    before = optimizer_state()
    for name in BUGS:
        with planted(name):
            pass
        with pytest.raises(RuntimeError):
            with planted(name):
                raise RuntimeError("boom")
        assert optimizer_state() == before, name


def test_unknown_bug_rejected():
    with pytest.raises(ValueError):
        with planted("no-such-bug"):
            pass


def test_known_triggers_fail_only_with_their_bug():
    for name, src in TRIGGERS.items():
        assert check_source(src).status == "ok", name            # correct compiler
        with planted(name):
            v = check_source(src)
        assert v.status in ("mismatch", "crash"), name
        assert BUGS[name].home in v.suspects, name              # blamed on the right pass


def test_bugs_never_change_the_reference_interpreter():
    prog = parse(TRIGGERS["fold-div-floor"])
    check(prog)
    expected = run(prog)
    for name in BUGS:
        with planted(name):
            assert run(prog) == expected, name


# ---------------------------------------------------------------- generator option
def test_swap_option_off_by_default_and_valid_when_on():
    assert generate_source(3).startswith("var b = 60;\nprint (b != b)")   # seed 3 unchanged
    cfg = GenConfig(swap_reuse=0.5)
    changed = 0
    for s in range(80):
        src = generate_source(s, cfg)
        p = parse(src)
        check(p)
        run(p, fuel=200_000)
        changed += src != generate_source(s)
    assert changed > 0


# ---------------------------------------------------------------- reducer
def test_reducer_shrinks_a_real_failure_and_keeps_the_bug():
    with planted("copyprop-no-kill"):
        res = reduce_program(generate(110))
    assert res.before["lines"] == 62 and res.after["lines"] == 5 and res.complete
    assert res.source == "var a = 1;\nvar b = 0;\nb = a;\na = 0;\nprint b;\n"
    assert res.verdict.status == "mismatch" and res.verdict.suspects == ["copyprop"]
    check(parse(res.source))                                   # still a valid program


def test_reducer_result_is_minimal_for_its_statements():
    with planted("fold-div-floor"):
        res = reduce_program(generate(48))
        assert res.after["statements"] < res.before["statements"]
        # removing any single top-level statement makes the failure disappear
        for i in range(len(res.program.body)):
            smaller = parse(res.source)
            del smaller.body[i]
            try:
                check(smaller)
            except Exception:
                continue
            from minicomp.harness import check_program
            assert check_program(smaller).status == "ok", i


def test_reducer_refuses_a_program_that_does_not_fail():
    with pytest.raises(NotAFailure):
        reduce_program(parse("print 1 + 2;"))


# ---------------------------------------------------------------- evaluation
def test_small_evaluation():
    report = evaluate(n=40, seed=0, bugs=["fold-unsigned-compare", "div-self-one"])
    assert all(base["failures"] == 0 for base in report.baseline.values())   # no false alarms
    by = {b.name: b for b in report.bugs}
    assert by["fold-unsigned-compare"].by_setting["default"].programs == 5
    assert by["div-self-one"].by_setting["default"].programs == 17
    for b in report.bugs:
        assert b.blamed is True
        assert b.reduced_to is not None and b.reduced_to < b.reduced_from
    text = report.markdown()
    assert "| `div-self-one` | constprop | program 17 |" in text
    assert "false alarms on the correct compiler (default): 0/40 programs" in text
