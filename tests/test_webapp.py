"""Tests for the web backend: compile logic, optimizer trace, fuzzing, and the HTTP API."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import webapp.server as srv
from minicomp.generator import generate, generate_source
from minicomp.lower import lower
from minicomp.opt import optimize
from minicomp.semantic import check

EXAMPLES = Path(__file__).parent.parent / "examples"


# ---------------------------------------------------------------- compile logic
def test_compile_returns_every_stage_with_timings():
    r = srv.compile_all((EXAMPLES / "gcd.mini").read_text())
    assert r["ok"] and r["verdict"]["status"] == "ok"
    assert set(r["timings_ms"]) == {"lex", "parse", "sema", "ref", "tac", "cfg", "opt", "vm", "regs", "diff"}
    assert r["counts"]["tokens"] == 36 and r["counts"]["tac_O0"] == 10 and r["counts"]["tac_O1"] == 9
    assert r["verdict"]["outputs"]["reference"] == [21] and len(r["verdict"]["outputs"]) == 9
    assert r["tac"]["O1"] == r["trace"][-1]["code"]            # trace ends at the final code
    assert r["steps"]["O1"] < r["steps"]["O0"]
    assert r["machine"]["units"][0]["spilled"] == [] and r["machine"]["steps"] > 0


def test_compile_shows_each_function():
    r = srv.compile_all((EXAMPLES / "functions.mini").read_text())
    assert r["verdict"]["outputs"]["reference"] == [1594323, 610, -2147483648]
    assert [u["unit"] for u in r["cfg"]["O1"]] == ["main", "power", "fib", "show"]
    assert [u["unit"] for u in r["machine"]["units"]] == ["main", "power", "fib", "show"]
    assert {(s["name"], s["kind"]) for s in r["symbols"]} >= {("fib", "function"), ("n", "param")}


def test_compile_shows_arrays_globals_and_loops():
    r = srv.compile_all((EXAMPLES / "arrays.mini").read_text())
    assert r["verdict"]["status"] == "ok" and r["verdict"]["outputs"]["reference"] == [29, 29, 832040, 59]
    kinds = {(s["name"], s["kind"]) for s in r["symbols"]}
    assert {("memo", "array[32]"), ("primes", "array[10]"), ("calls", "var")} <= kinds
    assert "array @memo[32]" in r["tac"]["O0"]                # a global array, created in main's prologue
    assert any("@calls" in line for line in r["machine"]["code"])


def test_trace_reaches_exactly_what_the_optimizer_produces():
    for seed in range(60):
        p = generate(seed)
        check(p)
        code0 = lower(p)
        final, trace, _ = srv.traced_optimize(code0)       # programs with functions too
        assert final == optimize(code0), seed
        assert trace[0]["pass"] == "input" and all(s["changed"] for s in trace[1:])


def test_errors_stop_at_the_right_stage():
    r = srv.compile_all("var a = b; var a = 1;")
    assert r["phase"] == "semantic" and len(r["errors"]) == 2
    assert set(r["timings_ms"]) == {"lex", "parse", "sema"}
    assert srv.compile_all("print 1")["phase"] == "syntax"
    assert srv.compile_all("x = 1 @ 2;")["phase"] == "lexical"
    deep = "print " + "(" * 5000 + "1" + ")" * 5000 + ";"
    assert srv.compile_all(deep)["errors"][0]["message"] == "the program is nested too deeply"
    assert srv.compile_all("print 1;" * 5000)["phase"] == "input"


def test_fuzz_summary_and_rate_limit():
    results = [srv.fuzz_one(s) for s in range(8)]
    s = srv.summarize(results, 1.0)
    assert s["programs"] == 8 and s["ok"] == 8 and s["failures"] == []
    bucket = srv.TokenBucket(capacity=20, rate=0)
    assert bucket.take("a", 10) and bucket.take("a", 10) and not bucket.take("a", 1)
    assert bucket.take("b", 10)                                 # clients are separate


# ---------------------------------------------------------------- HTTP API
def start_server():
    srv.BUCKET = srv.TokenBucket(capacity=1000, rate=100)       # no throttling in tests
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def test_http_api_end_to_end():
    httpd, base = start_server()
    try:
        status, headers, body = call(base + "/api/v1/health")
        assert status == 200 and json.loads(body)["status"] == "ok"
        assert len(headers["X-Request-Id"]) == 12

        status, _, body = call(base + "/api/v1/compile", {"code": "print 6 * 7;"})
        r = json.loads(body)
        assert status == 200 and r["tac"]["O1"] == ["print 42"] and r["verdict"]["status"] == "ok"

        status, _, body = call(base + "/api/v1/generate", {"seed": 3})
        assert json.loads(body)["code"] == generate_source(3)

        status, headers, body = call(base + "/api/v1/fuzz/stream?n=3&seed=0")
        assert headers["Content-Type"] == "text/event-stream"
        events = [e for e in body.split("\n\n") if e.strip()]
        assert [e.split("\n")[0] for e in events] == ["event: program"] * 3 + ["event: done"]
        assert json.loads(events[-1].split("data: ", 1)[1])["ok"] == 3

        assert call(base + "/api/v1/fuzz", {"n": 2, "seed": 0})[0] == 200
        assert call(base + "/api/v1/fuzz/stream?n=9999")[0] == 400
        assert call(base + "/api/v1/compile", {"code": 5})[0] == 400
        assert call(base + "/api/v1/nope")[0] == 404
        assert call(base + "/")[0] == 200
    finally:
        httpd.shutdown()


def test_rate_limit_returns_429():
    httpd, base = start_server()
    srv.BUCKET = srv.TokenBucket(capacity=10, rate=0)
    try:
        assert call(base + "/api/v1/fuzz/stream?n=1")[0] == 200
        status, _, body = call(base + "/api/v1/fuzz/stream?n=1")
        assert status == 429 and "too many requests" in json.loads(body)["error"]
    finally:
        httpd.shutdown()


def test_batch_fuzz_keeps_ok_as_the_success_flag():
    # regression: the summary's own "ok" count (0 when every program fails)
    # used to replace the success flag, so the page reported an error
    from minicomp.bugs import planted
    httpd, base = start_server()
    try:
        with planted("liveness-ignores-branch"):
            status, _, body = call(base + "/api/v1/fuzz", {"n": 2, "seed": 0})
        r = json.loads(body)
        assert status == 200 and r["ok"] is True
        assert r["summary"]["programs"] == 2 and len(r["summary"]["failures"]) >= 1
        assert len(r["results"]) == 2
    finally:
        httpd.shutdown()
