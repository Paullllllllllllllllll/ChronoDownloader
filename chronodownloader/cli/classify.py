"""The classify command: the page type of every stored page image.

The works are printed as a table with the pages asked and labelled and the
title page the store's ``title_page_picks`` view picks, then the model and
the decision usage: on stdout, or on stderr with ``--json``.
"""

from __future__ import annotations

import argparse

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.console import render_table, write_report
from chronodownloader.cli.summary import Summary
from chronodownloader.run import ClassifyReport, RunResult
from chronodownloader.spec import resolve_classify

__all__ = ["execute", "render"]

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
    "note",
)


def render(report: ClassifyReport) -> str:
    """Return *report* as a table followed by the model and its usage."""
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
            work.note or "",
        )
        for work in report.works
    ]
    lines = [render_table(HEADER, rows)]
    lines.append(
        f"Model {report.model} ({report.dialect}, profile {report.profile}), "
        f"fingerprint {report.fingerprint}.\n"
    )
    if report.dry_run:
        lines.append("Dry run: nothing was sent or written.\n")
    else:
        usage = report.usage
        cost = "" if usage.cost is None else f", cost {usage.cost:.4f} USD"
        lines.append(
            f"{usage.requests} request(s), {usage.input_tokens:,} input tokens "
            f"({usage.cached_tokens:,} cached), {usage.output_tokens:,} output "
            f"tokens{cost}.\n"
        )
    return "".join(lines)


def _print(result: RunResult, json: bool) -> None:
    if isinstance(result.report, ClassifyReport):
        write_report(render(result.report), json=json)


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed classify options and return the exit code."""
    return execute_command(args, summary, "classify", resolve_classify, report=_print)
