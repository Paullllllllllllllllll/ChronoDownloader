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
from main.interactive import run_interactive, _normalize_csv_columns
from main.execution import run_batch_downloads
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
    # Configure base logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
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

    # Load and validate CSV
    if not os.path.exists(args.csv_file):
        logger.error("CSV file not found at %s", args.csv_file)
        return
    
    try:
        works_df = pd.read_csv(args.csv_file)
    except Exception as e:
        logger.error("Error reading CSV file: %s", e)
        return
    
    # Apply column mappings from config
    works_df = _normalize_csv_columns(works_df, args.config, logger)
    
    if "Title" not in works_df.columns:
        logger.error("CSV file must contain a 'Title' column or a mapped equivalent.")
        return

    # Get parallel download configuration
    dl_config = config.get("download", {})
    max_parallel = int(dl_config.get("max_parallel_downloads", 1) or 1)
    
    logger.info("Starting downloader. Output directory: %s", args.output_dir)
    logger.info("Works to process: %d, Parallel downloads: %d", len(works_df), max_parallel)
    
    # Use shared execution module for both parallel and sequential modes
    stats = run_batch_downloads(
        works_df=works_df,
        output_dir=args.output_dir,
        config=config,
        dry_run=args.dry_run,
        use_parallel=(max_parallel > 1),
        logger=logger,
    )
    
    # Process any deferred downloads (e.g., Anna's Archive quota-limited items)
    deferred = pipeline.get_deferred_downloads()
    if deferred:
        logger.info(
            "%d download(s) were deferred due to quota limits. "
            "Processing deferred downloads after quota reset...",
            len(deferred)
        )
        pipeline.process_deferred_downloads(wait_for_reset=True)
    
    logger.info("Downloader finished.")


def main() -> None:
    """Main entry point supporting both interactive and CLI modes."""
    try:
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
