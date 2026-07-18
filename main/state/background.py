"""Eager retry helper for deferred (quota-limited) downloads.

Instead of a background daemon thread (which exited with the process before it
could do useful work), deferred items are retried synchronously at the start of
each run via :meth:`BackgroundRetryScheduler.retry_ready_now`. On success the
works CSV, work.json, and index.csv are all updated so a subsequent run does not
re-download (and re-spend quota on) the same item.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from api.core.context import (
    clear_all_context,
    reset_counters,
    set_current_entry,
    set_current_name_stem,
    set_current_provider,
    set_current_work,
)
from api.model import QuotaDeferredException, SearchResult

from .deferred import DeferredItem, DeferredQueue, get_deferred_queue
from .quota import QuotaManager, get_quota_manager

logger = logging.getLogger(__name__)


def _consumed_quota_unit(provider_key: str) -> bool:
    """Return True when a just-succeeded retry actually used a quota-gated unit.

    Mirrors ``pipeline._quota_record``: only Anna's Archive's fast-download API
    is quota-gated, and only when its thread-local marker confirms the fast
    path (not the public-scraping fallback) was used.
    """
    if provider_key == "annas_archive":
        from api.providers.annas_archive import consume_fast_api_used

        return consume_fast_api_used()
    return False


class BackgroundRetryScheduler:
    """Eager retry helper for deferred downloads.

    Retries deferred queue items whose quota reset time has passed, driven
    synchronously from ``retry_ready_now`` at run start (no daemon thread).
    """

    _instance: BackgroundRetryScheduler | None = None
    _lock = threading.Lock()

    def __new__(cls) -> BackgroundRetryScheduler:
        """Singleton pattern."""
        with cls._lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instance = instance
            return cls._instance

    def __init__(self) -> None:
        """Initialize the eager deferred-retry helper."""
        if getattr(self, "_initialized", False):
            return

        # Components (bound lazily on first retry_ready_now call)
        self._queue: DeferredQueue | None = None
        self._quota_manager: QuotaManager | None = None

        # Provider download functions (set during integration)
        self._provider_download_fns: dict[str, Callable[..., Any]] = {}

        # Statistics
        self._stats_lock = threading.Lock()
        self._stats = {
            "checks": 0,
            "retries_attempted": 0,
            "retries_succeeded": 0,
            "retries_failed": 0,
            "retries_redeferred": 0,
        }

        # Callbacks
        self._on_retry_success: Callable[[DeferredItem], None] | None = None
        self._on_retry_failure: Callable[[DeferredItem, str], None] | None = None

        self._initialized = True
        logger.debug("BackgroundRetryScheduler initialized (eager-retry mode)")

    def set_provider_download_fn(
        self, provider_key: str, download_fn: Callable[[SearchResult, str], bool]
    ) -> None:
        """Register a download function for a provider.

        Args:
            provider_key: Provider identifier
            download_fn: Function that takes (SearchResult, output_dir) and returns bool
        """
        self._provider_download_fns[provider_key] = download_fn

    def set_callbacks(
        self,
        on_success: Callable[[DeferredItem], None] | None = None,
        on_failure: Callable[[DeferredItem, str], None] | None = None,
    ) -> None:
        """Set optional callbacks for retry events.

        Args:
            on_success: Called when a retry succeeds
            on_failure: Called when a retry permanently fails
        """
        self._on_retry_success = on_success
        self._on_retry_failure = on_failure

    def get_stats(self) -> dict[str, int]:
        """Get scheduler statistics.

        Returns:
            Dictionary of statistics
        """
        with self._stats_lock:
            return dict(self._stats)

    def _register_all_providers(self) -> None:
        """Register download functions for every known provider."""
        from api.providers import PROVIDERS

        for provider_key, (_search_fn, download_fn, _name) in PROVIDERS.items():
            self._provider_download_fns[provider_key] = download_fn

    def retry_ready_now(
        self, csv_path: str | None = None
    ) -> tuple[dict[str, int], set[str]]:
        """Synchronously retry every currently-ready deferred item (eager retry).

        Called at run start instead of a background daemon thread. On success,
        the works CSV, work.json, and index.csv are all updated so a subsequent
        run does not re-download (and re-spend quota on) the same item.

        Args:
            csv_path: Optional works-CSV path to mark retried items successful.

        Returns:
            A tuple of ``(stats, completed_entry_ids)`` where ``stats`` holds
            ``attempted``, ``succeeded``, ``failed`` counts and
            ``completed_entry_ids`` is the set of entry_ids (as ``str``) whose
            retry just completed. The caller uses the latter to drop rows the
            batch loop would otherwise re-download (and possibly re-defer).
        """
        self._queue = get_deferred_queue()
        self._quota_manager = get_quota_manager()
        if not self._provider_download_fns:
            self._register_all_providers()

        ready = self._queue.get_ready()
        stats = {"attempted": 0, "succeeded": 0, "failed": 0}
        completed_entry_ids: set[str] = set()
        if not ready:
            return stats, completed_entry_ids

        logger.info("Eager retry: %d deferred item(s) ready", len(ready))
        for item in ready:
            stats["attempted"] += 1
            if self._retry_item(item):
                stats["succeeded"] += 1
                self._persist_retry_success(item, csv_path)
                if item.entry_id:
                    completed_entry_ids.add(str(item.entry_id))
            else:
                stats["failed"] += 1
        return stats, completed_entry_ids

    def _persist_retry_success(self, item: DeferredItem, csv_path: str | None) -> None:
        """Write a successful retry through work.json, index.csv, and works CSV."""
        from main.data.index import build_index_row, update_index_csv
        from main.data.work import compute_work_id, update_work_status
        from main.data.works_csv import mark_success

        work_json_path = (
            os.path.join(item.work_dir, "work.json") if item.work_dir else ""
        )
        if work_json_path:
            try:
                update_work_status(
                    work_json_path,
                    "completed",
                    {
                        "provider": item.provider_name,
                        "provider_key": item.provider_key,
                        "source_id": item.source_id,
                    },
                )
            except Exception:
                logger.exception("Failed to update work.json for %s", item.title)

        try:
            work_id = compute_work_id(item.title, item.creator)
            search_result = self._reconstruct_search_result(item)
            row = build_index_row(
                work_id=work_id,
                entry_id=item.entry_id,
                work_dir=item.work_dir,
                title=item.title,
                creator=item.creator,
                selected=search_result,
                selected_source_id=item.source_id,
                work_json_path=work_json_path,
                status="completed",
                item_url=item.item_url,
            )
            update_index_csv(item.base_output_dir, row)
        except Exception:
            logger.exception("Failed to update index.csv for %s", item.title)

        if csv_path and item.entry_id:
            try:
                mark_success(
                    csv_path, item.entry_id, item.item_url or "", item.provider_name
                )
            except Exception:
                logger.exception("Failed to update works CSV for %s", item.title)

    def _retry_item(self, item: DeferredItem) -> bool:
        """Attempt to retry a deferred item.

        Args:
            item: DeferredItem to retry

        Returns:
            True if successful, False otherwise
        """
        with self._stats_lock:
            self._stats["retries_attempted"] += 1

        provider_key = item.provider_key

        # Check if we have a download function for this provider
        download_fn = self._provider_download_fns.get(provider_key)
        if not download_fn:
            logger.warning(
                "No download function registered for provider %s, skipping %s",
                provider_key,
                item.title,
            )
            return False

        # Check quota before attempting
        if self._quota_manager:
            can_download, wait_seconds = self._quota_manager.can_download(provider_key)
            if not can_download:
                # Still quota limited - update reset time and skip.
                # Quota waits are typically hours; timedelta handles overflow
                # correctly, whereas datetime.replace(second=...) raises when
                # the sum exceeds 59.
                if wait_seconds:
                    new_reset = datetime.now(UTC) + timedelta(seconds=wait_seconds)
                    item.reset_time = new_reset.isoformat()
                    if self._queue:
                        self._queue._save_queue()
                logger.debug(
                    "Quota still exhausted for %s, skipping %s",
                    provider_key,
                    item.title,
                )
                return False

        logger.info(
            "Retrying deferred download: '%s' from %s", item.title, item.provider_name
        )

        # Reconstruct SearchResult from stored data
        search_result = self._reconstruct_search_result(item)
        if not search_result:
            if self._queue:
                self._queue.mark_failed(item.id, "Could not reconstruct search result")
            with self._stats_lock:
                self._stats["retries_failed"] += 1
            if self._on_retry_failure:
                self._on_retry_failure(item, "Could not reconstruct search result")
            return False

        # Reproduce the original run's naming context so a retried download
        # lands in the same work directory with the same filename stem, rather
        # than a stem derived from the entry_id/item id.
        from main.data.work import compute_work_id

        work_id = compute_work_id(item.title, item.creator)
        name_stem = (
            os.path.basename(item.work_dir.rstrip("/\\"))
            if item.work_dir
            else (item.entry_id or item.id)
        )

        try:
            # Set thread-local context
            set_current_work(work_id)
            set_current_entry(item.entry_id)
            set_current_provider(provider_key)
            set_current_name_stem(name_stem)
            reset_counters()

            try:
                success = download_fn(search_result, item.work_dir)
            finally:
                clear_all_context()

            if success:
                # Only record a consumed quota unit when the provider actually
                # used its quota-gated fast-download path (mirrors
                # pipeline._quota_record); a non-quota fallback (e.g. public
                # scraping) must not burn a quota unit.
                if self._quota_manager and _consumed_quota_unit(provider_key):
                    self._quota_manager.record_download(provider_key)

                if self._queue:
                    self._queue.mark_completed(item.id)
                with self._stats_lock:
                    self._stats["retries_succeeded"] += 1

                logger.info("Deferred download succeeded: '%s'", item.title)

                if self._on_retry_success:
                    self._on_retry_success(item)

                return True
            else:
                # Download failed but not due to quota
                can_retry = self._queue.mark_retrying(item.id) if self._queue else False
                if not can_retry:
                    with self._stats_lock:
                        self._stats["retries_failed"] += 1
                    if self._on_retry_failure:
                        self._on_retry_failure(item, "Max retries exceeded")
                return False

        except QuotaDeferredException as qde:
            # Quota hit again - update reset time
            with self._stats_lock:
                self._stats["retries_redeferred"] += 1

            if self._queue:
                self._queue.mark_retrying(item.id, qde.reset_time)
            logger.info(
                "Deferred download hit quota again: '%s' - %s", item.title, qde.message
            )
            return False

        except Exception as e:
            logger.exception("Error retrying deferred download '%s': %s", item.title, e)
            can_retry = self._queue.mark_retrying(item.id) if self._queue else False
            if not can_retry:
                with self._stats_lock:
                    self._stats["retries_failed"] += 1
                if self._on_retry_failure:
                    self._on_retry_failure(item, str(e))
            return False

    def _reconstruct_search_result(self, item: DeferredItem) -> SearchResult | None:
        """Reconstruct a SearchResult from stored DeferredItem data.

        Args:
            item: DeferredItem with stored raw_data

        Returns:
            SearchResult or None if reconstruction fails
        """
        try:
            raw = item.raw_data or {}

            return SearchResult(
                title=item.title,
                creators=[item.creator] if item.creator else [],
                source_id=item.source_id,
                provider=item.provider_name,
                provider_key=item.provider_key,
                item_url=item.item_url,
                raw=raw,
            )
        except Exception as e:
            logger.warning(
                "Failed to reconstruct SearchResult for %s: %s", item.title, e
            )
            return None


def get_background_scheduler() -> BackgroundRetryScheduler:
    """Get the singleton BackgroundRetryScheduler instance.

    Returns:
        BackgroundRetryScheduler instance
    """
    return BackgroundRetryScheduler()


__all__ = [
    "BackgroundRetryScheduler",
    "get_background_scheduler",
]
