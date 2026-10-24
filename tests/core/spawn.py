"""Spawned child interpreters for the multi-process tests.

A child runs a script under ``tests/core`` with the test's interpreter and
the repository root as working directory; it talks through lines on stdin
and stdout. A timer kills a child that is still running after
:data:`CHILD_TIMEOUT` seconds, so a hung child fails its test instead of
hanging the suite.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHILD_TIMEOUT = 120.0


@contextlib.contextmanager
def child(script: str, *args: str) -> Iterator[subprocess.Popen[str]]:
    """Run ``tests/core/<script>`` with *args*; kill it on exit if still alive."""
    process = subprocess.Popen(
        [sys.executable, f"tests/core/{script}", *args],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    watchdog = threading.Timer(CHILD_TIMEOUT, process.kill)
    watchdog.start()
    try:
        yield process
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill()
        process.communicate()


def line(process: subprocess.Popen[str]) -> str:
    """Return the child's next stdout line; fail the test at end of output."""
    assert process.stdout is not None
    text = process.stdout.readline()
    if not text:
        process.kill()
        _out, err = process.communicate()
        pytest.fail(f"child ended without output:\n{err}")
    return text.strip()


def expect(process: subprocess.Popen[str], word: str) -> list[str]:
    """Read a line that starts with *word*; return its remaining fields."""
    text = line(process)
    fields = text.split()
    if not fields or fields[0] != word:
        process.kill()
        _out, err = process.communicate()
        pytest.fail(f"child printed {text!r} instead of {word!r}:\n{err}")
    return fields[1:]


def send(process: subprocess.Popen[str], text: str = "go") -> None:
    """Write one line to the child's stdin."""
    assert process.stdin is not None
    process.stdin.write(text + "\n")
    process.stdin.flush()
