"""Persistent deferred download queue.

This module provides a thread-safe queue for tracking downloads that were
deferred due to quota exhaustion or other temporary failures.

Persistence is handled via the unified StateManager.

Quota-Limited Providers:
- Only providers with daily/hourly quotas (e.g., Anna's Archive) defer downloads
- Unlimited providers (MDZ, Gallica) use rate limiting and never defer
- Deferred items include reset_time to indicate when quota will be available again

Features:
- Automatic persistence via StateManager (survives script restarts)
- Thread-safe operations
- Retry tracking with configurable max retries
- Provider-based filtering for selective retry
- Auto-cleanup of old completed/failed items
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterator

from api.core.config import get_config

if TYPE_CHECKING:
    from main.state_manager import StateManager

logger = logging.getLogger(__name__)

# Legacy constant (kept for backward compatibility)
DEFAULT_QUEUE_FILE = ".deferred_queue.json"

@dataclass
class DeferredItem:
    """A single deferred download item.
    
    Attributes:
        id: Unique identifier for this deferred item
        title: Work title
        creator: Optional creator/author
        entry_id: Entry ID from CSV
        provider_key: Provider that caused the deferral
        provider_name: Human-readable provider name
        source_id: Provider-specific item ID (e.g., MD5 for Anna's Archive)
        work_dir: Target directory for download
        base_output_dir: Base output directory
        item_url: URL to the item
        deferred_at: When the item was deferred (ISO format)
        reset_time: When quota is expected to reset (ISO format)
        retry_count: Number of retry attempts
        last_retry_at: Last retry attempt time (ISO format)
        status: Current status (pending, retrying, completed, failed)
        error_message: Last error message if any
        raw_data: Additional raw data from SearchResult
    """
    id: str
    title: str
    creator: str | None
    entry_id: str | None
    provider_key: str
    provider_name: str
    source_id: str | None
    work_dir: str
    base_output_dir: str
    item_url: str | None = None
    deferred_at: str | None = None
    reset_time: str | None = None
    retry_count: int = 0
    last_retry_at: str | None = None
    status: str = "pending"
    error_message: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeferredItem":
        """Create from dictionary."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            title=data.get("title", "Unknown"),
            creator=data.get("creator"),
            entry_id=data.get("entry_id"),
            provider_key=data.get("provider_key", "unknown"),
            provider_name=data.get("provider_name", "Unknown"),
            source_id=data.get("source_id"),
            work_dir=data.get("work_dir", ""),
            base_output_dir=data.get("base_output_dir", "downloaded_works"),
            item_url=data.get("item_url"),
            deferred_at=data.get("deferred_at"),
            reset_time=data.get("reset_time"),
            retry_count=int(data.get("retry_count", 0)),
            last_retry_at=data.get("last_retry_at"),
            status=data.get("status", "pending"),
            error_message=data.get("error_message"),
            raw_data=data.get("raw_data", {}),
        )
    
    def get_reset_datetime(self) -> datetime | None:
        """Get reset time as datetime.
        
        Returns:
            Reset datetime (UTC) or None
        """
        if not self.reset_time:
            return None
        try:
            dt = datetime.fromisoformat(self.reset_time)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None
    
    def is_ready_for_retry(self) -> bool:
        """Check if item is ready to retry (reset time has passed).
        
        Returns:
            True if ready for retry
        """
        if self.status not in ("pending", "retrying"):
            return False
        
        reset_dt = self.get_reset_datetime()
        if not reset_dt:
            return True  # No reset time specified, assume ready
        
        now = datetime.now(timezone.utc)
        return now >= reset_dt
    
    def seconds_until_ready(self) -> float:
        """Get seconds until item is ready for retry.
        
        Returns:
            Seconds until ready, or 0 if ready now
        """
        reset_dt = self.get_reset_datetime()
        if not reset_dt:
            return 0.0
        
        now = datetime.now(timezone.utc)
        delta = (reset_dt - now).total_seconds()
        return max(0.0, delta)

class DeferredQueue:
    """Thread-safe, persistent queue for deferred downloads.
    
    Manages a queue of downloads that were deferred due to quota exhaustion
    or other temporary failures. Automatically persists to JSON.
    """
    
    _instance: "DeferredQueue" | None = None
    _lock = threading.Lock()
    
    def __new__(cls, queue_file: str | None = None) -> "DeferredQueue":
        """Singleton pattern - only one DeferredQueue instance."""
        with cls._lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instance = instance
            return cls._instance
    
    def __init__(self, queue_file: str | None = None):
        """Initialize the deferred queue.
        
        Args:
            queue_file: Ignored (kept for backward compatibility)
        """
        if getattr(self, "_initialized", False):
            return
        
        self._items: dict[str, DeferredItem] = {}
        self._data_lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._state_manager: StateManager | None = None  # Lazy init to avoid circular imports
        
        # Get max retries from config
        cfg = get_config()
        deferred_cfg = cfg.get("deferred", {})
        self._max_retries = int(deferred_cfg.get("max_retries", 5))
        
        # Load existing queue
        self._load_queue()
        self._initialized = True
        logger.debug("DeferredQueue initialized")
    
    def _get_state_manager(self) -> StateManager:
        """Get the StateManager instance (lazy init)."""
        if self._state_manager is None:
            from main.state_manager import get_state_manager
            self._state_manager = get_state_manager()
        return self._state_manager
    
    def _load_queue(self) -> None:
        """Load queue from StateManager."""
        try:
            state_manager = self._get_state_manager()
            items_data = state_manager.get_deferred_items()
            
            for item_data in items_data:
                item = DeferredItem.from_dict(item_data)
                self._items[item.id] = item
            
            if self._items:
                # Count by status
                pending = sum(1 for i in self._items.values() if i.status == "pending")
                logger.info(
                    "Loaded %d deferred item(s) from queue (%d pending)",
                    len(self._items), pending
                )
                
                # Auto-cleanup old completed/failed items
                self.cleanup_old_items(max_age_days=7)
        except Exception as e:
            logger.warning("Failed to load deferred queue: %s", e)
    
    def _save_queue(self) -> None:
        """Save queue to StateManager."""
        with self._save_lock:
            try:
                state_manager = self._get_state_manager()
                items_data = [item.to_dict() for item in self._items.values()]
                state_manager.set_deferred_items(items_data)
            except Exception as e:
                logger.warning("Failed to save deferred queue: %s", e)
    
    def add(
        self,
        title: str,
        creator: str | None,
        entry_id: str | None,
        provider_key: str,
        provider_name: str,
        source_id: str | None,
        work_dir: str,
        base_output_dir: str,
        item_url: str | None = None,
        reset_time: datetime | None = None,
        raw_data: dict[str, Any] | None = None,
    ) -> DeferredItem:
        """Add a new item to the deferred queue.
        
        Args:
            title: Work title
            creator: Optional creator/author
            entry_id: Entry ID from CSV
            provider_key: Provider that caused deferral
            provider_name: Human-readable provider name
            source_id: Provider-specific item ID
            work_dir: Target directory for download
            base_output_dir: Base output directory
            item_url: URL to the item
            reset_time: When quota is expected to reset
            raw_data: Additional raw data
            
        Returns:
            Created DeferredItem
        """
        with self._data_lock:
            # Check for duplicate (same entry_id and provider)
            for existing in self._items.values():
                if (existing.entry_id == entry_id and 
                    existing.provider_key == provider_key and
                    existing.status == "pending"):
                    logger.debug(
                        "Item already in queue: %s from %s",
                        title, provider_name
                    )
                    # Update reset time if newer
                    if reset_time:
                        existing.reset_time = reset_time.isoformat()
                        self._save_queue()
                    return existing
            
            item = DeferredItem(
                id=str(uuid.uuid4()),
                title=title,
                creator=creator,
                entry_id=entry_id,
                provider_key=provider_key,
                provider_name=provider_name,
                source_id=source_id,
                work_dir=work_dir,
                base_output_dir=base_output_dir,
                item_url=item_url,
                deferred_at=datetime.now(timezone.utc).isoformat(),
                reset_time=reset_time.isoformat() if reset_time else None,
                status="pending",
                raw_data=raw_data or {},
            )
            
            self._items[item.id] = item
            self._save_queue()
            
            logger.info(
                "Added to deferred queue: '%s' from %s (reset in %.1f hours)",
                title, provider_name,
                item.seconds_until_ready() / 3600 if item.seconds_until_ready() > 0 else 0
            )
            
            return item
    
    def remove(self, item_id: str) -> bool:
        """Remove an item from the queue.
        
        Args:
            item_id: Item ID to remove
            
        Returns:
            True if removed, False if not found
        """
        with self._data_lock:
            if item_id in self._items:
                del self._items[item_id]
                self._save_queue()
                return True
            return False
    
    def mark_completed(self, item_id: str) -> bool:
        """Mark an item as successfully completed.
        
        Args:
            item_id: Item ID
            
        Returns:
            True if marked, False if not found
        """
        with self._data_lock:
            if item_id in self._items:
                self._items[item_id].status = "completed"
                self._save_queue()
                logger.info("Deferred item completed: %s", self._items[item_id].title)
                return True
            return False
    
    def mark_failed(self, item_id: str, error_message: str | None = None) -> bool:
        """Mark an item as permanently failed.
        
        Args:
            item_id: Item ID
            error_message: Error description
            
        Returns:
            True if marked, False if not found
        """
        with self._data_lock:
            if item_id in self._items:
                item = self._items[item_id]
                item.status = "failed"
                item.error_message = error_message
                self._save_queue()
                logger.warning(
                    "Deferred item failed: %s - %s",
                    item.title, error_message or "Unknown error"
                )
                return True
            return False
    
    def mark_retrying(
        self,
        item_id: str,
        new_reset_time: datetime | None = None
    ) -> bool:
        """Mark an item as being retried (increment retry count).
        
        If max retries exceeded, marks as failed instead.
        
        Args:
            item_id: Item ID
            new_reset_time: New reset time if quota hit again
            
        Returns:
            True if can continue retrying, False if max retries exceeded
        """
        with self._data_lock:
            if item_id not in self._items:
                return False
            
            item = self._items[item_id]
            item.retry_count += 1
            item.last_retry_at = datetime.now(timezone.utc).isoformat()
            
            if item.retry_count >= self._max_retries:
                item.status = "failed"
                item.error_message = f"Max retries ({self._max_retries}) exceeded"
                self._save_queue()
                logger.warning(
                    "Deferred item exceeded max retries: %s (%d attempts)",
                    item.title, item.retry_count
                )
                return False
            
            item.status = "retrying"
            if new_reset_time:
                item.reset_time = new_reset_time.isoformat()
            
            self._save_queue()
            logger.info(
                "Deferred item retry %d/%d: %s",
                item.retry_count, self._max_retries, item.title
            )
            return True
    
    def get(self, item_id: str) -> DeferredItem | None:
        """Get an item by ID.
        
        Args:
            item_id: Item ID
            
        Returns:
            DeferredItem or None
        """
        with self._data_lock:
            return self._items.get(item_id)
    
    def get_pending(self) -> list[DeferredItem]:
        """Get all pending items (not completed or failed).
        
        Returns:
            List of pending DeferredItems
        """
        with self._data_lock:
            return [
                item for item in self._items.values()
                if item.status in ("pending", "retrying")
            ]
    
    def get_ready(self) -> list[DeferredItem]:
        """Get items that are ready for retry (reset time passed).
        
        Returns:
            List of ready DeferredItems
        """
        with self._data_lock:
            return [
                item for item in self._items.values()
                if item.is_ready_for_retry()
            ]
    
    def get_by_provider(self, provider_key: str) -> list[DeferredItem]:
        """Get pending items for a specific provider.
        
        Args:
            provider_key: Provider identifier
            
        Returns:
            List of DeferredItems for the provider
        """
        with self._data_lock:
            return [
                item for item in self._items.values()
                if item.provider_key == provider_key and item.status in ("pending", "retrying")
            ]
    
    def get_next_ready_time(self) -> datetime | None:
        """Get the earliest reset time among pending items.
        
        Returns:
            Earliest reset datetime or None if no pending items
        """
        with self._data_lock:
            earliest: datetime | None = None
            
            for item in self._items.values():
                if item.status not in ("pending", "retrying"):
                    continue
                
                reset_dt = item.get_reset_datetime()
                if reset_dt and (earliest is None or reset_dt < earliest):
                    earliest = reset_dt
            
            return earliest
    
    def count_by_status(self) -> dict[str, int]:
        """Get count of items by status.
        
        Returns:
            Dictionary of status -> count
        """
        with self._data_lock:
            counts: dict[str, int] = {}
            for item in self._items.values():
                counts[item.status] = counts.get(item.status, 0) + 1
            return counts
    
    def __len__(self) -> int:
        """Get total number of items in queue."""
        with self._data_lock:
            return len(self._items)
    
    def __iter__(self) -> Iterator[DeferredItem]:
        """Iterate over all items."""
        with self._data_lock:
            return iter(list(self._items.values()))
    
    def clear_completed(self) -> int:
        """Remove all completed items from queue.
        
        Returns:
            Number of items removed
        """
        with self._data_lock:
            to_remove = [
                item_id for item_id, item in self._items.items()
                if item.status == "completed"
            ]
            for item_id in to_remove:
                del self._items[item_id]
            
            if to_remove:
                self._save_queue()
                logger.info("Cleared %d completed item(s) from queue", len(to_remove))
            
            return len(to_remove)
    
    def cleanup_old_items(self, max_age_days: int = 7) -> int:
        """Remove completed and failed items older than max_age_days.
        
        This is called automatically on queue load to prevent unbounded growth.
        
        Args:
            max_age_days: Maximum age in days for completed/failed items
            
        Returns:
            Number of items removed
        """
        with self._data_lock:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=max_age_days)
            
            to_remove = []
            for item_id, item in self._items.items():
                if item.status not in ("completed", "failed"):
                    continue
                
                # Check deferred_at timestamp
                if item.deferred_at:
                    try:
                        deferred = datetime.fromisoformat(item.deferred_at)
                        if deferred.tzinfo is None:
                            deferred = deferred.replace(tzinfo=timezone.utc)
                        if deferred < cutoff:
                            to_remove.append(item_id)
                    except Exception:
                        pass
            
            for item_id in to_remove:
                del self._items[item_id]
            
            if to_remove:
                self._save_queue()
                logger.info(
                    "Auto-cleanup: Removed %d old item(s) (older than %d days)",
                    len(to_remove), max_age_days
                )
            
            return len(to_remove)
    
    def clear_all(self) -> int:
        """Remove all items from queue.
        
        Returns:
            Number of items removed
        """
        with self._data_lock:
            count = len(self._items)
            self._items.clear()
            self._save_queue()
            logger.info("Cleared all %d item(s) from queue", count)
            return count

def get_deferred_queue() -> DeferredQueue:
    """Get the singleton DeferredQueue instance.
    
    Returns:
        DeferredQueue instance
    """
    return DeferredQueue()

__all__ = [
    "DeferredQueue",
    "DeferredItem",
    "get_deferred_queue",
]
