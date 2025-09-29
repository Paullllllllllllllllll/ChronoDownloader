import os
import sys
import argparse
import logging
from typing import Any, List, Tuple

import pandas as pd
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from main import pipeline
from api import utils

# Providers are registered centrally in api/providers.py as PROVIDERS


def load_enabled_apis(config_path: str) -> List[Tuple[str, Any, Any, str]]:
    """DEPRECATED: Use main.pipeline.load_enabled_apis instead."""
    return pipeline.load_enabled_apis(config_path)


# Back-compat: default placeholder; will be set from pipeline in main()
ENABLED_APIS: List[Tuple[str, Any, Any, str]] = []


def _filter_enabled_providers_for_keys(enabled: List[Tuple[str, Any, Any, str]]) -> List[Tuple[str, Any, Any, str]]:
    """DEPRECATED: Use main.pipeline.filter_enabled_providers_for_keys instead."""
    return pipeline.filter_enabled_providers_for_keys(enabled)


def _get_selection_config():  # noqa: N802 (legacy name)
    """DEPRECATED shim to pipeline internals."""
    return pipeline._get_selection_config()  # type: ignore[attr-defined]


def _title_slug(title: str, max_len: int = 80) -> str:  # noqa: N802
    """DEPRECATED shim to pipeline internals."""
    return pipeline._title_slug(title, max_len)  # type: ignore[attr-defined]


def _compute_work_id(title, creator):  # noqa: N802
    """DEPRECATED shim to pipeline internals."""
    return pipeline._compute_work_id(title, creator)  # type: ignore[attr-defined]


def _provider_order(enabled, hierarchy):  # noqa: N802
    """DEPRECATED shim to pipeline internals."""
    return pipeline._provider_order(enabled, hierarchy)  # type: ignore[attr-defined]


def _get_naming_config():  # noqa: N802
    """DEPRECATED shim to pipeline internals."""
    return pipeline._get_naming_config()  # type: ignore[attr-defined]


def process_work(title, creator=None, entry_id=None, base_output_dir="downloaded_works", dry_run=False):
    """DEPRECATED: Use main.pipeline.process_work instead."""
    return pipeline.process_work(title, creator=creator, entry_id=entry_id, base_output_dir=base_output_dir, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description="Download historical sources from various digital libraries.")
    parser.add_argument("csv_file", help="Path to the CSV file containing works to download. Must have a 'Title' column. Optional 'Creator' column.")
    parser.add_argument("--output_dir", default="downloaded_works", help="Directory to save downloaded files.")
    parser.add_argument("--dry-run", action="store_true", help="Run searches and create folders, but skip downloads.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to JSON config file to enable/disable providers.",
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

    # Load providers from config (if exists), otherwise defaults remain (IA only)
    # Resolve providers using the shared pipeline module
    providers = pipeline.load_enabled_apis(args.config)
    providers = pipeline.filter_enabled_providers_for_keys(providers)
    pipeline.ENABLED_APIS = providers
    # Back-compat: mirror to this module in case other code imports downloader.ENABLED_APIS
    global ENABLED_APIS
    ENABLED_APIS = providers
    if not providers:
        logger.warning("No providers are enabled. Update %s to enable providers.", args.config)

    if not os.path.exists(args.csv_file):
        logger.error("CSV file not found at %s", args.csv_file)
        return
    try:
        works_df = pd.read_csv(args.csv_file)
    except Exception as e:
        logger.error("Error reading CSV file: %s", e)
        return
    if "Title" not in works_df.columns:
        logger.error("CSV file must contain a 'Title' column.")
        return
    logger.info("Starting downloader. Output directory: %s", args.output_dir)
    for index, row in works_df.iterrows():
        title = row["Title"]
        creator = row.get("Creator")
        entry_id = row.get("entry_id") if "entry_id" in works_df.columns else None
        if pd.isna(entry_id) or (isinstance(entry_id, str) and not entry_id.strip()):
            # Backward compatibility: generate a stable fallback for this run
            entry_id = f"E{index + 1:04d}"
        if pd.isna(title) or not str(title).strip():
            logger.warning("Skipping row %d due to missing or empty title.", index + 1)
            continue
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
