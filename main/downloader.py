"""CLI entry point for ChronoDownloader.

This module provides the command-line interface for the downloader tool.
All orchestration logic has been moved to main/pipeline.py.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd

# Ensure parent directory is in path for direct script execution
if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from main import pipeline
from api import utils


def _normalize_csv_columns(df: pd.DataFrame, config_path: str, logger: logging.Logger) -> pd.DataFrame:
    """Normalize CSV column names based on configured mappings.
    
    Args:
        df: Input DataFrame
        config_path: Path to configuration JSON file
        logger: Logger instance
        
    Returns:
        DataFrame with normalized column names
    """
    import json
    
    # Try to load column mappings from config
    mappings = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                mappings = cfg.get("csv_column_mapping", {})
        except Exception as e:
            logger.warning("Could not load csv_column_mapping from config: %s", e)
    
    # Apply mappings: for each target column, find first matching source column
    for target_col, source_candidates in mappings.items():
        if not isinstance(source_candidates, list):
            source_candidates = [source_candidates]
        
        # Check if target already exists
        if target_col.capitalize() in df.columns:
            continue
            
        # Find first matching source column
        for source_col in source_candidates:
            if source_col in df.columns:
                # Rename to standard format (Title, Creator, entry_id)
                if target_col == "title":
                    df.rename(columns={source_col: "Title"}, inplace=True)
                    logger.info("Mapped column '%s' to 'Title'", source_col)
                elif target_col == "creator":
                    df.rename(columns={source_col: "Creator"}, inplace=True)
                    logger.info("Mapped column '%s' to 'Creator'", source_col)
                elif target_col == "entry_id":
                    df.rename(columns={source_col: "entry_id"}, inplace=True)
                    logger.info("Mapped column '%s' to 'entry_id'", source_col)
                break
    
    return df


def main() -> None:
    """Parse CLI arguments and run the downloader pipeline."""
    parser = argparse.ArgumentParser(
        description="Download historical sources from various digital libraries."
    )
    parser.add_argument(
        "csv_file",
        help="Path to the CSV file containing works to download. Must have a 'Title' column. Optional 'Creator' column."
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
    
    args = parser.parse_args()

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

    logger.info("Starting downloader. Output directory: %s", args.output_dir)
    
    # Process each work in the CSV
    for index, row in works_df.iterrows():
        title = row["Title"]
        creator = row.get("Creator")
        entry_id = row.get("entry_id") if "entry_id" in works_df.columns else None
        
        # Generate fallback entry_id if missing
        if pd.isna(entry_id) or (isinstance(entry_id, str) and not entry_id.strip()):
            entry_id = f"E{index + 1:04d}"
        
        if pd.isna(title) or not str(title).strip():
            logger.warning("Skipping row %d due to missing or empty title.", index + 1)
            continue
        
        # Delegate to pipeline
        pipeline.process_work(
            str(title),
            None if pd.isna(creator) else str(creator),
            None if pd.isna(entry_id) else str(entry_id),
            args.output_dir,
            dry_run=args.dry_run,
        )
        
        logger.info("%s", "-" * 50)
        
        # Stop early if the global download budget has been exhausted
        try:
            if utils.budget_exhausted():
                logger.warning("Download budget exhausted; stopping further processing.")
                break
        except Exception:
            pass
    
    logger.info("All works processed.")


if __name__ == "__main__":
    main()
