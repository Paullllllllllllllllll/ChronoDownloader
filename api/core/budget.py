"""Download budget tracking and enforcement for ChronoDownloader.

Manages global and per-work download limits by content type (images, PDFs, metadata)
to prevent runaway jobs and respect configured constraints.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .config import get_download_limits

logger = logging.getLogger(__name__)

class DownloadBudget:
    """Tracks and enforces download limits across the whole run.

    Limits are configured under config.json -> download_limits:
    {
      "total": {
        "images_gb": 100,
        "pdfs_gb": 50,
        "metadata_gb": 1
      },
      "per_work": {
        "images_gb": 5,
        "pdfs_gb": 3,
        "metadata_mb": 10
      },
      "on_exceed": "skip"  # "skip" | "stop"
    }
    """

    def __init__(self):
        self._lock = threading.Lock()
        # Global counters by content type (in bytes)
        self.total_images_bytes = 0
        self.total_pdfs_bytes = 0
        self.total_metadata_bytes = 0
        # Per-work counters: {work_id: {"images": bytes, "pdfs": bytes, "metadata": bytes}}
        self.per_work: dict[str, dict[str, int]] = {}
        self._exhausted = False

    @staticmethod
    def _gb_to_bytes(gb: Any) -> int | None:
        """Convert GB value to bytes, or None if unlimited."""
        try:
            val = float(gb)
            return int(val * 1024 * 1024 * 1024) if val > 0 else None
        except Exception:
            return None

    @staticmethod
    def _mb_to_bytes(mb: Any) -> int | None:
        """Convert MB value to bytes, or None if unlimited."""
        try:
            val = float(mb)
            return int(val * 1024 * 1024) if val > 0 else None
        except Exception:
            return None

    @staticmethod
    def _limit_value(v: Any) -> int | None:
        """Convert config value to an integer limit or None if unlimited."""
        try:
            iv = int(v)
            return iv if iv > 0 else None
        except Exception:
            return None

    def _policy(self) -> str:
        """Get the on_exceed policy: 'skip' or 'stop'."""
        dl = get_download_limits()
        pol = str(dl.get("on_exceed", "skip") or "skip").lower()
        return "stop" if pol == "stop" else "skip"

    def exhausted(self) -> bool:
        """Check if the download budget has been exhausted."""
        with self._lock:
            return self._exhausted

    def _inc(self, bucket: dict[str, dict[str, int]], key: str, field: str, delta: int) -> int:
        """Increment a counter in a nested bucket."""
        m = bucket.setdefault(key, {"images": 0, "pdfs": 0, "metadata": 0})
        m[field] = int(m.get(field, 0)) + int(delta)
        return m[field]

    def _get(self, bucket: dict[str, dict[str, int]], key: str, field: str) -> int:
        """Get a counter value from a nested bucket."""
        return int(bucket.get(key, {}).get(field, 0))

    def allow_content(self, content_type: str, work_id: str | None, add_bytes: int | None) -> bool:
        """Check if additional content can be downloaded within budget limits.
        
        Args:
            content_type: Type of content ("images", "pdfs", or "metadata")
            work_id: Work ID for per-work limits
            add_bytes: Number of bytes to add
            
        Returns:
            True if content is allowed, False if limit would be exceeded
        """
        if not add_bytes or add_bytes <= 0:
            return True
        
        if content_type not in ["images", "pdfs", "metadata"]:
            logger.warning(f"Unknown content type: {content_type}, defaulting to allow")
            return True
        
        dl = get_download_limits()
        
        # Get total limits
        total_limits = dl.get("total", {})
        
        # Global limit for this content type
        limit_key = f"{content_type}_gb"
        max_total = self._gb_to_bytes(total_limits.get(limit_key))
        
        current_total = getattr(self, f"total_{content_type}_bytes", 0)
        if max_total is not None and (current_total + add_bytes) > max_total:
            logger.info(f"Global {content_type} limit would be exceeded: {(current_total + add_bytes) / (1024**3):.2f} GB > {max_total / (1024**3):.2f} GB")
            if self._policy() == "stop":
                with self._lock:
                    self._exhausted = True
            return False
        
        # Per-work limit for this content type
        if work_id:
            per_work_limits = dl.get("per_work", {})
            
            # Handle MB for metadata, GB for others
            if content_type == "metadata":
                limit_key = "metadata_mb"
                max_work = self._mb_to_bytes(per_work_limits.get(limit_key))
            else:
                limit_key = f"{content_type}_gb"
                max_work = self._gb_to_bytes(per_work_limits.get(limit_key))
            
            current_work = self._get(self.per_work, work_id, content_type)
            if max_work is not None and (current_work + add_bytes) > max_work:
                logger.info(f"Per-work {content_type} limit would be exceeded for {work_id}")
                if self._policy() == "stop":
                    with self._lock:
                        self._exhausted = True
                return False
        
        return True

    def record_download(self, content_type: str, work_id: str | None, size_bytes: int) -> None:
        """Record a completed download.
        
        Args:
            content_type: Type of content ("images", "pdfs", or "metadata")
            work_id: Work ID for per-work tracking
            size_bytes: Size of downloaded content in bytes
        """
        if content_type not in ["images", "pdfs", "metadata"]:
            logger.warning(f"Unknown content type for recording: {content_type}")
            return
        
        with self._lock:
            # Update global counter
            attr_name = f"total_{content_type}_bytes"
            current = getattr(self, attr_name, 0)
            setattr(self, attr_name, current + size_bytes)
            
            # Update per-work counter
            if work_id:
                self._inc(self.per_work, work_id, content_type, size_bytes)

    def log_summary(self) -> None:
        """Log a summary of current download statistics."""
        with self._lock:
            logger.info("=== Download Budget Summary ===")
            logger.info(f"Images: {self.total_images_bytes / (1024**3):.2f} GB")
            logger.info(f"PDFs: {self.total_pdfs_bytes / (1024**3):.2f} GB")
            logger.info(f"Metadata: {self.total_metadata_bytes / (1024**2):.2f} MB")
            logger.info(f"Total works tracked: {len(self.per_work)}")
            
            # Log per-work details if not too many
            if len(self.per_work) <= 10:
                for wid, stats in self.per_work.items():
                    logger.info(f"  Work {wid}: images={stats.get('images', 0) / (1024**2):.1f}MB, "
                              f"pdfs={stats.get('pdfs', 0) / (1024**2):.1f}MB, "
                              f"metadata={stats.get('metadata', 0) / 1024:.1f}KB")

    # Legacy compatibility methods
    def allow_new_file(self, provider: str | None, work_id: str | None) -> bool:
        """Legacy method for backward compatibility. Always returns True."""
        return not self._exhausted

    def allow_bytes(self, provider: str | None, work_id: str | None, add_bytes: int | None) -> bool:
        """Legacy method for backward compatibility. Checks against total limits."""
        if not add_bytes or add_bytes <= 0:
            return True
        # Default to checking against images limit for legacy calls
        return self.allow_content("images", work_id, add_bytes)

    def add_bytes(self, provider: str | None, work_id: str | None, add_bytes: int) -> bool:
        """Add bytes to budget during streaming download.
        
        Args:
            provider: Provider key (unused in new system)
            work_id: Work ID for per-work tracking
            add_bytes: Number of bytes to add
            
        Returns:
            True if bytes were accepted, False if limit exceeded
        """
        # Try to allow the bytes first (checks limits)
        if not self.allow_content("images", work_id, add_bytes):
            return False
        
        # Record the bytes
        with self._lock:
            self.total_images_bytes += add_bytes
            if work_id:
                self._inc(self.per_work, work_id, "images", add_bytes)
        
        return True

    def add_file(self, provider: str | None, work_id: str | None) -> None:
        """Record a completed file download (legacy method).
        
        Args:
            provider: Provider key (unused in new system)
            work_id: Work ID for per-work tracking
        """
        # This method is called after successful download, no size needed
        # The size was already tracked via add_bytes during download
        pass

    def record_file(self, provider: str | None, work_id: str | None, size_bytes: int) -> None:
        """Legacy method for backward compatibility."""
        self.record_download("images", work_id, size_bytes)

# Global singleton budget tracker
_BUDGET = DownloadBudget()

def get_budget() -> DownloadBudget:
    """Get the global download budget tracker."""
    return _BUDGET

def budget_exhausted() -> bool:
    """Check if the global download budget has been exhausted."""
    return _BUDGET.exhausted()
