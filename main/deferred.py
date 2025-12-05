"""Deferred download management for ChronoDownloader.

This module handles tracking and retrying downloads that were deferred
due to quota exhaustion (e.g., Anna's Archive daily limits).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from api.core.context import clear_current_provider, set_current_provider
from api.model import QuotaDeferredException

logger = logging.getLogger(__name__)

# Thread-safe deferred downloads tracking
# Structure: List of dicts with keys: title, creator, entry_id, base_output_dir,
#           selected, provider_tuple, work_dir, reset_time, provider
_deferred_downloads: List[Dict[str, Any]] = []
_deferred_downloads_lock = threading.Lock()


def add_deferred_download(item: Dict[str, Any]) -> None:
    """Thread-safe addition to deferred downloads list.
    
    Args:
        item: Deferred download info dict with keys:
            - title, creator, entry_id, base_output_dir
            - selected (SearchResult)
            - provider_tuple (key, search_fn, download_fn, name)
            - work_dir, reset_time, provider
    """
    with _deferred_downloads_lock:
        _deferred_downloads.append(item)


def get_deferred_downloads() -> List[Dict[str, Any]]:
    """Get the list of deferred downloads waiting for quota reset.
    
    Thread-safe.
    
    Returns:
        List of deferred download info dicts (copy)
    """
    with _deferred_downloads_lock:
        return list(_deferred_downloads)


def clear_deferred_downloads() -> None:
    """Clear the deferred downloads list.
    
    Thread-safe.
    """
    with _deferred_downloads_lock:
        _deferred_downloads.clear()


def remove_deferred_download(item: Dict[str, Any]) -> bool:
    """Remove a specific item from the deferred downloads list.
    
    Thread-safe.
    
    Args:
        item: The item to remove
        
    Returns:
        True if item was found and removed, False otherwise
    """
    with _deferred_downloads_lock:
        try:
            _deferred_downloads.remove(item)
            return True
        except ValueError:
            return False


def process_deferred_downloads(wait_for_reset: bool = True) -> int:
    """Process downloads that were deferred due to quota exhaustion.
    
    This function loops until all deferred downloads are complete (or permanently
    failed). After each pass, if quota-limited items remain, it waits for the
    quota reset time before retrying.
    
    Thread-safe.
    
    Args:
        wait_for_reset: If True, wait for the quota reset time before retrying.
                       If False, only retry items whose reset time has passed.
    
    Returns:
        Number of successfully processed deferred downloads
    """
    total_processed = 0
    pass_number = 0
    permanent_failures: set = set()  # Track items that failed for non-quota reasons
    
    while True:
        pass_number += 1
        
        # Get current deferred downloads (excluding permanent failures)
        with _deferred_downloads_lock:
            pending = [
                item for item in _deferred_downloads
                if id(item) not in permanent_failures
            ]
            if not pending:
                if pass_number == 1:
                    logger.info("No deferred downloads to process.")
                break
        
        logger.info(
            "Deferred downloads pass %d: %d item(s) to process...",
            pass_number, len(pending)
        )
        
        # Find the earliest reset time among pending items
        earliest_reset = None
        for item in pending:
            reset_time = item.get("reset_time")
            if reset_time and (earliest_reset is None or reset_time < earliest_reset):
                earliest_reset = reset_time
        
        # Wait for quota reset if needed
        if wait_for_reset and earliest_reset:
            now = datetime.now(timezone.utc)
            # Handle naive datetime from Anna's Archive
            if earliest_reset.tzinfo is None:
                earliest_reset = earliest_reset.replace(tzinfo=timezone.utc)
            
            wait_seconds = (earliest_reset - now).total_seconds()
            if wait_seconds > 0:
                wait_hours = wait_seconds / 3600
                logger.info(
                    "Waiting %.1f hours for quota reset before processing deferred downloads...",
                    wait_hours
                )
                # Sleep in chunks and log progress
                remaining = wait_seconds
                while remaining > 0:
                    sleep_time = min(remaining, 3600)  # Sleep max 1 hour at a time
                    logger.info(
                        "Deferred downloads: %.1f hours remaining until quota reset...",
                        remaining / 3600
                    )
                    time.sleep(sleep_time)
                    remaining -= sleep_time
                logger.info("Quota reset time reached. Retrying deferred downloads...")
        
        # Process pending items in this pass
        pass_processed = 0
        quota_limited = []
        
        for item in pending:
            title = item.get("title")
            selected = item.get("selected")
            provider_tuple = item.get("provider_tuple")
            work_dir = item.get("work_dir")
            
            if not selected or not provider_tuple or not work_dir:
                logger.warning("Incomplete deferred download info for '%s', skipping.", title)
                permanent_failures.add(id(item))
                continue
            
            pkey, _search_func, download_func, pname = provider_tuple
            logger.info("Retrying deferred download for '%s' from %s", title, pname)
            
            try:
                set_current_provider(pkey)
                ok = download_func(selected, work_dir)
                if ok:
                    logger.info("Deferred download succeeded for '%s'", title)
                    pass_processed += 1
                    total_processed += 1
                    # Remove from deferred list
                    remove_deferred_download(item)
                else:
                    logger.warning("Deferred download failed for '%s' (non-quota reason)", title)
                    permanent_failures.add(id(item))
            except QuotaDeferredException as qde:
                logger.info(
                    "Deferred download for '%s' quota-limited: %s",
                    title, qde.message
                )
                # Update reset time for next pass
                item["reset_time"] = qde.reset_time
                quota_limited.append(item)
            except Exception:
                logger.exception("Error retrying deferred download for '%s'", title)
                permanent_failures.add(id(item))
            finally:
                try:
                    clear_current_provider()
                except Exception:
                    pass
        
        logger.info(
            "Pass %d complete: %d succeeded, %d quota-limited, %d permanently failed",
            pass_number, pass_processed, len(quota_limited), len(permanent_failures)
        )
        
        # If no quota-limited items remain, we're done
        if not quota_limited:
            break
        
        # Check if we should continue waiting for quota resets
        if not wait_for_reset:
            logger.info(
                "wait_for_reset=False; %d quota-limited items will remain deferred.",
                len(quota_limited)
            )
            break
    
    # Final summary
    with _deferred_downloads_lock:
        remaining = len(_deferred_downloads)
    
    if remaining > 0:
        logger.info(
            "Deferred downloads complete: %d succeeded, %d still pending.",
            total_processed, remaining
        )
    else:
        logger.info("All deferred downloads processed: %d succeeded.", total_processed)
    
    return total_processed


__all__ = [
    "add_deferred_download",
    "get_deferred_downloads",
    "clear_deferred_downloads",
    "remove_deferred_download",
    "process_deferred_downloads",
]
