"""The run loop of ``download``, ``search`` and ``fetch``.

The loop runs on the run's main thread. It keeps at most ``--concurrency``
works in flight; for each it

1. leases the work in the store (another run's lease: ``blocked``, counted,
   the run goes on) and reads its stored candidates; the acquisition
   re-checks the stored status against the run's selection
   (:attr:`MatchOptions.reselect`), and a work another run finished since
   planning is counted as skipped without a write;
2. hands matching to a worker (:func:`~chronodownloader._worker.match_work`),
   then records the candidates, their text scores and new judgments, and
   decides the work (:func:`~chronodownloader.matching.policy.decide_work`):
   ``no_match``, ``needs_review`` with its reason, ``deferred`` with
   ``retry_after`` after a judge outage, ``failed`` when every search failed
   or the judge refused;
3. for an accepted work of a download, prepares the lease's staging folder,
   hands the download to a worker
   (:func:`~chronodownloader._worker.download_item`), then promotes the
   staged files in the lease-checked commit and records the status
   (``complete``, ``partial`` or ``failed``) with the selected candidate.
   A download that yields no files (failed, or metadata-only) is recorded
   as an attempt and the next acceptable candidate in preference order is
   downloaded (same decision group, a downloading provider); the work ends
   with the first that yields files, else ``failed`` (``needs_review``
   with ``metadata_only`` when every candidate was metadata-only) with
   every attempt's reason;
4. releases the lease.

Every store write and every run event happens here, between network calls
and never inside one (single-writer rule). A write refused because the
lease passed to another run counts the work as blocked. Without a store
(``search`` without ``--output``) nothing is written and the candidates are
collected for the printed table.

A ``search`` that accepts a candidate clears the work's stored outcome and
selects the candidate, so that the next ``download`` takes the work as
``new`` and reuses the judgments; the run counts it as ``accepted``.
"""

from __future__ import annotations

import collections
import contextlib
import functools
import logging
import shutil
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from chronodownloader.events import (
    CandidateFound,
    CandidateJudged,
    Downloaded,
    RunEvents,
    StatusChanged,
    WorkStarted,
    WorkStatus,
)
from chronodownloader.matching import policy
from chronodownloader.net.credentials import redact_text
from chronodownloader.output.paths import inspect_dir, object_path
from chronodownloader.output.store import (
    CandidateInput,
    FileRow,
    FolderConflictError,
    Lease,
    LeaseLostError,
    NotSelectedError,
    Promotion,
    Store,
    StoredStatus,
    bounded_error,
)
from chronodownloader.process.heartbeat import Heartbeat, RateEta
from chronodownloader.providers.base import (
    DownloadFormat,
    DownloadRequest,
    DownloadResult,
    ProviderError,
    SearchRecord,
    page_limit,
)

from ._budget import SizeBudget
from ._core import FetchTarget, PlannedWork, SearchRow, WorkCounts
from ._runtime import Services
from ._worker import (
    DownloadOutcome,
    Match,
    MatchJob,
    Scored,
    download_item,
    judgment_of,
    link_record,
    match_work,
)

__all__ = ["DownloadOptions", "Tally", "WorkLoop", "describe_result", "fetch_item"]

logger = logging.getLogger(__name__)

POLL_S = 0.25
"""Seconds the loop waits for a worker before it looks again (Ctrl+C)."""

UNIT_LABEL = "work"
"""The heartbeat's unit."""

_NONE_OFFERED: dict[str, str] = {"images": "no page images", "pdf": "no rendering"}


@dataclass
class Tally:
    """The works of a run by outcome, counted as they finish."""

    complete: int = 0
    partial: int = 0
    failed: int = 0
    no_match: int = 0
    needs_review: int = 0
    deferred: int = 0
    blocked: int = 0
    skipped: int = 0
    accepted: int = 0
    rows: list[SearchRow] = field(default_factory=list)

    def add(self, status: str) -> None:
        """Count one work with *status*."""
        setattr(self, status, getattr(self, status) + 1)

    @property
    def finished(self) -> int:
        """Return the number of works counted so far."""
        return (
            self.accepted
            + self.complete
            + self.partial
            + self.failed
            + self.no_match
            + self.needs_review
            + self.deferred
            + self.blocked
        )

    def counts(self, total: int, skipped: int) -> WorkCounts:
        """Return the counts with *total* works and *skipped* planned skips."""
        return WorkCounts(
            total=total,
            complete=self.complete,
            partial=self.partial,
            failed=self.failed,
            no_match=self.no_match,
            skipped=skipped + self.skipped,
            needs_review=self.needs_review,
            deferred=self.deferred,
            blocked=self.blocked,
            accepted=self.accepted,
        )


@dataclass(frozen=True)
class DownloadOptions:
    """How a run downloads: format, page limit, metadata, force, budget."""

    format: DownloadFormat
    max_pages: int
    include_metadata: bool
    force: bool
    budget: SizeBudget


type Reselect = Callable[[PlannedWork, Mapping[str, Any] | None], PlannedWork | None]
"""The selection re-check at lease time: takes a planned work and its stored
``work_status`` row read inside the lease acquisition, and returns the work
as the run takes it now, or None when it no longer fits the selection."""


@dataclass(frozen=True)
class MatchOptions:
    """How a run matches: mode, edition policy, providers, research, and the
    selection re-check at lease time (None: none)."""

    mode: policy.Mode
    edition_policy: policy.EditionPolicy
    providers: tuple[str, ...]
    max_candidates: int
    search_timeout: float
    research: bool
    reselect: Reselect | None = None


type Step = Literal["match", "download"]


@dataclass
class _Active:
    """A work in flight: its lease, step and chosen candidate.

    *fallbacks* are the further acceptable candidates (with their stored
    ids) a download tries in turn when the chosen one yields no files;
    *tried* holds each finished attempt's item and reason.
    """

    planned: PlannedWork
    index: int
    lease: Lease | None
    step: Step = "match"
    chosen: Scored | None = None
    candidate_id: int | None = None
    fetch: FetchTarget | None = None
    record: SearchRecord | None = None
    fallbacks: list[tuple[Scored, int | None]] = field(default_factory=list)
    tried: list[tuple[StoredStatus, str]] = field(default_factory=list)


def describe_result(result: DownloadResult) -> str | None:
    """Return why a download is short of complete, or None when complete.

    Each part short of complete says how many items are missing and their
    causes, or that the item offers none; a stopped download says why.
    """
    if result.complete:
        return None
    notes: list[str] = []
    for part in result.parts:
        if not part.expected:
            notes.append(f"{part.part}: the item offers {_NONE_OFFERED[part.part]}")
            continue
        missing = part.missing
        if not missing:
            continue
        causes = collections.Counter(part.causes.values())
        detail = ", ".join(f"{cause} {n}" for cause, n in sorted(causes.items()))
        note = f"{part.part}: {len(missing)} of {len(part.expected)} missing"
        notes.append(f"{note} ({detail})" if detail else note)
    if not result.parts:
        notes.append("the item offers nothing of the requested format")
    if result.stopped is not None:
        notes.append(f"stopped: {result.stopped}")
    return "; ".join(notes) or "incomplete"


def _bounded(reason: str | None) -> str | None:
    """Return *reason* masked and cut to the store's error length; None when
    empty."""
    return bounded_error(redact_text(reason)) if reason else None


def _outcome(result: DownloadResult) -> StoredStatus:
    if result.complete:
        return "complete"
    if any(part.present for part in result.parts):
        return "partial"
    return "failed"


def fetch_item(
    services: Services,
    target: FetchTarget,
    *,
    staging_dir: Path,
    request: Callable[[Any], DownloadRequest],
) -> tuple[SearchRecord, DownloadOutcome]:
    """Download a ``fetch`` item (worker side); return its record and outcome.

    A ``--url`` is read for the record's metadata first (:func:`link_record`:
    the IIIF manifest it names, or the manifest of the item an item page
    names) and downloaded from that manifest. An item page whose provider
    offers no manifest for it, and a ``--provider``/``--id`` item, are
    recorded by the identifier and downloaded through the provider.
    """
    bare = SearchRecord(target.provider, target.item_id, "", item_url=target.url)
    item: SearchRecord | str = target.item_id
    if target.url is not None:
        linked = link_record(services, target.url)
        if isinstance(linked, SearchRecord):
            bare = item = linked
        elif target.manifest:
            failed = DownloadOutcome(
                target.provider,
                target.item_id,
                services.wiring.clock(),
                error=linked.removeprefix("link: "),
            )
            return bare, failed
        else:
            logger.info("%s; downloading by its id", linked.removeprefix("link: "))
    outcome = download_item(
        services,
        item,
        provider=target.provider,
        origin="fetch",
        staging_dir=staging_dir,
        request=request,
    )
    return bare, outcome


class WorkLoop:
    """The main-thread loop over the works of one run.

    *store* is None for a ``search`` without ``--output``; *download* None
    for a ``search``. *command* names the run in the events and the store.
    """

    def __init__(
        self,
        services: Services,
        store: Store | None,
        events: RunEvents,
        *,
        command: str,
        matching: MatchOptions | None,
        download: DownloadOptions | None,
        concurrency: int,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self.services = services
        self.store = store
        self.events = events
        self.command = command
        self.matching = matching
        self.download = download
        self.concurrency = max(1, concurrency)
        self.heartbeat = heartbeat
        self.tally = Tally()
        self._eta = RateEta()
        self._skip_keys: set[str] = set()
        self._total = 0
        self._pending: dict[Future[Any], _Active] = {}

    # -- the loop ------------------------------------------------------------

    def run(self, works: Sequence[PlannedWork]) -> Tally:
        """Process *works*; return the tally. Ctrl+C propagates."""
        self._total = len(works)
        queue = collections.deque(enumerate(works, start=1))
        self._beat(force=True)
        while queue or self._pending:
            while queue and len(self._pending) < self.concurrency:
                if self._stopped_by_budget(len(queue)):
                    queue.clear()
                    break
                index, planned = queue.popleft()
                self._start(index, planned)
            if not self._pending:
                continue
            done, _ = wait(list(self._pending), POLL_S, FIRST_COMPLETED)
            for future in done:
                active = self._pending.pop(future)
                self._advance(active, future)
        self._beat(force=True)
        return self.tally

    def run_fetch(self, target: FetchTarget) -> Tally:
        """Download the item of a ``fetch``; return the tally."""
        self._total = 1
        self._beat(force=True)
        active = self._lease(1, target.planned)
        if active is not None:
            active.fetch = target
            try:
                self._submit_download(active, target.provider, target.item_id)
            except LeaseLostError as exc:
                self._lost(active, exc)
            while self._pending:
                done, _ = wait(list(self._pending), POLL_S, FIRST_COMPLETED)
                for future in done:
                    item = self._pending.pop(future)
                    self._advance(item, future)
        self._beat(force=True)
        return self.tally

    def _stopped_by_budget(self, waiting: int) -> bool:
        if self.download is None or not self.download.budget.stopped:
            return False
        self.tally.skipped += waiting
        self.events.warning(
            f"A run size limit was reached (--on-exceed stop); {waiting} "
            "work(s) were not started."
        )
        return True

    def _advance(self, active: _Active, future: Future[Any]) -> None:
        try:
            result = future.result()
        # The per-work crash boundary: whatever a worker raised fails this
        # work alone (its staging discarded, its lease released) and the run
        # goes on; KeyboardInterrupt is no Exception and still propagates.
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: worker failed", active.planned.work.entry_id)
            self._crashed(active, exc)
            return
        try:
            if active.fetch is not None:
                record, outcome = result
                active.record = record
                self._on_download(active, outcome)
            elif active.step == "match":
                self._on_match(active, result)
            else:
                self._on_download(active, result)
        except LeaseLostError as exc:
            self._lost(active, exc)

    # -- start -----------------------------------------------------------------

    def _lease(self, index: int, planned: PlannedWork) -> _Active | None:
        work = planned.work
        self.events.work_started(
            WorkStarted(work.entry_id, work.title, index, self._total)
        )
        if self.store is None:
            return _Active(planned, index, None)
        taken = planned
        reselect = None if self.matching is None else self.matching.reselect

        def select(row: Mapping[str, Any] | None) -> bool:
            nonlocal taken
            assert reselect is not None
            again = reselect(planned, row)
            if again is not None:
                taken = again
            return again is not None

        try:
            lease = self.store.acquire(
                work, planned.folder, select=None if reselect is None else select
            )
        except FolderConflictError as exc:
            self.events.warning(str(exc))
            self._count(planned, "failed", str(exc))
            return None
        except NotSelectedError as exc:
            stored = exc.row.get("stored_status") or "new"
            self.events.warning(
                f"{work.entry_id}: skipped; another run left it {stored} since "
                "this run was planned"
            )
            self.tally.skipped += 1
            self._beat()
            return None
        if lease is None:
            self._count(planned, "blocked", "leased by another run")
            return None
        return _Active(taken, index, lease)

    def _start(self, index: int, planned: PlannedWork) -> None:
        active = self._lease(index, planned)
        if active is None:
            return
        assert self.matching is not None
        try:
            stored = (
                ()
                if self.store is None or self._research(active)
                else tuple(self.store.candidates(planned.work.entry_id))
            )
        except LeaseLostError as exc:
            self._lost(active, exc)
            return
        job = MatchJob(
            work=planned.work,
            stored=stored,
            mode=self.matching.mode,
            providers=self.matching.providers,
            max_candidates=self.matching.max_candidates,
            search_timeout=self.matching.search_timeout,
            skip=frozenset(self._skip_keys),
        )
        future = self.services.pool.submit(
            functools.partial(match_work, self.services, job)
        )
        self._pending[future] = active

    # -- matching ----------------------------------------------------------------

    def _research(self, active: _Active) -> bool:
        """Return True when the work is searched and judged anew: under
        ``--research``, or because its input fields changed."""
        assert self.matching is not None
        return self.matching.research or active.planned.changed

    def _decisions(self, match: Match) -> dict[int, str]:
        """Return the decision on each judged candidate, by record identity."""
        assert self.matching is not None
        thresholds = self.services.settings.judge.thresholds
        decided: dict[int, str] = {}
        for item in match.candidates:
            if item.answer is None and self.matching.mode != "fuzzy":
                continue
            try:
                decision = policy.decide_candidate(
                    item.candidate(), self.matching.mode, thresholds
                )
            except ValueError:
                continue
            decided[id(item.record)] = decision.decision
        return decided

    def _record(self, active: _Active, match: Match) -> dict[int, int]:
        """Store the candidates; return their ids by record identity."""
        assert self.matching is not None
        decided = self._decisions(match)
        inputs = [
            CandidateInput(
                record=item.record,
                origin=item.origin,
                rank=item.rank,
                text_score=item.text.total,
                judgment=(
                    judgment_of(item.answer, decided.get(id(item.record), "review"))
                    if item.judged and item.answer is not None
                    else None
                ),
            )
            for item in match.candidates
        ]
        entry_id = active.planned.work.entry_id
        for item in match.candidates:
            if item.found:
                self.events.candidate_found(
                    CandidateFound(
                        entry_id,
                        item.record.provider,
                        item.record.source_id,
                        item.record.title,
                    )
                )
        if self.store is None or active.lease is None:
            return {}
        ids = self.store.add_candidates(
            active.lease, inputs, research=self._research(active)
        )
        return {
            id(item.record): ident
            for item, ident in zip(match.candidates, ids, strict=True)
        }

    def _notes(self, match: Match) -> None:
        for note in match.notes:
            self.events.warning(note)
        for key in match.missing_keys:
            if key not in self._skip_keys:
                self._skip_keys.add(key)
                self.events.warning(
                    f"{key} skipped: its API key is not set "
                    f"({self.services.settings.api_key_env(key)})"
                )

    def _on_match(self, active: _Active, match: Match) -> None:
        assert self.matching is not None
        self._notes(match)
        ids = self._record(active, match)
        if match.review is not None:
            self._finish(active, "needs_review", match.review)
            return
        if match.deferral is not None:
            reason, retry_after = match.deferral
            self._finish(
                active, "deferred", f"judge: {reason}", retry_after=retry_after
            )
            return
        if match.failure is not None:
            self._finish(active, "failed", match.failure)
            return
        outcome = policy.decide_work(
            [item.candidate() for item in match.candidates],
            mode=self.matching.mode,
            thresholds=self.services.settings.judge.thresholds,
            edition_policy=self.matching.edition_policy,
            provider_order=self.matching.providers,
        )
        by_record = {id(item.record): item for item in match.candidates}
        acceptable = self._acceptable(outcome)
        chosen = acceptable[0] if acceptable else None
        self._judged_events(active, outcome, by_record, chosen)
        linked = any(item.origin == "link" for item in match.candidates)
        if outcome.outcome == "no_match":
            if linked:
                self._finish(active, "needs_review", "link: the judge rejects the item")
            else:
                count = len(match.candidates)
                reason = (
                    "no candidates found"
                    if not count
                    else f"no acceptable candidate among {count}"
                )
                self._finish(active, "no_match", reason)
            return
        if chosen is None:
            self._finish(active, "needs_review", self._review_reason(outcome))
            return
        item = by_record[id(chosen.record)]
        active.chosen = item
        active.candidate_id = ids.get(id(item.record))
        if self.download is None:
            self._accepted(active)
            return
        active.fallbacks = [
            (by_record[id(other.record)], ids.get(id(other.record)))
            for other in acceptable[1:]
        ]
        self._submit_download(active, item.record.provider, item.record.source_id)

    def _acceptable(self, outcome: policy.WorkOutcome) -> list[policy.Candidate]:
        """Return the acceptable candidates in preference order: those of the
        first decision group (accepted, not held by the edition policy) that
        a download can fetch; under ``search`` every one of them."""
        if outcome.outcome != "accepted":
            return []
        found: list[policy.Candidate] = []
        for decided in outcome.decisions:
            if decided.decision != "accept" or decided.held is not None:
                break
            if self.download is None or self._downloads(decided.candidate.record):
                found.append(decided.candidate)
        return found

    def _downloads(self, record: SearchRecord) -> bool:
        try:
            return self.services.registry.get(record.provider).capabilities.downloads
        except (ProviderError, ValueError):
            return False

    @staticmethod
    def _review_reason(outcome: policy.WorkOutcome) -> str:
        if outcome.outcome == "accepted" and outcome.chosen is not None:
            record = outcome.chosen.record
            return (
                f"metadata_only: the accepted candidate {record.provider}:"
                f"{record.source_id} cannot be downloaded"
            )
        reason = outcome.reason or "review_band"
        for decided in outcome.decisions:
            if decided.decision != "reject":
                record = decided.candidate.record
                return f"{reason}: {record.provider}:{record.source_id}"
        return reason

    def _judged_events(
        self,
        active: _Active,
        outcome: policy.WorkOutcome,
        by_record: dict[int, Scored],
        chosen: policy.Candidate | None,
    ) -> None:
        entry_id = active.planned.work.entry_id
        result = outcome.outcome
        for decided in outcome.decisions:
            item = by_record[id(decided.candidate.record)]
            answer = item.answer
            self.events.candidate_judged(
                CandidateJudged(
                    entry_id,
                    item.record.provider,
                    item.record.source_id,
                    decided.decision,
                    None if answer is None else answer.same_work,
                    item.text.total,
                )
            )
            self.tally.rows.append(
                SearchRow(
                    entry_id=entry_id,
                    provider=item.record.provider,
                    item_id=item.record.source_id,
                    title=item.record.title,
                    year=item.text.candidate_year,
                    text_score=item.text.total,
                    same_work=None if answer is None else answer.same_work,
                    relation=None if answer is None else answer.relation,
                    decision=decided.decision,
                    outcome=result,
                    chosen=chosen is not None and decided.candidate is chosen,
                )
            )

    def _accepted(self, active: _Active) -> None:
        """Close a ``search`` that accepted a candidate."""
        if self.store is not None and active.lease is not None:
            self.store.set_status(active.lease, None, selected=active.candidate_id)
        self.tally.add("accepted")
        self._release(active)
        self._beat()

    # -- download ------------------------------------------------------------------

    def _copies(self, entry_id: str) -> dict[tuple[str, str], list[FileRow]]:
        """Return the work's recorded page and rendering files by source item."""
        assert self.store is not None
        attempts = {
            attempt.attempt_id: (attempt.provider, attempt.source_id)
            for attempt in self.store.attempts(entry_id)
        }
        copies: dict[tuple[str, str], list[FileRow]] = {}
        for row in self.store.files(entry_id):
            source = attempts.get(row.attempt_id)
            if row.kind != "metadata" and source is not None:
                copies.setdefault(source, []).append(row)
        return copies

    def _switch_copy(
        self, active: _Active, copies: dict[tuple[str, str], list[FileRow]]
    ) -> bool:
        """Clear the way for another copy than the recorded one.

        Without ``--force`` the work goes to review and False is returned;
        with it the other copies' recorded files are deleted first.
        """
        assert self.download is not None
        assert self.store is not None
        assert active.lease is not None
        if not self.download.force:
            provider, source_id = next(iter(copies))
            self._finish(
                active,
                "needs_review",
                f"replacement: the work holds the copy {provider}:{source_id}; "
                "'fetch --force' or 'drop' switches copies",
            )
            return False
        paths = [row.path for rows in copies.values() for row in rows]
        self.store.remove_files(active.lease, paths)
        return True

    def _submit_download(self, active: _Active, provider: str, source_id: str) -> None:
        assert self.download is not None
        assert self.store is not None
        assert active.lease is not None
        options = self.download
        entry_id = active.planned.work.entry_id
        copies = self._copies(entry_id)
        held = copies.pop((provider, source_id), [])
        if copies and not self._switch_copy(active, copies):
            return
        staging = self.store.staging_dir(active.lease)
        present = frozenset() if options.force else frozenset(row.item for row in held)
        provider_max = self.services.settings.providers[provider].max_pages
        request = functools.partial(
            _request,
            format=options.format,
            max_pages=page_limit(options.max_pages, provider_max),
            include_metadata=options.include_metadata,
            present=present,
            budget=options.budget,
            entry_id=entry_id,
        )
        active.step = "download"
        if active.fetch is not None:
            future: Future[Any] = self.services.pool.submit(
                functools.partial(
                    fetch_item,
                    self.services,
                    active.fetch,
                    staging_dir=staging,
                    request=request,
                )
            )
        else:
            assert active.chosen is not None
            future = self.services.pool.submit(
                functools.partial(
                    download_item,
                    self.services,
                    active.chosen.record,
                    provider=provider,
                    origin=active.chosen.origin,
                    staging_dir=staging,
                    request=request,
                )
            )
        self._pending[future] = active

    def _promotions(self, active: _Active, result: DownloadResult) -> list[Promotion]:
        assert active.lease is not None
        promotions: list[Promotion] = []
        finals: set[str] = set()
        for staged in result.files:
            try:
                path = object_path(
                    staged, work_stem=active.lease.folder, provider=result.provider
                )
            except ValueError as exc:
                self.events.warning(f"{active.planned.work.entry_id}: {exc}")
                continue
            if path.casefold() in finals:
                self.events.warning(
                    f"{active.planned.work.entry_id}: {staged.name} maps to "
                    f"{path}, which another staged file takes; not kept"
                )
                continue
            finals.add(path.casefold())
            promotions.append(Promotion(staged, path))
        return promotions

    def _on_download(self, active: _Active, outcome: DownloadOutcome) -> None:
        assert self.store is not None
        assert self.download is not None
        assert active.lease is not None
        lease = active.lease
        entry_id = active.planned.work.entry_id
        if active.fetch is not None and active.record is not None:
            active.candidate_id = self._fetch_candidate(lease, active.record)
        result = outcome.result
        status: StoredStatus
        if result is None:
            empty = DownloadResult(
                outcome.provider, outcome.source_id, self.download.format, ()
            )
            self.store.commit_download(
                lease,
                empty,
                candidate_id=active.candidate_id,
                started_at=outcome.started_at,
                error=outcome.error,
            )
            if outcome.unsupported:
                status, reason = "needs_review", f"metadata_only: {outcome.error}"
            else:
                status, reason = "failed", f"download failed: {outcome.error}"
        else:
            promotions = self._promotions(active, result)
            self.store.commit_download(
                lease,
                result,
                promotions,
                candidate_id=active.candidate_id,
                started_at=outcome.started_at,
            )
            status = _outcome(result)
            self.events.downloaded(
                Downloaded(
                    entry_id,
                    result.provider,
                    result.source_id,
                    tuple(promotion.path for promotion in promotions),
                )
            )
            if active.fetch is not None and status != "failed":
                self._clear_inspect(entry_id)
            described = describe_result(result)
            if outcome.note is not None:
                described = f"{described}; {outcome.note}"
            reason = described or ""
        if status not in ("failed", "needs_review"):
            self._finish(
                active,
                status,
                self._fetch_reason(active, _bounded(reason)),
                selected=active.candidate_id,
            )
            return
        active.tried.append(
            (status, f"{outcome.provider}:{outcome.source_id}: {reason}")
        )
        if self._fall_back(active):
            return
        self._finish(
            active,
            self._without_files(active, status),
            self._fetch_reason(active, self._tried_reason(active, reason)),
            selected=active.candidate_id,
        )

    def _fall_back(self, active: _Active) -> bool:
        """Download the next acceptable candidate after one without files.

        Returns False when none is left or a size limit stopped the run.
        """
        assert self.download is not None
        if not active.fallbacks or self.download.budget.stopped:
            return False
        item, candidate_id = active.fallbacks.pop(0)
        record = item.record
        _status, last = active.tried[-1]
        self.events.warning(
            f"{active.planned.work.entry_id}: no files from {_bounded(last)}; "
            f"trying {record.provider}:{record.source_id}"
        )
        active.chosen = item
        active.candidate_id = candidate_id
        self._submit_download(active, record.provider, record.source_id)
        return True

    @staticmethod
    def _without_files(active: _Active, status: StoredStatus) -> StoredStatus:
        """Return a work's status when no candidate yielded files:
        ``needs_review`` when every one is metadata-only, else ``failed``."""
        if len(active.tried) < 2:
            return status
        if all(tried == "needs_review" for tried, _text in active.tried):
            return "needs_review"
        return "failed"

    @staticmethod
    def _tried_reason(active: _Active, reason: str) -> str | None:
        """Return why a work has no files, bounded and masked: the one
        download's reason, or each candidate tried and its reason."""
        if len(active.tried) < 2:
            return _bounded(reason)
        statuses = {status for status, _text in active.tried}
        prefix = "metadata_only: " if statuses == {"needs_review"} else ""
        texts = "; ".join(text for _status, text in active.tried)
        return _bounded(
            f"{prefix}no files from {len(active.tried)} candidates: {texts}"
        )

    def _fetch_candidate(self, lease: Lease, record: SearchRecord) -> int:
        """Return the id of the fetched item's candidate, recorded if new.

        A current candidate of the same item (found by a search) is kept as
        it is, so its metadata and judgment stay.
        """
        assert self.store is not None
        for row in self.store.candidates(lease.entry_id):
            if (row.record.provider, row.record.source_id) == (
                record.provider,
                record.source_id,
            ):
                return row.candidate_id
        (candidate_id,) = self.store.add_candidates(
            lease, [CandidateInput(record, origin="fetch")]
        )
        return candidate_id

    def _fetch_reason(self, active: _Active, reason: str | None) -> str | None:
        if active.fetch is None or active.fetch.reason is None:
            return reason
        note = f"fetch: {active.fetch.reason}"
        return note if reason is None else f"{reason}; {note}"

    def _clear_inspect(self, entry_id: str) -> None:
        assert self.store is not None
        folder = inspect_dir(self.store.root, entry_id)
        if folder.is_dir():
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                self.events.warning(f"{entry_id}: inspect pages not removed: {exc}")

    # -- endings -------------------------------------------------------------------

    def _finish(
        self,
        active: _Active,
        status: StoredStatus,
        reason: str | None,
        *,
        retry_after: float | None = None,
        selected: int | None = None,
    ) -> None:
        if self.store is not None and active.lease is not None:
            self.store.set_status(
                active.lease,
                status,
                reason=reason,
                retry_after=retry_after,
                selected=selected,
            )
        self._count(active.planned, status, reason)
        self._release(active)

    def _count(
        self, planned: PlannedWork, status: WorkStatus, reason: str | None
    ) -> None:
        self.events.status_changed(StatusChanged(planned.work.entry_id, status, reason))
        self.tally.add(status)
        self._beat()

    def _release(self, active: _Active) -> None:
        if self.store is None or active.lease is None:
            return
        lease, active.lease = active.lease, None
        with contextlib.suppress(LeaseLostError):
            self.store.release(lease)

    def _crashed(self, active: _Active, exc: Exception) -> None:
        reason = bounded_error(redact_text(f"error: {type(exc).__name__}: {exc}"))
        self.events.warning(f"{active.planned.work.entry_id}: {reason}")
        try:
            self._finish(active, "failed", reason)
        except LeaseLostError as lost:
            self._lost(active, lost)

    def _lost(self, active: _Active, exc: LeaseLostError) -> None:
        self.events.warning(
            f"{active.planned.work.entry_id}: {exc} Another run holds the work; "
            "this run left it."
        )
        active.lease = None
        self._count(active.planned, "blocked", "the lease passed to another run")

    def _beat(self, *, force: bool = False) -> None:
        if self.heartbeat is None:
            return
        done = self.tally.finished
        eta = self._eta.seconds(done, self._total)
        self.heartbeat.update(done, self._total, UNIT_LABEL, eta, force=force)


def _request(
    slot: Any,
    *,
    format: DownloadFormat,
    max_pages: int,
    include_metadata: bool,
    present: frozenset[str],
    budget: SizeBudget,
    entry_id: str,
) -> DownloadRequest:
    return DownloadRequest(
        slot=slot,
        format=format,
        max_pages=max_pages,
        include_metadata=include_metadata,
        present=present,
        budget=budget.for_work(entry_id),
    )
