"""MiniComp console: HTTP + JSON API with live streaming (Python standard library only).

    python3 -m webapp.server            # then open http://127.0.0.1:8000
    python3 -m webapp.server --port 9000

On a hosting platform (e.g. Render):
    python -m webapp.server --host 0.0.0.0     # port taken from $PORT

API (version 1, all JSON unless noted):
    GET  /api/v1/health                      liveness + uptime
    GET  /api/v1/meta                        version, passes, limits, language rules
    GET  /api/v1/examples                    the programs in examples/
    POST /api/v1/compile    {"code"}         every stage with timings, optimizer trace,
                                             CFGs and the 8-way differential verdict
    POST /api/v1/generate   {"seed"}         the random program for a seed
    GET  /api/v1/fuzz/stream?n=N&seed=S      Server-Sent Events: one `program` event per
                                             generated program, then `done` (N <= 500)
    POST /api/v1/fuzz       {"n", "seed"}    same campaign as one JSON reply (N <= 100);
                                             used when streaming is unavailable

Every response carries an X-Request-Id header; each request is logged as one
JSON line. Clients are rate-limited with a token bucket per IP address.
Submitted programs only run inside the Mini interpreter and VM, with step limits.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import time
import uuid
from dataclasses import fields, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from minicomp import interp, vm
from minicomp.cfg import EXIT, build_cfg, connect, linearize
from minicomp.errors import CompileError
from minicomp.generator import generate, generate_source
from minicomp.harness import check_program
from minicomp.lexer import tokenize
from minicomp.lower import lower
from minicomp.opt import ALL_PASSES, MAX_ROUNDS, PASS_FUNCS
from minicomp.parser import parse
from minicomp.printer import to_source
from minicomp.semantic import SemanticErrors, check

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
EXAMPLES = ROOT.parent / "examples"
STARTED = time.time()

LIMITS = {
    "body_bytes": 50_000,
    "source_chars": 20_000,
    "fuzz_stream_max": 500,
    "fuzz_batch_max": 100,
    "trace_steps_max": 80,
}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ======================================================================
# Rate limiting: token bucket per client
# ======================================================================
class TokenBucket:
    """Each client may spend up to `capacity` tokens; they refill at `rate` per second."""

    def __init__(self, capacity: float = 60, rate: float = 4) -> None:
        self.capacity, self.rate = capacity, rate
        self.state: Dict[str, Tuple[float, float]] = {}     # ip -> (tokens, last time)
        self.lock = threading.Lock()

    def take(self, ip: str, cost: float = 1) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.state.get(ip, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < cost:
                self.state[ip] = (tokens, now)
                return False
            self.state[ip] = (tokens - cost, now)
            if len(self.state) > 10_000:                     # forget idle clients
                self.state = {k: v for k, v in self.state.items() if now - v[1] < 600}
            return True


BUCKET = TokenBucket()


# ======================================================================
# Compilation with per-stage timing
# ======================================================================
def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


def ast_json(node):
    if isinstance(node, list):
        return [ast_json(n) for n in node]
    if not is_dataclass(node):
        return node
    out = {"type": type(node).__name__}
    for f in fields(node):
        value = getattr(node, f.name)
        if f.name in ("line", "col"):
            out[f.name] = value
        elif value is not None:
            out[f.name] = ast_json(value)
    return out


def count_nodes(node) -> int:
    if isinstance(node, list):
        return sum(count_nodes(n) for n in node)
    if not is_dataclass(node):
        return 0
    return 1 + sum(count_nodes(getattr(node, f.name)) for f in fields(node) if f.compare)


def cfg_json(code):
    return [{"id": b.id, "labels": b.labels, "instrs": [str(i) for i in b.instrs],
             "succ": ["EXIT" if s == EXIT else s for s in b.succ], "pred": b.pred}
            for b in build_cfg(code).blocks]


def traced_optimize(code0):
    """Exactly what opt.optimize does, recording the code after every pass."""
    trace = [{"round": 0, "pass": "input", "size": len(code0), "changed": False,
              "code": [str(i) for i in code0]}]
    code = code0
    for rnd in range(1, MAX_ROUNDS + 1):
        cfg = build_cfg(code)
        prev = linearize(cfg)
        for name in ALL_PASSES:
            PASS_FUNCS[name](cfg)
            connect(cfg.blocks)
            snap = linearize(cfg)
            changed = snap != prev
            if changed and len(trace) < LIMITS["trace_steps_max"]:
                trace.append({"round": rnd, "pass": name, "size": len(snap), "changed": True,
                              "code": [str(i) for i in snap]})
            prev = snap
        new_code = linearize(cfg)
        if new_code == code:
            return code, trace, rnd
        code = new_code
    return code, trace, MAX_ROUNDS


def compile_all(source: str) -> dict:
    if len(source) > LIMITS["source_chars"]:
        return {"ok": False, "phase": "input", "timings_ms": {}, "errors": [
            {"line": 1, "col": 1, "message": f"program is longer than {LIMITS['source_chars']} characters"}]}
    t = {}
    try:
        t0 = time.perf_counter(); tokens = tokenize(source); t["lex"] = _ms(t0)
        t0 = time.perf_counter(); program = parse(source); t["parse"] = _ms(t0)
    except CompileError as e:
        return {"ok": False, "phase": e.phase, "timings_ms": t,
                "errors": [{"line": e.line, "col": e.col, "message": e.message}]}
    except RecursionError:
        return {"ok": False, "phase": "syntax", "timings_ms": t,
                "errors": [{"line": 1, "col": 1, "message": "the program is nested too deeply"}]}
    tok_json = [{"kind": k.kind, "text": k.text, "line": k.line, "col": k.col} for k in tokens]
    t0 = time.perf_counter()
    try:
        table = check(program)
        t["sema"] = _ms(t0)
    except SemanticErrors as e:
        t["sema"] = _ms(t0)
        return {"ok": False, "phase": "semantic", "timings_ms": t, "tokens": tok_json,
                "ast": ast_json(program),
                "errors": [{"line": x.line, "col": x.col, "message": x.message} for x in e.errors]}

    t0 = time.perf_counter()
    try:
        interp.run(program, fuel=200_000)
        finished = True
    except interp.OutOfFuel:
        finished = False
    t["ref"] = _ms(t0)
    t0 = time.perf_counter(); code0 = lower(program); t["tac"] = _ms(t0)
    t0 = time.perf_counter(); cfg0 = cfg_json(code0); t["cfg"] = _ms(t0)
    t0 = time.perf_counter(); code1, trace, rounds = traced_optimize(code0); t["opt"] = _ms(t0)
    steps = {"O0": None, "O1": None}
    t0 = time.perf_counter()
    if finished:
        for name, code in (("O0", code0), ("O1", code1)):
            try:
                steps[name] = vm.run(code, fuel=2_000_000).steps
            except Exception:
                steps[name] = None
    t["vm"] = _ms(t0)
    t0 = time.perf_counter(); verdict = check_program(program); t["diff"] = _ms(t0)

    return {
        "ok": True,
        "timings_ms": t,
        "counts": {"tokens": len(tokens) - 1, "ast_nodes": count_nodes(program),
                   "symbols": len(table.all_symbols), "tac_O0": len(code0), "tac_O1": len(code1),
                   "blocks_O0": len(cfg0), "opt_rounds": rounds},
        "tokens": tok_json,
        "ast": ast_json(program),
        "symbols": [{"name": s.name, "line": s.line, "col": s.col, "depth": s.depth}
                    for s in table.all_symbols],
        "tac": {"O0": [str(i) for i in code0], "O1": [str(i) for i in code1]},
        "cfg": {"O0": cfg0, "O1": cfg_json(code1)},
        "trace": trace,
        "steps": steps,
        "verdict": {"status": verdict.status, "outputs": verdict.outputs, "failing": verdict.failing,
                    "suspects": verdict.suspects, "sizes": verdict.code_size},
    }


# ======================================================================
# Fuzzing
# ======================================================================
def fuzz_one(seed: int) -> dict:
    t0 = time.perf_counter()
    program = generate(seed)
    check(program)
    v = check_program(program)
    return {"seed": seed, "status": v.status, "failing": v.failing, "suspects": v.suspects,
            "size_O0": v.code_size.get("O0"), "size_O1": v.code_size.get("O1"),
            "changed": [p for p, c in v.pass_changed.items() if c], "ms": _ms(t0)}


def summarize(results, seconds: float) -> dict:
    judged = [r for r in results if r["status"] != "timeout"]
    activity = {p: sum(p in r["changed"] for r in judged) for p in ALL_PASSES}
    return {"programs": len(results), "ok": sum(r["status"] == "ok" for r in results),
            "timeouts": len(results) - len(judged),
            "failures": [r for r in results if r["status"] in ("mismatch", "crash")],
            "size_O0": sum(r["size_O0"] or 0 for r in judged),
            "size_O1": sum(r["size_O1"] or 0 for r in judged),
            "pass_activity": activity, "seconds": round(seconds, 2)}


def _int_param(value, name: str, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"'{name}' must be an integer") from None
    if not lo <= n <= hi:
        raise ApiError(400, f"'{name}' must be between {lo} and {hi}")
    return n


def list_examples():
    out = []
    for path in sorted(EXAMPLES.glob("*.mini")):
        text = path.read_text(encoding="utf-8")
        first = text.splitlines()[0] if text else ""
        out.append({"name": path.name, "about": first.lstrip("/ ").strip(), "code": text})
    return out


# ======================================================================
# HTTP layer
# ======================================================================
Route = Callable[["Handler", dict], Optional[dict]]
ROUTES: Dict[Tuple[str, str], Tuple[Route, float]] = {}      # (method, path) -> (handler, cost)


def route(method: str, path: str, cost: float = 1):
    def register(fn: Route) -> Route:
        ROUTES[(method, path)] = (fn, cost)
        return fn
    return register


@route("GET", "/api/v1/health", cost=0)
def _health(h, q):
    return {"status": "ok", "version": VERSION, "uptime_s": round(time.time() - STARTED)}


@route("GET", "/api/v1/meta", cost=0)
def _meta(h, q):
    return {"version": VERSION, "passes": ALL_PASSES, "limits": LIMITS,
            "rules": ["32-bit integers wrap around", "x / 0 == 0", "x % 0 == x",
                      "shift amounts use the low 5 bits", "comparisons give 1 or 0"]}


@route("GET", "/api/v1/examples")
def _examples(h, q):
    return {"examples": list_examples()}


@route("POST", "/api/v1/compile", cost=2)
def _compile(h, body):
    code = body.get("code")
    if not isinstance(code, str):
        raise ApiError(400, "body must be {\"code\": \"...\"}")
    return compile_all(code)


@route("POST", "/api/v1/generate")
def _generate(h, body):
    seed = _int_param(body.get("seed", 0), "seed", 0, 10**9)
    return {"seed": seed, "code": generate_source(seed)}


@route("POST", "/api/v1/fuzz", cost=10)
def _fuzz_batch(h, body):
    n = _int_param(body.get("n", 50), "n", 1, LIMITS["fuzz_batch_max"])
    seed = _int_param(body.get("seed", 0), "seed", 0, 10**9)
    t0 = time.perf_counter()
    results = [fuzz_one(seed + i) for i in range(n)]
    return summarize(results, time.perf_counter() - t0) | {"results": results}


@route("GET", "/api/v1/fuzz/stream", cost=10)
def _fuzz_stream(h, q):
    n = _int_param(q.get("n", "100"), "n", 1, LIMITS["fuzz_stream_max"])
    seed = _int_param(q.get("seed", "0"), "seed", 0, 10**9)
    h.start_stream()
    t0 = time.perf_counter()
    results = []
    try:
        for i in range(n):
            r = fuzz_one(seed + i)
            results.append(r)
            h.send_event("program", r | {"index": i})
        h.send_event("done", summarize(results, time.perf_counter() - t0))
    except (BrokenPipeError, ConnectionResetError):
        h.log_extra["aborted"] = len(results)          # client closed the stream
    return None                                         # response already written


class Handler(BaseHTTPRequestHandler):
    server_version = f"MiniComp/{VERSION}"

    # -- plumbing ---------------------------------------------------------
    def client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For")          # set by hosting proxies
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def _begin(self):
        self.request_id = uuid.uuid4().hex[:12]
        self.t_start = time.perf_counter()
        self.status_sent = 0
        self.log_extra = {}

    def _headers(self, status: int, ctype: str, length: Optional[int] = None, extra=None):
        self.send_response(status)
        self.status_sent = status
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("X-Request-Id", self.request_id)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json", len(body))
        self.wfile.write(body)

    def _api_error(self, status: int, message: str):
        self._json({"ok": False, "error": message, "request_id": self.request_id}, status)

    def start_stream(self):
        self._headers(200, "text/event-stream", extra={"X-Accel-Buffering": "no", "Connection": "close"})
        self.close_connection = True

    def send_event(self, event: str, data: dict):
        payload = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def log_message(self, fmt, *args):            # replaced by one JSON line per request
        pass

    def _log(self):
        entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "id": self.request_id, "method": self.command,
                 "path": urlparse(self.path).path, "status": self.status_sent,
                 "ms": _ms(self.t_start)} | self.log_extra
        print(json.dumps(entry), flush=True)

    # -- dispatch ---------------------------------------------------------
    def _dispatch(self, method: str):
        self._begin()
        url = urlparse(self.path)
        try:
            entry = ROUTES.get((method, url.path))
            if entry is None:
                if method in ("GET", "HEAD") and not url.path.startswith("/api/"):
                    return self._static(url.path, head=(method == "HEAD"))
                raise ApiError(404, f"no route for {method} {url.path}")
            fn, cost = entry
            if cost and not BUCKET.take(self.client_ip(), cost):
                raise ApiError(429, "too many requests; wait a few seconds")
            if method == "POST":
                payload = self._read_json()
            else:
                payload = {k: v[0] for k, v in parse_qs(url.query).items()}
            result = fn(self, payload)
            if result is not None:
                self._json(result if "ok" in result else {"ok": True, **result})
        except ApiError as e:
            self._api_error(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                                   # never leave a client hanging
            self.log_extra["exception"] = f"{type(e).__name__}: {e}"
            try:
                self._api_error(500, "internal error")
            except Exception:
                pass
        finally:
            self._log()

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "invalid Content-Length") from None
        if length < 0 or length > LIMITS["body_bytes"]:
            raise ApiError(413, f"body must be under {LIMITS['body_bytes']} bytes")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(400, "body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _static(self, path: str, head: bool = False):
        if path == "/":
            path = "/index.html"
        target = (STATIC / path.lstrip("/")).resolve()
        if STATIC not in target.parents or not target.is_file():   # blocks ../ escapes
            raise ApiError(404, "not found")
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._headers(200, ctype, len(body))
        if not head:
            self.wfile.write(body)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the MiniComp console.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="default 127.0.0.1 (this computer only); use 0.0.0.0 on a hosting platform")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")),
                    help="default: the PORT environment variable if set, otherwise 8000")
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(json.dumps({"event": "start", "url": f"http://{args.host}:{args.port}", "version": VERSION}), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
