"""Fixtures of the CLI tests: hermetic settings and the write guard."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chronodownloader import settings
from tests.support.testing import WriteGuard

_GUARD = WriteGuard()
_INSTALLED: list[bool] = []


def _install() -> WriteGuard:
    """Register the guard's audit hook once per session; it stays inactive
    outside the tests that start it."""
    if not _INSTALLED:
        sys.addaudithook(_GUARD.audit)
        _INSTALLED.append(True)
    return _GUARD


@pytest.fixture(autouse=True)
def _no_machine_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the packaged example instead of this machine's settings file."""
    monkeypatch.setattr(
        settings, "DEFAULT_SETTINGS_PATH", tmp_path / "config" / "settings.yaml"
    )


@pytest.fixture
def write_guard(tmp_path_factory: pytest.TempPathFactory) -> Iterator[WriteGuard]:
    """Fail the test on a write outside the pytest temp directory."""
    guard = _install()
    guard.start([tmp_path_factory.getbasetemp()])
    try:
        yield guard
    finally:
        leaked = guard.stop()
    if leaked:
        pytest.fail(
            "Test wrote outside the pytest temp directory:\n  " + "\n  ".join(leaked)
        )


@pytest.fixture
def strict_guard() -> Iterator[WriteGuard]:
    """Record every write, the temp directory included; the test checks it."""
    guard = _install()
    guard.start([])
    try:
        yield guard
    finally:
        guard.stop()


def json_lines(text: str) -> list[dict[str, Any]]:
    """Parse each line of *text* as a JSON object."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]
