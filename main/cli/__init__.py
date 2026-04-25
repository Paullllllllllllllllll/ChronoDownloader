"""Command-line interface package.

Public entry points:

- :func:`main` -- CLI/interactive dispatcher; invoke via
  ``python -m main.cli`` or programmatically
- :func:`create_cli_parser` -- argparse factory used by tests and tools

Internal layout:

- :mod:`main.cli.parser` -- argparse definition
- :mod:`main.cli.overrides` -- override/filter helpers
- :mod:`main.cli.dispatch` -- run_cli() router
- :mod:`main.cli.entry` -- main() with quota/cleanup short-circuits
- :mod:`main.cli.commands` -- per-subcommand handlers
"""
from __future__ import annotations

from .dispatch import run_cli
from .entry import main
from .parser import create_cli_parser

__all__ = ["main", "run_cli", "create_cli_parser"]
