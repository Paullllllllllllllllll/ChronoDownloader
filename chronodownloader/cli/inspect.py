"""The inspect command: sample and check the first pages of candidates.

The page checks are printed as a table, one row per item: on stdout, or on
stderr with ``--json``. ``title_page`` and ``imprint_year`` are the model's
probabilities that one of the pages is the sought work's title page and
shows its year; ``page_type`` is the kind of page that best shows the title.
"""

from __future__ import annotations

import argparse

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.console import render_table, write_report
from chronodownloader.cli.summary import Summary
from chronodownloader.run import InspectReport, PageCheck, RunResult
from chronodownloader.spec import resolve_inspect

__all__ = ["check_cells", "execute", "render"]

HEADER = (
    "work",
    "item",
    "outcome",
    "pages",
    "title_page",
    "imprint_year",
    "page_type",
    "note",
)


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def check_cells(check: PageCheck) -> tuple[str, str, str, str, str]:
    """Return the outcome, page count, answer cells and note of *check*."""
    answer = check.answer
    if answer is None:
        cells = ("-", "-", "-")
    else:
        cells = (
            _number(answer.title_page),
            _number(answer.imprint_year_matches),
            answer.title_page_type,
        )
    return (check.outcome, str(check.pages), *cells)


def render(report: InspectReport) -> str:
    """Return the page checks of *report* as a table, with each folder."""
    if not report.checks:
        return "No candidates to inspect.\n"
    rows = [
        (
            check.entry_id or "-",
            f"{check.provider}:{check.item_id}",
            *check_cells(check),
            check.note or str(check.folder or ""),
        )
        for check in report.checks
    ]
    return render_table(HEADER, rows)


def _print(result: RunResult, json: bool) -> None:
    if isinstance(result.report, InspectReport):
        write_report(render(result.report), json=json)


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed inspect options and return the exit code."""
    return execute_command(args, summary, "inspect", resolve_inspect, report=_print)
