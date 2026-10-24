"""Atomic file writes: a temporary file promoted with ``os.replace``.

Copied from ChronoDownloader v1.30.3 ``api/core/atomic.py`` on 19.10.2026.
Changed: paths may be ``os.PathLike``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from typing import Any

__all__ = ["atomic_write_json", "atomic_write_text", "replace_with_retry"]

# On Windows a transiently open destination (a virus scanner, a file viewer)
# makes os.replace raise PermissionError; a short bounded retry rides it out.
_REPLACE_ATTEMPTS = 5
_REPLACE_BASE_SLEEP_S = 0.1

type PathText = str | os.PathLike[str]


def replace_with_retry(path: PathText, tmp_path: PathText) -> None:
    """Promote *tmp_path* onto *path*, retrying a transient Windows lock.

    On failure the temporary file is removed and the error re-raised.
    """
    try:
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_BASE_SLEEP_S * (attempt + 1))
    except BaseException:  # no temporary file survives any exit; re-raised
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def atomic_write_text(
    path: PathText, data: str, encoding: str = "utf-8", newline: str = "\n"
) -> None:
    """Write *data* to *path* atomically (temporary file and ``os.replace``)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(data)
            # On NTFS the data is not journaled: without the fsync a crash
            # shortly after the replace can leave a zero-length file.
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:  # no temporary file survives any exit; re-raised
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
    replace_with_retry(path, tmp_path)


def atomic_write_json(
    path: PathText, obj: Any, *, indent: int | None = 2, ensure_ascii: bool = False
) -> None:
    """Serialize *obj* to JSON and write it to *path* atomically."""
    atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii))
