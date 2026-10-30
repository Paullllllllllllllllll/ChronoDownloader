"""The types and store checks the run core shares between its modules.

:mod:`chronodownloader.run` re-exports :class:`PlanError`,
:class:`WorkCounts`, :class:`RunPlan`, :class:`RunResult` and the reports
of the listing and checking commands; the helper modules behind it import
them from here, which keeps the import graph acyclic.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from chronodownloader.matching.answers import PageAnswer
from chronodownloader.matching.judge import MissingJudgeKey, judge_key
from chronodownloader.options.options import Source
from chronodownloader.output.index import write_index
from chronodownloader.output.store import (
    DATABASE_NAME,
    DropReport,
    Store,
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
    "ClassifiedWork",
    "ClassifyOutcome",
    "ClassifyReport",
    "DecisionTotals",
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
    "close_store",
    "open_store",
    "read_statuses",
    "tool_version",
]

logger = logging.getLogger(__name__)


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


def tool_version() -> str | None:
    """Return the installed chronodownloader version, or None."""
    try:
        return version("chronodownloader")
    except PackageNotFoundError:
        return None


def open_store(root: Path, clock: Callable[[], float]) -> Store:
    """Open the store of *root* for a run.

    Raises:
        PlanError: The store's database is refused (exit 2).
    """
    try:
        return Store(root, clock=clock)
    except StoreError as exc:
        raise PlanError(str(exc)) from exc


def close_store(store: Store, exit_code: int, *, settle: bool) -> None:
    """Record the run's end, close the store and render ``index.csv``.

    Without *settle* the released leases keep their staging folders.
    """
    try:
        if store.run_id is not None:
            store.end_run(exit_code)
    except sqlite3.Error as exc:
        logger.warning("Could not record the run end in %s: %s", store.path, exc)
    store.close(settle=settle)
    try:
        write_index(store.root)
    except (StoreError, sqlite3.Error, OSError) as exc:
        logger.warning("index.csv not written in %s: %s", store.root, exc)


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


type ClassifyOutcome = Literal[
    "planned", "complete", "failed", "deferred", "blocked", "skipped"
]
"""How a work's classification ended: ``planned`` (a dry run), ``complete``
(every page image carries a current label), ``failed`` (a page could not be
read or classified), ``deferred`` (the decision model is unavailable),
``blocked`` (another run holds the work) or ``skipped`` (no page images)."""


@dataclass(frozen=True)
class ClassifiedWork:
    """One work as ``classify`` found and left it.

    *pages* counts its page images; *asked* the pages sent to the model (or
    to be sent, in a dry run); *current* those whose label was current and
    not asked again; *labelled* the labels written; *stale* the labels
    dropped because the page's file changed meanwhile; *failed* the pages
    not classified. *title_page* is the path the ``title_page_picks`` rule
    picks (None: no pick) with its *probability*; *labelled_leading* counts
    the current labels among the leading pages that rule reads.
    *transcribed* marks a title-page reading written in this run; *title* is
    the title of the work's current reading (a model reading, not catalog
    data), None without one.
    """

    entry_id: str
    folder: str
    outcome: ClassifyOutcome
    pages: int = 0
    asked: int = 0
    current: int = 0
    labelled: int = 0
    stale: int = 0
    failed: int = 0
    title_page: str | None = None
    probability: float | None = None
    labelled_leading: int = 0
    transcribed: bool = False
    title: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class DecisionTotals:
    """The decision requests of a run and their summed token usage.

    *cost* is the billed USD sum where the dialect reports one, else None.
    """

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float | None = None


@dataclass(frozen=True)
class ClassifyReport:
    """The works of a ``classify`` with the model asked and the usage.

    *fingerprint* is the request fingerprint the labels carry; *title_model*
    the chat model that reads title pages (None: off), *title_usage* its
    requests; *dry_run* marks a plan that sent and wrote nothing.
    """

    works: tuple[ClassifiedWork, ...] = ()
    model: str = ""
    dialect: str = ""
    endpoint: str = ""
    profile: str = ""
    fingerprint: str = ""
    usage: DecisionTotals = DecisionTotals()
    title_model: str | None = None
    title_usage: DecisionTotals = DecisionTotals()
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


type Report = (
    InspectReport
    | VerifyReport
    | ClassifyReport
    | StatusReport
    | DropReport
    | ProvidersReport
)
"""What a command reports beyond its counts."""


@dataclass(frozen=True)
class RunResult:
    """The outcome of a run.

    *rows* holds the candidates of a ``search``, *report* the report of
    ``inspect``, ``verify``, ``classify``, ``status``, ``drop`` and
    ``providers``.
    """

    counts: WorkCounts
    rows: tuple[SearchRow, ...] = ()
    report: Report | None = None
