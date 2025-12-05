"""Shared execution logic for batch downloads.

This module provides a unified interface for running batch downloads in either
parallel or sequential mode. It is used by both CLI and interactive modes to
ensure consistent behavior.

The execution flow:
1. Sequential mode: Process works one at a time via pipeline.process_work()
2. Parallel mode: Search sequentially, queue downloads to DownloadScheduler
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from api import utils
from main import pipeline
from main.download_scheduler import DownloadScheduler, DownloadTask, get_parallel_download_config


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
) -> Dict[str, int]:
    """Run batch downloads with automatic mode selection.
    
    Args:
        works_df: DataFrame with 'Title' column (and optional 'Creator', 'entry_id')
        output_dir: Base directory for downloaded works
        config: Configuration dictionary (from get_config())
        dry_run: If True, skip actual downloads
        use_parallel: Whether to attempt parallel downloads
        max_workers_override: Override for max_parallel_downloads config
        logger: Logger instance (creates default if None)
        on_submit: Optional callback when a task is submitted (parallel mode)
        on_complete: Optional callback when a task completes (parallel mode)
        
    Returns:
        Dictionary with execution statistics:
        - 'processed': Total works processed
        - 'succeeded': Successful downloads (parallel mode only)
        - 'failed': Failed downloads (parallel mode only)
        - 'skipped': Works skipped due to missing title
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    # Determine effective parallel settings
    dl_config = config.get("download", {})
    max_parallel = max_workers_override or int(dl_config.get("max_parallel_downloads", 1) or 1)
    
    # Use parallel mode only when configured and not dry-run
    if use_parallel and max_parallel > 1 and not dry_run:
        return _run_parallel(
            works_df, output_dir, config, max_workers_override, logger,
            on_submit, on_complete
        )
    else:
        return _run_sequential(works_df, output_dir, dry_run, logger)


def _run_sequential(
    works_df: pd.DataFrame,
    output_dir: str,
    dry_run: bool,
    logger: logging.Logger
) -> Dict[str, int]:
    """Run downloads sequentially.
    
    Args:
        works_df: DataFrame with works
        output_dir: Output directory
        dry_run: Whether to skip downloads
        logger: Logger instance
        
    Returns:
        Statistics dictionary
    """
    processed = 0
    skipped = 0
    
    for index, row in works_df.iterrows():
        title = row["Title"]
        creator = row.get("Creator")
        entry_id = row.get("entry_id") if "entry_id" in works_df.columns else None
        
        # Generate fallback entry_id if missing
        if pd.isna(entry_id) or (isinstance(entry_id, str) and not entry_id.strip()):
            entry_id = f"E{index + 1:04d}"
        
        if pd.isna(title) or not str(title).strip():
            logger.warning("Skipping row %d due to missing or empty title.", index + 1)
            skipped += 1
            continue
        
        # Delegate to pipeline
        pipeline.process_work(
            str(title),
            None if pd.isna(creator) else str(creator),
            None if pd.isna(entry_id) else str(entry_id),
            output_dir,
            dry_run=dry_run,
        )
        
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
    return {"processed": processed, "succeeded": 0, "failed": 0, "skipped": skipped}


def _run_parallel(
    works_df: pd.DataFrame,
    output_dir: str,
    config: Dict[str, Any],
    max_workers_override: Optional[int],
    logger: logging.Logger,
    on_submit: Optional[Callable[[DownloadTask], None]] = None,
    on_complete: Optional[Callable[[DownloadTask, bool, Optional[Exception]], None]] = None,
) -> Dict[str, int]:
    """Run downloads in parallel using DownloadScheduler.
    
    Searches are sequential to avoid overwhelming providers,
    but downloads are parallelized across works.
    
    Args:
        works_df: DataFrame with works
        output_dir: Output directory
        config: Configuration dictionary
        max_workers_override: Override for max workers
        logger: Logger instance
        on_submit: Optional callback for task submission
        on_complete: Optional callback for task completion
        
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
            
            title = row["Title"]
            creator = row.get("Creator")
            entry_id = row.get("entry_id") if "entry_id" in works_df.columns else None
            
            # Generate fallback entry_id if missing
            if pd.isna(entry_id) or (isinstance(entry_id, str) and not entry_id.strip()):
                entry_id = f"E{index + 1:04d}"
            
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
            
            results = scheduler.wait_all(timeout=worker_timeout)
            
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
            scheduler.wait_all(timeout=30)
    
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
