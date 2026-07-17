"""Download budget tracking and enforcement for ChronoDownloader.

Manages global and per-work download limits by content type (images, PDFs, metadata)
to prevent runaway jobs and respect configured constraints.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, NamedTuple

from .config import get_download_limits

logger = logging.getLogger(__name__)


class _Limits(NamedTuple):
    """Resolved byte limits and policy for one (content_type, work_id).

    Snapshotting these once per streaming session lets the per-chunk budget
    check avoid rebuilding the config dict and re-converting GB/MB thresholds
    on every chunk. Only the limit *values* are captured; the live byte
    counters are still read and compared under the lock on each chunk, so
    mid-file cutoff behavior is unchanged.
    """

    max_total: int | None
    max_work: int | None
    policy: str


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

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Global counters by content type (in bytes)
        self.total_images_bytes = 0
        self.total_pdfs_bytes = 0
        self.total_metadata_bytes = 0
        # Per-work counters:
        # {work_id: {"images": bytes, "pdfs": bytes, "metadata": bytes}}
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

    def resolve_limits(self, content_type: str, work_id: str | None) -> _Limits:
        """Resolve the applicable byte limits + policy for one download.

        Reads the download-limits config exactly once and converts the GB/MB
        thresholds to bytes, so a streaming loop can pass the result to
        ``add_bytes`` per chunk instead of rebuilding and re-parsing the config
        on every chunk. The returned limits capture the same values that a live
        per-chunk resolution would see for this ``content_type``/``work_id``.
        """
        dl = get_download_limits()
        total_limits = dl.get("total", {})
        max_total = self._gb_to_bytes(total_limits.get(f"{content_type}_gb"))

        max_work: int | None = None
        if work_id:
            per_work_limits = dl.get("per_work", {})
            if content_type == "metadata":
                max_work = self._mb_to_bytes(per_work_limits.get("metadata_mb"))
            else:
                max_work = self._gb_to_bytes(per_work_limits.get(f"{content_type}_gb"))

        pol = str(dl.get("on_exceed", "skip") or "skip").lower()
        policy = "stop" if pol == "stop" else "skip"
        return _Limits(max_total, max_work, policy)

    def exhausted(self) -> bool:
        """Check if the download budget has been exhausted."""
        with self._lock:
            return self._exhausted

    def _inc(
        self, bucket: dict[str, dict[str, int]], key: str, field: str, delta: int
    ) -> int:
        """Increment a counter in a nested bucket."""
        m = bucket.setdefault(key, {"images": 0, "pdfs": 0, "metadata": 0})
        m[field] = int(m.get(field, 0)) + int(delta)
        return m[field]

    def _get(self, bucket: dict[str, dict[str, int]], key: str, field: str) -> int:
        """Get a counter value from a nested bucket."""
        return int(bucket.get(key, {}).get(field, 0))

    def allow_content(
        self, content_type: str, work_id: str | None, add_bytes: int | None
    ) -> bool:
        """Check if additional content can be downloaded within budget limits.

        The check runs entirely under the internal lock so concurrent workers
        cannot both pass a limit check and jointly overshoot it (TOCTOU).

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
            logger.warning(
                "Unknown content type: %s, defaulting to allow", content_type
            )
            return True

        with self._lock:
            return self._allow_content_locked(content_type, work_id, add_bytes)

    def _allow_content_locked(
        self,
        content_type: str,
        work_id: str | None,
        add_bytes: int,
        limits: _Limits | None = None,
    ) -> bool:
        """Limit check body; caller must hold ``self._lock``.

        ``limits`` may be a pre-resolved snapshot (once per streaming session)
        to avoid re-reading and re-parsing the config per chunk. When omitted
        it is resolved live, preserving the original per-call behavior.
        """
        if limits is None:
            limits = self.resolve_limits(content_type, work_id)

        # Global limit for this content type
        current_total = getattr(self, f"total_{content_type}_bytes", 0)
        max_total = limits.max_total
        if max_total is not None and (current_total + add_bytes) > max_total:
            logger.info(
                "Global %s limit would be exceeded: %.2f GB > %.2f GB",
                content_type,
                (current_total + add_bytes) / (1024**3),
                max_total / (1024**3),
            )
            if limits.policy == "stop":
                self._exhausted = True
            return False

        # Per-work limit for this content type
        if work_id and limits.max_work is not None:
            current_work = self._get(self.per_work, work_id, content_type)
            if (current_work + add_bytes) > limits.max_work:
                logger.info(
                    "Per-work %s limit would be exceeded for %s", content_type, work_id
                )
                if limits.policy == "stop":
                    self._exhausted = True
                return False

        return True

    def record_download(
        self, content_type: str, work_id: str | None, size_bytes: int
    ) -> None:
        """Record a completed download.

        Args:
            content_type: Type of content ("images", "pdfs", or "metadata")
            work_id: Work ID for per-work tracking
            size_bytes: Size of downloaded content in bytes
        """
        if content_type not in ["images", "pdfs", "metadata"]:
            logger.warning("Unknown content type for recording: %s", content_type)
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
            logger.info("Images: %.2f GB", self.total_images_bytes / (1024**3))
            logger.info("PDFs: %.2f GB", self.total_pdfs_bytes / (1024**3))
            logger.info("Metadata: %.2f MB", self.total_metadata_bytes / (1024**2))
            logger.info("Total works tracked: %d", len(self.per_work))

            if len(self.per_work) <= 10:
                for wid, stats in self.per_work.items():
                    logger.info(
                        "  Work %s: images=%.1fMB, pdfs=%.1fMB, metadata=%.1fKB",
                        wid,
                        stats.get("images", 0) / (1024**2),
                        stats.get("pdfs", 0) / (1024**2),
                        stats.get("metadata", 0) / 1024,
                    )

    # Legacy compatibility methods
    def allow_new_file(self, provider: str | None, work_id: str | None) -> bool:
        """Return True while the budget is not exhausted (legacy compatibility).

        Returns False once a "stop" on_exceed policy has tripped the exhausted
        flag; otherwise True.
        """
        return not self._exhausted

    def allow_bytes(
        self,
        provider: str | None,
        work_id: str | None,
        add_bytes: int | None,
        content_type: str = "images",
    ) -> bool:
        """Check byte allowance for a pending download.

        Args:
            provider: Provider key (unused; kept for signature compatibility)
            work_id: Work ID for per-work limits
            add_bytes: Number of bytes about to be downloaded
            content_type: Budget bucket ("images", "pdfs", or "metadata"),
                classified by the caller from the file extension.
        """
        if not add_bytes or add_bytes <= 0:
            return True
        return self.allow_content(content_type, work_id, add_bytes)

    def add_bytes(
        self,
        provider: str | None,
        work_id: str | None,
        add_bytes: int,
        content_type: str = "images",
        limits: _Limits | None = None,
    ) -> bool:
        """Atomically check-and-record bytes during a streaming download.

        The limit check and the counter update run under one lock acquisition
        so concurrent workers cannot jointly overshoot a limit.

        Args:
            provider: Provider key (unused; kept for signature compatibility)
            work_id: Work ID for per-work tracking
            add_bytes: Number of bytes to add
            content_type: Budget bucket ("images", "pdfs", or "metadata"),
                classified by the caller from the file extension.
            limits: Optional pre-resolved limit snapshot (see ``resolve_limits``)
                to skip re-reading the config on every chunk. When omitted, the
                limits are resolved live for each call.

        Returns:
            True if bytes were accepted, False if limit exceeded
        """
        if content_type not in ("images", "pdfs", "metadata"):
            content_type = "images"

        with self._lock:
            if not self._allow_content_locked(content_type, work_id, add_bytes, limits):
                return False

            attr_name = f"total_{content_type}_bytes"
            setattr(self, attr_name, getattr(self, attr_name, 0) + add_bytes)
            if work_id:
                self._inc(self.per_work, work_id, content_type, add_bytes)

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


# Global singleton budget tracker
_BUDGET = DownloadBudget()


def get_budget() -> DownloadBudget:
    """Get the global download budget tracker."""
    return _BUDGET


def budget_exhausted() -> bool:
    """Check if the global download budget has been exhausted."""
    return _BUDGET.exhausted()
