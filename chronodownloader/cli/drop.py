"""The drop command: delete works from an output root's store and disk.

What was dropped, refused and kept goes to stdout, or to stderr with
``--json``; ``--dry-run`` lists the same and changes nothing.
"""

from __future__ import annotations

import argparse

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.console import write_report
from chronodownloader.cli.summary import Summary
from chronodownloader.run import DropReport, RunResult
from chronodownloader.spec import resolve_drop

__all__ = ["execute", "render"]


def render(report: DropReport) -> str:
    """Return what *report* dropped, refused, deleted and kept."""
    verb = "Would drop" if report.dry_run else "Dropped"
    lines = [f"{verb} {len(report.dropped)} work(s)"]
    lines[0] += f": {', '.join(report.dropped)}\n" if report.dropped else ".\n"
    for refusal in report.refused:
        lines.append(f"Refused {refusal.entry_id}: {refusal.reason}\n")
    deleted = "would be deleted" if report.dry_run else "deleted"
    lines.append(f"Recorded files {deleted}: {len(report.deleted_files)}\n")
    if report.kept_files:
        lines.append(
            f"Files no record names, kept ({len(report.kept_files)}):\n"
            + "".join(f"  {path}\n" for path in report.kept_files)
        )
    return "".join(lines)


def _print(result: RunResult, json: bool) -> None:
    if isinstance(result.report, DropReport):
        write_report(render(result.report), json=json)


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed drop options and return the exit code."""
    return execute_command(args, summary, "drop", resolve_drop, report=_print)
