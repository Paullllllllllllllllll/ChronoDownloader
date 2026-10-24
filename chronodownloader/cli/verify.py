"""The verify command: check the recorded files, and with ``--pages`` the
first pages, of the works in an output root.

The works are printed as a table, then the missing files and those of
another size: on stdout, or on stderr with ``--json``. The page check is
reported only; it never changes a status.
"""

from __future__ import annotations

import argparse

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.console import render_table, write_report
from chronodownloader.cli.inspect import check_cells
from chronodownloader.cli.summary import Summary
from chronodownloader.run import RunResult, VerifyReport
from chronodownloader.spec import resolve_verify

__all__ = ["execute", "render"]

HEADER = (
    "work",
    "status",
    "files",
    "missing",
    "mismatched",
    "page_check",
    "pages",
    "title_page",
    "imprint_year",
    "page_type",
    "note",
)

_NO_CHECK = ("-", "-", "-", "-", "-")


def render(report: VerifyReport) -> str:
    """Return *report* as a table followed by the files it found wanting."""
    if not report.works:
        return "No works to verify.\n"
    rows = [
        (
            work.entry_id,
            work.status,
            str(work.files),
            str(len(work.missing)),
            str(len(work.mismatched)),
            *(_NO_CHECK if work.pages is None else check_cells(work.pages)),
            "" if work.pages is None else work.pages.note or "",
        )
        for work in report.works
    ]
    lines = [render_table(HEADER, rows)]
    if report.dry_run:
        lines.append("Dry run: no status was changed.\n")
    for work in report.works:
        for path in work.missing:
            lines.append(f"{work.entry_id}: missing {work.folder}/{path}\n")
        for path in work.mismatched:
            lines.append(f"{work.entry_id}: size differs {work.folder}/{path}\n")
        if work.downgraded:
            done = "would be set" if report.dry_run else "set"
            lines.append(f"{work.entry_id}: {done} from complete to partial\n")
    return "".join(lines)


def _print(result: RunResult, json: bool) -> None:
    if isinstance(result.report, VerifyReport):
        write_report(render(result.report), json=json)


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed verify options and return the exit code."""
    return execute_command(args, summary, "verify", resolve_verify, report=_print)
