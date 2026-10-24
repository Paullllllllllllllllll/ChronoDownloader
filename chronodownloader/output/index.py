"""``index.csv``: the store's ``work_status`` view rendered for spreadsheets.

The file is rendered from the store after each run and is never read back;
the store stays the only status record. It holds one row per work, ordered
by entry id, with the columns of :data:`INDEX_COLUMNS` (UTF-8, comma
separated, ``\\n`` line ends). A root without a store gets no file.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from chronodownloader.net.atomic import atomic_write_text
from chronodownloader.sqlite import DEFAULT_BUSY_TIMEOUT

from .store import DATABASE_NAME, read_work_status

__all__ = ["INDEX_COLUMNS", "INDEX_NAME", "render_index", "write_index"]

INDEX_NAME: Final = "index.csv"

INDEX_COLUMNS: Final = (
    "entry_id",
    "title",
    "creator",
    "year",
    "folder",
    "status",
    "status_reason",
    "retry_after",
    "selected_provider",
    "selected_source_id",
    "selected_item_url",
    "last_provider",
    "last_source_id",
    "last_format",
    "last_outcome",
    "last_attempt_at",
    "pages_expected",
    "pages_present",
    "file_count",
    "file_bytes",
    "updated_at",
)
"""The ``work_status`` columns ``index.csv`` holds, in order."""


def render_index(rows: Sequence[Mapping[str, Any]]) -> str:
    """Return ``index.csv`` text for ``work_status`` *rows*; None becomes ''."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(INDEX_COLUMNS)
    for row in rows:
        writer.writerow(
            "" if row.get(column) is None else row[column] for column in INDEX_COLUMNS
        )
    return buffer.getvalue()


def write_index(
    root: Path, *, busy_timeout: float = DEFAULT_BUSY_TIMEOUT
) -> Path | None:
    """Render ``<root>/index.csv`` from the store; return its path.

    Returns None, writing nothing, when *root* has no store.

    Raises:
        StoreError: The database is versionless, newer or unreadable.
        OSError: The file could not be written.
    """
    root = Path(root)
    if not (root / DATABASE_NAME).is_file():
        return None
    path = root / INDEX_NAME
    rows = read_work_status(root, busy_timeout=busy_timeout)
    atomic_write_text(path, render_index(rows))
    return path
