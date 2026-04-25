"""CSV-batch CLI handler: the most common run_cli code path."""
from __future__ import annotations

import argparse
import logging
import os
from typing import Any

from main.data.works_csv import (
    DIRECT_LINK_COL,
    TITLE_COL,
    get_stats,
    load_works_csv,
)
from main.orchestration.execution import run_batch_downloads
from main.state.background import get_background_scheduler
from main.state.deferred import get_deferred_queue

from ..overrides import _filter_pending_rows, _split_csv_values


def run_batch_cli(
    args: argparse.Namespace,
    config: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Execute the CSV-based batch download path."""
    if not args.csv_file:
        logger.error("CSV file path is required in CLI mode. Use --interactive for guided setup.")
        return

    csv_path = os.path.abspath(args.csv_file)
    try:
        works_df = load_works_csv(csv_path)
    except FileNotFoundError:
        logger.error("CSV file not found at %s", csv_path)
        return
    except ValueError as e:
        logger.error("CSV validation error: %s", e)
        return
    except Exception as e:
        logger.error("Error reading CSV file: %s", e)
        return

    if TITLE_COL not in works_df.columns and DIRECT_LINK_COL not in works_df.columns:
        logger.error(
            "CSV file must contain '%s' or '%s'.",
            TITLE_COL,
            DIRECT_LINK_COL,
        )
        return

    initial_stats = get_stats(csv_path)
    pending_df = _filter_pending_rows(works_df, args)

    logger.info(
        "CSV status: %d total, %d completed, %d failed, %d pending",
        initial_stats["total"],
        initial_stats["completed"],
        initial_stats["failed"],
        initial_stats["pending"],
    )

    pending_mode = getattr(args, "pending_mode", "all")
    if pending_mode != "all":
        logger.info("Pending filter active: mode=%s", pending_mode)

    entry_ids = getattr(args, "entry_ids", None)
    if entry_ids:
        logger.info(
            "Entry filter active: %d requested entry_id value(s)",
            len(_split_csv_values(entry_ids)),
        )

    limit = getattr(args, "limit", None)
    if limit is not None:
        logger.info("Limit filter active: processing at most %d row(s)", max(0, limit))

    if len(pending_df) == 0:
        logger.info("No works to process after applying pending/entry/limit filters.")
        return

    dl_config = config.get("download", {})
    max_parallel = int(dl_config.get("max_parallel_downloads", 1) or 1)

    logger.info("Starting downloader. Output directory: %s", args.output_dir)
    logger.info(
        "Works to process: %d (pending), Parallel downloads: %d",
        len(pending_df),
        max_parallel,
    )

    stats = run_batch_downloads(
        works_df=pending_df,
        output_dir=args.output_dir,
        config=config,
        dry_run=args.dry_run,
        use_parallel=(max_parallel > 1),
        logger=logger,
        csv_path=csv_path,
        enable_background_retry=True,
    )

    queue = get_deferred_queue()
    deferred_count = len(queue.get_pending())

    if deferred_count > 0:
        scheduler = get_background_scheduler()
        if scheduler.is_running():
            logger.info(
                "%d download(s) deferred due to quota limits. "
                "Background scheduler is running and will retry when quotas reset.",
                deferred_count,
            )
            logger.info(
                "You can safely exit - deferred queue is persisted to disk. "
                "Run the downloader again to resume background retries."
            )
        else:
            logger.info(
                "%d download(s) deferred. Background scheduler not running. "
                "Run the downloader again to process deferred items.",
                deferred_count,
            )

    logger.info(
        "Batch complete: %d processed, %d succeeded, %d failed, %d deferred",
        stats.get("processed", 0),
        stats.get("succeeded", 0),
        stats.get("failed", 0),
        stats.get("deferred", 0),
    )

    logger.info("Downloader finished.")
