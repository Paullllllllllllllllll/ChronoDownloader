"""The status command: list the works of an output root.

The listing (works, review queue, ``unstored`` folders, ``inspect`` scratch
leftovers) goes to stdout, or to stderr with ``--json``. The command reads
only and exits 0.
"""

from __future__ import annotations

import argparse

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.console import render_table, write_report
from chronodownloader.cli.summary import Summary
from chronodownloader.run import RunResult, StatusReport, StatusRow
from chronodownloader.spec import resolve_status

__all__ = ["execute", "render"]

HEADER = ("work", "status", "files", "pages", "selected", "retry_after", "reason")


def _pages(row: StatusRow) -> str:
    if row.pages_expected is None:
        return "-"
    return f"{row.pages_present or 0}/{row.pages_expected}"


def render(report: StatusReport) -> str:
    """Return the listing of *report*."""
    lines: list[str] = []
    if report.works:
        rows = [
            (
                row.entry_id,
                row.status,
                str(row.files),
                _pages(row),
                row.selected or "-",
                row.retry_after or "-",
                row.reason or "",
            )
            for row in report.works
        ]
        lines.append(render_table(HEADER, rows))
    else:
        lines.append("No works.\n")
    review = report.review
    if review:
        lines.append(f"\nReview queue ({len(review)}):\n")
        lines += [f"  {row.entry_id}: {row.reason or '-'}\n" for row in review]
    if report.unstored:
        lines.append(f"\nUnstored folders ({len(report.unstored)}):\n")
        lines += [f"  {name}\n" for name in report.unstored]
    if report.leftovers:
        lines.append(f"\nInspect leftovers ({len(report.leftovers)}):\n")
        for leftover in report.leftovers:
            where = "" if leftover.stored else " (no work in the store)"
            items = ", ".join(leftover.items) or "-"
            lines.append(f"  {leftover.name}{where}: {items}\n")
    return "".join(lines)


def _print(result: RunResult, json: bool) -> None:
    if isinstance(result.report, StatusReport):
        write_report(render(result.report), json=json)


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed status options and return the exit code."""
    return execute_command(
        args, summary, "status", resolve_status, report=_print, listing=True
    )
