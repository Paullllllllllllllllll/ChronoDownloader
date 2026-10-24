"""The fetch command: download a known item without search."""

from __future__ import annotations

import argparse

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.summary import Summary
from chronodownloader.spec import resolve_fetch

__all__ = ["execute"]


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed fetch options and return the exit code."""
    return execute_command(args, summary, "fetch", resolve_fetch)
