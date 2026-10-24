"""The search command: search and judge without downloading.

The candidates are printed as a table, best first per work: on stdout, or
on stderr with ``--json``, whose stdout holds only the summary line.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.summary import Summary
from chronodownloader.run import RunResult, SearchRow
from chronodownloader.spec import resolve_search

__all__ = ["execute", "render_table"]

TITLE_WIDTH = 60
HEADER = ("work", "item", "year", "text", "same_work", "relation", "decision", "title")


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _cells(row: SearchRow) -> tuple[str, ...]:
    title = row.title if len(row.title) <= TITLE_WIDTH else row.title[:57] + "..."
    mark = "*" if row.chosen else ""
    return (
        row.entry_id,
        f"{mark}{row.provider}:{row.item_id}",
        "-" if row.year is None else str(row.year),
        f"{row.text_score:.1f}",
        _number(row.same_work),
        row.relation or "-",
        row.decision,
        title,
    )


def render_table(rows: Sequence[SearchRow]) -> str:
    """Return *rows* as an aligned text table; ``*`` marks the accepted item."""
    if not rows:
        return "No candidates.\n"
    lines = [HEADER, *(_cells(row) for row in rows)]
    widths = [max(len(line[index]) for line in lines) for index in range(len(HEADER))]
    return "".join(
        "  ".join(
            cell.ljust(width) for cell, width in zip(line, widths, strict=True)
        ).rstrip()
        + "\n"
        for line in lines
    )


def _print(result: RunResult, json: bool) -> None:
    stream = sys.stderr if json else sys.stdout
    try:
        stream.write(render_table(result.rows))
    except UnicodeEncodeError:
        text = render_table(result.rows)
        stream.write(text.encode("ascii", "replace").decode("ascii"))
    stream.flush()


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed search options and return the exit code."""
    return execute_command(args, summary, "search", resolve_search, report=_print)
