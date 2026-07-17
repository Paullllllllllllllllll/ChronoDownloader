"""Index CSV management for ChronoDownloader.

This module provides thread-safe operations for managing the index.csv file
that tracks all processed works and their download status. The ledger is an
upsert-by-``work_id`` table with a fixed, stable column set, so re-running a
work updates its single row instead of appending duplicates, and the header
never freezes on a partial first row.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from typing import Any

import pandas as pd

from api.core.atomic import atomic_write_text
from api.model import SearchResult

logger = logging.getLogger(__name__)

# Thread-safe lock for index.csv updates
_index_csv_lock = threading.Lock()

# In-process cache of parsed index.csv rows, keyed by absolute path and gated
# on the file's (mtime, size) stat signature. This lets update_index_csv skip
# re-reading the whole file when this process made the last write, while a
# stat mismatch (e.g. an external process rewrote the file) forces a re-read.
# All access happens under _index_csv_lock, so no separate lock is needed.
_index_cache: dict[str, tuple[list[dict[str, str]], float, int]] = {}

# Fixed, stable column set. The header is always written in full so a partial
# first row (e.g. a dry-run row lacking a status) can never drop a column for
# every subsequent write.
INDEX_COLUMNS: list[str] = [
    "work_id",
    "entry_id",
    "work_dir",
    "title",
    "creator",
    "selected_provider",
    "selected_provider_key",
    "selected_source_id",
    "selected_dir",
    "work_json",
    "item_url",
    "status",
    "pages_expected",
    "pages_downloaded",
]


def _index_path(base_output_dir: str) -> str:
    return os.path.join(base_output_dir, "index.csv")


def _read_existing_rows(index_path: str) -> list[dict[str, str]]:
    if not os.path.exists(index_path):
        return []
    try:
        if os.path.getsize(index_path) == 0:
            return []
    except OSError:
        return []
    try:
        with open(index_path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        logger.exception("Failed to read existing index.csv; starting fresh")
        return []


def _index_stat(index_path: str) -> tuple[float, int]:
    """Return the (mtime, size) signature of ``index_path`` (zeros if absent)."""
    try:
        st = os.stat(index_path)
    except OSError:
        return (0.0, 0)
    return (st.st_mtime, st.st_size)


def _load_rows_for_update(index_path: str) -> list[dict[str, str]]:
    """Return parsed index rows, from cache when the stat signature matches.

    On a cache miss the file is read via ``_read_existing_rows`` and the result
    is stored keyed on the file's own post-read stat signature.
    """
    key = _index_stat(index_path)
    cached = _index_cache.get(index_path)
    if cached is not None:
        rows, cached_mtime, cached_size = cached
        if (cached_mtime, cached_size) == key:
            return rows

    rows = _read_existing_rows(index_path)
    mtime, size = _index_stat(index_path)
    _index_cache[index_path] = (rows, mtime, size)
    return rows


def update_index_csv(base_output_dir: str, row: dict[str, Any]) -> None:
    """Thread-safe upsert of a row into index.csv keyed by ``work_id``.

    Existing rows are preserved; a row with a matching ``work_id`` is replaced
    in place, otherwise the row is appended. The full stable header is always
    written and the file is rewritten atomically.

    Args:
        base_output_dir: Base output directory containing index.csv
        row: Dictionary of row data (keys outside INDEX_COLUMNS are ignored)
    """
    with _index_csv_lock:
        try:
            os.makedirs(base_output_dir, exist_ok=True)
            index_path = _index_path(base_output_dir)

            rows = _load_rows_for_update(index_path)
            work_id = str(row.get("work_id", "") or "")

            normalized = {col: _cell(row.get(col)) for col in INDEX_COLUMNS}

            replaced = False
            if work_id:
                for i, existing in enumerate(rows):
                    if str(existing.get("work_id", "")) == work_id:
                        merged = {
                            col: normalized[col]
                            if row.get(col) is not None
                            else _cell(existing.get(col))
                            for col in INDEX_COLUMNS
                        }
                        rows[i] = merged
                        replaced = True
                        break
            if not replaced:
                rows.append(normalized)

            buf = _render_csv(rows)
            atomic_write_text(index_path, buf)
            # Refresh the cache with the just-written rows, keyed on the file's
            # own stat signature so the next call can skip the re-read.
            mtime, size = _index_stat(index_path)
            _index_cache[index_path] = (rows, mtime, size)
        except Exception:
            logger.exception("Failed to update index.csv")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _render_csv(rows: list[dict[str, str]]) -> str:
    import io

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=INDEX_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({col: r.get(col, "") for col in INDEX_COLUMNS})
    return out.getvalue()


def build_index_row(
    work_id: str,
    entry_id: str | None,
    work_dir: str,
    title: str,
    creator: str | None,
    selected: SearchResult | None,
    selected_source_id: str | None,
    work_json_path: str,
    status: str | None = None,
    item_url: str | None = None,
    pages_expected: int | None = None,
    pages_downloaded: int | None = None,
) -> dict[str, Any]:
    """Build a row dictionary for index.csv.

    Args:
        work_id: Computed work ID
        entry_id: Entry identifier from CSV
        work_dir: Path to work directory
        title: Work title
        creator: Optional creator name
        selected: Selected SearchResult or None
        selected_source_id: Pre-computed source ID
        work_json_path: Path to work.json
        status: Optional status
        item_url: Optional item URL
        pages_expected: Expected page count (IIIF completeness)
        pages_downloaded: Downloaded page count (IIIF completeness)

    Returns:
        Dictionary suitable for index.csv upsert
    """
    row: dict[str, Any] = {
        "work_id": work_id,
        "entry_id": entry_id,
        "work_dir": work_dir,
        "title": title,
        "creator": creator,
        "selected_provider": selected.provider if selected else None,
        "selected_provider_key": selected.provider_key if selected else None,
        "selected_source_id": selected_source_id,
        "selected_dir": work_dir if selected else None,
        "work_json": work_json_path,
        "item_url": item_url if item_url else (selected.item_url if selected else None),
        "status": status,
        "pages_expected": pages_expected,
        "pages_downloaded": pages_downloaded,
    }
    return row


def read_index_csv(base_output_dir: str) -> pd.DataFrame | None:
    """Read the index.csv file.

    Args:
        base_output_dir: Base output directory containing index.csv

    Returns:
        DataFrame or None if file doesn't exist or can't be read
    """
    index_path = _index_path(base_output_dir)
    if not os.path.exists(index_path):
        return None

    try:
        return pd.read_csv(index_path, encoding="utf-8")
    except Exception:
        logger.exception("Failed to read index.csv")
        return None


def get_processed_work_ids(base_output_dir: str) -> set[str]:
    """Get set of already-processed work IDs from index.csv.

    Args:
        base_output_dir: Base output directory containing index.csv

    Returns:
        Set of work_id strings
    """
    df = read_index_csv(base_output_dir)
    if df is None or "work_id" not in df.columns:
        return set()
    return set(df["work_id"].dropna().astype(str))


__all__ = [
    "INDEX_COLUMNS",
    "update_index_csv",
    "build_index_row",
    "read_index_csv",
    "get_processed_work_ids",
]
