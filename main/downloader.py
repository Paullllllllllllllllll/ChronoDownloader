"""CLI entry point for ChronoDownloader.

This module provides both command-line and interactive interfaces for the downloader.
Supports two modes:
1. Interactive mode: Guided workflow with user prompts (interactive_mode: true in config)
2. CLI mode: Command-line arguments for automation (interactive_mode: false in config)

Parallel download support:
- When max_parallel_downloads > 1, downloads run concurrently across works
- Per-provider concurrency limits prevent overwhelming rate-limited providers
- Thread-safe operations for index.csv and deferred downloads

All orchestration logic has been moved to main/pipeline.py.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

# Ensure parent directory is in path for direct script execution
if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from main import pipeline
from main.mode_selector import run_with_mode_detection
from main.interactive import run_interactive
from main.execution import run_batch_downloads
from main.background_scheduler import get_background_scheduler, stop_background_scheduler
from main.deferred_queue import get_deferred_queue
from main.quota_manager import get_quota_manager
from main.unified_csv import (
    load_works_csv,
    get_pending_works,
    get_stats,
    TITLE_COL,
)
from api import utils


def create_cli_parser() -> argparse.ArgumentParser:
    """Create argument parser for CLI mode.
    
    Returns:
        Configured ArgumentParser for download operations
    """
    parser = argparse.ArgumentParser(
        description="ChronoDownloader - Historical Document Download Tool (CLI Mode)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download works from a CSV file
  python main/downloader.py sample_works.csv --output_dir my_downloads

  # Dry run (search only, no downloads)
  python main/downloader.py works.csv --dry-run --log-level DEBUG

  # Use a different config file
  python main/downloader.py works.csv --config config_small.json

  # Force interactive mode
  python main/downloader.py --interactive
        """
    )
    
    # Positional argument (optional in interactive mode)
    parser.add_argument(
        "csv_file",
        nargs="?",
        default=None,
        help="Path to the CSV file containing works to download. Must have a 'Title' column."
    )
    
    parser.add_argument(
        "--output_dir",
        default="downloaded_works",
        help="Directory to save downloaded files."
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run searches and create folders, but skip downloads."
    )
    
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level"
    )
    
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to JSON config file to enable/disable providers."
    )
    
    # Mode override flags
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Force interactive mode regardless of config setting."
    )
    
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Force CLI mode regardless of config setting."
    )
    
    parser.add_argument(
        "--quota-status",
        action="store_true",
        help="Display quota and deferred queue status, then exit."
    )
    
    parser.add_argument(
        "--cleanup-deferred",
        action="store_true",
        help="Remove completed items from deferred queue, then exit."
    )
    
    return parser


def run_cli(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Run the downloader in CLI mode.
    
    Supports both sequential and parallel download modes based on config.
    When max_parallel_downloads > 1, downloads are parallelized across works
    while respecting per-provider concurrency limits.
    
    Args:
        args: Parsed command-line arguments
        config: Configuration dictionary
    """
    # Configure base logging with UTF-8 encoding for proper Unicode support (Windows)
    # Ensure stdout/stderr use UTF-8 encoding for proper display of international characters
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[union-attr]
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[union-attr]
        except (AttributeError, OSError):
            # Fallback for older Python or restricted environments
            pass
    
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler(stream=sys.stdout)],
    )
    
    # Reduce noisy retry logs from urllib3
    try:
        logging.getLogger("urllib3").setLevel(logging.ERROR)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    except Exception:
        pass

    logger = logging.getLogger(__name__)

    # Ensure utils.get_config() reads the same config path
    try:
        os.environ["CHRONO_CONFIG_PATH"] = args.config
    except Exception:
        pass

    # Load and validate providers
    providers = pipeline.load_enabled_apis(args.config)
    providers = pipeline.filter_enabled_providers_for_keys(providers)
    pipeline.ENABLED_APIS = providers
    
    if not providers:
        logger.warning("No providers are enabled. Update %s to enable providers.", args.config)
        return

    # Validate CSV file argument
    if not args.csv_file:
        logger.error("CSV file path is required in CLI mode. Use --interactive for guided setup.")
        return

    # Load CSV using unified CSV system (expects notebook column format)
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
    
    # Validate required column
    if TITLE_COL not in works_df.columns:
        logger.error(
            "CSV file must contain a '%s' column (sampling notebook format).",
            TITLE_COL
        )
        return
    
    # Filter to pending works only (resume support)
    initial_stats = get_stats(csv_path)
    pending_df = get_pending_works(works_df)
    
    logger.info("CSV status: %d total, %d completed, %d failed, %d pending",
                initial_stats["total"], initial_stats["completed"],
                initial_stats["failed"], initial_stats["pending"])
    
    if len(pending_df) == 0:
        logger.info("No pending works to process. All items already have a status.")
        return

    # Get parallel download configuration
    dl_config = config.get("download", {})
    max_parallel = int(dl_config.get("max_parallel_downloads", 1) or 1)
    
    logger.info("Starting downloader. Output directory: %s", args.output_dir)
    logger.info("Works to process: %d (pending), Parallel downloads: %d", len(pending_df), max_parallel)
    
    # Use shared execution module for both parallel and sequential modes
    # Pass csv_path for status updates back to the source CSV
    # Background scheduler handles deferred downloads automatically
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
    
    # Check for deferred downloads
    queue = get_deferred_queue()
    deferred_count = len(queue.get_pending())
    
    if deferred_count > 0:
        scheduler = get_background_scheduler()
        if scheduler.is_running():
            logger.info(
                "%d download(s) deferred due to quota limits. "
                "Background scheduler is running and will retry when quotas reset.",
                deferred_count
            )
            logger.info(
                "You can safely exit - deferred queue is persisted to disk. "
                "Run the downloader again to resume background retries."
            )
        else:
            logger.info(
                "%d download(s) deferred. Background scheduler not running. "
                "Run the downloader again to process deferred items.",
                deferred_count
            )
    
    # Log final statistics
    logger.info(
        "Batch complete: %d processed, %d succeeded, %d failed, %d deferred",
        stats.get("processed", 0),
        stats.get("succeeded", 0),
        stats.get("failed", 0),
        stats.get("deferred", 0),
    )
    
    logger.info("Downloader finished.")


def show_quota_status() -> None:
    """Display quota and deferred queue status."""
    from datetime import datetime, timezone
    
    print("\n" + "=" * 60)
    print("QUOTA & DEFERRED QUEUE STATUS")
    print("=" * 60)
    
    # Quota status
    quota_manager = get_quota_manager()
    quota_providers = quota_manager.get_quota_limited_providers()
    
    if quota_providers:
        print("\n[QUOTA STATUS]")
        for provider_key in quota_providers:
            status = quota_manager.get_quota_status(provider_key)
            remaining = status["remaining"]
            daily_limit = status["daily_limit"]
            used = status["downloads_used"]
            
            if status["is_exhausted"]:
                reset_secs = status["seconds_until_reset"]
                hours = reset_secs / 3600
                print(f"  * {provider_key}: {used}/{daily_limit} used (EXHAUSTED - resets in {hours:.1f}h)")
            else:
                print(f"  * {provider_key}: {used}/{daily_limit} used ({remaining} remaining)")
    else:
        print("\n[QUOTA STATUS] No quota-limited providers configured.")
    
    # Deferred queue status
    queue = get_deferred_queue()
    counts = queue.count_by_status()
    pending = counts.get("pending", 0) + counts.get("retrying", 0)
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    
    print("\n[DEFERRED QUEUE]")
    print(f"  * Pending: {pending}")
    print(f"  * Completed: {completed}")
    print(f"  * Failed: {failed}")
    
    if pending > 0:
        next_ready = queue.get_next_ready_time()
        if next_ready:
            now = datetime.now(timezone.utc)
            delta = (next_ready - now).total_seconds()
            if delta > 0:
                hours = delta / 3600
                print(f"  * Next retry in: {hours:.1f} hours")
            else:
                print(f"  * Ready for retry NOW")
        
        # Show pending items
        print("\n  Pending items:")
        for item in queue.get_pending()[:10]:  # Show first 10
            title_display = item.title[:50] if len(item.title) > 50 else item.title
            print(f"    - {title_display} ({item.provider_name})")
        if pending > 10:
            print(f"    ... and {pending - 10} more")
    
    # Background scheduler status
    scheduler = get_background_scheduler()
    print("\n[BACKGROUND SCHEDULER]")
    if scheduler.is_running():
        stats = scheduler.get_stats()
        print(f"  * Status: RUNNING")
        print(f"  * Checks: {stats.get('checks', 0)}")
        print(f"  * Retries attempted: {stats.get('retries_attempted', 0)}")
        print(f"  * Retries succeeded: {stats.get('retries_succeeded', 0)}")
    else:
        print(f"  * Status: STOPPED")
    
    print("\n" + "=" * 60 + "\n")


def cleanup_deferred_queue() -> None:
    """Remove completed items from deferred queue."""
    queue = get_deferred_queue()
    
    counts_before = queue.count_by_status()
    completed_before = counts_before.get("completed", 0)
    
    removed = queue.clear_completed()
    
    print(f"Cleaned up {removed} completed item(s) from deferred queue.")
    
    # Show remaining counts
    counts_after = queue.count_by_status()
    pending = counts_after.get("pending", 0) + counts_after.get("retrying", 0)
    failed = counts_after.get("failed", 0)
    print(f"Remaining: {pending} pending, {failed} failed")


def main() -> None:
    """Main entry point supporting both interactive and CLI modes."""
    try:
        # Quick check for status commands before full mode detection
        if "--quota-status" in sys.argv:
            show_quota_status()
            return
        
        if "--cleanup-deferred" in sys.argv:
            cleanup_deferred_queue()
            return
        
        # Use centralized mode detection
        config, interactive_mode, args = run_with_mode_detection(
            interactive_handler=run_interactive,
            cli_handler=run_cli,
            parser_factory=create_cli_parser,
            script_name="downloader"
        )
        
        # Check for explicit mode override flags
        if args:
            if args.interactive:
                interactive_mode = True
            elif args.cli:
                interactive_mode = False
        
        # Route to appropriate handler
        if interactive_mode:
            run_interactive()
        else:
            if args is None:
                # This shouldn't happen, but handle gracefully
                print("Error: CLI mode requires arguments. Use --interactive for guided setup.")
                sys.exit(1)
            run_cli(args, config)
            
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        logging.exception("Unexpected error: %s", e)
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
