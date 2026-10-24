"""process/interrupt.py: Ctrl+Break follows the current Ctrl+C handler.

ChronoTranscriber v1.0.0 has no test of ``common/interrupt.py``; these pin the
copied behavior.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from types import FrameType
from typing import Any

import pytest

from chronodownloader.process.interrupt import (
    break_as_interrupt,
    install_break_handler,
)


def _current(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(signal, "getsignal", lambda signum: handler)


def test_the_default_ctrl_c_handler_raises_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _current(monkeypatch, signal.default_int_handler)
    with pytest.raises(KeyboardInterrupt):
        break_as_interrupt(signal.SIGINT, None)


def test_an_installed_handler_receives_sigint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, FrameType | None]] = []

    def handler(signum: int, frame: FrameType | None) -> None:
        calls.append((signum, frame))

    _current(monkeypatch, handler)
    break_as_interrupt(99, None)
    assert calls == [(signal.SIGINT, None)]


def test_an_ignored_ctrl_c_ignores_ctrl_break(monkeypatch: pytest.MonkeyPatch) -> None:
    _current(monkeypatch, signal.SIG_IGN)
    break_as_interrupt(signal.SIGINT, None)


def test_the_system_default_raises_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _current(monkeypatch, signal.SIG_DFL)
    with pytest.raises(KeyboardInterrupt):
        break_as_interrupt(signal.SIGINT, None)


@pytest.fixture
def sigbreak() -> Iterator[int]:
    number = getattr(signal, "SIGBREAK", None)
    if number is None:
        pytest.skip("SIGBREAK exists on Windows only")
    before = signal.getsignal(number)
    yield number
    signal.signal(number, before)


def test_install_routes_sigbreak_from_the_main_thread(sigbreak: int) -> None:
    install_break_handler()
    assert signal.getsignal(sigbreak) is break_as_interrupt


def test_install_off_the_main_thread_keeps_the_default(sigbreak: int) -> None:
    before = signal.getsignal(sigbreak)
    worker = threading.Thread(target=install_break_handler)
    worker.start()
    worker.join()
    assert signal.getsignal(sigbreak) is before
