"""Tests for arrays, for/break/continue, globals and recursion: the language
rules, the compiler (TAC, optimizer, register allocation), the generator,
and the step bound the harness relies on."""

import pytest

from minicomp import interp, vm
from minicomp.generator import GenConfig, generate, generate_source
from minicomp.harness import check_source
from minicomp.lower import escaping_globals, lower
from minicomp.nodes import ArrayDecl, Break, Call, Continue, For
from minicomp.opt import optimize
from minicomp.parser import parse
from minicomp.printer import to_source
from minicomp.regalloc import allocate, run_machine
from minicomp.semantic import SemanticErrors, check
from minicomp.tac import format_code


def prog(src):
    p = parse(src)
    check(p)
    return p


def output(src):
    return interp.run(prog(src))


def all_runs_agree(src):
    v = check_source(src)
    assert v.status == "ok", (v.status, v.outputs)
    return v.outputs["reference"]


# ---------------------------------------------------------------- language rules
def test_arrays_start_at_zero_and_indexes_wrap():
    src = ("var a[3];\nprint a[0];\na[0] = 7;\na[4] = 8;\na[-1] = 9;\n"
           "print a[0]; print a[1]; print a[2]; print a[3]; print a[-3]; print a[2147483647];\n")
    # 4 mod 3 = 1, -1 mod 3 = 2, 3 mod 3 = 0, -3 mod 3 = 0, 2147483647 mod 3 = 1
    assert all_runs_agree(src) == [0, 7, 8, 9, 7, 7, 8]


def test_array_index_is_evaluated_before_the_value():
    src = ("var a[4];\nvar i = 0;\nfun bump() { i = i + 1; return 5; }\n"
           "a[i] = bump();\nprint a[0]; print a[1]; print i;\n")
    assert all_runs_agree(src) == [5, 0, 1]


def test_array_declared_in_a_loop_is_fresh_each_iteration():
    src = "for (var k = 0; k < 3; k = k + 1) { var t[2]; print t[0]; t[0] = k + 10; }"
    assert all_runs_agree(src) == [0, 0, 0]


def test_for_break_and_continue():
    src = ("var s = 0;\n"
           "for (var i = 0; i < 10; i = i + 1) {\n"
           "    if (i == 6) { break; }\n"
           "    if (i % 2 == 0) { continue; }\n"
           "    for (var j = 0; j < 100; j = j + 1) { if (j == 2) { break; } s = s + 1; }\n"
           "    s = s + i * 100;\n"
           "}\n"
           "var w = 0;\nwhile (1) { w = w + 1; if (w < 3) { continue; } break; }\n"
           "print s; print w;\n")
    # odd i < 6: 1, 3, 5 -> s = 3 * 2 + 900; continue in a for loop still runs the update
    assert all_runs_agree(src) == [906, 3]


def test_for_parts_are_optional_and_the_loop_variable_is_local():
    assert all_runs_agree("var i = 0; for (; i < 3; ) { i = i + 1; } print i;") == [3]
    with pytest.raises(SemanticErrors):
        check(parse("for (var i = 0; i < 3; i = i + 1) { } print i;"))


def test_functions_use_globals_which_are_zero_before_their_declaration():
    src = ("fun get() { return g; }\nfun set(v) { g = v; hist[v] = hist[v] + 1; return 0; }\n"
           "print get();\nvar g = 4;\nvar hist[3];\nprint get();\nset(7);\nprint g;\n"
           "print hist[1];\nfun shadow(g) { return g * 2; }\nprint shadow(5); print g;\n")
    # set(7) wrote hist[7 mod 3] = hist[1]; a parameter named g hides the global
    assert all_runs_agree(src) == [0, 4, 7, 1, 10, 7]


def test_a_declaration_resets_a_global_a_function_set_earlier():
    src = "fun f() { g = 5; t[0] = 5; return 0; }\nf();\nvar g = 1;\nvar t[2];\nprint g; print t[0];\n"
    assert all_runs_agree(src) == [1, 0]


def test_recursion_with_arrays_and_globals():
    src = ("var calls = 0;\nvar memo[32];\n"
           "fun fib(n) { calls = calls + 1; if (n < 2) { return n; }\n"
           "    if (memo[n] != 0) { return memo[n]; }\n"
           "    memo[n] = fib(n - 1) + fib(n - 2); return memo[n]; }\n"
           "print fib(30); print calls;\n")
    assert all_runs_agree(src) == [832040, 59]


def test_new_semantic_errors_are_all_reported():
    src = ("var a[0];\nvar b[4];\nvar x = 1;\nb = 3;\nprint b;\nx[1] = 2;\nprint x[0];\nprint c[1];\n"
           "break;\nfun f(p) {\n    continue;\n    return b[p] + x + y;\n}\n")
    with pytest.raises(SemanticErrors) as e:
        check(parse(src))
    assert [err.message for err in e.value.errors] == [
        "array size must be between 1 and 1024, not 0",
        "cannot assign to the whole array 'b'; assign to an element: b[i] = ...",
        "'b' is an array; read an element with b[i]",
        "'x' is not an array",
        "'x' is not an array",
        "use of undeclared array 'c'",
        "'break' outside a loop",
        "'continue' outside a loop",
        "use of undeclared variable 'y'",
    ]


def test_new_syntax_round_trips_through_the_printer():
    src = ("var a[8];\nfun f(n) {\n    for (var i = 0; i < n; i = i + 1) {\n        if (a[i] > -a[-i]) {\n"
           "            continue;\n        }\n        a[i % 3] = a[a[i]] + 1;\n        break;\n    }\n"
           "    for (; n; ) {\n        n = 0;\n    }\n    return a[0];\n}\nprint f(3);\n")
    p = parse(src)
    assert to_source(p) == src and parse(to_source(p)) == p


# ---------------------------------------------------------------- compiler
def test_a_global_read_is_copied_before_a_later_call():
    src = "var g = 1;\nfun f() { g = 5; return 0; }\nfun two(a, b) { return a * 10 + b; }\n" \
          "print g + f();\ng = 1;\nprint two(g, f());\nprint f() + g;\n"
    text = format_code(lower(prog(src)))
    assert "%1 = @g" in text                         # g + f(): g read before the call
    assert all_runs_agree(src) == [1, 10, 5]


def test_only_globals_that_functions_use_become_memory():
    p = prog("var a = 1; var b[2]; var c = 2; fun f() { return a + b[0]; } print f() + c;")
    assert escaping_globals(p) == {"a", "b"}
    text = format_code(lower(p))
    assert "@a = 0" in text and "array @b[2]" in text and "@c" not in text and "c = 2" in text


def test_constant_propagation_forgets_globals_at_calls():
    src = "var g = 1;\nfun f() { g = 2; return 0; }\nf();\nprint g;\n"
    assert "print 1" not in format_code(optimize(lower(prog(src))))
    assert all_runs_agree(src) == [2]


def test_dead_code_elimination_keeps_global_stores_in_functions():
    src = "var g = 1;\nfun f() { g = 2; return 0; }\nf();\nprint g;\n"
    text = format_code(optimize(lower(prog(src)), ["dce"]))
    assert "@g = 2" in text


def test_cse_reuses_an_array_read_only_until_a_store():
    reuse = format_code(optimize(lower(prog("var a[4]; var i = 2; print a[i] + a[i];")), ["cse"]))
    assert reuse.count("= a[i]") == 1
    src = "var a[4]; var i = 2; var x = a[i]; a[i] = 5; var y = a[i]; print x; print y;"
    stored = format_code(optimize(lower(prog(src)), ["cse"]))
    assert stored.count("= a[i]") == 2
    assert all_runs_agree(src) == [0, 5]


def test_cse_forgets_global_array_reads_at_calls_but_keeps_local_ones():
    src = ("var t[2];\nfun f() { t[0] = 9; return 0; }\nvar u[2];\n"
           "var x = t[0] + u[0];\nf();\nprint t[0] + u[0];\nprint x;\n")
    text = format_code(optimize(lower(prog(src)), ["cse"]))
    assert text.count("= @t[0]") == 2 and text.count("= u[0]") == 1
    assert all_runs_agree(src) == [9, 0]


def test_short_circuit_uses_jumps_unless_the_right_side_is_a_variable_or_constant():
    simple = format_code(lower(prog("var x = 1; var y = 0; print x && y;")))
    assert "x && y" in simple and "goto" not in simple
    jumps = format_code(lower(prog("var x = 0; var y = 3; print x && y + 1;")))
    assert "if x goto" in jumps and "&&" not in jumps


def test_globals_and_arrays_never_get_registers():
    src = ("var g = 3;\nvar t[4];\nfun f(p) { t[p] = g * p; g = g + 1; return t[p - 1]; }\n"
           "for (var i = 0; i < 6; i = i + 1) { print f(i); }\nprint g;\n")
    expected = output(src)
    for k in (1, 2, 5):
        m = allocate(optimize(lower(prog(src))), k)
        assert all(not v.startswith("@") for u in m.units for v in u.locations)
        assert run_machine(m).output == expected, k


def test_step_bound_holds_exactly_for_adversarial_programs():
    """Correct code never runs more than 4 TAC (16 machine) instructions per reference step."""
    long_right = " + ".join(["y"] * 60)
    many_args = ", ".join(["g"] * 30)
    sources = [
        # the reference skips the right operand; compiled code must skip it too
        f"var y = 1; var x = 0; for (var i = 0; i < 50; i = i + 1) {{ x = 0 && ({long_right}); }} print x;",
        # globals copied before a later call: paid for by the arguments
        f"var g = 1;\nfun h() {{ g = g + 1; return 0; }}\nfun f({', '.join(f'p{i}' for i in range(31))}) "
        f"{{ return p0; }}\nfor (var i = 0; i < 20; i = i + 1) {{ print f({many_args}, h()); }}",
        # many globals in main's prologue
        "fun f() { return " + " + ".join(f"v{i}" for i in range(40)) + "; }\n"
        + "".join(f"var v{i} = {i};\n" for i in range(40)) + "print f();\n",
        # a function that falls off its end, called in a loop
        "fun f() { } for (var i = 0; i < 30; i = i + 1) { f(); }",
    ]
    for src in sources:
        p = prog(src)
        _, steps = interp.run_counted(p)
        code0 = lower(p)
        assert vm.run(code0).steps <= 4 * steps
        assert run_machine(allocate(optimize(code0), 1)).steps <= 16 * steps
        assert check_source(src).status == "ok"


# ---------------------------------------------------------------- generator
def _nodes(n, out):
    out.append(n)
    if isinstance(n, (list, tuple)):
        for x in n:
            _nodes(x, out)
    elif hasattr(n, "__dataclass_fields__"):
        for f in n.__dataclass_fields__:
            _nodes(getattr(n, f), out)
    return out


def test_generator_uses_every_new_feature():
    counts = dict(arrays=0, globals=0, recursion=0, for_=0, brk=0, cont=0)
    for s in range(120):
        p = generate(s)
        nodes = _nodes(p.body, [])
        counts["arrays"] += any(isinstance(x, ArrayDecl) for x in nodes)
        counts["globals"] += bool(escaping_globals(p))
        counts["recursion"] += any(isinstance(x, Call) and x.args and to_source_expr(x.args[0]) == "n - 1"
                                   for x in nodes)
        counts["for_"] += any(isinstance(x, For) for x in nodes)
        counts["brk"] += any(isinstance(x, Break) for x in nodes)
        counts["cont"] += any(isinstance(x, Continue) for x in nodes)
    assert all(v >= 25 for v in counts.values()), counts


def to_source_expr(e):
    from minicomp.printer import expr_source
    return expr_source(e)


def test_each_generator_switch_removes_its_feature():
    for s in range(40):
        assert "[" not in generate_source(s, GenConfig(arrays=False))
        assert "for (" not in generate_source(s, GenConfig(loop_control=False))
        src = generate_source(s, GenConfig(loop_control=False))
        assert "break;" not in src and "continue;" not in src
        assert not escaping_globals(generate(s, GenConfig(globals=False)))
        assert "(n - 1" not in generate_source(s, GenConfig(recursion=False))
