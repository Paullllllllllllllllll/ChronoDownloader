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
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from api import utils
from main import pipeline
from main.download_scheduler import DownloadScheduler, DownloadTask, get_parallel_download_config
from main.unified_csv import (
    TITLE_COL,
    CREATOR_COL,
    ENTRY_ID_COL,
    get_pending_works,
    mark_success,
    mark_failed,
    mark_deferred,
)
from main.background_scheduler import (
    BackgroundRetryScheduler,
    get_background_scheduler,
    start_background_scheduler,
    stop_background_scheduler,
)
from main.deferred_queue import get_deferred_queue
from api.providers import PROVIDERS


def _setup_background_scheduler(
    config: Dict[str, Any],
    logger: logging.Logger,
) -> Optional[BackgroundRetryScheduler]:
    """Set up and start the background retry scheduler.
    
    Args:
        config: Configuration dictionary
        logger: Logger instance
        
    Returns:
        BackgroundRetryScheduler instance if started, None if disabled
    """
    deferred_cfg = config.get("deferred", {})
    if not deferred_cfg.get("background_enabled", True):
        logger.debug("Background retry scheduler disabled in config")
        return None
    
    scheduler = get_background_scheduler()
    
    # Register download functions for all providers
    for provider_key, (search_fn, download_fn, provider_name) in PROVIDERS.items():
        scheduler.set_provider_download_fn(provider_key, download_fn)
    
    # Set up callbacks for logging
    def on_success(item):
        logger.info("Background retry succeeded: '%s'", item.title)
    
    def on_failure(item, error):
        logger.warning("Background retry failed: '%s' - %s", item.title, error)
    
    scheduler.set_callbacks(on_success=on_success, on_failure=on_failure)
    
    # Start the scheduler
    if scheduler.start():
        logger.info("Background retry scheduler started")
        return scheduler
    
    return None


def run_batch_downloads(
    works_df: pd.DataFrame,
    output_dir: str,
    config: Dict[str, Any],
    dry_run: bool = False,
    use_parallel: bool = True,
    max_workers_override: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
    on_submit: Optional[Callable[[DownloadTask], None]] = None,
    on_complete: Optional[Callable[[DownloadTask, bool, Optional[Exception]], None]] = None,
    csv_path: Optional[str] = None,
    enable_background_retry: bool = True,
) -> Dict[str, int]:
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
    
    # Start background retry scheduler if enabled
    bg_scheduler: Optional[BackgroundRetryScheduler] = None
    if enable_background_retry and not dry_run:
        bg_scheduler = _setup_background_scheduler(config, logger)
    
    try:
        # Determine effective parallel settings
        dl_config = config.get("download", {})
        max_parallel = max_workers_override or int(dl_config.get("max_parallel_downloads", 1) or 1)
        
        # Use parallel mode only when configured and not dry-run
        if use_parallel and max_parallel > 1 and not dry_run:
            stats = _run_parallel(
                works_df, output_dir, config, max_workers_override, logger,
                on_submit, on_complete, csv_path
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
                "Background scheduler will retry when quotas reset.",
                deferred_count
            )
        
        return stats
    finally:
        # Don't stop the background scheduler here - let it continue running
        # It will be stopped by the main downloader when appropriate
        pass


def _run_sequential(
    works_df: pd.DataFrame,
    output_dir: str,
    dry_run: bool,
    logger: logging.Logger,
    csv_path: Optional[str] = None,
) -> Dict[str, int]:
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
    
    for index, row in works_df.iterrows():
        title = row.get(TITLE_COL)
        creator = row.get(CREATOR_COL)
        entry_id = row.get(ENTRY_ID_COL)
        
        # entry_id is required from the sampling CSV
        if pd.isna(entry_id) or (isinstance(entry_id, str) and not entry_id.strip()):
            logger.warning("Skipping row %d due to missing entry_id.", index + 1)
            skipped += 1
            continue
        
        if pd.isna(title) or not str(title).strip():
            logger.warning("Skipping row %d due to missing or empty title.", index + 1)
            skipped += 1
            continue
        
        # Delegate to pipeline
        result = pipeline.process_work(
            str(title),
            None if pd.isna(creator) else str(creator),
            str(entry_id),
            output_dir,
            dry_run=dry_run,
        )
        
        # Update CSV status if path provided
        if csv_path and not dry_run:
            if result and isinstance(result, dict):
                status = result.get("status", "")
                if status == "completed":
                    # Success - result contains item_url and provider
                    item_url = result.get("item_url", "")
                    provider = result.get("provider", "")
                    if mark_success(csv_path, str(entry_id), item_url, provider):
                        logger.debug("Marked entry %s as success in source CSV", entry_id)
                    else:
                        logger.warning("Failed to mark entry %s as success in source CSV", entry_id)
                    succeeded += 1
                elif status == "failed":
                    # Explicit failure
                    if mark_failed(csv_path, str(entry_id)):
                        logger.debug("Marked entry %s as failed in source CSV", entry_id)
                    else:
                        logger.warning("Failed to mark entry %s as failed in source CSV", entry_id)
                    failed += 1
                # Other statuses (dry_run, no_match) - don't update CSV
            # result is None means deferred/skipped - don't update CSV
        
        processed += 1
        logger.info("%s", "-" * 50)
        
        # Stop early if the global download budget has been exhausted
        try:
            if utils.budget_exhausted():
                logger.warning("Download budget exhausted; stopping further processing.")
                break
        except Exception:
            pass
    
    logger.info("All works processed.")
    return {"processed": processed, "succeeded": succeeded, "failed": failed, "skipped": skipped}


def _run_parallel(
    works_df: pd.DataFrame,
    output_dir: str,
    config: Dict[str, Any],
    max_workers_override: Optional[int],
    logger: logging.Logger,
    on_submit: Optional[Callable[[DownloadTask], None]] = None,
    on_complete: Optional[Callable[[DownloadTask, bool, Optional[Exception]], None]] = None,
    csv_path: Optional[str] = None,
) -> Dict[str, int]:
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
    max_workers = max_workers_override or int(dl_config.get("max_parallel_downloads", 4) or 4)
    provider_concurrency = dict(dl_config.get("provider_concurrency", {}) or {})
    worker_timeout = float(dl_config.get("worker_timeout_s", 600) or 600)
    
    # Track statistics
    submitted_count = 0
    skipped_count = 0
    
    # Default callbacks if none provided
    def default_on_submit(task: DownloadTask) -> None:
        nonlocal submitted_count
        submitted_count += 1
        logger.debug("Queued download %d: '%s'", submitted_count, task.title)
    
    def default_on_complete(task: DownloadTask, success: bool, error: Optional[Exception]) -> None:
        status = "completed" if success else "failed"
        if error:
            logger.warning("Download %s for '%s': %s", status, task.title, error)
        else:
            logger.info("Download %s for '%s'", status, task.title)
        
        # Update CSV status if path provided
        if csv_path and task.entry_id:
            try:
                if success:
                    item_url = getattr(task, "item_url", "") or ""
                    provider = getattr(task, "provider", "") or ""
                    if mark_success(csv_path, task.entry_id, item_url, provider):
                        logger.debug("Marked entry %s as success in source CSV", task.entry_id)
                    else:
                        logger.warning("Failed to mark entry %s as success in source CSV", task.entry_id)
                else:
                    if mark_failed(csv_path, task.entry_id):
                        logger.debug("Marked entry %s as failed in source CSV", task.entry_id)
                    else:
                        logger.warning("Failed to mark entry %s as failed in source CSV", task.entry_id)
            except Exception as csv_err:
                logger.error("Exception updating source CSV for entry %s: %s", task.entry_id, csv_err)
        elif not csv_path:
            logger.debug("No csv_path provided, skipping source CSV update")
        elif not task.entry_id:
            logger.warning("Task has no entry_id, cannot update source CSV for '%s'", task.title)
    
    # Use provided callbacks or defaults
    submit_callback = on_submit or default_on_submit
    complete_callback = on_complete or default_on_complete
    
    # Wrap callbacks to track stats while calling user callbacks
    actual_submitted = [0]
    
    def wrapped_submit(task: DownloadTask) -> None:
        actual_submitted[0] += 1
        submit_callback(task)
    
    def wrapped_complete(task: DownloadTask, success: bool, error: Optional[Exception]) -> None:
        complete_callback(task, success, error)
    
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
        provider_concurrency or "default"
    )
    
    try:
        # Phase 1: Search and queue downloads
        for index, row in works_df.iterrows():
            # Check budget before searching
            try:
                if utils.budget_exhausted():
                    logger.warning("Download budget exhausted; stopping further searches.")
                    break
            except Exception:
                pass
            
            title = row.get(TITLE_COL)
            creator = row.get(CREATOR_COL)
            entry_id = row.get(ENTRY_ID_COL)
            
            # entry_id is required from the sampling CSV
            if pd.isna(entry_id) or (isinstance(entry_id, str) and not entry_id.strip()):
                logger.warning("Skipping row %d due to missing entry_id.", index + 1)
                skipped_count += 1
                continue
            
            if pd.isna(title) or not str(title).strip():
                logger.warning("Skipping row %d due to missing or empty title.", index + 1)
                skipped_count += 1
                continue
            
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
            
            logger.info("%s", "-" * 50)
        
        # Phase 2: Wait for all downloads to complete
        pending = scheduler.pending_count
        if pending > 0:
            logger.info("Search phase complete. Waiting for %d pending download(s)...", pending)
            
            try:
                results = scheduler.wait_all(timeout=worker_timeout)
            except TimeoutError as te:
                # Some futures didn't complete within timeout, but downloads may have finished
                # The scheduler tracks completed/succeeded/failed counts independently
                logger.warning(
                    "Timeout waiting for all futures: %s. Some downloads may still be in progress.",
                    te
                )
            
            stats = scheduler.get_stats()
            logger.info(
                "Download phase complete: %d succeeded, %d failed",
                stats["succeeded"],
                stats["failed"]
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
                logger.warning("Timeout waiting for in-progress downloads after interrupt")
    
    finally:
        scheduler.shutdown(wait=True)
        stats = scheduler.get_stats()
        logger.info(
            "Scheduler shutdown. Final stats: %d completed (%d succeeded, %d failed)",
            stats["completed"],
            stats["succeeded"],
            stats["failed"]
        )
    
    final_stats = scheduler.get_stats()
    return {
        "processed": actual_submitted[0],
        "succeeded": final_stats["succeeded"],
        "failed": final_stats["failed"],
        "skipped": skipped_count,
    }


def create_interactive_callbacks(logger: logging.Logger):
    """Create progress callbacks suitable for interactive mode.
    
    Args:
        logger: Logger instance
        
    Returns:
        Tuple of (on_submit, on_complete) callback functions
    """
    from main.console_ui import ConsoleUI
    
    submitted = [0]
    completed = [0]
    
    def on_submit(task: DownloadTask) -> None:
        submitted[0] += 1
        title_short = task.title[:50] + "..." if len(task.title) > 50 else task.title
        ConsoleUI.print_info(f"Queued [{submitted[0]}]", title_short)
    
    def on_complete(task: DownloadTask, success: bool, error: Optional[Exception]) -> None:
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


__all__ = ["run_batch_downloads", "create_interactive_callbacks"]
