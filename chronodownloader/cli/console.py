"""Console messages on stderr, and the reports of the listing commands.

With ``--json`` stdout carries only the JSON summary line; every
human-readable message goes to stderr. Messages carry their ANSI style only
when stderr is a terminal and ``NO_COLOR`` is unset or empty, so logs hold
plain text. A command's report (:func:`write_report`) goes to stdout, or to
stderr under ``--json``; :func:`render_table` aligns its columns.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TextIO

__all__ = [
    "iso_time",
    "print_error",
    "print_info",
    "print_warning",
    "render_table",
    "write_line",
    "write_report",
]

INFO = "\033[0;36m"
WARNING = "\033[1;33m"
ERROR = "\033[1;31m"
RESET = "\033[0m"


def _color(stream: TextIO) -> bool:
    """Whether *stream* takes ANSI styles: a terminal, and no ``NO_COLOR``."""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def write_line(message: str, style: str = "") -> None:
    """Write *message* to stderr, in the ANSI *style* on a terminal.

    Text the stream cannot encode is written with ``?`` in its place.
    """
    stream = sys.stderr
    if not _color(stream):
        style = ""
    try:
        stream.write((f"{style}{message}{RESET}" if style else message) + "\n")
    except UnicodeEncodeError:
        safe = message.encode("ascii", "replace").decode("ascii")
        stream.write((f"{style}{safe}{RESET}" if style else safe) + "\n")
    stream.flush()


def print_info(message: str) -> None:
    """Write an informational message."""
    write_line(f"[INFO] {message}", INFO)


def print_warning(message: str) -> None:
    """Write a warning."""
    write_line(f"[WARNING] {message}", WARNING)


def print_error(message: str) -> None:
    """Write an error."""
    write_line(f"[ERROR] {message}", ERROR)


def write_report(text: str, *, json: bool) -> None:
    """Write a command's report: on stdout, or on stderr under ``--json``.

    Text the stream cannot encode is written with ``?`` in its place.
    """
    stream = sys.stderr if json else sys.stdout
    try:
        stream.write(text)
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "replace").decode("ascii"))
    stream.flush()


def render_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Return *header* and *rows* as text with aligned columns."""
    lines = [tuple(header), *(tuple(row) for row in rows)]
    widths = [max(len(line[index]) for line in lines) for index in range(len(header))]
    return "".join(
        "  ".join(
            cell.ljust(width) for cell, width in zip(line, widths, strict=True)
        ).rstrip()
        + "\n"
        for line in lines
    )


def iso_time(epoch: float | None) -> str:
    """Return epoch seconds as ISO 8601 UTC to the second; None gives ``-``."""
    if epoch is None:
        return "-"
    stamp = datetime.fromtimestamp(epoch, tz=UTC).replace(microsecond=0)
    return stamp.isoformat().replace("+00:00", "Z")
