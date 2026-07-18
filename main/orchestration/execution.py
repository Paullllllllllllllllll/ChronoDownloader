"""Shared execution logic for batch downloads.

This module provides a unified interface for running batch downloads in either
parallel or sequential mode. It is used by both CLI and interactive modes to
ensure consistent behavior.

The execution flow:
1. Sequential mode: Process works one at a time via pipeline.process_work()
2. Parallel mode: Search sequentially, queue downloads to DownloadScheduler

Expected CSV columns (from bib_sampling.ipynb):
- short_title: Work title for search
- main_author: Creator/author for search
- entry_id: Unique identifier
- retrievable: Download status (True/False/empty)
- link: Item URL (populated after download)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, cast

import pandas as pd

from api.core.budget import budget_exhausted
from api.iiif import (
    download_from_iiif_manifest,
    is_direct_download_enabled,
    is_iiif_manifest_url,
    preview_manifest,
)
from main.data.works_csv import (
    CREATOR_COL,
    DIRECT_LINK_COL,
    ENTRY_ID_COL,
    LINK_COL,
    TITLE_COL,
    backup_works_csv,
    mark_deferred,
    mark_failed,
    mark_success,
)
from main.state.background import get_background_scheduler
from main.state.deferred import get_deferred_queue

from . import pipeline
from .scheduler import DownloadScheduler, DownloadTask, get_parallel_download_config


def _run_eager_deferred_retry(
    config: dict[str, Any],
    logger: logging.Logger,
    csv_path: str | None,
) -> set[str]:
    """Synchronously retry any ready deferred items before starting new work.

    Replaces the former background daemon thread (which exited with the
    process before it could do anything). Ready items are retried in-line and,
    on success, written through the works CSV, work.json, and index.csv.

    Returns:
        The set of entry_ids (as ``str``) that the retry completed, so the
        caller can drop those rows from the pending works before the batch
        loop re-processes (and potentially re-defers, clobbering the freshly
        written "completed" status) them.
    """
    deferred_cfg = config.get("deferred", {})
    if not deferred_cfg.get("background_enabled", True):
        logger.debug("Eager deferred retry disabled in config")
        return set()

    try:
        scheduler = get_background_scheduler()
        stats, completed_entry_ids = scheduler.retry_ready_now(csv_path=csv_path)
        if stats.get("attempted"):
            logger.info(
                "Eager deferred retry: %d attempted, %d succeeded, %d failed",
                stats.get("attempted", 0),
                stats.get("succeeded", 0),
                stats.get("failed", 0),
            )
        return completed_entry_ids
    except Exception:
        logger.exception("Eager deferred retry failed")
        return set()


def run_batch_downloads(
    works_df: pd.DataFrame,
    output_dir: str,
    config: dict[str, Any],
    dry_run: bool = False,
    use_parallel: bool = True,
    max_workers_override: int | None = None,
    logger: logging.Logger | None = None,
    on_submit: Callable[[DownloadTask], None] | None = None,
    on_complete: Callable[[DownloadTask, bool, Exception | None], None] | None = None,
    csv_path: str | None = None,
    enable_background_retry: bool = True,
) -> dict[str, int]:
    """Run batch downloads with automatic mode selection.

    Args:
        works_df: DataFrame with columns from sampling notebook:
                  short_title, main_author, entry_id, retrievable, link
        output_dir: Base directory for downloaded works
        config: Configuration dictionary (from get_config())
        dry_run: If True, skip actual downloads
        use_parallel: Whether to attempt parallel downloads
        max_workers_override: Override for max_parallel_downloads config
        logger: Logger instance (creates default if None)
        on_submit: Optional callback when a task is submitted (parallel mode)
        on_complete: Optional callback when a task completes (parallel mode)
        csv_path: Path to the source CSV for status updates (unified CSV mode)

    Returns:
        Dictionary with execution statistics:
        - 'processed': Total works processed
        - 'succeeded': Successful downloads (parallel mode only)
        - 'failed': Failed downloads (parallel mode only)
        - 'skipped': Works skipped due to missing title
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # Back up the works CSV once per run before any status writes.
    if csv_path and not dry_run:
        backup_works_csv(csv_path)

    # Eagerly retry ready deferred items at run start (synchronous, replaces
    # the former no-op background daemon thread). Any entry the retry just
    # completed must be dropped from the pending works: otherwise the batch
    # loop re-searches/re-downloads it and, under resume_from_csv (which never
    # skips on work.json), a quota-gated provider re-defers it, clobbering the
    # freshly written "completed" cell and duplicating the deferred entry.
    if enable_background_retry and not dry_run:
        completed_entry_ids = _run_eager_deferred_retry(config, logger, csv_path)
        if completed_entry_ids and ENTRY_ID_COL in works_df.columns:
            already_done = works_df[ENTRY_ID_COL].astype(str).isin(completed_entry_ids)
            drop_count = int(already_done.sum())
            if drop_count:
                works_df = works_df[~already_done]
                logger.info(
                    "Skipping %d row(s) already completed by eager deferred retry",
                    drop_count,
                )

    # Determine effective parallel settings
    dl_config = config.get("download", {})
    max_parallel = max_workers_override or int(
        dl_config.get("max_parallel_downloads", 1) or 1
    )

    # Use parallel mode only when configured and not dry-run
    if use_parallel and max_parallel > 1 and not dry_run:
        stats = _run_parallel(
            works_df,
            output_dir,
            config,
            max_workers_override,
            logger,
            on_submit,
            on_complete,
            csv_path,
        )
    else:
        stats = _run_sequential(works_df, output_dir, dry_run, logger, csv_path)

    # Add deferred count to stats
    queue = get_deferred_queue()
    deferred_count = len(queue.get_pending())
    stats["deferred"] = deferred_count

    if deferred_count > 0:
        logger.info(
            "%d download(s) deferred due to quota limits. "
            "Ready items are retried at the start of the next run.",
            deferred_count,
        )

    return stats


def process_direct_iiif(
    manifest_url: str,
    output_dir: str,
    entry_id: str | None = None,
    title: str | None = None,
    creator: str | None = None,
    file_stem: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Process a single direct IIIF manifest download.

    This is the single entry point for all direct-IIIF downloads, used by
    CLI (``--iiif``), interactive mode, and CSV rows with ``direct_link``.

    Args:
        manifest_url: URL of the IIIF manifest
        output_dir: Base directory for downloaded works
        entry_id: Optional entry identifier (used for work directory naming)
        title: Optional work title (used for logging and directory naming)
        creator: Optional creator/author name
        file_stem: Optional naming stem for output files
        dry_run: If True, fetch manifest info without downloading

    Returns:
        Result dictionary with keys: status, item_url, provider, and
        optionally error, preview (when dry_run).
    """
    _log = logging.getLogger(__name__)

    if dry_run:
        _log.info("Dry-run: previewing IIIF manifest: %s", manifest_url)
        info = preview_manifest(manifest_url)
        if info is None:
            return {
                "status": "failed",
                "item_url": manifest_url,
                "provider": "Direct IIIF",
                "error": "Failed to fetch manifest",
            }
        _log.info(
            "Manifest preview: %s | %d pages | renderings: %s | label: %s",
            info.get("provider", "unknown"),
            info.get("page_count", 0),
            info.get("rendering_formats") or "none",
            info.get("label") or "(none)",
        )
        return {
            "status": "dry_run",
            "item_url": manifest_url,
            "provider": info.get("provider", "Direct IIIF"),
            "preview": info,
        }

    # Compute work directory
    from main.data.work import compute_work_dir

    dir_title = title or "iiif_download"
    work_dir, _work_dir_name = compute_work_dir(
        output_dir, str(entry_id) if entry_id else None, dir_title
    )

    dl_result = download_from_iiif_manifest(
        manifest_url=manifest_url,
        output_folder=work_dir,
        title=title,
        entry_id=str(entry_id) if entry_id else None,
        file_stem=file_stem,
    )

    pages_expected = dl_result.get("pages_expected")
    pages_downloaded = dl_result.get("pages_downloaded")

    if dl_result["success"]:
        # A work is only "completed" when every expected page arrived; a
        # gap yields "partial" so it is not counted as retrievable and can
        # be re-downloaded (completeness contract).
        status = dl_result.get("status") or "completed"
        _record_direct_iiif_completeness(work_dir, status, dl_result)
        return {
            "status": status,
            "item_url": manifest_url,
            "provider": dl_result["provider"],
            "pages_expected": pages_expected,
            "pages_downloaded": pages_downloaded,
        }
    return {
        "status": "failed",
        "item_url": manifest_url,
        "provider": dl_result["provider"],
        "error": dl_result.get("error", "unknown"),
        "pages_expected": pages_expected,
        "pages_downloaded": pages_downloaded,
    }


def _record_direct_iiif_completeness(
    work_dir: str,
    status: str,
    dl_result: dict[str, Any],
) -> None:
    """Persist a direct-IIIF completeness marker into the work directory.

    Best-effort: only writes when the work directory already exists (i.e. a
    real download happened), so heavily-mocked unit tests are unaffected.
    Records the status and page counts into ``work.json`` so ``--verify`` and
    resume logic can distinguish complete from partial works.
    """
    if not work_dir or not os.path.isdir(work_dir):
        return
    import json

    from api.core.atomic import atomic_write_json

    work_json_path = os.path.join(work_dir, "work.json")
    try:
        meta: dict[str, Any] = {}
        if os.path.exists(work_json_path):
            with open(work_json_path, encoding="utf-8") as f:
                meta = json.load(f)
        meta["status"] = status
        meta["pages_expected"] = dl_result.get("pages_expected")
        meta["pages_downloaded"] = dl_result.get("pages_downloaded")
        meta.setdefault("source", "direct_iiif")
        meta["item_url"] = dl_result.get("item_url")
        atomic_write_json(work_json_path, meta)
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to record completeness for %s", work_dir, exc_info=True
        )


def _get_direct_link(row: pd.Series) -> str | None:
    """Extract a direct IIIF link from a CSV row if present and valid.

    Checks the configured ``direct_iiif.link_column`` first (falling back to the
    default ``direct_link`` column), then, when ``direct_iiif.check_link_column``
    is enabled, the generic ``link`` column for IIIF manifest URLs.

    Args:
        row: DataFrame row

    Returns:
        IIIF manifest URL if found and valid, None otherwise
    """
    from api.iiif import get_check_link_column, get_direct_link_column

    def _valid_url(col: str) -> str | None:
        if col not in row.index:
            return None
        value: object = row[col]
        if isinstance(value, str) and value.strip():
            url = value.strip()
            if is_iiif_manifest_url(url):
                return url
        return None

    # Configured direct-link column first, falling back to the default column.
    for col in dict.fromkeys([get_direct_link_column(), DIRECT_LINK_COL]):
        found = _valid_url(col)
        if found:
            return found

    # Optionally also check the generic 'link' column (backward compatibility).
    if get_check_link_column():
        return _valid_url(LINK_COL)

    return None


def _parse_work_row(
    row: pd.Series,
    index: Any,
    logger: logging.Logger,
) -> tuple[Any, Any, Any, str | None, bool] | None:
    """Extract and validate the searchable fields from a works_df row.

    Returns ``(title, creator, entry_id, direct_link, title_missing)`` when the
    row is processable, or ``None`` (after logging a skip warning) when the row
    must be skipped due to a missing entry_id or a missing title without a
    usable direct IIIF link. Callers increment their own skip counter on None.

    Args:
        row: DataFrame row
        index: Row index (for one-based logging)
        logger: Logger instance

    Returns:
        Parsed field tuple or None when the row must be skipped
    """
    title = row.get(TITLE_COL)
    creator = row.get(CREATOR_COL)
    entry_id = row.get(ENTRY_ID_COL)

    # entry_id is required from the sampling CSV
    if pd.isna(entry_id) or (isinstance(entry_id, str) and not entry_id.strip()):
        logger.warning("Skipping row %d due to missing entry_id.", cast(int, index) + 1)
        return None

    # Check for direct IIIF link first (bypasses search)
    direct_link = _get_direct_link(row) if is_direct_download_enabled() else None

    title_missing = pd.isna(title) or not str(title).strip()
    if title_missing and not direct_link:
        logger.warning(
            "Skipping row %d due to missing or empty title.", cast(int, index) + 1
        )
        return None

    return title, creator, entry_id, direct_link, title_missing


def _mark_no_match_failed(
    csv_path: str,
    output_dir: str,
    entry_id: str,
    title: str,
    logger: logging.Logger,
    creator: str | None = None,
) -> None:
    """Mark a parallel-mode no-match work failed, unless it was resume-skipped.

    ``search_and_select`` returns None both for resume-skips (work already
    complete) and for genuine no-matches. Only the latter should be marked
    failed in the works CSV; a resume-skip must keep its completed status.

    ``creator`` must match the value ``_prepare_work`` used so the computed work
    directory (which may fold in the creator per naming config) is identical.
    """
    from main.data.work import check_work_status, compute_work_dir

    try:
        work_dir, _ = compute_work_dir(output_dir, entry_id, title, creator=creator)
        should_skip, _reason = check_work_status(work_dir)
        if should_skip:
            return
        mark_failed(csv_path, entry_id)
        logger.debug("Marked entry %s as failed (no match) in source CSV", entry_id)
    except Exception:
        logger.exception("Failed to mark no-match entry %s in source CSV", entry_id)


def _run_sequential(
    works_df: pd.DataFrame,
    output_dir: str,
    dry_run: bool,
    logger: logging.Logger,
    csv_path: str | None = None,
) -> dict[str, int]:
    """Run downloads sequentially.

    Args:
        works_df: DataFrame with works (using notebook column names)
        output_dir: Output directory
        dry_run: Whether to skip downloads
        logger: Logger instance
        csv_path: Path to source CSV for status updates

    Returns:
        Statistics dictionary
    """
    processed = 0
    succeeded = 0
    failed = 0
    skipped = 0
    direct_iiif_count = 0

    for index, row in works_df.iterrows():
        parsed = _parse_work_row(row, index, logger)
        if parsed is None:
            skipped += 1
            continue
        title, creator, entry_id, direct_link, title_missing = parsed

        if direct_link:
            logger.info(
                "Direct IIIF link detected for '%s': %s",
                title if not title_missing else entry_id,
                direct_link,
            )
            direct_iiif_count += 1
            result = process_direct_iiif(
                manifest_url=direct_link,
                output_dir=output_dir,
                entry_id=str(entry_id),
                title=None if title_missing else str(title),
                creator=None if pd.isna(creator) else str(creator),
                dry_run=dry_run,
            )
        else:
            # Standard search-based download
            result = (
                pipeline.process_work(
                    str(title),
                    None if pd.isna(creator) else str(creator),
                    str(entry_id),
                    output_dir,
                    dry_run=dry_run,
                )
                or {}
            )

        # Update CSV status if path provided
        if csv_path and not dry_run and result and isinstance(result, dict):
            status = result.get("status", "")
            if status == "completed":
                # Success - result contains item_url and provider
                item_url = result.get("item_url", "")
                provider = result.get("provider", "")
                if mark_success(csv_path, str(entry_id), item_url, provider):
                    logger.debug("Marked entry %s as success in source CSV", entry_id)
                else:
                    logger.warning(
                        "Failed to mark entry %s as success in source CSV", entry_id
                    )
                succeeded += 1
            elif status == "failed":
                # Explicit failure
                if mark_failed(csv_path, str(entry_id)):
                    logger.debug("Marked entry %s as failed in source CSV", entry_id)
                else:
                    logger.warning(
                        "Failed to mark entry %s as failed in source CSV", entry_id
                    )
                failed += 1
            elif status == "deferred":
                # Quota deferral: mark retriable (mirrors parallel mode) rather
                # than leaving the row silently pending.
                if mark_deferred(csv_path, str(entry_id)):
                    logger.debug("Marked entry %s as deferred in source CSV", entry_id)
            # Other statuses (dry_run, no_match) - don't update CSV
        # result is None means skipped (resume) - don't update CSV

        processed += 1

        # Stop early if the global download budget has been exhausted
        try:
            if budget_exhausted():
                logger.warning(
                    "Download budget exhausted; stopping further processing."
                )
                break
        except Exception:
            pass

    logger.info("All works processed.")
    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }


def _run_parallel(
    works_df: pd.DataFrame,
    output_dir: str,
    config: dict[str, Any],
    max_workers_override: int | None,
    logger: logging.Logger,
    on_submit: Callable[[DownloadTask], None] | None = None,
    on_complete: Callable[[DownloadTask, bool, Exception | None], None] | None = None,
    csv_path: str | None = None,
) -> dict[str, int]:
    """Run downloads in parallel using DownloadScheduler.

    Searches are sequential to avoid overwhelming providers,
    but downloads are parallelized across works.

    Args:
        works_df: DataFrame with works (using notebook column names)
        output_dir: Output directory
        config: Configuration dictionary
        max_workers_override: Override for max workers
        logger: Logger instance
        on_submit: Optional callback for task submission
        on_complete: Optional callback for task completion
        csv_path: Path to source CSV for status updates

    Returns:
        Statistics dictionary
    """
    # Get parallel download settings
    dl_config = get_parallel_download_config()
    max_workers = max_workers_override or int(
        dl_config.get("max_parallel_downloads", 4) or 4
    )
    provider_concurrency = dict(dl_config.get("provider_concurrency", {}) or {})
    # worker_timeout_s semantics: 0 (or omitted) = wait indefinitely for the
    # whole batch; a positive value is a TOTAL ceiling for the batch wait
    # (not per task).
    raw_timeout = float(dl_config.get("worker_timeout_s", 0) or 0)
    worker_timeout: float | None = raw_timeout if raw_timeout > 0 else None

    # Track statistics
    submitted_count = 0
    skipped_count = 0
    # Direct-IIIF rows are handled synchronously (not via the scheduler), so
    # their outcomes never reach scheduler.get_stats(); count them locally.
    direct_iiif_succeeded = 0
    direct_iiif_failed = 0

    # Default callbacks if none provided
    def default_on_submit(task: DownloadTask) -> None:
        nonlocal submitted_count
        submitted_count += 1
        logger.debug("Queued download %d: '%s'", submitted_count, task.title)

    # Note: CSV sync is handled in wrapped_complete, not in callbacks
    # This default is only used for logging when no custom callback provided

    # Use provided callbacks or defaults
    submit_callback = on_submit or default_on_submit
    complete_callback = on_complete if on_complete else None

    # Wrap callbacks to track stats while calling user callbacks
    actual_submitted = [0]

    def wrapped_submit(task: DownloadTask) -> None:
        actual_submitted[0] += 1
        submit_callback(task)

    def wrapped_complete(
        task: DownloadTask, success: bool, error: Exception | None
    ) -> None:
        # Always perform CSV sync (critical for resume functionality). The
        # task's status (set in pipeline.execute_download) distinguishes a
        # quota deferral from an outright failure: a deferred task must be
        # marked "deferred" (retriable) rather than "failed" so --pending-mode
        # new does not permanently drop it.
        task_status = getattr(task, "status", None)
        if csv_path and task.entry_id:
            try:
                if success:
                    item_url = getattr(task, "item_url", "") or ""
                    provider = getattr(task, "provider", "") or ""
                    mark_success(csv_path, task.entry_id, item_url, provider)
                elif task_status == "deferred":
                    mark_deferred(csv_path, task.entry_id)
                    logger.debug(
                        "Marked entry %s as deferred in source CSV", task.entry_id
                    )
                else:
                    mark_failed(csv_path, task.entry_id)
            except Exception as csv_err:
                logger.error(
                    "Exception updating source CSV for entry %s: %s",
                    task.entry_id,
                    csv_err,
                )

        # Call custom callback if provided, otherwise use default logging
        if complete_callback:
            complete_callback(task, success, error)
        else:
            if success:
                status = "completed"
            elif task_status == "deferred":
                status = "deferred"
            else:
                status = "failed"
            if error:
                logger.warning("Download %s for '%s': %s", status, task.title, error)
            else:
                logger.info("Download %s for '%s'", status, task.title)

    # Initialize scheduler
    scheduler = DownloadScheduler(
        max_workers=max_workers,
        provider_limits=provider_concurrency,
        on_submit=wrapped_submit,
        on_complete=wrapped_complete,
    )
    scheduler.start()

    logger.info(
        "Parallel download scheduler started: %d workers, provider limits: %s",
        max_workers,
        provider_concurrency or "default",
    )

    try:
        # Phase 1: Search and queue downloads
        for index, row in works_df.iterrows():
            # Check budget before searching
            try:
                if budget_exhausted():
                    logger.warning(
                        "Download budget exhausted; stopping further searches."
                    )
                    break
            except Exception:
                pass

            parsed = _parse_work_row(row, index, logger)
            if parsed is None:
                skipped_count += 1
                continue
            title, creator, entry_id, direct_link, title_missing = parsed

            if direct_link:
                # Direct IIIF download - handle synchronously in parallel mode
                logger.info(
                    "Direct IIIF link detected for '%s': %s",
                    title if not title_missing else entry_id,
                    direct_link,
                )
                dl_result = process_direct_iiif(
                    manifest_url=direct_link,
                    output_dir=output_dir,
                    entry_id=str(entry_id),
                    title=None if title_missing else str(title),
                    creator=None if pd.isna(creator) else str(creator),
                )

                # Fold this synchronous outcome into the batch stats. Mirror
                # the CSV policy: only "completed"/"failed" count; a "partial"
                # (incomplete page set) is left uncounted for retry.
                direct_status = dl_result.get("status")
                if direct_status == "completed":
                    direct_iiif_succeeded += 1
                elif direct_status == "failed":
                    direct_iiif_failed += 1

                # Update CSV for direct downloads. Mirror sequential mode: only
                # an explicit "completed"/"failed" writes the CSV; a "partial"
                # (incomplete page set) is left pending so it can be retried.
                if csv_path and entry_id:
                    try:
                        status = dl_result.get("status")
                        if status == "completed":
                            if mark_success(
                                csv_path,
                                str(entry_id),
                                direct_link,
                                dl_result.get("provider", ""),
                            ):
                                logger.debug(
                                    "Marked entry %s as success (direct IIIF)", entry_id
                                )
                        elif status == "failed" and mark_failed(
                            csv_path, str(entry_id)
                        ):
                            logger.debug(
                                "Marked entry %s as failed (direct IIIF)", entry_id
                            )
                    except Exception as csv_err:
                        logger.error(
                            "Exception updating CSV for direct IIIF entry %s: %s",
                            entry_id,
                            csv_err,
                        )

                actual_submitted[0] += 1
            else:
                # Search and select (runs in main thread)
                task = pipeline.search_and_select(
                    str(title),
                    None if pd.isna(creator) else str(creator),
                    None if pd.isna(entry_id) else str(entry_id),
                    output_dir,
                )

                if task:
                    # Submit download to worker pool
                    scheduler.submit(task, pipeline.execute_download)
                elif csv_path and entry_id:
                    # No task means either a resume-skip or a genuine no-match.
                    # Mirror sequential mode: mark genuine no-matches failed in
                    # the CSV, but never overwrite a resume-skipped (already
                    # completed) work.
                    _mark_no_match_failed(
                        csv_path,
                        output_dir,
                        str(entry_id),
                        str(title),
                        logger,
                        creator=None if pd.isna(creator) else str(creator),
                    )

        # Phase 2: Wait for all downloads to complete
        pending = scheduler.pending_count
        if pending > 0:
            logger.info(
                "Search phase complete. Waiting for %d pending download(s)...", pending
            )

            # wait_all catches its own TimeoutError internally and always
            # returns normally (with whatever completed within the deadline).
            scheduler.wait_all(timeout=worker_timeout)

            stats = scheduler.get_stats()
            logger.info(
                "Download phase complete: %d succeeded, %d failed",
                stats["succeeded"],
                stats["failed"],
            )

    except KeyboardInterrupt:
        logger.warning("Interrupt received; shutting down scheduler...")
        scheduler.request_shutdown()

        # Wait briefly for in-progress downloads
        pending = scheduler.pending_count
        if pending > 0:
            logger.info("Waiting for %d in-progress download(s) to finish...", pending)
            try:
                scheduler.wait_all(timeout=30)
            except TimeoutError:
                logger.warning(
                    "Timeout waiting for in-progress downloads after interrupt"
                )

    finally:
        # Bound the shutdown wait by the same worker timeout used for wait_all
        # so a stuck in-flight batch cannot block the process indefinitely;
        # None preserves the wait-forever default.
        scheduler.shutdown(wait=True, timeout=worker_timeout)
        stats = scheduler.get_stats()
        logger.info(
            "Scheduler shutdown. Final stats: %d completed (%d succeeded, %d failed)",
            stats["completed"],
            stats["succeeded"],
            stats["failed"],
        )

    final_stats = scheduler.get_stats()
    return {
        "processed": actual_submitted[0],
        "succeeded": final_stats["succeeded"] + direct_iiif_succeeded,
        "failed": final_stats["failed"] + direct_iiif_failed,
        "skipped": skipped_count,
    }


def create_interactive_callbacks(
    logger: logging.Logger,
) -> tuple[
    Callable[[DownloadTask], None],
    Callable[[DownloadTask, bool, Exception | None], None],
]:
    """Create progress callbacks suitable for interactive mode.

    Args:
        logger: Logger instance

    Returns:
        Tuple of (on_submit, on_complete) callback functions
    """
    from main.ui.console import ConsoleUI

    submitted = [0]
    completed = [0]

    def on_submit(task: DownloadTask) -> None:
        submitted[0] += 1
        title_short = task.title[:50] + "..." if len(task.title) > 50 else task.title
        ConsoleUI.print_info(f"Queued [{submitted[0]}]", title_short)

    def on_complete(task: DownloadTask, success: bool, error: Exception | None) -> None:
        completed[0] += 1
        title_short = task.title[:50] + "..." if len(task.title) > 50 else task.title
        if success:
            ConsoleUI.print_success(f"[{completed[0]}/{submitted[0]}] {title_short}")
        else:
            msg = f"[{completed[0]}/{submitted[0]}] {title_short}"
            if error:
                msg += f" - {error}"
            ConsoleUI.print_error(msg)

    return on_submit, on_complete


__all__ = ["run_batch_downloads", "create_interactive_callbacks", "process_direct_iiif"]
