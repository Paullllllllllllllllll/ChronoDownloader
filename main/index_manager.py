"""Index CSV management for ChronoDownloader.

This module provides thread-safe operations for managing the index.csv file
that tracks all processed works and their download status.
"""
from __future__ import annotations

import logging
import os
import csv
import threading
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Thread-safe lock for index.csv updates
_index_csv_lock = threading.Lock()

_index_header_cache: Dict[str, List[str]] = {}


def update_index_csv(base_output_dir: str, row: Dict[str, Any]) -> None:
    """Thread-safe update of index.csv.
    
    Appends a row to the index.csv file, creating headers if the file
    doesn't exist yet.
    
    Args:
        base_output_dir: Base output directory containing index.csv
        row: Dictionary of row data to append
    """
    with _index_csv_lock:
        try:
            os.makedirs(base_output_dir, exist_ok=True)
            index_path = os.path.join(base_output_dir, "index.csv")

            file_exists = os.path.exists(index_path)
            file_empty = False
            if file_exists:
                try:
                    file_empty = os.path.getsize(index_path) == 0
                except OSError:
                    file_empty = False

            header_cols: Optional[List[str]] = _index_header_cache.get(index_path)
            header_from_file = False
            if header_cols is None:
                header_cols = None
                if file_exists and not file_empty:
                    try:
                        with open(index_path, "r", encoding="utf-8", newline="") as f:
                            reader = csv.reader(f)
                            hdr = next(reader, None)
                            if hdr:
                                header_cols = [str(c) for c in hdr]
                                header_from_file = True
                    except Exception:
                        header_cols = None

                if header_cols is None:
                    header_cols = [str(k) for k in row.keys()]

                _index_header_cache[index_path] = header_cols
            else:
                header_from_file = True

            needs_header = (not file_exists) or file_empty
            if file_exists and (not file_empty) and (not header_from_file):
                needs_header = False

            with open(index_path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=header_cols, extrasaction="ignore")
                if needs_header:
                    writer.writeheader()
                writer.writerow(row)
        except Exception:
            logger.exception("Failed to update index.csv")


def build_index_row(
    work_id: str,
    entry_id: str | None,
    work_dir: str,
    title: str,
    creator: str | None,
    selected,
    selected_source_id: str | None,
    work_json_path: str,
    status: str | None = None,
    item_url: str | None = None,
) -> Dict[str, Any]:
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
        status: Optional status override
        
    Returns:
        Dictionary suitable for index.csv row
    """
    row = {
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
    }
    if status is not None:
        row["status"] = status
    return row


def read_index_csv(base_output_dir: str) -> pd.DataFrame | None:
    """Read the index.csv file.
    
    Args:
        base_output_dir: Base output directory containing index.csv
        
    Returns:
        DataFrame or None if file doesn't exist or can't be read
    """
    index_path = os.path.join(base_output_dir, "index.csv")
    if not os.path.exists(index_path):
        return None
    
    try:
        return pd.read_csv(index_path)
    except Exception:
        logger.exception("Failed to read index.csv")
        return None


def get_processed_work_ids(base_output_dir: str) -> set:
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
    "update_index_csv",
    "build_index_row",
    "read_index_csv",
    "get_processed_work_ids",
]
