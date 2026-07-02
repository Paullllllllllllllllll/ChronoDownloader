"""CSV-batch CLI handler: the most common run_cli code path."""

from __future__ import annotations

import argparse
import json
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
from main.state.deferred import get_deferred_queue

from ..exit_codes import EXIT_FAILURES, EXIT_OK, EXIT_USAGE
from ..overrides import _filter_pending_rows, _split_csv_values


def _emit_json_summary(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, ensure_ascii=False))


def run_batch_cli(
    args: argparse.Namespace,
    config: dict[str, Any],
    logger: logging.Logger,
) -> int:
    """Execute the CSV-based batch download path.

    Returns:
        A process exit code (see :mod:`main.cli.exit_codes`).
    """
    json_summary = getattr(args, "json_summary", False)

    if not args.csv_file:
        logger.error(
            "CSV file path is required in CLI mode. Use --interactive for guided setup."
        )
        return EXIT_USAGE

    csv_path = os.path.abspath(args.csv_file)
    try:
        works_df = load_works_csv(csv_path)
    except FileNotFoundError:
        logger.error("CSV file not found at %s", csv_path)
        return EXIT_USAGE
    except ValueError as e:
        logger.error("CSV validation error: %s", e)
        return EXIT_USAGE
    except Exception as e:
        logger.error("Error reading CSV file: %s", e)
        return EXIT_USAGE

    if TITLE_COL not in works_df.columns and DIRECT_LINK_COL not in works_df.columns:
        logger.error(
            "CSV file must contain '%s' or '%s'.",
            TITLE_COL,
            DIRECT_LINK_COL,
        )
        return EXIT_USAGE

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
        if json_summary:
            _emit_json_summary(
                {
                    "command": "batch",
                    "csv": csv_path,
                    "output_dir": args.output_dir,
                    "processed": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "deferred": 0,
                    "skipped": 0,
                    "dry_run": bool(args.dry_run),
                }
            )
        return EXIT_OK

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
        logger.info(
            "%d download(s) deferred due to quota limits. Ready items are "
            "retried automatically at the start of the next run.",
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

    failed = int(stats.get("failed", 0))
    deferred = int(stats.get("deferred", 0))

    if json_summary:
        _emit_json_summary(
            {
                "command": "batch",
                "csv": csv_path,
                "output_dir": args.output_dir,
                "processed": int(stats.get("processed", 0)),
                "succeeded": int(stats.get("succeeded", 0)),
                "failed": failed,
                "deferred": deferred,
                "skipped": int(stats.get("skipped", 0)),
                "dry_run": bool(args.dry_run),
            }
        )

    # Deferred items are not failures (they will be retried); only genuine
    # failures make the run exit non-zero.
    return EXIT_FAILURES if failed > 0 else EXIT_OK
