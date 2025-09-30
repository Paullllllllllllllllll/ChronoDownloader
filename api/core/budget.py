"""Download budget tracking and enforcement for ChronoDownloader.

Manages global, per-work, and per-provider download limits to prevent runaway jobs
and respect configured constraints.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from .config import get_download_limits

logger = logging.getLogger(__name__)


class DownloadBudget:
    """Tracks and enforces download limits across the whole run.

    Limits are configured under config.json -> download_limits:
    {
      "max_total_files": 0,            # 0 or missing = unlimited
      "max_total_bytes": 0,
      "per_work": { "max_files": 0, "max_bytes": 0 },
      "per_provider": { "mdz": {"max_files": 0, "max_bytes": 0}, ... },
      "on_exceed": "skip"             # "skip" | "stop"
    }
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.total_files = 0
        self.total_bytes = 0
        self.per_work: Dict[str, Dict[str, int]] = {}
        self.per_provider: Dict[str, Dict[str, int]] = {}
        self._exhausted = False

    @staticmethod
    def _limit_value(v: Any) -> Optional[int]:
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

    def _inc(self, bucket: Dict[str, Dict[str, int]], key: str, field: str, delta: int) -> int:
        """Increment a counter in a nested bucket."""
        m = bucket.setdefault(key, {"files": 0, "bytes": 0})
        m[field] = int(m.get(field, 0)) + int(delta)
        return m[field]

    def _get(self, bucket: Dict[str, Dict[str, int]], key: str, field: str) -> int:
        """Get a counter value from a nested bucket."""
        return int(bucket.get(key, {}).get(field, 0))

    def allow_new_file(self, provider: Optional[str], work_id: Optional[str]) -> bool:
        """Check if a new file can be downloaded within budget limits.
        
        Args:
            provider: Provider key for per-provider limits
            work_id: Work ID for per-work limits
            
        Returns:
            True if file is allowed, False if limit would be exceeded
        """
        dl = get_download_limits()
        
        # Global file limit
        max_total_files = self._limit_value(dl.get("max_total_files"))
        if max_total_files is not None and (self.total_files + 1) > max_total_files:
            if self._policy() == "stop":
                with self._lock:
                    self._exhausted = True
            return False
        
        # Per-provider file limit
        if provider:
            per = dict(dl.get("per_provider", {}) or {})
            pl = self._limit_value((per.get(provider) or {}).get("max_files"))
            if pl is not None and (self._get(self.per_provider, provider, "files") + 1) > pl:
                if self._policy() == "stop":
                    with self._lock:
                        self._exhausted = True
                return False
        
        # Per-work file limit
        if work_id:
            pw = dict(dl.get("per_work", {}) or {})
            wl = self._limit_value(pw.get("max_files"))
            if wl is not None and (self._get(self.per_work, work_id, "files") + 1) > wl:
                if self._policy() == "stop":
                    with self._lock:
                        self._exhausted = True
                return False
        
        return True

    def allow_bytes(self, provider: Optional[str], work_id: Optional[str], add_bytes: Optional[int]) -> bool:
        """Check if additional bytes can be downloaded within budget limits.
        
        Args:
            provider: Provider key for per-provider limits
            work_id: Work ID for per-work limits
            add_bytes: Number of bytes to add
            
        Returns:
            True if bytes are allowed, False if limit would be exceeded
        """
        if not add_bytes or add_bytes <= 0:
            return True
        
        dl = get_download_limits()
        
        # Global byte limit
        mtb = self._limit_value(dl.get("max_total_bytes"))
        if mtb is not None and (self.total_bytes + add_bytes) > mtb:
            if self._policy() == "stop":
                with self._lock:
                    self._exhausted = True
            return False
        
        # Per-provider byte limit
        if provider:
            per = dict(dl.get("per_provider", {}) or {})
            pl = self._limit_value((per.get(provider) or {}).get("max_bytes"))
            if pl is not None and (self._get(self.per_provider, provider, "bytes") + add_bytes) > pl:
                if self._policy() == "stop":
                    with self._lock:
                        self._exhausted = True
                return False
        
        # Per-work byte limit
        if work_id:
            pw = dict(dl.get("per_work", {}) or {})
            wl = self._limit_value(pw.get("max_bytes"))
            if wl is not None and (self._get(self.per_work, work_id, "bytes") + add_bytes) > wl:
                if self._policy() == "stop":
                    with self._lock:
                        self._exhausted = True
                return False
        
        return True

    def add_bytes(self, provider: Optional[str], work_id: Optional[str], n: int) -> bool:
        """Add bytes to counters; return True if still within limits.
        
        Args:
            provider: Provider key
            work_id: Work ID
            n: Number of bytes to add
            
        Returns:
            True if within limits after adding, False if exceeded
        """
        if n <= 0:
            return True
        
        with self._lock:
            self.total_bytes += n
            if provider:
                self._inc(self.per_provider, provider, "bytes", n)
            if work_id:
                self._inc(self.per_work, work_id, "bytes", n)
            
            # Re-check limits after adding
            ok = self.allow_bytes(provider, work_id, 0)
            if not ok and self._policy() == "stop":
                self._exhausted = True
            return ok

    def add_file(self, provider: Optional[str], work_id: Optional[str]) -> bool:
        """Add a file to counters; return True if still within limits.
        
        Args:
            provider: Provider key
            work_id: Work ID
            
        Returns:
            True if within limits after adding, False if exceeded
        """
        with self._lock:
            self.total_files += 1
            if provider:
                self._inc(self.per_provider, provider, "files", 1)
            if work_id:
                self._inc(self.per_work, work_id, "files", 1)
            
            # Re-check limits after adding
            ok = self.allow_new_file(provider, work_id)
            if not ok and self._policy() == "stop":
                self._exhausted = True
            return ok


# Global singleton budget tracker
_BUDGET = DownloadBudget()


def get_budget() -> DownloadBudget:
    """Get the global download budget tracker."""
    return _BUDGET


def budget_exhausted() -> bool:
    """Check if the global download budget has been exhausted."""
    return _BUDGET.exhausted()
