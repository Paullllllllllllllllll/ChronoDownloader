"""Ctrl+Break on Windows, handled like Ctrl+C.

Python's default for ``SIGBREAK`` ends the process at once, which would leave
an interrupted run's works blocked until their leases expire. With
:func:`install_break_handler` the run releases its leases and records its end,
as it does on Ctrl+C.

Copied from ChronoTranscriber v1.0.0 ``common/interrupt.py`` on 19.10.2026;
nothing removed. The docstrings describe the synchronous core of this tool
instead of an ``asyncio`` event loop.
"""

from __future__ import annotations

import signal
from types import FrameType

__all__ = ["break_as_interrupt", "install_break_handler"]


def break_as_interrupt(signum: int, frame: FrameType | None) -> None:
    """Handle Ctrl+Break as the current Ctrl+C handler would handle Ctrl+C.

    Python's default Ctrl+C handler raises ``KeyboardInterrupt`` in the main
    thread, so the run's cleanup (lease release, run end) runs in its
    ``finally`` blocks before the interrupt reaches the command. A handler
    the front end installed is called instead; an ignored Ctrl+C ignores
    Ctrl+Break as well.
    """
    handler = signal.getsignal(signal.SIGINT)
    if handler is signal.SIG_IGN:
        return
    if callable(handler):
        handler(signal.SIGINT, frame)
        return
    raise KeyboardInterrupt


def install_break_handler() -> None:
    """Route ``SIGBREAK`` to :func:`break_as_interrupt`; a no-op off Windows.

    Signal handlers can only be set from the main thread; elsewhere the
    default stays.
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:
        return
    try:
        signal.signal(sigbreak, break_as_interrupt)
    except ValueError:
        return
