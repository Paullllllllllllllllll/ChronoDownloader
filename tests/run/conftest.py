"""Fixtures of the run-core tests: a temporary home, no network, a judge key."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from chronodownloader import settings as settings_module
from chronodownloader.process.heartbeat import ENV_VAR
from chronodownloader.settings import Settings
from tests.run.support import run_settings
from tests.support.testing import home_env


class NetworkUsed(AssertionError):
    """A test of the run core opened a connection."""


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A home under *tmp_path*, no machine settings, no sockets, a judge key."""
    home = tmp_path / "home"
    home.mkdir()
    for name, value in home_env(home).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        settings_module, "DEFAULT_SETTINGS_PATH", tmp_path / "config" / "settings.yaml"
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.delenv(ENV_VAR, raising=False)

    def connect(*_args: Any) -> None:
        raise NetworkUsed("the run core opened a connection")

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with the state folder in *tmp_path* and no pacing."""
    return run_settings(tmp_path)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    """The output root of the test."""
    return tmp_path / "out"
