"""Argparse definition for the ChronoDownloader CLI.

Kept deliberately separate from dispatch logic so the full option surface
can be introspected (``-h`` / ``--help``) without importing the heavier
orchestration and state modules.
"""
from __future__ import annotations

import argparse


def create_cli_parser() -> argparse.ArgumentParser:
    """Create the argument parser for CLI mode."""
    parser = argparse.ArgumentParser(
        description="ChronoDownloader - Historical Document Download Tool (CLI Mode)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download works from a CSV file
  python -m main.cli sample_works.csv --output_dir my_downloads

  # Dry run (search only, no downloads)
  python -m main.cli works.csv --dry-run --log-level DEBUG

  # Use a different config file
  python -m main.cli works.csv --config config_small.json

  # Force interactive mode
  python -m main.cli --interactive

  # Download from a single IIIF manifest URL
  python -m main.cli --iiif "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k1511262r/manifest.json" --name Taillevent

  # Download multiple IIIF manifests
  python -m main.cli --iiif URL1 --iiif URL2 --output_dir my_downloads

  # Download by identifier with explicit provider
  python -m main.cli --id bsb11280551 --provider mdz --name Kochbuch

  # Download by identifier with auto-detection
  python -m main.cli --id bpt6k1511262r --output_dir my_downloads
        """,
    )

    parser.add_argument(
        "csv_file",
        nargs="?",
        default=None,
        help="Path to CSV file. Must contain 'short_title' or 'direct_link' (with required 'entry_id').",
    )

    parser.add_argument(
        "--output_dir",
        default="downloaded_works",
        help="Directory to save downloaded files.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run searches and create folders, but skip downloads.",
    )

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
        help="Output name stem for IIIF and identifier downloads (e.g. 'Taillevent_Viandier'). "
        "Used for folder and file naming.",
    )

    parser.add_argument(
        "--id",
        default=None,
        metavar="IDENTIFIER",
        help="Provider-specific item identifier (e.g., bsb11280551 for MDZ, "
        "bpt6k1511262r for Gallica). Use with --provider to specify the "
        "source, or let auto-detection determine it.",
    )

    parser.add_argument(
        "--provider",
        default=None,
        metavar="KEY",
        help="Provider key for --id lookup (e.g., mdz, bnf_gallica, "
        "internet_archive). See --list-providers for valid keys.",
    )

    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Force interactive mode regardless of config setting.",
    )

    parser.add_argument(
        "--cli",
        action="store_true",
        help="Force CLI mode regardless of config setting.",
    )

    parser.add_argument(
        "--quota-status",
        action="store_true",
        help="Display quota and deferred queue status, then exit.",
    )

    parser.add_argument(
        "--cleanup-deferred",
        action="store_true",
        help="Remove completed items from deferred queue, then exit.",
    )

    parser.add_argument(
        "--providers",
        action="append",
        default=None,
        metavar="KEYS",
        help="Comma-separated provider keys to use for this run (e.g. mdz,bnf_gallica,slub).",
    )

    parser.add_argument(
        "--enable-provider",
        action="append",
        default=None,
        metavar="KEYS",
        help="Provider key(s) to force-enable for this run (comma-separated or repeat flag).",
    )

    parser.add_argument(
        "--disable-provider",
        action="append",
        default=None,
        metavar="KEYS",
        help="Provider key(s) to force-disable for this run (comma-separated or repeat flag).",
    )

    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="List all available provider keys and exit.",
    )

    parser.add_argument(
        "--pending-mode",
        default="all",
        choices=["all", "new", "failed"],
        help="Which CSV rows to process: all pending+failed (default), only never-tried rows, or only failed rows.",
    )

    parser.add_argument(
        "--entry-ids",
        action="append",
        default=None,
        metavar="IDS",
        help="Restrict processing to specific entry_id values (comma-separated or repeat flag).",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N pending rows after filters are applied.",
    )

    parser.add_argument(
        "--resume-mode",
        choices=[
            "skip_completed",
            "reprocess_all",
            "skip_if_has_objects",
            "resume_from_csv",
        ],
        default=None,
        help="Override download.resume_mode for this run.",
    )

    parser.add_argument(
        "--selection-strategy",
        choices=["collect_and_select", "sequential_first_hit"],
        default=None,
        help="Override selection.strategy for this run.",
    )

    parser.add_argument(
        "--min-title-score",
        type=float,
        default=None,
        help="Override selection.min_title_score for this run.",
    )

    parser.add_argument(
        "--creator-weight",
        type=float,
        default=None,
        help="Override selection.creator_weight for this run (0.0-1.0).",
    )

    parser.add_argument(
        "--max-candidates-per-provider",
        type=int,
        default=None,
        help="Override selection.max_candidates_per_provider for this run.",
    )

    parser.add_argument(
        "--download-strategy",
        choices=["selected_only", "all"],
        default=None,
        help="Override selection.download_strategy for this run.",
    )

    parser.add_argument(
        "--keep-non-selected-metadata",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override selection.keep_non_selected_metadata for this run.",
    )

    parser.add_argument(
        "--prefer-pdf-over-images",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override download.prefer_pdf_over_images for this run.",
    )

    parser.add_argument(
        "--download-manifest-renderings",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override download.download_manifest_renderings for this run.",
    )

    parser.add_argument(
        "--max-renderings-per-manifest",
        type=int,
        default=None,
        help="Override download.max_renderings_per_manifest for this run.",
    )

    parser.add_argument(
        "--rendering-mime-whitelist",
        action="append",
        default=None,
        metavar="MIMES",
        help="Override download.rendering_mime_whitelist (comma-separated MIME values).",
    )

    parser.add_argument(
        "--overwrite-existing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override download.overwrite_existing for this run.",
    )

    parser.add_argument(
        "--include-metadata",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override download.include_metadata for this run.",
    )

    return parser
