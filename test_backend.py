"""Tests for TAC generation, the VM, the CFG, and each optimization."""

from pathlib import Path

import pytest

from minicomp import vm
from minicomp.tac import Unit, join_units, split_units
from minicomp.cfg import EXIT, build_cfg, linearize
from minicomp.interp import run
from minicomp.lower import lower
from minicomp.opt import ALL_PASSES, constant_states, fold, liveness, optimize
from minicomp.parser import parse
from minicomp.semantic import check
from minicomp.tac import binop, copy, format_code

EXAMPLES = Path(__file__).parent.parent / "examples"
RUNNABLE = ["fib.mini", "gcd.mini", "semantics.mini", "scopes.mini", "functions.mini", "arrays.mini"]


def compile_src(src, passes=None):
    prog = parse(src)
    check(prog)
    code = lower(prog)
    return optimize(code, passes) if passes else code


def text(code):
    return format_code(code)


# ---------------------------------------------------------------- lowering + VM
def test_vm_matches_reference_on_examples_at_every_level():
    for name in RUNNABLE:
        prog = parse((EXAMPLES / name).read_text())
        check(prog)
        ref = run(prog)
        code0 = lower(prog)
        assert vm.run(code0).output == ref, name
        assert vm.run(optimize(code0)).output == ref, name
        for p in ALL_PASSES:
            assert vm.run(optimize(code0, [p])).output == ref, (name, p)


def test_shadowed_variables_get_unique_names():
    t = text(compile_src("var x = 1; { var x = x + 1; print x; } print x;"))
    assert "x#1 = %1" in t and "print x#1" in t and t.rstrip().endswith("print x")


def test_while_translation_shape():
    t = text(compile_src("var i = 0; while (i < 3) { i = i + 1; }"))
    assert t.splitlines() == [
        "    i = 0", "L1:", "    %1 = i < 3", "    if %1 goto L2 else L3",
        "L2:", "    i = i + 1", "    goto L1", "L3:"]


# ---------------------------------------------------------------- CFG
def test_cfg_blocks_and_edges():
    cfg = build_cfg(compile_src((EXAMPLES / "gcd.mini").read_text()))
    assert len(cfg.blocks) == 4
    b0, head, body, after = cfg.blocks
    assert head.labels == ["L1"] and sorted(head.pred) == [b0.id, body.id]   # back edge
    assert head.succ == [body.id, after.id]
    assert after.succ == [EXIT]


def test_linearize_round_trip_preserves_behaviour():
    for name in RUNNABLE:
        code = compile_src((EXAMPLES / name).read_text())
        units = split_units(code)                        # one CFG per function (and main)
        rebuilt = join_units([Unit(u.header, linearize(build_cfg(u.body))) for u in units])
        assert vm.run(rebuilt).output == vm.run(code).output, name


# ---------------------------------------------------------------- folding rules
def test_fold_constants_and_identities():
    assert str(fold(binop("t", "*", 6, 7))) == "t = 42"
    assert str(fold(binop("t", "+", "x", 0))) == "t = x"
    assert str(fold(binop("t", "*", "x", 0))) == "t = 0"
    assert str(fold(binop("t", "-", "x", "x"))) == "t = 0"
    assert str(fold(binop("t", "/", 7, 0))) == "t = 0"        # Mini semantics, not a crash


def test_fold_does_not_apply_false_identity():
    # x / x is NOT always 1 in Mini (0 / 0 == 0), so it must stay as is
    assert str(fold(binop("t", "/", "x", "x"))) == "t = x / x"


# ---------------------------------------------------------------- individual passes
def test_constant_propagation_through_merge():
    src = "var c = 0; var a = 5; if (c) { a = 5; } else { a = 5; } print a * 2;"
    assert text(compile_src(src, ALL_PASSES)).strip() == "print 10"


def test_constant_propagation_stops_at_loop_variables():
    src = "var i = 0; while (i < 3) { i = i + 1; } print i;"
    code = compile_src(src, ["constprop"])
    states = constant_states(build_cfg(code))
    head = [b for b in build_cfg(code).blocks if "L1" in b.labels][0]
    assert states[head.id]["i"] == "NAC"           # 0 on entry, 1, 2, 3 around the loop
    assert vm.run(code).output == [3]


def test_branch_on_constant_removes_dead_branch():
    t = text(compile_src("if (1 < 2) { print 1; } else { print 2; }", ALL_PASSES))
    assert t.strip() == "print 1"


def test_copy_propagation():
    code = compile_src("var i = 0; while (i < 2) { var a = i; print a + 1; i = i + 1; }",
                       ["copyprop"])
    assert "%2 = i + 1" in text(code)              # reads i directly instead of a


def test_common_subexpression_elimination():
    code = compile_src("var i = 0; while (i < 1) { print (i * 7) + (i * 7); i = i + 1; }",
                       ["cse"])
    t = text(code)
    assert t.count("* 7") == 1 and "%3 = %2" in t


def test_dead_code_elimination():
    code = compile_src("var i = 0; while (i < 2) { var unused = i * 99; i = i + 1; } print i;",
                       ["dce"])
    assert "99" not in text(code)


def test_liveness_at_loop_head():
    code = compile_src((EXAMPLES / "gcd.mini").read_text())
    cfg = build_cfg(code)
    outs = liveness(cfg)
    assert outs[cfg.blocks[0].id] == {"x", "y"}   # both are read later


def test_optimizer_shrinks_code_and_keeps_output():
    prog = parse((EXAMPLES / "semantics.mini").read_text())
    check(prog)
    code0 = lower(prog)
    code1 = optimize(code0)
    assert len(code1) == 8 < len(code0)            # every value is a known constant
    assert all(ins.kind == "print" for ins in code1)
    assert vm.run(code1).output == run(prog)


def test_optimize_reaches_a_fixed_point():
    code = compile_src((EXAMPLES / "fib.mini").read_text(), ALL_PASSES)
    assert optimize(code) == code


def test_unknown_pass_rejected():
    with pytest.raises(ValueError):
        optimize([copy("x", 1)], ["nope"])
