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
from .exit_codes import EXIT_OK, EXIT_USAGE
from .overrides import (
    _apply_provider_cli_overrides,
    _apply_runtime_config_overrides,
)


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

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler(stream=sys.stdout)],
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

    with contextlib.suppress(Exception):
        os.environ["CHRONO_CONFIG_PATH"] = args.config

    config = _apply_runtime_config_overrides(args, config, logger)

    providers = pipeline.load_enabled_apis(args.config)
    providers = _apply_provider_cli_overrides(args, providers, logger)
    providers = pipeline.filter_enabled_providers_for_keys(providers)
    pipeline.ENABLED_APIS = providers

    if not providers:
        logger.warning(
            "No providers are enabled. Update %s to enable providers.",
            args.config,
        )
        return EXIT_USAGE

    if args.iiif_urls:
        return run_direct_iiif_cli(args, config, logger)

    if getattr(args, "id", None):
        return run_identifier_cli(args, config, logger)

    return run_batch_cli(args, config, logger)
