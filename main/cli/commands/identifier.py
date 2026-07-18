"""``--id`` CLI handler: resolve a provider-specific identifier to a download."""

from __future__ import annotations

import argparse
import logging
import os
from typing import Any

from api.providers import PROVIDERS
from main.orchestration.execution import process_direct_iiif

from ..exit_codes import EXIT_FAILURES, EXIT_OK, EXIT_USAGE


def run_identifier_cli(
    args: argparse.Namespace,
    config: dict[str, Any],
    logger: logging.Logger,
) -> int:
    """Resolve ``--id`` to a manifest URL (or native download) and fetch the work.

    Returns:
        A process exit code (see :mod:`main.cli.exit_codes`).
    """
    from api.identifier_resolver import (
        download_by_native_provider,
        resolve_identifier,
    )

    identifier = args.id
    provider_key = getattr(args, "provider", None)
    names = getattr(args, "names", None) or []
    name_stem = names[0] if names else None
    dry_run = getattr(args, "dry_run", False)
    output_dir = getattr(args, "output_dir", "downloaded_works")

    if provider_key and provider_key not in PROVIDERS:
        logger.error(
            "Unknown provider key '%s'. Use --list-providers for valid keys.",
            provider_key,
        )
        return EXIT_USAGE

    try:
        candidates = resolve_identifier(identifier, provider_key)
    except KeyError as exc:
        logger.error("%s", exc)
        return EXIT_USAGE

    if not candidates:
        logger.error(
            "Could not resolve identifier '%s' to any provider. "
            "Use --provider KEY to specify the source library. "
            "Available providers: %s",
            identifier,
            ", ".join(sorted(PROVIDERS.keys())),
        )
        return EXIT_USAGE

    entry_id = f"ID_{identifier}"
    title = name_stem
    file_stem = name_stem

    for candidate in candidates:
        pkey = candidate.provider_key

        if candidate.use_native:
            work_dir = os.path.join(output_dir, f"{entry_id}_{pkey}")
            os.makedirs(work_dir, exist_ok=True)

            if dry_run:
                logger.info(
                    "Dry-run: would download '%s' via native %s download",
                    identifier,
                    pkey,
                )
                return EXIT_OK

            ok = download_by_native_provider(
                identifier,
                pkey,
                work_dir,
                title=title,
            )
            if ok:
                logger.info(
                    "Download completed via %s for identifier '%s'",
                    pkey,
                    identifier,
                )
                return EXIT_OK
            logger.warning("Native download failed via %s", pkey)
            continue

        for manifest_url in candidate.manifest_urls:
            result = process_direct_iiif(
                manifest_url=manifest_url,
                output_dir=output_dir,
                entry_id=entry_id,
                title=title,
                file_stem=file_stem,
                dry_run=dry_run,
            )

            status = result.get("status", "")
            if status in ("completed", "dry_run"):
                logger.info(
                    "%s via %s for identifier '%s'",
                    "Dry-run complete" if status == "dry_run" else "Download completed",
                    pkey,
                    identifier,
                )
                return EXIT_OK

            # A partial result means pages already landed in this work dir.
            # Stop here rather than trying the next candidate, which would
            # interleave pages from two sources; mirror --iiif's exit contract
            # (partial maps to failure).
            if status == "partial":
                logger.warning(
                    "Download partial via %s (%s): %s",
                    pkey,
                    manifest_url,
                    result.get("error", "incomplete"),
                )
                return EXIT_FAILURES

            logger.warning(
                "Failed via %s (%s): %s",
                pkey,
                manifest_url,
                result.get("error", "unknown"),
            )

    logger.error("All provider candidates failed for identifier '%s'", identifier)
    return EXIT_FAILURES
