"""Tests for net/deadline.py.

``TestProcessExitNotBlocked`` ports v1.30.3
``tests/unit/test_search_timeout.py:240-290``: a stalled call must not pin
the interpreter at exit.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from chronodownloader.net.deadline import DeadlineExceeded, run_with_deadline

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_returns_the_value_in_time() -> None:
    assert run_with_deadline(lambda: 42, 5.0) == 42


def test_without_a_deadline_the_call_runs_inline() -> None:
    assert run_with_deadline(lambda: "inline", None) == "inline"
    assert run_with_deadline(lambda: "inline", 0) == "inline"


def test_an_exception_reaches_the_caller() -> None:
    def fail() -> None:
        raise LookupError("no such record")

    with pytest.raises(LookupError, match="no such record"):
        run_with_deadline(fail, 5.0)


def test_a_stalled_call_raises_after_its_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded) as excinfo:
        run_with_deadline(lambda: time.sleep(5), 0.2)
    assert time.monotonic() - started < 2.0
    assert isinstance(excinfo.value, TimeoutError)
    assert excinfo.value.timeout_s == 0.2


_PREAMBLE = """
import threading, time
from chronodownloader.net.deadline import DeadlineExceeded, run_with_deadline

def slow():
    time.sleep(30)
    return "slow"

def fast():
    return "fast"
"""


def _run_script(body: str) -> subprocess.CompletedProcess[str]:
    """Run *body* in a fresh interpreter; the 15 s timeout is the assertion."""
    return subprocess.run(
        [sys.executable, "-c", _PREAMBLE + textwrap.dedent(body)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.slow
class TestProcessExitNotBlocked:
    """A stalled call must not keep the interpreter alive at exit."""

    def test_parallel_fan_out_exits(self) -> None:
        proc = _run_script(
            """
            results = {}
            def search(name, fn):
                try:
                    results[name] = run_with_deadline(fn, 0.5)
                except DeadlineExceeded:
                    pass
            threads = [
                threading.Thread(target=search, args=(name, fn))
                for name, fn in (("slow", slow), ("fast", fast))
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            print("SENTINEL_DONE", sorted(results))
            """
        )
        assert proc.returncode == 0, proc.stderr
        assert "SENTINEL_DONE ['fast']" in proc.stdout

    def test_sequential_run_with_deadline_exits(self) -> None:
        proc = _run_script(
            """
            found = []
            for name, fn in (("slow", slow), ("fast", fast)):
                try:
                    found.append(run_with_deadline(fn, 0.5))
                except DeadlineExceeded:
                    pass
            print("SENTINEL_DONE", found)
            """
        )
        assert proc.returncode == 0, proc.stderr
        assert "SENTINEL_DONE ['fast']" in proc.stdout
