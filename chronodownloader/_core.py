"""The types and store checks the run core shares between its modules.

:mod:`chronodownloader.run` re-exports :class:`PlanError`,
:class:`WorkCounts`, :class:`RunPlan`, :class:`RunResult` and the reports
of the listing and checking commands; the helper modules behind it import
them from here, which keeps the import graph acyclic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from chronodownloader.matching.answers import PageAnswer
from chronodownloader.matching.judge import MissingJudgeKey, judge_key
from chronodownloader.options.options import Source
from chronodownloader.output.store import (
    DATABASE_NAME,
    DropReport,
    StoreError,
    WorkInput,
    check_schema,
    read_work_status,
)
from chronodownloader.process.contract import ExitCode
from chronodownloader.providers.base import SearchRecord
from chronodownloader.settings import Settings
from chronodownloader.spec import CommandSpec

__all__ = [
    "CheckOutcome",
    "FetchTarget",
    "InspectReport",
    "InspectTarget",
    "Leftover",
    "PageCheck",
    "PlanError",
    "PlannedWork",
    "ProviderState",
    "ProvidersReport",
    "Report",
    "RunPlan",
    "RunResult",
    "SearchRow",
    "StatusReport",
    "StatusRow",
    "VerifiedWork",
    "VerifyReport",
    "WorkCounts",
    "check_judge_key",
    "check_store",
    "read_statuses",
]


class PlanError(Exception):
    """The run cannot be planned or carried out.

    *items_total* is the number of works the run would have processed, for
    the run summary; *exit_code* is the process exit code of the error
    (2 by default: a usage or configuration error).
    """

    exit_code: ExitCode = ExitCode.USAGE

    def __init__(
        self,
        message: str,
        *,
        items_total: int = 0,
        exit_code: ExitCode | None = None,
    ) -> None:
        super().__init__(message)
        self.items_total = items_total
        if exit_code is not None:
            self.exit_code = exit_code


def check_judge_key(mode: str, settings: Settings) -> None:
    """Refuse a run that needs the decision model without its key.

    Raises:
        PlanError: *mode* is not ``fuzzy`` and the key is unset or unusable.
    """
    if mode == "fuzzy":
        return
    try:
        judge_key(settings)
    except MissingJudgeKey as exc:
        raise PlanError(str(exc)) from exc


def check_store(root: Path) -> None:
    """Refuse an output root whose database the store does not know.

    Raises:
        PlanError: The database is versionless, newer or unreadable (exit 2).
    """
    try:
        check_schema(root / DATABASE_NAME)
    except StoreError as exc:
        raise PlanError(str(exc)) from exc


def read_statuses(
    root: Path | None, entry_ids: Sequence[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Return the ``work_status`` rows of *root* by entry id, never writing.

    No root, or a root without a store, gives no rows.

    Raises:
        PlanError: The store's database is refused (exit 2).
    """
    if root is None:
        return {}
    try:
        rows = read_work_status(root, entry_ids)
    except StoreError as exc:
        raise PlanError(str(exc)) from exc
    return {str(row["entry_id"]): row for row in rows}


@dataclass(frozen=True)
class WorkCounts:
    """The works of a run by outcome.

    *skipped* works were left out by their stored status, the ``--limit``,
    an ``unstored`` folder, or a size limit that stopped the run. *partial*
    works stopped short of complete, which is unfinished work like
    *failed*; *no_match*, *needs_review*, *deferred* and *blocked* works are
    not. Under ``search``, *accepted* counts the works with an accepted
    candidate (nothing is downloaded, so none is *complete*).
    """

    total: int = 0
    complete: int = 0
    partial: int = 0
    failed: int = 0
    no_match: int = 0
    skipped: int = 0
    needs_review: int = 0
    deferred: int = 0
    blocked: int = 0
    accepted: int = 0


@dataclass(frozen=True)
class PlannedWork:
    """A work the run takes: its input fields, folder name and status.

    *status* is the status that selected it: ``new`` for a work without a
    store entry or outcome, ``blocked`` for one another run held at
    planning time, else its stored status. *changed* marks a work whose
    matching fields in the input differ from the stored ones; it is searched
    and judged anew, as under ``--research``.
    """

    work: WorkInput
    folder: str
    status: str = "new"
    changed: bool = False


@dataclass(frozen=True)
class FetchTarget:
    """The item a ``fetch`` downloads and the work it belongs to.

    *provider* serves the item; *item_id* is its identifier, *url* the
    given URL (*manifest* when it names a IIIF manifest). *stored_status*
    is the work's stored status, None for a work without one.
    """

    planned: PlannedWork
    provider: str
    item_id: str
    url: str | None = None
    manifest: bool = False
    stored_status: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RunPlan:
    """What a run of *spec* would do under *settings*.

    *sources* holds the source of each spec field; *counts* the planned
    works; *works* the works the run takes, in input order; *warnings*
    what planning found worth reporting (unknown CSV columns, entry ids not
    in the input); *fetch* the item of a ``fetch``. *report* is what the
    plan already knows to report: the listing of ``status``, and what a dry
    run of ``inspect``, ``verify`` or ``drop`` would do; *targets* the items
    an ``inspect`` samples.
    """

    spec: CommandSpec
    settings: Settings
    sources: Mapping[str, Source] = field(default_factory=dict)
    counts: WorkCounts = WorkCounts()
    works: tuple[PlannedWork, ...] = ()
    warnings: tuple[str, ...] = ()
    fetch: FetchTarget | None = None
    report: Report | None = None
    targets: tuple[InspectTarget, ...] = ()


@dataclass(frozen=True)
class SearchRow:
    """One candidate of a ``search`` for the printed table.

    *decision* is the policy's verdict (``accept``, ``reject``,
    ``review``); *outcome* the work's (``accepted``, ``needs_review``,
    ``no_match``, ``deferred``, ``failed``); *chosen* marks the accepted
    candidate.
    """

    entry_id: str
    provider: str
    item_id: str
    title: str
    year: int | None
    text_score: float
    same_work: float | None
    relation: str | None
    decision: str
    outcome: str
    chosen: bool = False


type CheckOutcome = Literal[
    "checked", "sampled", "planned", "skipped", "failed", "deferred"
]
"""How a page check ended: ``checked`` (the model answered), ``sampled``
(pages sampled, but no work names the item, so nothing could be asked),
``planned`` (a dry run), ``skipped`` (the item offers no page images),
``failed`` and ``deferred`` (the decision model is unavailable)."""


@dataclass(frozen=True)
class InspectTarget:
    """An item ``inspect`` samples, and the work it is checked against.

    *record* is the stored candidate's record (credentials unmasked), or
    None for an item named by ``--provider``/``--id`` alone; *work* is the
    sought work, None when no stored work names the item; *folder* is where
    the pages go.
    """

    provider: str
    item_id: str
    folder: Path
    record: SearchRecord | None = None
    work: WorkInput | None = None


@dataclass(frozen=True)
class PageCheck:
    """The page check of one item.

    *entry_id* names the work the check asked about (None for an item no
    work names); *folder* holds the pages checked; *pages* counts them;
    *answer* is the model's answer when *outcome* is ``checked``; *note*
    says why a check was skipped or failed; *retry_after* (epoch s) is when
    a deferred check may be asked again.
    """

    provider: str
    item_id: str
    entry_id: str | None
    folder: Path | None
    outcome: CheckOutcome
    pages: int = 0
    answer: PageAnswer | None = None
    note: str | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class InspectReport:
    """The page checks of an ``inspect``, one per item."""

    checks: tuple[PageCheck, ...] = ()


@dataclass(frozen=True)
class VerifiedWork:
    """One work as ``verify`` found it.

    *status* is the work's status after the check; *files* the number of
    recorded files checked; *missing* the recorded files not on disk and
    *mismatched* those whose size differs from the record (paths relative
    to the work folder); *downgraded* is True when the store's
    reconciliation set the work from ``complete`` to ``partial``; *pages*
    the page check under ``--pages``.
    """

    entry_id: str
    folder: str
    status: str
    files: int
    missing: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()
    downgraded: bool = False
    pages: PageCheck | None = None

    @property
    def intact(self) -> bool:
        """Return True when every recorded file is on disk at its size."""
        return not self.missing and not self.mismatched


@dataclass(frozen=True)
class VerifyReport:
    """The works a ``verify`` checked; *dry_run* when nothing was changed."""

    works: tuple[VerifiedWork, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class StatusRow:
    """One work of a ``status`` listing.

    ``new`` works have no store entry or no outcome yet. *selected* is
    ``provider:item`` of the selected candidate; *retry_after* is ISO 8601.
    """

    entry_id: str
    title: str
    status: str
    folder: str
    reason: str | None = None
    retry_after: str | None = None
    selected: str | None = None
    files: int = 0
    pages_present: int | None = None
    pages_expected: int | None = None


@dataclass(frozen=True)
class Leftover:
    """An ``inspect`` scratch folder under ``<output>/_inspect/``.

    *name* is the folder's name (the entry id made safe for a path);
    *items* the sampled items' folders (``<provider>_<id>``); *stored*
    whether a work of the store has that folder name.
    """

    name: str
    path: Path
    items: tuple[str, ...]
    stored: bool


@dataclass(frozen=True)
class StatusReport:
    """A ``status`` listing: works, ``unstored`` folders, scratch leftovers."""

    works: tuple[StatusRow, ...] = ()
    unstored: tuple[str, ...] = ()
    leftovers: tuple[Leftover, ...] = ()

    @property
    def review(self) -> tuple[StatusRow, ...]:
        """Return the works that need review, in listing order."""
        return tuple(row for row in self.works if row.status == "needs_review")


@dataclass(frozen=True)
class ProviderState:
    """A provider's capabilities, key and coordination state.

    *api_key* names the environment variable of a required key (None: no
    key needed) and *key_set* whether it is set; *available* is False with
    a *note* when the adapter cannot be used as configured. *breaker* is
    ``closed``, ``open`` or ``half_open``, *breaker_until* (epoch s) when an
    open breaker turns half-open. The quota fields are None for a provider
    without a quota; *quota_reset_at* is None without an open window.
    """

    key: str
    iiif: bool
    quota: bool
    downloads: bool
    image_max_px: int | None
    api_key: str | None
    key_set: bool | None
    available: bool = True
    note: str | None = None
    breaker: str = "closed"
    failures: int = 0
    breaker_until: float | None = None
    breaker_cause: str | None = None
    quota_used: int | None = None
    quota_limit: int | None = None
    quota_reset_at: float | None = None

    @property
    def quota_remaining(self) -> int | None:
        """Return the units left in the quota window; None without a quota."""
        if self.quota_limit is None:
            return None
        return max(self.quota_limit - (self.quota_used or 0), 0)


@dataclass(frozen=True)
class ProvidersReport:
    """The 17 providers in the settings order."""

    providers: tuple[ProviderState, ...] = ()


type Report = InspectReport | VerifyReport | StatusReport | DropReport | ProvidersReport
"""What a command reports beyond its counts."""


@dataclass(frozen=True)
class RunResult:
    """The outcome of a run.

    *rows* holds the candidates of a ``search``, *report* the report of
    ``inspect``, ``verify``, ``status``, ``drop`` and ``providers``.
    """

    counts: WorkCounts
    rows: tuple[SearchRow, ...] = ()
    report: Report | None = None
