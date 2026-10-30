"""Run web requests that use a planted bug in separate worker processes.

A planted bug (minicomp.bugs) works by replacing a function inside the
compiler for the duration of a `with planted(name):` block. That change
applies to the whole Python process, and the web server handles requests
in parallel threads that share one compiler. If a request switched a bug
on inside the server process, another visitor compiling at the same moment
would get the broken compiler too.

So the server never plants a bug in its own process. A request that asks
for one is handed to a worker process from a small pool. Each worker is a
separate Python process with its own copy of the compiler and runs one job
at a time: it switches the bug on, does the job, and switches it off again
(even if the job raises). The server process's compiler is never changed.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from typing import Optional

WORKERS = 2
_pool: Optional[ProcessPoolExecutor] = None
_lock = threading.Lock()


class SandboxError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def run_planted(bug: str, name: str, args: tuple):
    """Runs inside a worker process: webapp.server.<name>(*args) with `bug` switched on."""
    import webapp.server as server
    from minicomp.bugs import planted
    with planted(bug):
        return getattr(server, name)(*args)


def _watch_parent(parent: int) -> None:
    """Runs when a worker starts: exit if the server process goes away, even if it was killed."""
    def watch():
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def _ping() -> bool:
    import webapp.server  # noqa: F401  (loads the compiler so the first real job is fast)
    return True


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            # "spawn" starts each worker as a fresh interpreter, so it copies
            # nothing from the server's threads (forking a threaded process is unsafe)
            _pool = ProcessPoolExecutor(max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn"),
                                        initializer=_watch_parent, initargs=(os.getpid(),))
        return _pool


def _discard(pool: ProcessPoolExecutor) -> None:
    global _pool
    with _lock:
        if _pool is pool:
            _pool = None
    pool.shutdown(wait=False, cancel_futures=True)


def warm_up() -> None:
    """Start the workers in the background so the first planted-bug request is fast."""
    def go():
        try:
            pool = _get_pool()
            for f in [pool.submit(_ping) for _ in range(WORKERS)]:
                f.result(timeout=120)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def call(bug: str, name: str, *args, timeout: float = 120.0):
    """Return webapp.server.<name>(*args) computed in a worker with `bug` planted."""
    for _ in range(2):                       # one retry if a worker died
        pool = _get_pool()
        try:
            return pool.submit(run_planted, bug, name, args).result(timeout=timeout)
        except BrokenProcessPool:
            _discard(pool)
        except FutureTimeout:
            raise SandboxError(504, "the planted-bug run took too long") from None
    raise SandboxError(503, "the planted-bug worker is unavailable; try again")


def shutdown() -> None:
    with _lock:
        pool = _pool
    if pool is not None:
        _discard(pool)
