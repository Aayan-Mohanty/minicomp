"""Tests for the lexer, parser, semantic analysis, printer and reference interpreter."""

import random
from pathlib import Path

import pytest

from minicomp import ops
from minicomp.errors import CompileError
from minicomp.interp import OutOfFuel, run
from minicomp.lexer import tokenize
from minicomp.nodes import Binary, IntLit, Program, Print, Unary, Var
from minicomp.parser import BINARY_LEVELS, parse
from minicomp.printer import expr_source, to_source
from minicomp.semantic import SemanticErrors, check

EXAMPLES = Path(__file__).parent.parent / "examples"


def output(src, fuel=100_000):
    prog = parse(src)
    check(prog)
    return run(prog, fuel)


# ---------------------------------------------------------------- lexer
def test_tokens_and_positions():
    toks = tokenize("var x = 10;\n  x <<= 2")
    assert [t.kind for t in toks] == ["var", "IDENT", "=", "INT", ";", "IDENT", "<<", "=", "INT", "EOF"]
    assert (toks[5].line, toks[5].col) == (2, 3)


def test_maximal_munch_and_comments():
    kinds = [t.kind for t in tokenize("a<=b<<c // ignored <\n!=")]
    assert kinds == ["IDENT", "<=", "IDENT", "<<", "IDENT", "!=", "EOF"]


def test_lexical_errors():
    for bad, msg in [("x = 2147483648;", "larger than"), ("x = 007;", "leading zeros"),
                     ("x = 12ab;", "invalid number"), ("x = 1 @ 2;", "unexpected character")]:
        with pytest.raises(CompileError) as e:
            tokenize(bad)
        assert e.value.phase == "lexical" and msg in e.value.message


# ---------------------------------------------------------------- parser
def expr_of(src):
    return parse(f"print {src};").body[0].value


def test_precedence_and_associativity():
    assert expr_of("1 + 2 * 3") == Binary("+", IntLit(1), Binary("*", IntLit(2), IntLit(3)))
    assert expr_of("8 - 4 - 2") == Binary("-", Binary("-", IntLit(8), IntLit(4)), IntLit(2))
    assert expr_of("a || b && c") == Binary("||", Var("a"), Binary("&&", Var("b"), Var("c")))
    assert expr_of("1 << 2 + 3") == Binary("<<", IntLit(1), Binary("+", IntLit(2), IntLit(3)))
    assert expr_of("-x * 2") == Binary("*", Unary("-", Var("x")), IntLit(2))
    assert expr_of("(1 + 2) * 3") == Binary("*", Binary("+", IntLit(1), IntLit(2)), IntLit(3))


def test_else_if_chain():
    s = parse("if (a) { } else if (b) { } else { print 1; }").body[0]
    assert s.orelse is not None and s.orelse.orelse.body[0] == Print(IntLit(1))


def test_syntax_errors_have_positions():
    cases = [("print 1", "expected ';'", (1, 8)),
             ("var x;", "needs an initial value", (1, 6)),
             ("if x { }", "'(' after 'if'", (1, 4)),
             ("while (1) { print 1;", "missing '}'", (1, 11)),
             ("print (1 + );", "expected an expression", (1, 12))]
    for src, msg, pos in cases:
        with pytest.raises(CompileError) as e:
            parse(src)
        assert e.value.phase == "syntax" and msg in e.value.message, src
        assert (e.value.line, e.value.col) == pos, src


# ---------------------------------------------------------------- semantic analysis
def test_semantic_errors_all_reported():
    with pytest.raises(SemanticErrors) as e:
        check(parse("var a = b; var a = 1; { c = 2; } print d;"))
    msgs = [err.message for err in e.value.errors]
    assert len(msgs) == 4
    assert "undeclared variable 'b'" in msgs[0] and "already declared" in msgs[1]
    assert "'c'" in msgs[2] and "'d'" in msgs[3]


def test_initializer_sees_outer_variable_only():
    with pytest.raises(SemanticErrors):
        check(parse("var x = x;"))                    # no outer x
    check(parse("var x = 1; { var x = x + 1; }"))     # outer x exists: fine


def test_scope_ends_with_block():
    with pytest.raises(SemanticErrors):
        check(parse("{ var t = 1; } print t;"))


# ---------------------------------------------------------------- semantics (ops)
def test_division_matches_c_truncation():
    for a, b, q, r in [(7, 2, 3, 1), (-7, 2, -3, -1), (7, -2, -3, 1), (-7, -2, 3, -1)]:
        assert ops.div(a, b) == q and ops.mod(a, b) == r


def test_total_semantics_edge_cases():
    assert ops.div(5, 0) == 0 and ops.mod(5, 0) == 5
    assert ops.div(ops.INT_MIN, -1) == ops.INT_MIN       # overflow wraps
    assert ops.mod(ops.INT_MIN, -1) == 0
    assert ops.binary("+", ops.INT_MAX, 1) == ops.INT_MIN
    assert ops.binary("*", 65536, 65536) == 0
    assert ops.binary("<<", 1, 31) == ops.INT_MIN
    assert ops.binary("<<", 1, 32) == 1                  # amount masked to 0
    assert ops.binary(">>", -1, 5) == -1                 # arithmetic shift
    assert ops.unary("-", ops.INT_MIN) == ops.INT_MIN
    assert ops.unary("~", 0) == -1 and ops.unary("!", 7) == 0


def test_every_result_in_range():
    rng = random.Random(1)
    edge = [0, 1, -1, 2, 31, 32, ops.INT_MAX, ops.INT_MIN, ops.INT_MAX - 1, ops.INT_MIN + 1]
    all_ops = [op for level in BINARY_LEVELS for op in level]
    for _ in range(3000):
        a = rng.choice(edge + [rng.randint(ops.INT_MIN, ops.INT_MAX)])
        b = rng.choice(edge + [rng.randint(ops.INT_MIN, ops.INT_MAX)])
        for op in all_ops:
            assert ops.INT_MIN <= ops.binary(op, a, b) <= ops.INT_MAX


# ---------------------------------------------------------------- interpreter
def test_examples_produce_expected_output():
    expected = {
        "fib.mini": [0, 1, 1, 2, 3, 5, 8, 13, 21, 34],
        "gcd.mini": [21],
        "semantics.mini": [-2147483648, -3, -1, 0, 5, 2, -4, 1],
        "scopes.mini": [11, 22, 1, 5],
    }
    for name, want in expected.items():
        assert output((EXAMPLES / name).read_text()) == want, name


def test_loop_scope_is_fresh_each_iteration():
    assert output("var i = 0; while (i < 3) { var t = i * 10; print t; i = i + 1; }") == [0, 10, 20]


def test_step_limit_stops_infinite_loop():
    with pytest.raises(OutOfFuel) as e:
        output("var i = 0; print 7; while (1) { i = i + 1; }", fuel=500)
    assert e.value.output == [7]


# ---------------------------------------------------------------- printer round trip
def random_expr(rng, depth):
    if depth == 0 or rng.random() < 0.25:
        return IntLit(rng.randint(0, 50)) if rng.random() < 0.5 else Var(rng.choice("abc"))
    if rng.random() < 0.2:
        return Unary(rng.choice(["-", "!", "~"]), random_expr(rng, depth - 1))
    op = rng.choice([op for level in BINARY_LEVELS for op in level])
    return Binary(op, random_expr(rng, depth - 1), random_expr(rng, depth - 1))


def test_printer_round_trip_random_expressions():
    rng = random.Random(42)
    for _ in range(500):
        e = random_expr(rng, 5)
        assert expr_of(expr_source(e)) == e, expr_source(e)


def test_printer_round_trip_examples():
    for path in EXAMPLES.glob("*.mini"):
        if path.name == "errors.mini":
            continue
        prog = parse(path.read_text())
        assert parse(to_source(prog)) == prog, path.name
