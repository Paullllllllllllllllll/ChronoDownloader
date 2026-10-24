"""Logging setup for front ends.

Library modules call ``logging.getLogger(__name__)`` and never configure
handlers. A front end calls :func:`configure_logging` once at startup; it
places the handlers on one top-level logger (the root by default), and child
loggers reach them through propagation, so each record is emitted once.

Copied from ChronoTranscriber v1.0.0 ``common/log_setup.py`` on 19.10.2026;
nothing removed. Added here: the *redact* filter on every handler (messages
and tracebacks pass through it before any handler writes them) and the
check that the log file can be opened (else stderr only, with a warning),
so that a log target never raises into the code that logs.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

__all__ = ["FILE_FORMAT", "SIMPLE_FORMAT", "configure_logging"]

SIMPLE_FORMAT = "[%(levelname)s] %(message)s"
FILE_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

_HANDLER_MARKER = "_log_setup_handler"


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Stream handler that writes to whatever ``sys.stderr`` is at emit time."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> TextIO:
        return sys.stderr


class _Redacting(logging.Filter):
    """Pass a record's message and traceback through *redact*."""

    def __init__(self, redact: Callable[[str], str]) -> None:
        super().__init__()
        self._redact = redact

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            traceback = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._redact(traceback)
        return True


def _writable(log_file: Path) -> OSError | None:
    """Open *log_file* for appending once; return the error, if any."""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        return exc
    return None


def configure_logging(
    *,
    logger_name: str = "",
    level: int = logging.WARNING,
    log_file: Path | None = None,
    file_level: int = logging.INFO,
    redact: Callable[[str], str] | None = None,
) -> None:
    """Install a stderr handler, and optionally a file handler, on one logger.

    ``logger_name`` selects the logger; a dotted name is reduced to its
    top-level part, and ``""`` is the root logger. Calling again replaces the
    handlers an earlier call installed. The file handler appends UTF-8 text
    to ``log_file`` and creates its folder; a file that cannot be opened is
    left out with a warning on stderr. *redact* rewrites every message and
    traceback before a handler writes it.
    """
    target = logging.getLogger(logger_name.split(".", 1)[0])
    unwritable = log_file
    failure = None if log_file is None else _writable(log_file)
    if failure is not None:
        log_file = None
    for existing in list(target.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            target.removeHandler(existing)
            existing.close()

    console = _StderrHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(SIMPLE_FORMAT))
    handlers: list[logging.Handler] = [console]
    effective = level

    if log_file is not None:
        file_handler = logging.FileHandler(log_file, encoding="utf-8", delay=True)
        file_handler.setLevel(file_level)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
        handlers.append(file_handler)
        effective = min(level, file_level)

    for handler in handlers:
        setattr(handler, _HANDLER_MARKER, True)
        if redact is not None:
            handler.addFilter(_Redacting(redact))
        target.addHandler(handler)
    target.setLevel(effective)
    if failure is not None:
        target.warning(
            "Log file %s cannot be written (%s); logging to stderr only.",
            unwritable,
            failure,
        )
