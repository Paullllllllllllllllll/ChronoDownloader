"""Atomic file-write helpers.

Provides a single, shared implementation of the temp-file + ``os.replace``
pattern so that state files, CSV ledgers, and JSON metadata are never left
half-written when a process crashes mid-write. Lives in ``api/`` so both the
library layer and the application layer can use it without violating the
api/ -> main/ layering rule (api/ must not import main/).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import Any


def _atomic_replace(path: str, tmp_path: str) -> None:
    try:
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def atomic_write_text(
    path: str, data: str, encoding: str = "utf-8", newline: str = "\n"
) -> None:
    """Write ``data`` to ``path`` atomically (temp file + ``os.replace``).

    Args:
        path: Destination path.
        data: Text to write.
        encoding: Text encoding (default UTF-8).
        newline: Newline policy passed to ``open`` (default ``"\\n"``).
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
    _atomic_replace(path, tmp_path)


def atomic_write_json(
    path: str,
    obj: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
) -> None:
    """Serialize ``obj`` to JSON and write it to ``path`` atomically.

    Args:
        path: Destination path.
        obj: JSON-serializable object.
        indent: Indentation for ``json.dumps`` (default 2).
        ensure_ascii: Passed to ``json.dumps`` (default False for human UTF-8).
    """
    text = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii)
    atomic_write_text(path, text)


__all__ = ["atomic_write_text", "atomic_write_json"]
