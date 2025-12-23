"""Unified CSV management for ChronoDownloader.

This module provides thread-safe operations for reading and writing to the
sampling CSV file, which serves as the single source of truth for download
status and URLs.

Expected CSV columns (from bib_sampling.ipynb):
    entry_id, short_title, primary_category, stratum_abbrev, selection_type,
    re_sampled, full_title, main_author, earliest_year, regional_editions,
    total_editions, pps_weight, design_weight, short_note, retrievable, link
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Thread-safe lock for CSV updates
_csv_lock = threading.Lock()

# Column names from the sampling notebook
ENTRY_ID_COL = "entry_id"
TITLE_COL = "short_title"
CREATOR_COL = "main_author"
STATUS_COL = "retrievable"
LINK_COL = "link"

# Additional columns we may add
PROVIDER_COL = "download_provider"
TIMESTAMP_COL = "download_timestamp"


def load_works_csv(csv_path: str) -> pd.DataFrame:
    """Load the works CSV file.
    
    Args:
        csv_path: Path to the CSV file
        
    Returns:
        DataFrame with all columns preserved
        
    Raises:
        FileNotFoundError: If CSV file doesn't exist
        ValueError: If required columns are missing
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    
    df = pd.read_csv(csv_path)
    
    # Validate required columns
    required = [ENTRY_ID_COL, TITLE_COL]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")
    
    # Ensure status and link columns exist
    if STATUS_COL not in df.columns:
        df[STATUS_COL] = pd.NA
    if LINK_COL not in df.columns:
        df[LINK_COL] = pd.NA
    
    return df


def get_pending_works(df: pd.DataFrame) -> pd.DataFrame:
    """Get works that need to be processed.
    
    A work is pending if:
    - retrievable is empty/NA (never attempted)
    - retrievable is False (failed, eligible for retry)
    
    Args:
        df: DataFrame loaded from CSV
        
    Returns:
        Filtered DataFrame of pending works
    """
    # Convert to nullable boolean for proper comparison
    status = df[STATUS_COL]
    
    # Pending = not True (i.e., NA, empty, False, or any non-True value)
    def is_pending(val):
        if pd.isna(val):
            return True
        if isinstance(val, bool):
            return not val
        if isinstance(val, str):
            return val.strip().lower() not in ("true", "1", "yes", "y")
        return True
    
    mask = status.apply(is_pending)
    return df[mask].copy()


def get_completed_entry_ids(df: pd.DataFrame) -> set:
    """Get set of entry_ids that are already completed.
    
    Args:
        df: DataFrame loaded from CSV
        
    Returns:
        Set of entry_id values where retrievable=True
    """
    def is_completed(val):
        if pd.isna(val):
            return False
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("true", "1", "yes", "y")
        return False
    
    mask = df[STATUS_COL].apply(is_completed)
    return set(df.loc[mask, ENTRY_ID_COL].astype(str))


def _backup_csv(csv_path: str) -> str:
    """Create a timestamped backup of the CSV file.
    
    Args:
        csv_path: Path to the CSV file
        
    Returns:
        Path to the backup file
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{csv_path}.backup_{timestamp}"
    shutil.copy2(csv_path, backup_path)
    return backup_path


def _save_csv(df: pd.DataFrame, csv_path: str) -> None:
    """Save DataFrame to CSV, preserving all columns.
    
    Args:
        df: DataFrame to save
        csv_path: Path to save to
    """
    df.to_csv(csv_path, index=False, encoding="utf-8")


def mark_success(
    csv_path: str,
    entry_id: str,
    item_url: str,
    provider: Optional[str] = None,
) -> bool:
    """Mark a work as successfully downloaded.
    
    Thread-safe update that sets retrievable=True and populates link.
    
    Args:
        csv_path: Path to the CSV file
        entry_id: Entry ID to update
        item_url: URL of the downloaded item
        provider: Optional provider name
        
    Returns:
        True if update succeeded, False otherwise
    """
    with _csv_lock:
        try:
            df = pd.read_csv(csv_path)
            
            # Find the row
            mask = df[ENTRY_ID_COL].astype(str) == str(entry_id)
            if not mask.any():
                logger.warning("Entry ID %s not found in CSV", entry_id)
                return False
            
            # Update status and link
            df.loc[mask, STATUS_COL] = True
            df.loc[mask, LINK_COL] = item_url
            
            # Add provider if column exists or create it
            if provider:
                if PROVIDER_COL not in df.columns:
                    df[PROVIDER_COL] = pd.NA
                df.loc[mask, PROVIDER_COL] = provider
            
            # Add timestamp
            if TIMESTAMP_COL not in df.columns:
                df[TIMESTAMP_COL] = pd.NA
            df.loc[mask, TIMESTAMP_COL] = datetime.now(timezone.utc).isoformat()
            
            _save_csv(df, csv_path)
            logger.debug("Marked entry %s as success", entry_id)
            return True
            
        except Exception:
            logger.exception("Failed to mark entry %s as success", entry_id)
            return False


def mark_failed(
    csv_path: str,
    entry_id: str,
    reason: Optional[str] = None,
) -> bool:
    """Mark a work as failed to download.
    
    Thread-safe update that sets retrievable=False.
    
    Args:
        csv_path: Path to the CSV file
        entry_id: Entry ID to update
        reason: Optional failure reason (not stored in CSV, just logged)
        
    Returns:
        True if update succeeded, False otherwise
    """
    with _csv_lock:
        try:
            df = pd.read_csv(csv_path)
            
            # Find the row
            mask = df[ENTRY_ID_COL].astype(str) == str(entry_id)
            if not mask.any():
                logger.warning("Entry ID %s not found in CSV", entry_id)
                return False
            
            # Update status
            df.loc[mask, STATUS_COL] = False
            
            # Add timestamp
            if TIMESTAMP_COL not in df.columns:
                df[TIMESTAMP_COL] = pd.NA
            df.loc[mask, TIMESTAMP_COL] = datetime.now(timezone.utc).isoformat()
            
            _save_csv(df, csv_path)
            
            if reason:
                logger.debug("Marked entry %s as failed: %s", entry_id, reason)
            else:
                logger.debug("Marked entry %s as failed", entry_id)
            return True
            
        except Exception:
            logger.exception("Failed to mark entry %s as failed", entry_id)
            return False


def mark_deferred(
    csv_path: str,
    entry_id: str,
) -> bool:
    """Mark a work as deferred (e.g., rate-limited, will retry later).
    
    Deferred works are left with retrievable=NA so they will be retried.
    
    Args:
        csv_path: Path to the CSV file
        entry_id: Entry ID to update
        
    Returns:
        True if update succeeded, False otherwise
    """
    with _csv_lock:
        try:
            df = pd.read_csv(csv_path)
            
            # Find the row
            mask = df[ENTRY_ID_COL].astype(str) == str(entry_id)
            if not mask.any():
                logger.warning("Entry ID %s not found in CSV", entry_id)
                return False
            
            # Leave status as NA (pending for retry)
            df.loc[mask, STATUS_COL] = pd.NA
            
            _save_csv(df, csv_path)
            logger.debug("Marked entry %s as deferred", entry_id)
            return True
            
        except Exception:
            logger.exception("Failed to mark entry %s as deferred", entry_id)
            return False


def get_stats(csv_path: str) -> dict:
    """Get download statistics from the CSV.
    
    Args:
        csv_path: Path to the CSV file
        
    Returns:
        Dictionary with counts: total, completed, failed, pending
    """
    try:
        df = pd.read_csv(csv_path)
        total = len(df)
        
        def classify(val):
            if pd.isna(val):
                return "pending"
            if isinstance(val, bool):
                return "completed" if val else "failed"
            if isinstance(val, str):
                lowered = val.strip().lower()
                if lowered in ("true", "1", "yes", "y"):
                    return "completed"
                if lowered in ("false", "0", "no", "n"):
                    return "failed"
            return "pending"
        
        status_counts = df[STATUS_COL].apply(classify).value_counts()
        
        return {
            "total": total,
            "completed": int(status_counts.get("completed", 0)),
            "failed": int(status_counts.get("failed", 0)),
            "pending": int(status_counts.get("pending", 0)),
        }
    except Exception:
        logger.exception("Failed to get stats from CSV")
        return {"total": 0, "completed": 0, "failed": 0, "pending": 0}


__all__ = [
    "ENTRY_ID_COL",
    "TITLE_COL",
    "CREATOR_COL",
    "STATUS_COL",
    "LINK_COL",
    "load_works_csv",
    "get_pending_works",
    "get_completed_entry_ids",
    "mark_success",
    "mark_failed",
    "mark_deferred",
    "get_stats",
]
