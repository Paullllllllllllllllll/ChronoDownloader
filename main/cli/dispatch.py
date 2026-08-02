"""CLI dispatch: configure logging, apply overrides, and route to the matching
subcommand.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from typing import Any

from main.orchestration import pipeline

from .commands.batch import run_batch_cli
from .commands.direct_iiif import run_direct_iiif_cli
from .commands.identifier import run_identifier_cli
from .commands.providers import list_providers
from .commands.search import run_search_cli
from .exit_codes import EXIT_OK, EXIT_USAGE
from .overrides import (
    _apply_provider_cli_overrides,
    _apply_runtime_config_overrides,
)


def _warn_ignored_selectors(args: argparse.Namespace, logger: logging.Logger) -> None:
    """Warn about selectors the dispatch precedence will silently discard.

    Purely advisory: precedence and exit codes are unchanged.
    """
    csv_file = getattr(args, "csv_file", None)
    ad_hoc_search = getattr(args, "search", None)
    search_active = bool(ad_hoc_search or getattr(args, "search_only", False))
    iiif_urls = getattr(args, "iiif_urls", None)
    identifier = getattr(args, "id", None)

    ignored: list[str] = []
    if search_active:
        if iiif_urls:
            ignored.append("--iiif")
        if identifier:
            ignored.append("--id")
        if ad_hoc_search and csv_file:
            ignored.append(f"the CSV file '{csv_file}'")
        winner = "--search" if ad_hoc_search else "--search-only"
    elif iiif_urls:
        if identifier:
            ignored.append("--id")
        if csv_file:
            ignored.append(f"the CSV file '{csv_file}'")
        winner = "--iiif"
    elif identifier:
        if csv_file:
            ignored.append(f"the CSV file '{csv_file}'")
        winner = "--id"
    else:
        winner = ""

    if ignored:
        logger.warning("%s takes precedence; ignoring %s.", winner, ", ".join(ignored))

    if getattr(args, "provider", None) and not identifier:
        logger.warning("--provider only applies to --id; ignoring it for this run.")


def run_cli(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Run the downloader in CLI mode.

    Configures logging, applies runtime overrides, loads providers, and
    dispatches to the appropriate subcommand handler (IIIF, identifier,
    list-providers, or CSV batch).

    Returns:
        A process exit code (see :mod:`main.cli.exit_codes`).
    """
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    # Any --json run reserves stdout for the machine-readable document, so the
    # log handler must start on stderr. Keying this on "json AND search" left
    # a plain --json batch run writing INFO lines ahead of its summary, which
    # broke json.loads on the captured stdout.
    search_requested = bool(
        getattr(args, "search", None) or getattr(args, "search_only", False)
    )
    json_output = bool(getattr(args, "json_summary", False))

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(stream=sys.stderr if json_output else sys.stdout)
        ],
    )

    try:
        logging.getLogger("urllib3").setLevel(logging.ERROR)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    except Exception:
        pass

    logger = logging.getLogger(__name__)

    if getattr(args, "list_providers", False):
        list_providers()
        return EXIT_OK

    # Resolve the effective config path once: an explicit --config wins,
    # then a pre-set CHRONO_CONFIG_PATH, then the default config.json.
    config_path = args.config or os.environ.get("CHRONO_CONFIG_PATH") or "config.json"
    with contextlib.suppress(Exception):
        os.environ["CHRONO_CONFIG_PATH"] = config_path

    config = _apply_runtime_config_overrides(args, config, logger)

    providers = pipeline.load_enabled_apis(config_path)
    providers = _apply_provider_cli_overrides(args, providers, logger)
    providers = pipeline.filter_enabled_providers_for_keys(providers)
    pipeline.ENABLED_APIS = providers

    # Direct-IIIF and --id downloads need no search provider (--id resolves
    # through the full registry and static manifest templates), so exempt
    # both from the empty-provider guard; the other subcommands still
    # require providers.
    if not providers and not args.iiif_urls and not getattr(args, "id", None):
        logger.warning(
            "No providers are enabled. Update %s to enable providers.",
            config_path,
        )
        return EXIT_USAGE

    _warn_ignored_selectors(args, logger)

    if search_requested:
        return run_search_cli(args, config, logger)

    if args.iiif_urls:
        return run_direct_iiif_cli(args, config, logger)

    if getattr(args, "id", None):
        return run_identifier_cli(args, config, logger)

    return run_batch_cli(args, config, logger)
