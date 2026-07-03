"""``--iiif`` CLI handler: download directly from IIIF manifest URLs."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from main.orchestration.execution import process_direct_iiif

from ..exit_codes import EXIT_FAILURES, EXIT_OK, EXIT_USAGE


def run_direct_iiif_cli(
    args: argparse.Namespace,
    config: dict[str, Any],
    logger: logging.Logger,
) -> int:
    """Download one or more IIIF manifests, bypassing CSV and provider search.

    Returns:
        A process exit code (see :mod:`main.cli.exit_codes`).
    """
    urls = args.iiif_urls or []
    if not urls:
        logger.error("No IIIF manifest URLs provided.")
        return EXIT_USAGE

    names = getattr(args, "names", None) or []
    dry_run = getattr(args, "dry_run", False)
    output_dir = getattr(args, "output_dir", "downloaded_works")

    if len(names) > 1 and len(names) != len(urls):
        logger.error(
            "Got %d --name values for %d --iiif URLs; provide either one "
            "shared name, one name per URL, or none.",
            len(names),
            len(urls),
        )
        return EXIT_USAGE

    succeeded = 0
    failed = 0
    partial = 0

    for i, url in enumerate(urls, start=1):
        # Pair the Nth --name with the Nth --iiif URL; a single --name for
        # multiple URLs acts as a shared stem with a counter suffix.
        if len(names) == len(urls):
            name_stem = names[i - 1]
        else:
            name_stem = names[0] if names else None
        if name_stem and (len(urls) == 1 or len(names) == len(urls)):
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
        elif status == "partial":
            partial += 1
            logger.warning(
                "Download partial for manifest %d/%d: %s",
                i,
                len(urls),
                result.get("error", "incomplete"),
            )
        else:
            failed += 1
            logger.warning(
                "Download failed for manifest %d/%d: %s",
                i,
                len(urls),
                result.get("error", "unknown"),
            )

    logger.info(
        "Direct IIIF batch complete: %d processed, %d succeeded, %d partial, %d failed",
        len(urls),
        succeeded,
        partial,
        failed,
    )

    return EXIT_FAILURES if (failed > 0 or partial > 0) else EXIT_OK
