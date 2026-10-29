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
    "call-result-undef": "fun f1() { return 1; } var k1 = 0; while (k1 < 1 && f1()) { k1 = 1; print 0; }",
    "copyprop-meet-union": "fun f0() { } var d = f0(); var k3 = 0; while (k3 < 1) { k3 = 1; d = 1; } print d;",
    "cse-meet-union": ("var k1 = 0; while (k1 < 1) { k1 = k1 + 1; var k3 = 0; while (k3 < 1) { k3 = 1; "
                       "if (k1 | -2147483647) { print k1 | -2147483647; } } }"),
    "dce-deletes-call": "fun f0() { print 0; } var g = f0();",
    "regalloc-dead-def": "fun f0() { } var k2 = 0; while (k2 < 1) { k2 = 1; var a = f0(); }",
    "regalloc-entry-params": "fun f1(p, q) { print p; } if (f1(1, 0)) { }",
    "cse-load-ignores-store": "var ga[8]; ga[0] = ga[0]; ga[0] = 1; print ga[0];",
    "call-keeps-globals": "fun f0() { g0 = 0; } var g0 = 1; var g1 = 0; g1 = f0(); print g0;",
    "copyprop-call-keeps-globals": "var g = 1; fun f() { g = 2; return 0; } fun h(p) { var x = g; f(); print x; return 0; } h(0);",
    "liveness-return-ignores-globals": "var g = 1; fun f() { g = 2; return 0; } f(); print g;",
}


def optimizer_state():
    from minicomp import regalloc
    return (opt.fold_binary, opt.fold_unary, opt._identity, opt._meet, opt.liveness,
            opt._uses, opt._transfer_const, opt._copy_transfer, opt._copy_join, opt._cse_transfer, opt._cse_join,
            opt._call_clobbers, opt._copy_call_kill, opt._cse_store_kills, opt._ret_reads_globals,
            dict(opt.PASS_FUNCS), frozenset(opt.COMMUTATIVE), Instr.uses, Instr.has_effect,
            regalloc._def_edges, regalloc._param_edges, regalloc.scratch_for)


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
def test_generator_settings_are_valid():
    from minicomp.evaluate import SETTINGS
    no_swap, no_fun = SETTINGS["no-swap"], SETTINGS["no-functions"]
    differs = 0
    for s in range(40):
        for cfg in SETTINGS.values():
            src = generate_source(s, cfg)
            p = parse(src)
            check(p)
            run(p, fuel=200_000)
        assert "fun " not in generate_source(s, no_fun)
        differs += generate_source(s, no_swap) != generate_source(s)
    assert differs > 0                                    # swapping is on by default


# ---------------------------------------------------------------- reducer
def test_reducer_shrinks_a_real_failure_and_keeps_the_bug():
    with planted("copyprop-no-kill"):
        res = reduce_program(generate(7))
    assert res.before["lines"] == 37 and res.after["lines"] == 12 and res.complete
    assert res.source == ("var g0 = 0;\nvar f = 0;\ng0 = f;\nf = 1;\nvar k2 = 0;\nwhile (k2 < 1) {\n"
                          "    k2 = 1;\n    if (g0) {\n        continue;\n    }\n    print 0;\n}\n")
    assert res.verdict.status == "mismatch" and res.verdict.suspects == ["copyprop"]
    check(parse(res.source))                                   # still a valid program


def test_reducer_result_is_minimal_for_its_statements():
    with planted("cse-no-kill"):
        res = reduce_program(generate(9))
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
    assert by["fold-unsigned-compare"].by_setting["default"].programs == 26
    assert by["fold-unsigned-compare"].by_setting["no-functions"].programs == 7
    assert by["div-self-one"].by_setting["default"].programs == 23
    assert by["div-self-one"].by_setting["no-loop-control"].programs == 1
    for b in report.bugs:
        assert b.blamed is True
        assert b.reduced_to is not None and b.reduced_to < b.reduced_from
    text = report.markdown()
    assert "| `div-self-one` | constprop | program 23 |" in text
    assert "false alarms on the correct compiler (default): 0/40 programs" in text
