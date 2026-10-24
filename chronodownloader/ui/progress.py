"""Run screen of the guided UI: live progress, plan and summary tables.

:class:`RichProgress` implements :class:`~chronodownloader.events.RunEvents`
on a rich console. On a terminal it shows a bar counting the finished works
of the run, the work started last with its latest candidate, judgment or
download, and the statuses reached so far; warnings and one result line per
work are printed above it. On any other console it prints plain lines
only. :func:`plan_view` shows what a dry run would do and
:func:`summary_view` what a run did: the counts, each work's status, and the
candidates of a ``search`` or the files of a ``verify``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import fields
from types import TracebackType
from typing import Final, Self

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from chronodownloader import run as core
from chronodownloader.events import (
    CandidateFound,
    CandidateJudged,
    Downloaded,
    StatusChanged,
    WorkStarted,
)

__all__ = ["STYLES", "RichProgress", "counts_text", "plan_view", "summary_view"]

STYLES: Final[Mapping[str, str]] = {
    "complete": "green",
    "partial": "cyan",
    "failed": "red",
    "no_match": "yellow",
    "needs_review": "magenta",
    "deferred": "yellow",
    "blocked": "yellow",
}
"""The style of each work status."""

TITLE_WIDTH: Final = 60


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _short(title: str) -> str:
    return title if len(title) <= TITLE_WIDTH else title[: TITLE_WIDTH - 3] + "..."


def counts_text(counts: core.WorkCounts) -> str:
    """Return ``3 work(s): 2 complete, 1 failed``; zero counts are left out."""
    parts = [
        f"{getattr(counts, field.name)} {field.name}"
        for field in fields(counts)
        if field.name != "total" and getattr(counts, field.name)
    ]
    return f"{counts.total} work(s)" + (f": {', '.join(parts)}" if parts else "")


class RichProgress:
    """Show run events on a rich console; use as a context manager."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console if console is not None else Console(stderr=True)
        self._progress = Progress(
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=self.console,
            auto_refresh=False,
        )
        self._task: TaskID | None = None
        self._current = ""
        self._activity = ""
        self._tally: Counter[str] = Counter()
        self._finished: dict[str, StatusChanged] = {}
        self._live: Live | None = None

    @property
    def live(self) -> bool:
        """Whether the live display is running."""
        return self._live is not None

    @property
    def finished(self) -> Mapping[str, StatusChanged]:
        """The last status each work reached, by entry id, in finishing order."""
        return self._finished

    def __enter__(self) -> Self:
        if self.console.is_terminal and not self.console.is_dumb_terminal:
            self._live = Live(
                console=self.console,
                get_renderable=self._render,
                refresh_per_second=8,
                redirect_stdout=False,
                redirect_stderr=True,
                transient=False,
            )
            self._live.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._current = self._activity = ""
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _render(self) -> RenderableType:
        parts: list[RenderableType] = [self._progress.get_renderable()]
        if self._current:
            parts.append(Text(f"Current: {self._current}", overflow="ellipsis"))
        if self._activity:
            parts.append(Text(f"  {self._activity}", style="dim", overflow="ellipsis"))
        if self._tally:
            parts.append(Text(self._tally_line()))
        return Group(*parts)

    def _tally_line(self) -> str:
        return "Statuses: " + ", ".join(
            f"{count} {status}" for status, count in sorted(self._tally.items())
        )

    def _print(self, text: Text) -> None:
        self.console.print(text, soft_wrap=True)

    def _note(self, line: str) -> None:
        """Show *line* as the current activity, or print it without a display."""
        if self.live:
            self._activity = line
        else:
            self._print(Text(line))

    def work_started(self, event: WorkStarted) -> None:
        """Size the work counter and name the current work."""
        if self._task is None:
            self._task = self._progress.add_task("Works", total=event.total)
        else:
            self._progress.update(self._task, total=event.total)
        self._current = f"[{event.index}/{event.total}] {event.entry_id}: {event.title}"
        self._activity = ""
        if not self.live:
            self._print(Text(self._current))

    def candidate_found(self, event: CandidateFound) -> None:
        """Show the candidate."""
        self._note(
            f"{event.entry_id}: candidate {event.provider}:{event.item_id} "
            f"{_short(event.title)}"
        )

    def candidate_judged(self, event: CandidateJudged) -> None:
        """Show the decision and its scores."""
        self._note(
            f"{event.entry_id}: {event.decision} {event.provider}:{event.item_id} "
            f"(same_work {_number(event.same_work)}, text score "
            f"{event.text_score:.2f})"
        )

    def downloaded(self, event: Downloaded) -> None:
        """Show the files written."""
        self._note(
            f"{event.entry_id}: {len(event.files)} file(s) from "
            f"{event.provider}:{event.item_id}"
        )

    def status_changed(self, event: StatusChanged) -> None:
        """Count the finished work and print its result line."""
        self._finished.pop(event.entry_id, None)
        self._finished[event.entry_id] = event
        self._tally = Counter(change.status for change in self._finished.values())
        if self._task is not None:
            self._progress.update(self._task, completed=len(self._finished))
        line = Text(f"{event.entry_id}: ")
        line.append(event.status, style=STYLES.get(event.status, ""))
        if event.reason:
            line.append(f" ({event.reason})")
        self._print(line)

    def warning(self, message: str) -> None:
        """Print a warning above the display."""
        for line in message.splitlines() or [""]:
            self._print(Text(f"Warning: {line}", style="yellow"))


def _works_table(progress: RichProgress) -> Table:
    table = Table(title="Works", title_justify="left")
    table.add_column("Work")
    table.add_column("Status")
    table.add_column("Reason", overflow="fold")
    for event in progress.finished.values():
        table.add_row(
            Text(event.entry_id),
            Text(event.status, style=STYLES.get(event.status, "")),
            Text(event.reason or ""),
        )
    return table


def _search_table(rows: tuple[core.SearchRow, ...]) -> Table:
    table = Table(title="Candidates (* accepted)", title_justify="left")
    for header in ("Work", "Item", "Year", "Text", "Same work", "Relation"):
        table.add_column(header)
    table.add_column("Decision")
    table.add_column("Title", overflow="fold")
    for row in rows:
        mark = "*" if row.chosen else ""
        table.add_row(
            Text(row.entry_id),
            Text(f"{mark}{row.provider}:{row.item_id}"),
            "-" if row.year is None else str(row.year),
            f"{row.text_score:.1f}",
            _number(row.same_work),
            row.relation or "-",
            row.decision,
            Text(_short(row.title)),
        )
    return table


def _verify_view(report: core.VerifyReport) -> RenderableType:
    table = Table(title="Verified works", title_justify="left")
    headers = ("Work", "Status", "Files", "Missing", "Mismatched", "Page check")
    for header in (*headers, "Pages", "Title page", "Imprint year", "Page type"):
        table.add_column(header)
    table.add_column("Note", overflow="fold")
    lines: list[RenderableType] = [table]
    for work in report.works:
        check = work.pages
        answer = None if check is None else check.answer
        table.add_row(
            Text(work.entry_id),
            Text(work.status, style=STYLES.get(work.status, "")),
            str(work.files),
            str(len(work.missing)),
            str(len(work.mismatched)),
            "-" if check is None else check.outcome,
            "-" if check is None else str(check.pages),
            "-" if answer is None else _number(answer.title_page),
            "-" if answer is None else _number(answer.imprint_year_matches),
            "-" if answer is None else answer.title_page_type,
            Text("" if check is None else check.note or ""),
        )
        lines += [Text(f"{work.entry_id}: missing {path}") for path in work.missing]
        lines += [
            Text(f"{work.entry_id}: size differs {path}") for path in work.mismatched
        ]
        if work.downgraded:
            done = "would be set" if report.dry_run else "set"
            lines.append(Text(f"{work.entry_id}: {done} from complete to partial"))
    if report.dry_run:
        lines.append(Text("Dry run: no status was changed."))
    return Group(*lines)


def plan_view(run_plan: core.RunPlan) -> RenderableType:
    """Return what a dry run would do: the planned works or the report."""
    if isinstance(run_plan.report, core.VerifyReport):
        return _verify_view(run_plan.report)
    table = Table(
        title=f"Dry run: {counts_text(run_plan.counts)}", title_justify="left"
    )
    table.add_column("#", justify="right")
    table.add_column("Work")
    table.add_column("Status")
    table.add_column("Title", overflow="fold")
    for index, planned in enumerate(run_plan.works, start=1):
        table.add_row(
            str(index),
            Text(planned.work.entry_id),
            planned.status,
            Text(_short(planned.work.title)),
        )
    parts: list[RenderableType] = [table]
    target = run_plan.fetch
    if target is not None:
        parts.append(Text(f"Item: {target.provider}:{target.item_id}"))
    parts.append(Text("Nothing was requested or written."))
    return Group(*parts)


def summary_view(result: core.RunResult, progress: RichProgress) -> RenderableType:
    """Return the run summary: the counts, each work, the command's report."""
    parts: list[RenderableType] = []
    if progress.finished:
        parts.append(_works_table(progress))
    if result.rows:
        parts.append(_search_table(result.rows))
    if isinstance(result.report, core.VerifyReport):
        parts.append(_verify_view(result.report))
    parts.append(Text(f"Run summary: {counts_text(result.counts)}", style="bold"))
    return Group(*parts)
