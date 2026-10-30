"""The classify command: the page type of every stored page image.

The works are printed as a table with the pages asked and labelled, the
title page the store's ``title_page_picks`` view picks and the title the chat
model read on it, then the models and their usage: on stdout, or on stderr
with ``--json``.
"""

from __future__ import annotations

import argparse

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.console import render_table, write_report
from chronodownloader.cli.summary import Summary
from chronodownloader.run import ClassifyReport, DecisionTotals, RunResult
from chronodownloader.spec import resolve_classify

__all__ = ["execute", "render", "usage_line"]

HEADER = (
    "work",
    "outcome",
    "pages",
    "asked",
    "current",
    "labelled",
    "stale",
    "failed",
    "title_page",
    "p",
    "title",
    "note",
)

TITLE_WIDTH = 40
"""The characters of a read title the table shows."""


def _title(title: str | None) -> str:
    if title is None:
        return "-"
    return title if len(title) <= TITLE_WIDTH else title[: TITLE_WIDTH - 3] + "..."


def usage_line(usage: DecisionTotals) -> str:
    """Return *usage* as one line: requests, tokens and the cost if known."""
    cost = "" if usage.cost is None else f", cost {usage.cost:.4f} USD"
    return (
        f"{usage.requests} request(s), {usage.input_tokens:,} input tokens "
        f"({usage.cached_tokens:,} cached), {usage.output_tokens:,} output "
        f"tokens{cost}"
    )


def render(report: ClassifyReport) -> str:
    """Return *report* as a table followed by the models and their usage."""
    if not report.works:
        return "No works to classify.\n"
    rows = [
        (
            work.entry_id,
            work.outcome,
            str(work.pages),
            str(work.asked),
            str(work.current),
            str(work.labelled),
            str(work.stale),
            str(work.failed),
            work.title_page or "-",
            "-" if work.probability is None else f"{work.probability:.2f}",
            _title(work.title),
            work.note or "",
        )
        for work in report.works
    ]
    lines = [render_table(HEADER, rows)]
    lines.append(
        f"Model {report.model} ({report.dialect}, profile {report.profile}), "
        f"fingerprint {report.fingerprint}.\n"
    )
    title_model = report.title_model or "off"
    if report.dry_run:
        lines.append(f"Title pages: {title_model}.\n")
        lines.append("Dry run: nothing was sent or written.\n")
    else:
        lines.append(f"Page types: {usage_line(report.usage)}.\n")
        lines.append(
            f"Title pages ({title_model}): {usage_line(report.title_usage)}.\n"
        )
    return "".join(lines)


def _print(result: RunResult, json: bool) -> None:
    if isinstance(result.report, ClassifyReport):
        write_report(render(result.report), json=json)


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed classify options and return the exit code."""
    return execute_command(args, summary, "classify", resolve_classify, report=_print)
