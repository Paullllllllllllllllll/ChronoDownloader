"""The download command: search, judge and download the works of a CSV."""

from __future__ import annotations

import argparse

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.summary import Summary
from chronodownloader.spec import resolve_download

__all__ = ["execute"]


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed download options and return the exit code."""
    return execute_command(args, summary, "download", resolve_download)
