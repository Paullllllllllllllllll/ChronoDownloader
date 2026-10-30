"""The JSON summary line and the exit code of a finished run.

With ``--json`` every command but ``ui`` writes exactly one line to stdout:
``dry_run``, the ``command``, the work counts and the command's own fields
(:data:`COMMAND_FIELDS`). A run exits 1 when a work failed or stayed
partial, since both are unfinished work. Works left ``no_match``,
``needs_review``, ``deferred`` or ``blocked`` are counted but exit 0: the
search found nothing acceptable, or the work is open for review, a later
retry or another run.

``search`` counts the works with an accepted candidate as
``items_accepted``, not as ``items_complete``: nothing was downloaded.
The counts of the other commands: ``inspect`` counts items (``complete``:
checked; ``failed``: not sampled or the check failed; ``deferred``: the
decision model is unavailable; ``skipped``: no pages, or no work to ask
about); ``verify`` counts works (``complete``: every recorded file on disk
at its size; ``failed``: files missing or of another size, or a failed page
check; ``skipped``: no recorded files); ``classify`` counts works
(``complete``: every page image carries a current label; ``failed``: a page
could not be read or classified; ``deferred``: the decision model is
unavailable; ``blocked``: another run holds the work; ``skipped``: no page
images) and adds the pages, the decision usage, ``decision_cost`` null
where the dialect reports no cost, and the title-page readings with their
usage; ``status`` counts the listed works
by status (``items_new`` for works without an outcome); ``drop`` counts the
works dropped as ``complete``, those another run holds as ``blocked`` and
unknown entry ids as ``skipped``; ``providers`` counts the providers.
``status`` and ``providers`` are listings and exit 0.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, TextIO

from chronodownloader.process.contract import ExitCode, SummaryEmitter, exit_code_for
from chronodownloader.run import (
    ClassifyReport,
    DropReport,
    InspectReport,
    ProvidersReport,
    Report,
    StatusReport,
    VerifyReport,
    WorkCounts,
)
from chronodownloader.spec import COMMANDS

__all__ = [
    "COMMAND_FIELDS",
    "Summary",
    "count_fields",
    "exit_code",
    "report_fields",
    "summary_fields",
    "summary_for",
]

COMMAND_FIELDS: Mapping[str, Mapping[str, int | float | None]] = MappingProxyType(
    {
        "search": {"items_accepted": 0},
        "inspect": {"pages_sampled": 0},
        "verify": {
            "files_checked": 0,
            "files_missing": 0,
            "files_mismatched": 0,
            "pages_checked": 0,
        },
        "classify": {
            "pages_total": 0,
            "pages_asked": 0,
            "pages_current": 0,
            "pages_labelled": 0,
            "pages_stale": 0,
            "pages_failed": 0,
            "title_pages": 0,
            "decision_requests": 0,
            "decision_input_tokens": 0,
            "decision_output_tokens": 0,
            "decision_cached_tokens": 0,
            "decision_cache_write_tokens": 0,
            "decision_cost": None,
            "title_pages_transcribed": 0,
            "title_requests": 0,
            "title_input_tokens": 0,
            "title_output_tokens": 0,
            "title_cost": None,
        },
        "status": {"items_new": 0, "unstored_folders": 0, "inspect_leftovers": 0},
        "drop": {"files_deleted": 0, "files_kept": 0},
        "providers": {"breakers_open": 0, "keys_missing": 0},
    }
)
"""The fields a command adds to the summary, with their values on an exit
that reached no report."""


def count_fields(command: str, counts: WorkCounts) -> dict[str, int]:
    """Return the command's own summary fields from its *counts*.

    Only ``search`` has one: ``items_accepted``.
    """
    if command == "search":
        return {"items_accepted": counts.accepted}
    return {}


def _classify_fields(report: ClassifyReport) -> dict[str, int | float | None]:
    works, usage = report.works, report.usage
    return {
        "pages_total": sum(work.pages for work in works),
        "pages_asked": sum(work.asked for work in works),
        "pages_current": sum(work.current for work in works),
        "pages_labelled": sum(work.labelled for work in works),
        "pages_stale": sum(work.stale for work in works),
        "pages_failed": sum(work.failed for work in works),
        "title_pages": sum(1 for work in works if work.title_page is not None),
        "decision_requests": usage.requests,
        "decision_input_tokens": usage.input_tokens,
        "decision_output_tokens": usage.output_tokens,
        "decision_cached_tokens": usage.cached_tokens,
        "decision_cache_write_tokens": usage.cache_write_tokens,
        "decision_cost": usage.cost,
        "title_pages_transcribed": sum(1 for work in works if work.transcribed),
        "title_requests": report.title_usage.requests,
        "title_input_tokens": report.title_usage.input_tokens,
        "title_output_tokens": report.title_usage.output_tokens,
        "title_cost": report.title_usage.cost,
    }


def report_fields(report: Report | None) -> dict[str, int | float | None]:
    """Return the command's own summary fields from its *report*."""
    if isinstance(report, ClassifyReport):
        return _classify_fields(report)
    if isinstance(report, InspectReport):
        return {"pages_sampled": sum(check.pages for check in report.checks)}
    if isinstance(report, VerifyReport):
        works = report.works
        return {
            "files_checked": sum(work.files for work in works),
            "files_missing": sum(len(work.missing) for work in works),
            "files_mismatched": sum(len(work.mismatched) for work in works),
            "pages_checked": sum(
                1
                for work in works
                if work.pages is not None and work.pages.outcome == "checked"
            ),
        }
    if isinstance(report, StatusReport):
        return {
            "items_new": sum(1 for row in report.works if row.status == "new"),
            "unstored_folders": len(report.unstored),
            "inspect_leftovers": len(report.leftovers),
        }
    if isinstance(report, DropReport):
        return {
            "files_deleted": len(report.deleted_files),
            "files_kept": len(report.kept_files),
        }
    if isinstance(report, ProvidersReport):
        return {
            "breakers_open": sum(
                1 for state in report.providers if state.breaker != "closed"
            ),
            "keys_missing": sum(
                1 for state in report.providers if state.key_set is False
            ),
        }
    return {}


def summary_fields(counts: WorkCounts | None = None) -> dict[str, int]:
    """Return the count fields of the summary; None gives zero counts."""
    counts = counts or WorkCounts()
    return {
        "items_total": counts.total,
        "items_complete": counts.complete,
        "items_partial": counts.partial,
        "items_failed": counts.failed,
        "items_no_match": counts.no_match,
        "items_skipped": counts.skipped,
        "items_needs_review": counts.needs_review,
        "items_deferred": counts.deferred,
        "items_blocked": counts.blocked,
    }


def exit_code(counts: WorkCounts) -> ExitCode:
    """Return 1 when a work failed or stayed partial, else 0."""
    return exit_code_for(failed=counts.failed + counts.partial)


class Summary:
    """The JSON summary line of one command, written at most once.

    Without a stream nothing is written. :meth:`emit` writes the zero counts
    updated by its fields, so every exit path writes every key.
    """

    def __init__(
        self, stream: TextIO | None, command: str | None, *, dry_run: bool = False
    ) -> None:
        self._emitter = SummaryEmitter(stream, dry_run=dry_run)
        own = COMMAND_FIELDS.get(command or "", {})
        self._base: dict[str, Any] = {"command": command, **summary_fields(), **own}

    def emit(self, code: int, **fields: Any) -> int:
        """Write the line once and return *code*."""
        self._emitter.emit({**self._base, **fields})
        return int(code)


def summary_for(argv: Sequence[str]) -> Summary:
    """Return the summary of the command line *argv*, read before parsing.

    The command is the first word when it names one; ``ui`` writes no
    summary. ``--json`` and ``--dry-run`` are read from the words, which
    matches the parse because the parsers take no abbreviated flags.
    """
    words = list(argv)
    command = words[0] if words and words[0] in COMMANDS else None
    if command == "ui":
        return Summary(None, command)
    stream = sys.stdout if "--json" in words else None
    return Summary(stream, command, dry_run="--dry-run" in words)
