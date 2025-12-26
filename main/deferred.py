"""Deferred download management for ChronoDownloader.

DEPRECATED: This module is kept for backwards compatibility.
New code should use main.deferred_queue and main.background_scheduler instead.

The new system provides:
- Persistent queue (survives script restarts)
- Background scheduler (non-blocking retry)
- Centralized quota management
"""
from __future__ import annotations

import logging
import warnings
from typing import Any, Dict, List

from main.deferred_queue import DeferredQueue, DeferredItem, get_deferred_queue
from main.background_scheduler import get_background_scheduler

logger = logging.getLogger(__name__)


def add_deferred_download(item: Dict[str, Any]) -> None:
    """DEPRECATED: Add a deferred download item.
    
    This function is kept for backwards compatibility.
    New code should use get_deferred_queue().add() instead.
    
    Args:
        item: Deferred download info dict (legacy format)
    """
    warnings.warn(
        "add_deferred_download is deprecated. Use get_deferred_queue().add() instead.",
        DeprecationWarning,
        stacklevel=2
    )
    
    queue = get_deferred_queue()
    
    # Extract fields from legacy format
    title = item.get("title", "Unknown")
    creator = item.get("creator")
    entry_id = item.get("entry_id")
    provider_tuple = item.get("provider_tuple")
    work_dir = item.get("work_dir", "")
    base_output_dir = item.get("base_output_dir", "downloaded_works")
    reset_time = item.get("reset_time")
    selected = item.get("selected")
    
    # Extract provider info
    provider_key = "unknown"
    provider_name = "Unknown"
    if provider_tuple and len(provider_tuple) >= 4:
        provider_key = provider_tuple[0]
        provider_name = provider_tuple[3]
    
    # Extract source_id from selected
    source_id = None
    item_url = None
    raw_data = {}
    if selected:
        source_id = getattr(selected, "source_id", None)
        item_url = getattr(selected, "item_url", None)
        raw_data = getattr(selected, "raw", {})
    
    queue.add(
        title=title,
        creator=creator,
        entry_id=entry_id,
        provider_key=provider_key,
        provider_name=provider_name,
        source_id=source_id,
        work_dir=work_dir,
        base_output_dir=base_output_dir,
        item_url=item_url,
        reset_time=reset_time,
        raw_data=raw_data,
    )


def get_deferred_downloads() -> List[Dict[str, Any]]:
    """DEPRECATED: Get list of deferred downloads.
    
    This function is kept for backwards compatibility.
    New code should use get_deferred_queue().get_pending() instead.
    
    Returns:
        List of deferred items (converted to legacy format)
    """
    warnings.warn(
        "get_deferred_downloads is deprecated. Use get_deferred_queue().get_pending() instead.",
        DeprecationWarning,
        stacklevel=2
    )
    
    queue = get_deferred_queue()
    pending = queue.get_pending()
    
    # Convert to legacy format for backwards compatibility
    legacy_items = []
    for item in pending:
        legacy_items.append({
            "title": item.title,
            "creator": item.creator,
            "entry_id": item.entry_id,
            "work_dir": item.work_dir,
            "base_output_dir": item.base_output_dir,
            "reset_time": item.get_reset_datetime(),
            "provider": item.provider_name,
            "_deferred_item": item,  # Reference to new item for internal use
        })
    
    return legacy_items


def clear_deferred_downloads() -> None:
    """DEPRECATED: Clear all deferred downloads.
    
    This function is kept for backwards compatibility.
    New code should use get_deferred_queue().clear_all() instead.
    """
    warnings.warn(
        "clear_deferred_downloads is deprecated. Use get_deferred_queue().clear_all() instead.",
        DeprecationWarning,
        stacklevel=2
    )
    
    queue = get_deferred_queue()
    queue.clear_all()


def remove_deferred_download(item: Dict[str, Any]) -> bool:
    """DEPRECATED: Remove a specific deferred download.
    
    This function is kept for backwards compatibility.
    New code should use get_deferred_queue().remove() instead.
    
    Args:
        item: The item to remove (legacy format)
        
    Returns:
        True if removed, False otherwise
    """
    warnings.warn(
        "remove_deferred_download is deprecated. Use get_deferred_queue().remove() instead.",
        DeprecationWarning,
        stacklevel=2
    )
    
    # Try to get the underlying DeferredItem reference
    deferred_item = item.get("_deferred_item")
    if deferred_item and hasattr(deferred_item, "id"):
        queue = get_deferred_queue()
        return queue.remove(deferred_item.id)
    
    return False


def process_deferred_downloads(wait_for_reset: bool = True) -> int:
    """DEPRECATED: Process deferred downloads synchronously.
    
    This function is kept for backwards compatibility but now simply
    checks if the background scheduler is running and returns the
    count of pending items.
    
    New code should rely on the BackgroundRetryScheduler which handles
    retries automatically in the background.
    
    Args:
        wait_for_reset: Ignored (background scheduler handles waiting)
    
    Returns:
        Number of pending deferred downloads
    """
    warnings.warn(
        "process_deferred_downloads is deprecated. "
        "The BackgroundRetryScheduler now handles retries automatically.",
        DeprecationWarning,
        stacklevel=2
    )
    
    queue = get_deferred_queue()
    scheduler = get_background_scheduler()
    
    pending = queue.get_pending()
    if not pending:
        logger.info("No deferred downloads to process.")
        return 0
    
    if scheduler.is_running():
        logger.info(
            "%d deferred download(s) queued. "
            "Background scheduler is running and will retry when quotas reset.",
            len(pending)
        )
    else:
        logger.info(
            "%d deferred download(s) queued. "
            "Start the background scheduler to process them.",
            len(pending)
        )
    
    return len(pending)


__all__ = [
    "add_deferred_download",
    "get_deferred_downloads",
    "clear_deferred_downloads",
    "remove_deferred_download",
    "process_deferred_downloads",
]
