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
import copy
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd

# Ensure parent directory is in path for direct script execution
if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from main import pipeline
from main.mode_selector import run_with_mode_detection
from main.interactive import run_interactive
from main.execution import run_batch_downloads, process_direct_iiif
from main.background_scheduler import get_background_scheduler
from main.deferred_queue import get_deferred_queue
from main.quota_manager import get_quota_manager
from api.providers import PROVIDERS
import api.core.config as core_config
from main.unified_csv import (
    DIRECT_LINK_COL,
    ENTRY_ID_COL,
    STATUS_COL,
    load_works_csv,
    get_pending_works,
    get_stats,
    TITLE_COL,
)

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

  # Download from a single IIIF manifest URL
  python main/downloader.py --iiif "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k1511262r/manifest.json" --name Taillevent

  # Download multiple IIIF manifests
  python main/downloader.py --iiif URL1 --iiif URL2 --output_dir my_downloads
        """
    )
    
    # Positional argument (optional in interactive mode)
    parser.add_argument(
        "csv_file",
        nargs="?",
        default=None,
        help="Path to CSV file. Must contain 'short_title' or 'direct_link' (with required 'entry_id')."
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
    
    # Direct IIIF download flags
    parser.add_argument(
        "--iiif",
        action="append",
        dest="iiif_urls",
        metavar="URL",
        help="Direct IIIF manifest URL(s) to download (repeatable). Bypasses CSV and search.",
    )
    
    parser.add_argument(
        "--name",
        default=None,
        help="Output name stem for IIIF downloads (e.g. 'Taillevent_Viandier'). "
             "Used for folder and file naming.",
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

    # Provider selection overrides (keep provider concurrency in config)
    parser.add_argument(
        "--providers",
        action="append",
        default=None,
        metavar="KEYS",
        help="Comma-separated provider keys to use for this run (e.g. mdz,bnf_gallica,slub)."
    )

    parser.add_argument(
        "--enable-provider",
        action="append",
        default=None,
        metavar="KEYS",
        help="Provider key(s) to force-enable for this run (comma-separated or repeat flag)."
    )

    parser.add_argument(
        "--disable-provider",
        action="append",
        default=None,
        metavar="KEYS",
        help="Provider key(s) to force-disable for this run (comma-separated or repeat flag)."
    )

    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="List all available provider keys and exit."
    )

    # Processing scope controls for agentic workflows
    parser.add_argument(
        "--pending-mode",
        default="all",
        choices=["all", "new", "failed"],
        help="Which CSV rows to process: all pending+failed (default), only never-tried rows, or only failed rows."
    )

    parser.add_argument(
        "--entry-ids",
        action="append",
        default=None,
        metavar="IDS",
        help="Restrict processing to specific entry_id values (comma-separated or repeat flag)."
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N pending rows after filters are applied."
    )

    # Configurable processing options exposed to CLI (concurrency remains config-only)
    parser.add_argument(
        "--resume-mode",
        choices=["skip_completed", "reprocess_all", "skip_if_has_objects", "resume_from_csv"],
        default=None,
        help="Override download.resume_mode for this run."
    )

    parser.add_argument(
        "--selection-strategy",
        choices=["collect_and_select", "sequential_first_hit"],
        default=None,
        help="Override selection.strategy for this run."
    )

    parser.add_argument(
        "--min-title-score",
        type=float,
        default=None,
        help="Override selection.min_title_score for this run."
    )

    parser.add_argument(
        "--creator-weight",
        type=float,
        default=None,
        help="Override selection.creator_weight for this run (0.0-1.0)."
    )

    parser.add_argument(
        "--max-candidates-per-provider",
        type=int,
        default=None,
        help="Override selection.max_candidates_per_provider for this run."
    )

    parser.add_argument(
        "--download-strategy",
        choices=["selected_only", "all"],
        default=None,
        help="Override selection.download_strategy for this run."
    )

    parser.add_argument(
        "--keep-non-selected-metadata",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override selection.keep_non_selected_metadata for this run."
    )

    parser.add_argument(
        "--prefer-pdf-over-images",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override download.prefer_pdf_over_images for this run."
    )

    parser.add_argument(
        "--download-manifest-renderings",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override download.download_manifest_renderings for this run."
    )

    parser.add_argument(
        "--max-renderings-per-manifest",
        type=int,
        default=None,
        help="Override download.max_renderings_per_manifest for this run."
    )

    parser.add_argument(
        "--rendering-mime-whitelist",
        action="append",
        default=None,
        metavar="MIMES",
        help="Override download.rendering_mime_whitelist (comma-separated MIME values)."
    )

    parser.add_argument(
        "--overwrite-existing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override download.overwrite_existing for this run."
    )

    parser.add_argument(
        "--include-metadata",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override download.include_metadata for this run."
    )
    
    return parser


_TRUTHY = frozenset({"true", "1", "yes", "y"})
_FALSY = frozenset({"false", "0", "no", "n"})


def _split_csv_values(values: list[str] | None) -> list[str]:
    """Split comma-separated CLI values and strip whitespace."""
    if not values:
        return []
    result: list[str] = []
    for raw in values:
        if not raw:
            continue
        for part in str(raw).split(","):
            item = part.strip()
            if item:
                result.append(item)
    return result


def _dedupe_keep_order(values: list[str]) -> list[str]:
    """Deduplicate strings while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _classify_status(value: Any) -> str:
    """Classify CSV status cell as completed, failed, or pending."""
    if pd.isna(value):
        return "pending"
    if isinstance(value, bool):
        return "completed" if value else "failed"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            return "completed"
        if lowered in _FALSY:
            return "failed"
    return "pending"


def _apply_runtime_config_overrides(
    args: argparse.Namespace,
    config: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, Any]:
    """Apply CLI overrides to runtime config and refresh config cache."""
    merged = copy.deepcopy(config or {})
    merged.setdefault("download", {})
    merged.setdefault("selection", {})

    dl_cfg = dict(merged.get("download") or {})
    sel_cfg = dict(merged.get("selection") or {})

    resume_mode = getattr(args, "resume_mode", None)
    prefer_pdf = getattr(args, "prefer_pdf_over_images", None)
    manifest_renderings = getattr(args, "download_manifest_renderings", None)
    max_renderings = getattr(args, "max_renderings_per_manifest", None)
    rendering_mimes = getattr(args, "rendering_mime_whitelist", None)
    overwrite_existing = getattr(args, "overwrite_existing", None)
    include_metadata = getattr(args, "include_metadata", None)

    selection_strategy = getattr(args, "selection_strategy", None)
    min_title_score = getattr(args, "min_title_score", None)
    creator_weight = getattr(args, "creator_weight", None)
    max_candidates_per_provider = getattr(args, "max_candidates_per_provider", None)
    download_strategy = getattr(args, "download_strategy", None)
    keep_non_selected_metadata = getattr(args, "keep_non_selected_metadata", None)

    if resume_mode is not None:
        dl_cfg["resume_mode"] = resume_mode
    if prefer_pdf is not None:
        dl_cfg["prefer_pdf_over_images"] = bool(prefer_pdf)
    if manifest_renderings is not None:
        dl_cfg["download_manifest_renderings"] = bool(manifest_renderings)
    if max_renderings is not None:
        dl_cfg["max_renderings_per_manifest"] = int(max(0, max_renderings))
    if rendering_mimes:
        mime_values = _dedupe_keep_order(_split_csv_values(rendering_mimes))
        if mime_values:
            dl_cfg["rendering_mime_whitelist"] = mime_values
    if overwrite_existing is not None:
        dl_cfg["overwrite_existing"] = bool(overwrite_existing)
    if include_metadata is not None:
        dl_cfg["include_metadata"] = bool(include_metadata)

    if selection_strategy is not None:
        sel_cfg["strategy"] = selection_strategy
    if min_title_score is not None:
        sel_cfg["min_title_score"] = float(min_title_score)
    if creator_weight is not None:
        sel_cfg["creator_weight"] = float(creator_weight)
    if max_candidates_per_provider is not None:
        sel_cfg["max_candidates_per_provider"] = int(max(1, max_candidates_per_provider))
    if download_strategy is not None:
        sel_cfg["download_strategy"] = download_strategy
    if keep_non_selected_metadata is not None:
        sel_cfg["keep_non_selected_metadata"] = bool(keep_non_selected_metadata)

    merged["download"] = dl_cfg
    merged["selection"] = sel_cfg

    # Ensure config consumers using get_config() see the same runtime overrides.
    core_config._CONFIG_CACHE = merged
    logger.debug("Applied CLI runtime config overrides to in-memory config cache")
    return merged


def _apply_provider_cli_overrides(
    args: argparse.Namespace,
    providers: list[Any],
    logger: logging.Logger,
) -> list[Any]:
    """Apply provider selection overrides while preserving provider ordering."""
    explicit_keys = _dedupe_keep_order(_split_csv_values(getattr(args, "providers", None)))
    force_enable = _dedupe_keep_order(_split_csv_values(getattr(args, "enable_provider", None)))
    force_disable = set(_dedupe_keep_order(_split_csv_values(getattr(args, "disable_provider", None))))

    if not explicit_keys and not force_enable and not force_disable:
        return providers

    available = set(PROVIDERS.keys())
    unknown = [k for k in explicit_keys + force_enable + list(force_disable) if k not in available]
    if unknown:
        logger.warning("Ignoring unknown provider key(s): %s", ", ".join(sorted(set(unknown))))

    current_keys = [p[0] for p in providers if isinstance(p, tuple) and len(p) >= 4]

    if explicit_keys:
        ordered_keys = [k for k in explicit_keys if k in available]
    else:
        ordered_keys = list(current_keys)

    for key in force_enable:
        if key in available and key not in ordered_keys:
            ordered_keys.append(key)

    ordered_keys = [k for k in ordered_keys if k not in force_disable and k in available]

    overridden: list[Any] = []
    for key in ordered_keys:
        search_fn, download_fn, name = PROVIDERS[key]
        overridden.append((key, search_fn, download_fn, name))

    logger.info("Provider override active. Effective providers: %s", ", ".join(ordered_keys) or "(none)")
    return overridden


def _filter_pending_rows(works_df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    """Apply pending-mode, entry-id, and limit filters to the work DataFrame."""
    pending_mode = getattr(args, "pending_mode", "all")
    if pending_mode == "all":
        pending_df = get_pending_works(works_df)
    else:
        status_series = works_df[STATUS_COL] if STATUS_COL in works_df.columns else pd.Series([pd.NA] * len(works_df))
        status_labels = status_series.apply(_classify_status)
        if pending_mode == "new":
            pending_df = works_df[status_labels == "pending"].copy()
        else:  # pending_mode == "failed"
            pending_df = works_df[status_labels == "failed"].copy()

    requested_ids = _dedupe_keep_order(_split_csv_values(getattr(args, "entry_ids", None)))
    if requested_ids:
        id_set = {str(i) for i in requested_ids}
        pending_df = pending_df[pending_df[ENTRY_ID_COL].astype(str).isin(id_set)].copy()

    limit = getattr(args, "limit", None)
    if limit is not None and limit >= 0:
        pending_df = pending_df.head(limit).copy()

    return pending_df


def _looks_like_cli_invocation(argv: list[str]) -> bool:
    """Heuristically detect CLI intent so automation need not toggle config first."""
    if not argv:
        return False

    if "--interactive" in argv:
        return False

    cli_flags = {
        "--cli",
        "--help",
        "-h",
        "--output_dir",
        "--dry-run",
        "--log-level",
        "--config",
        "--iiif",
        "--name",
        "--providers",
        "--enable-provider",
        "--disable-provider",
        "--list-providers",
        "--pending-mode",
        "--entry-ids",
        "--limit",
        "--resume-mode",
        "--selection-strategy",
        "--min-title-score",
        "--creator-weight",
        "--max-candidates-per-provider",
        "--download-strategy",
        "--keep-non-selected-metadata",
        "--no-keep-non-selected-metadata",
        "--prefer-pdf-over-images",
        "--no-prefer-pdf-over-images",
        "--download-manifest-renderings",
        "--no-download-manifest-renderings",
        "--max-renderings-per-manifest",
        "--rendering-mime-whitelist",
        "--overwrite-existing",
        "--no-overwrite-existing",
        "--include-metadata",
        "--no-include-metadata",
    }

    for token in argv:
        if token in cli_flags:
            return True
        if not token.startswith("-"):
            return True
    return False

def run_cli(args: argparse.Namespace, config: dict[str, Any]) -> None:
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

    if getattr(args, "list_providers", False):
        print("Available providers:")
        for key, (_search_fn, _download_fn, name) in sorted(PROVIDERS.items(), key=lambda kv: kv[0]):
            print(f"  - {key}: {name}")
        return

    # Ensure utils.get_config() reads the same config path
    try:
        os.environ["CHRONO_CONFIG_PATH"] = args.config
    except Exception:
        pass

    config = _apply_runtime_config_overrides(args, config, logger)

    # Load and validate providers
    providers = pipeline.load_enabled_apis(args.config)
    providers = _apply_provider_cli_overrides(args, providers, logger)
    providers = pipeline.filter_enabled_providers_for_keys(providers)
    pipeline.ENABLED_APIS = providers
    
    if not providers:
        logger.warning("No providers are enabled. Update %s to enable providers.", args.config)
        return

    # Handle direct IIIF mode (--iiif takes precedence over csv_file)
    if args.iiif_urls:
        _run_direct_iiif_cli(args, config, logger)
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
    if TITLE_COL not in works_df.columns and DIRECT_LINK_COL not in works_df.columns:
        logger.error(
            "CSV file must contain '%s' or '%s'.",
            TITLE_COL,
            DIRECT_LINK_COL,
        )
        return
    
    # Filter to pending works only (resume support)
    initial_stats = get_stats(csv_path)
    pending_df = _filter_pending_rows(works_df, args)
    
    logger.info("CSV status: %d total, %d completed, %d failed, %d pending",
                initial_stats["total"], initial_stats["completed"],
                initial_stats["failed"], initial_stats["pending"])

    pending_mode = getattr(args, "pending_mode", "all")
    if pending_mode != "all":
        logger.info("Pending filter active: mode=%s", pending_mode)

    entry_ids = getattr(args, "entry_ids", None)
    if entry_ids:
        logger.info("Entry filter active: %d requested entry_id value(s)", len(_split_csv_values(entry_ids)))

    limit = getattr(args, "limit", None)
    if limit is not None:
        logger.info("Limit filter active: processing at most %d row(s)", max(0, limit))
    
    if len(pending_df) == 0:
        logger.info("No works to process after applying pending/entry/limit filters.")
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

def _run_direct_iiif_cli(
    args: argparse.Namespace,
    config: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Handle --iiif CLI invocations.

    Downloads one or more IIIF manifests directly, bypassing CSV loading
    and provider search.

    Args:
        args: Parsed CLI arguments (must have iiif_urls populated)
        config: Configuration dictionary
        logger: Logger instance
    """
    urls = args.iiif_urls or []
    name_stem = getattr(args, "name", None)
    dry_run = getattr(args, "dry_run", False)
    output_dir = getattr(args, "output_dir", "downloaded_works")

    succeeded = 0
    failed = 0

    for i, url in enumerate(urls, start=1):
        # Derive entry_id and per-item name
        if len(urls) == 1 and name_stem:
            entry_id = f"IIIF_{name_stem}"
            title = name_stem
            file_stem = name_stem
        else:
            entry_id = f"IIIF_{i:04d}"
            title = name_stem if name_stem else None
            file_stem = f"{name_stem}_{i:04d}" if name_stem else None

        logger.info("Processing IIIF manifest %d/%d: %s", i, len(urls), url)

        result = process_direct_iiif(
            manifest_url=url,
            output_dir=output_dir,
            entry_id=entry_id,
            title=title,
            file_stem=file_stem,
            dry_run=dry_run,
        )

        status = result.get("status", "")
        if status == "completed":
            succeeded += 1
            logger.info("Download completed for manifest %d/%d", i, len(urls))
        elif status == "dry_run":
            logger.info("Dry-run complete for manifest %d/%d", i, len(urls))
        else:
            failed += 1
            logger.warning(
                "Download failed for manifest %d/%d: %s",
                i, len(urls), result.get("error", "unknown"),
            )

    logger.info(
        "Direct IIIF batch complete: %d processed, %d succeeded, %d failed",
        len(urls), succeeded, failed,
    )

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

        if "--cli" not in sys.argv and _looks_like_cli_invocation(sys.argv[1:]):
            sys.argv.insert(1, "--cli")
        
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
