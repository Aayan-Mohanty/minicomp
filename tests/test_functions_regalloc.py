"""Tests for functions, the independent interpreter arithmetic, global copy
propagation and CSE, register allocation and the register machine."""

import random
import subprocess
import sys
from pathlib import Path

import pytest

from minicomp import interp, ops, vm
from minicomp.errors import CompileError
from minicomp.generator import GenConfig, generate, generate_with_attempts
from minicomp.harness import check_source
from minicomp.lower import lower
from minicomp.opt import ALL_PASSES, optimize
from minicomp.parser import parse
from minicomp.printer import to_source
from minicomp.regalloc import allocate, interference, run_machine
from minicomp.semantic import SemanticErrors, check
from minicomp.tac import format_code, split_units

ROOT = Path(__file__).parent.parent


def prog(src):
    p = parse(src)
    check(p)
    return p


def output(src):
    return interp.run(prog(src))


def opt_text(src, passes):
    return format_code(optimize(lower(prog(src)), passes))


# ---------------------------------------------------------------- functions: language
def test_functions_run_with_recursion_early_return_and_default_zero():
    src = ("fun fact(n) { if (n <= 1) { return 1; } return n * fact(n - 1); }\n"
           "fun nothing(x) { var y = x; }\n"
           "fun first(a, b) { while (1) { return a; } return b; }\n"
           "print fact(10); print nothing(5); print first(7, 8); print later(2);\n"
           "fun later(z) { return z + 1; }\n")
    assert output(src) == [3628800, 0, 7, 3]


def test_function_errors_are_all_reported():
    src = ("var g = 1;\nfun f(a, a) { return zz; }\nfun f() { return 0; }\nreturn 1;\n"
           "print h(1);\nprint two(1);\nfun two(x, y) { return x; }\n")
    with pytest.raises(SemanticErrors) as e:
        check(parse(src))
    messages = [err.message for err in e.value.errors]
    assert messages == [
        "parameter 'a' appears twice in 'f'",
        "use of undeclared variable 'zz'",
        "function 'f' is already declared (line 2)",
        "'return' outside a function",
        "call to undeclared function 'h'",
        "'two' takes 2 arguments, but 1 was given",
    ]


def test_functions_only_at_top_level_and_return_needs_a_value():
    with pytest.raises(CompileError):
        parse("fun f() { fun g() { return 1; } return 0; }")
    with pytest.raises(CompileError):
        parse("fun f() { return; }")


def test_printer_round_trip_with_functions():
    for s in range(40):
        p = generate(s)
        assert parse(to_source(p)) == p


def test_calls_evaluate_left_to_right_and_short_circuit():
    src = ("fun t(x) { print x; return x; }\n"
           "print t(1) + t(2);\nprint 0 && t(3);\nprint 1 || t(4);\nprint 1 && t(5);\nt(6);\n")
    assert output(src) == [1, 2, 3, 0, 1, 5, 1, 6]
    assert check_source(src).status == "ok"              # every compiled version agrees


def test_deep_recursion_is_a_resource_limit_not_a_bug():
    v = check_source("fun f(n) { return f(n + 1); } print f(0);")
    assert v.status == "timeout" and v.failing == []
    with pytest.raises(interp.StackOverflow):
        interp.run(prog("fun f(n) { return f(n + 1); } print f(0);"))


# ---------------------------------------------------------------- independent arithmetic
def test_interpreter_arithmetic_is_a_separate_implementation_that_agrees():
    source = (ROOT / "minicomp" / "interp.py").read_text()
    assert "import ops" not in source and "from . import ops" not in source
    rng = random.Random(7)
    values = [0, 1, -1, 2, -2, 31, 32, 33, -32, ops.INT_MIN, ops.INT_MAX, ops.INT_MIN + 1, 65536]
    values += [rng.randint(ops.INT_MIN, ops.INT_MAX) for _ in range(150)]
    for op, f in interp.BINARY.items():
        for a in values:
            for b in values:
                assert f(a, b) == ops.binary(op, a, b), (op, a, b)
    for op, f in interp.UNARY.items():
        for a in values:
            assert f(a) == ops.unary(op, a), (op, a)


# ---------------------------------------------------------------- global copy propagation and CSE
def test_copy_propagation_crosses_blocks_only_when_every_path_agrees():
    same = "fun f(p, c) { var b = p; if (c) { print 1; } return b; }"
    assert "return p" in opt_text(same, ["copyprop"])
    differs = "fun f(p, q, c) { var b = p; if (c) { b = q; } return b; }"
    assert "return b" in opt_text(differs, ["copyprop"])


def test_cse_crosses_blocks_only_when_every_path_agrees():
    same = "fun f(p, q, c) { var x = p + q; if (c) { print 1; } return p + q; }"
    text = opt_text(same, ["cse"])
    assert text.count("p + q") == 1 and "%2 = %1" in text     # %1 holds p + q on every path
    killed = "fun f(p, q, c) { var x = p + q; if (c) { p = 1; } return p + q; }"
    assert opt_text(killed, ["cse"]).count("p + q") == 2


def test_parameters_and_call_results_are_not_constants():
    src = "fun f(p) { if (p) { p = 5; } return p; } fun g() { return 3; } print f(0) + f(1); print g() + 1;"
    code = optimize(lower(prog(src)))
    assert vm.run(code).output == [5, 4]
    assert "return p" in format_code(code)


def test_calls_are_never_removed_or_merged():
    src = "fun f() { print 1; return 2; } var a = f(); var b = f(); a = 0; print 0;"
    code = optimize(lower(prog(src)))
    assert format_code(code).count("call f()") == 2
    assert vm.run(code).output == [1, 1, 0]


def test_each_function_is_optimized_on_its_own():
    code = optimize(lower(prog("fun f(x) { var y = 2 * 3; return x + y; } print f(1);")))
    units = split_units(code)
    assert [u.name for u in units] == ["main", "f"]
    assert "x + 6" in format_code(units[1].body)


# ---------------------------------------------------------------- register allocation
def test_interfering_variables_never_share_a_register():
    for s in range(60):
        code = optimize(lower(generate(s)))
        machine = allocate(code)
        for unit, info in zip(split_units(code), machine.units):
            graph, _ = interference(unit)
            for v, others in graph.items():
                for w in others:
                    assert info.locations[v] != info.locations[w], (s, unit.name, v, w)


def test_machine_code_is_correct_with_few_registers():
    for s in range(40):
        p = generate(s)
        ref = interp.run(p)
        code = optimize(lower(p))
        for k in (1, 2, 3, 5):
            assert run_machine(allocate(code, k)).output == ref, (s, k)


def test_spilling_uses_memory_and_scratch_registers():
    src = "var a = 1; var b = 2; var c = 3; var d = 4; print a + b; print c + d; print a + d; print b + c;"
    m = allocate(lower(prog(src)), k=2)
    text = "\n".join(m.text())
    assert m.units[0].spilled and "[s0]" in text and "t0 = [s" in text
    assert run_machine(m).output == [3, 7, 5, 5]


def test_copies_can_share_a_register():
    m = allocate(optimize(lower(prog((ROOT / "examples" / "gcd.mini").read_text()))))
    assert m.units[0].registers_used == 3 and len(m.units[0].locations) == 4
    assert run_machine(m).output == [21]


def test_parameters_get_registers_on_entry():
    m = allocate(optimize(lower(prog((ROOT / "examples" / "functions.mini").read_text()))))
    headers = [str(i) for i in m.code if i.kind == "func"]
    assert headers[0].startswith("fun power(r") and run_machine(m).output == [1594323, 610, -2147483648]


# ---------------------------------------------------------------- generator and harness
def test_generated_programs_finish_within_budget():
    retries = 0
    for s in range(150):
        p, attempt = generate_with_attempts(s)
        retries += attempt > 0
        interp.run(p, fuel=GenConfig().max_steps)
    assert retries < 15


def test_compiled_code_stays_far_inside_the_step_bound():
    from minicomp.harness import STEP_FACTOR
    for s in range(60):
        p = generate(s)
        _, steps = interp.run_counted(p)
        code0 = lower(p)
        assert vm.run(code0).steps <= 4 * steps
        assert run_machine(allocate(optimize(code0))).steps <= 16 * steps < STEP_FACTOR * steps


def test_cli_asm_shows_machine_code():
    r = subprocess.run([sys.executable, "-m", "minicomp", "asm", "examples/gcd.mini", "--registers", "2"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and "r0" in r.stdout and "main: 4 variables in 2 registers" in r.stderr
