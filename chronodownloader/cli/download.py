"""The download command: search, judge and download the works of a CSV.

After a post-phase (``--decisions shadow``, ``--classify``) the gate checks
are printed as a table with the model and its usage, then the classification
as ``classify`` prints it: on stdout, or on stderr with ``--json``.
"""

from __future__ import annotations

import argparse

from chronodownloader.cli import classify
from chronodownloader.cli.command import execute_command
from chronodownloader.cli.console import render_table, write_report
from chronodownloader.cli.summary import Summary
from chronodownloader.run import DownloadReport, RunResult
from chronodownloader.spec import resolve_download

__all__ = ["execute", "render"]

HEADER = ("work", "band", "item", "outcome", "same_work", "relation", "note")


def render(report: DownloadReport) -> str:
    """Return the gate checks and the classification of *report*."""
    lines: list[str] = []
    gate = report.gate
    if gate.mode != "off":
        if gate.checks:
            rows = [
                (
                    check.entry_id,
                    check.band,
                    f"{check.provider}:{check.source_id}",
                    check.outcome,
                    "-" if check.same_work is None else f"{check.same_work:.2f}",
                    check.relation or "-",
                    check.note or "",
                )
                for check in gate.checks
            ]
            lines.append(render_table(HEADER, rows))
        else:
            lines.append("No gate checks.\n")
        lines.append(
            f"Gate ({gate.mode}, model {gate.model}): "
            f"{classify.usage_line(gate.usage)}.\n"
        )
    if report.classify is not None:
        lines.append(classify.render(report.classify))
    return "".join(lines)


def _print(result: RunResult, json: bool) -> None:
    if isinstance(result.report, DownloadReport):
        text = render(result.report)
        if text:
            write_report(text, json=json)


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed download options and return the exit code."""
    return execute_command(args, summary, "download", resolve_download, report=_print)
