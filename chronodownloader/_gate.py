"""The post-phase of ``download``: the gate in shadow mode and ``--classify``.

The post-phase runs in the download's run after the work loop has finished
every work, so its requests never share a provider's slots, pacing or
breaker with a download, and before ``index.csv`` is written. It is skipped
on a dry run (nothing runs) and when a size limit stopped the run.

Gate (``--decisions shadow``). :class:`StatusLog` records the status each work
reached in this run. Two kinds of work are checked
(:class:`~chronodownloader.matching.gate.GateAsker`), each once:

- a work this run left ``complete`` or ``partial``: its selected candidate,
  from its first :data:`~chronodownloader.matching.gate.GATE_PAGES` page
  images by source position, read from disk; no provider request. A work
  without page images (a PDF only) is recorded ``unavailable``;
- a work this run left ``needs_review`` for a reason of the decision policy
  (the review band or an edition the policy holds; not ``metadata_only`` and
  not a rejected link): its best review-band candidate, the one the reason
  names, sampled with :func:`~chronodownloader.pages.sample_pages` into the
  work's ``inspect`` scratch folder under one of the provider's slots.

Pages go at :data:`~chronodownloader.matching.gate.GATE_PAGE_PX` pixels and
are hashed as sent; a page that cannot be read fails the check. A stored
``ok`` check whose fingerprint, sought-work hash, provider and item and
ordered page hashes all match is reused without a request, as a new row that
names it in ``reused_from``. The main thread commits each check: it takes the
work's lease again without writing input fields, ``last_run_id`` or
``updated_at``, and checks that the sought work is unchanged and that the
work is still ``complete`` or ``partial`` with the same selected candidate
and the same files and attempts of its leading pages (downloaded), or still
``needs_review`` with the candidate current (review band). It then records the
check; otherwise the check is dropped as stale. A work another run holds is
skipped as blocked.

Shadow. No check changes a status, a decision, the download order or the
exit code: a failed request, an outage or a store error is logged and the
check counted ``failed`` (recorded where the store took it). An error that
stops the gate keeps the checks done so far, and ``--classify`` still runs.

Classify (``--classify``). The works this run left ``complete`` or ``partial``
are classified as ``classify`` does
(:func:`~chronodownloader._classify.classify_stored`); its outcome does not
change the download's exit code either.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from chronodownloader.common.decisions import DecisionUsage
from chronodownloader.coord import CoordinationError
from chronodownloader.events import (
    CandidateFound,
    CandidateJudged,
    Downloaded,
    RunEvents,
    StatusChanged,
    WorkStarted,
)
from chronodownloader.matching.classifier import (
    decision_target,
    downscale,
)
from chronodownloader.matching.gate import (
    GATE_PAGE_PX,
    GATE_PAGES,
    GATE_QUESTION_VERSION,
    GateAsker,
    page_hash,
    sought_hash,
)
from chronodownloader.matching.judge import (
    DecisionTarget,
    JudgeRefused,
    JudgeUnavailable,
    MalformedAnswer,
)
from chronodownloader.matching.policy import REVIEW_REASONS
from chronodownloader.net import NetworkError
from chronodownloader.net.credentials import redact_text
from chronodownloader.output.paths import inspect_dir, sanitize_filename
from chronodownloader.output.store import (
    GateCheck,
    LeaseLostError,
    NotSelectedError,
    Store,
    WorkInput,
    WorkRow,
)
from chronodownloader.pages import sample_pages
from chronodownloader.providers.base import (
    ProviderError,
    SearchQuery,
    SearchRecord,
    UnsupportedCapability,
)
from chronodownloader.spec import DownloadDecisions

from ._classify import classify_stored, source_pages
from ._core import (
    ClassifyReport,
    DecisionTotals,
    DownloadReport,
    GateReport,
    GateResult,
    PlannedWork,
)
from ._runtime import Services
from ._worker import query_of, unmask

__all__ = ["DOWNLOADED", "StatusLog", "decision_target_of", "run_post_phase"]

logger = logging.getLogger(__name__)

POLL_S: Final = 0.25
"""Seconds the main thread waits for a worker before it looks again."""

DOWNLOADED: Final = ("complete", "partial")
"""The statuses of a work this run downloaded."""


class StatusLog:
    """Run events passed on to *events*, with the status each work reached."""

    def __init__(self, events: RunEvents) -> None:
        self._events = events
        self.statuses: dict[str, tuple[str, str | None]] = {}

    def work_started(self, event: WorkStarted) -> None:
        """Pass the event on."""
        self._events.work_started(event)

    def candidate_found(self, event: CandidateFound) -> None:
        """Pass the event on."""
        self._events.candidate_found(event)

    def candidate_judged(self, event: CandidateJudged) -> None:
        """Pass the event on."""
        self._events.candidate_judged(event)

    def downloaded(self, event: Downloaded) -> None:
        """Pass the event on."""
        self._events.downloaded(event)

    def status_changed(self, event: StatusChanged) -> None:
        """Record the work's status and pass the event on."""
        self.statuses[event.entry_id] = (event.status, event.reason)
        self._events.status_changed(event)

    def warning(self, message: str) -> None:
        """Pass the warning on."""
        self._events.warning(message)


def decision_target_of(settings: Any, decision: DownloadDecisions) -> DecisionTarget:
    """Return the gate's and ``--classify``'s model: the flags, else ``decisions``.

    Raises:
        ValueError: ``--decision-provider`` names no dialect.
    """
    return decision_target(
        settings, decision.model, decision.provider, decision.endpoint
    )


@dataclass(frozen=True)
class _Job:
    """One candidate to check, as the main thread found it."""

    entry_id: str
    folder: str
    band: str
    candidate_id: int | None
    provider: str
    source_id: str
    query: SearchQuery
    sought: str
    paths: tuple[Path, ...] = ()
    files: tuple[tuple[str, int], ...] = ()
    record: SearchRecord | None = None
    reusable: tuple[GateCheck, ...] = ()


@dataclass(frozen=True)
class _Asked:
    """What a worker made of a job."""

    status: str
    page_hashes: tuple[str, ...] = ()
    answers: Mapping[str, Any] | None = None
    usage: DecisionUsage | None = None
    error: str | None = None
    sent: bool = False
    reused_from: int | None = None


def _work_input(row: WorkRow) -> WorkInput:
    return WorkInput(
        entry_id=row.entry_id,
        title=row.title,
        creator=row.creator,
        year=row.year,
        place=row.place,
        publisher=row.publisher,
        language=row.language,
        edition=row.edition,
        link=row.link,
    )


def _review_item(reason: str | None) -> tuple[str, str] | None:
    """Return the provider and item a policy review *reason* names, or None."""
    if not reason:
        return None
    kind, _, rest = reason.partition(": ")
    if kind not in REVIEW_REASONS:
        return None
    provider, _, source_id = rest.partition(":")
    return (provider, source_id) if provider and source_id else None


def _load(paths: Sequence[Path]) -> tuple[list[bytes], list[str]]:
    """Return the pages downscaled as sent, and what could not be read."""
    images: list[bytes] = []
    errors: list[str] = []
    for path in paths:
        try:
            images.append(downscale(path.read_bytes(), GATE_PAGE_PX))
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
    return images, errors


def _sample(services: Services, job: _Job, root: Path) -> list[Path]:
    """Sample the review-band candidate's first pages (worker side)."""
    assert job.record is not None
    folder = inspect_dir(root, job.entry_id) / sanitize_filename(
        f"{job.provider}_{job.source_id}"
    )
    provider = services.registry.get(job.provider)
    with services.network.hold_slot(job.provider) as slot:
        sample = sample_pages(
            provider,
            job.record,
            network=services.network,
            slot=slot,
            dest_dir=folder,
            first_n=GATE_PAGES,
            max_px=GATE_PAGE_PX,
        )
    return [folder / page.file.name for page in sample.sampled if page.file is not None]


def _check(services: Services, asker: GateAsker, job: _Job, root: Path) -> _Asked:
    """Read or sample the job's pages, then reuse or ask a check (worker side)."""
    paths: Sequence[Path] = job.paths
    if job.band == "review":
        try:
            paths = _sample(services, job, root)
        except UnsupportedCapability as exc:
            return _Asked("unavailable", error=str(exc))
        except (ProviderError, NetworkError, CoordinationError, OSError) as exc:
            return _Asked("failed", error=redact_text(f"sample: {exc}"))
        if not paths:
            return _Asked("failed", error="no page could be sampled")
    if not paths:
        return _Asked("unavailable", error="no page images recorded")
    images, errors = _load(paths)
    if errors:
        return _Asked("failed", error="unreadable page: " + "; ".join(errors))
    hashes = tuple(page_hash(image) for image in images)
    for stored in reversed(job.reusable):
        if stored.page_hashes == hashes:
            return _Asked(
                "reused", hashes, answers=stored.answers, reused_from=stored.check_id
            )
    try:
        answer = asker.ask(job.query, images)
    except JudgeUnavailable as exc:
        return _Asked("failed", hashes, error=f"outage: {exc.reason}")
    except (JudgeRefused, MalformedAnswer, ValueError) as exc:
        sent = isinstance(exc, MalformedAnswer) or (
            isinstance(exc, JudgeRefused) and exc.status is not None
        )
        return _Asked("failed", hashes, error=str(exc), sent=sent)
    return _Asked("ok", hashes, answers=answer.answers, usage=answer.usage, sent=True)


@dataclass
class _Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float | None = None

    def add(self, asked: _Asked) -> None:
        self.requests += asked.sent
        usage = asked.usage
        if usage is None:
            return
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cached_tokens += usage.cached_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        if usage.cost is not None:
            self.cost = (self.cost or 0.0) + usage.cost

    def totals(self) -> DecisionTotals:
        return DecisionTotals(
            self.requests,
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
            self.cache_write_tokens,
            self.cost,
        )


@dataclass
class _Gate:
    """The main-thread side of the gate over one store."""

    store: Store
    services: Services
    asker: GateAsker
    results: list[GateResult] = field(default_factory=list)
    usage: _Usage = field(default_factory=_Usage)

    def jobs(self, statuses: Mapping[str, tuple[str, str | None]]) -> list[_Job]:
        """Return the checks of the works *statuses* names, in entry order."""
        found: list[_Job] = []
        for entry_id in sorted(statuses):
            status, reason = statuses[entry_id]
            row = self.store.work(entry_id)
            if row is None:
                continue
            if status in DOWNLOADED:
                job = self._downloaded(row)
            elif status == "needs_review":
                job = self._review(row, _review_item(reason))
            else:
                job = None
            if job is not None:
                found.append(job)
        return found

    def _base(self, row: WorkRow) -> dict[str, Any]:
        query = query_of(_work_input(row))
        return {
            "entry_id": row.entry_id,
            "folder": row.folder,
            "query": query,
            "sought": sought_hash(query),
        }

    def _reusable(
        self, entry_id: str, provider: str, source_id: str, sought: str
    ) -> tuple[GateCheck, ...]:
        return tuple(
            check
            for check in self.store.page_checks(entry_id)
            if check.status == "ok"
            and check.fingerprint == self.asker.fingerprint
            and check.sought_hash == sought
            and (check.provider, check.source_id) == (provider, source_id)
        )

    def _pages(self, entry_id: str) -> tuple[tuple[str, int], ...]:
        """Return the path and attempt of the work's leading page images."""
        pages = source_pages(self.store.files(entry_id))[:GATE_PAGES]
        return tuple((page.file.path, page.file.attempt_id) for page in pages)

    def _downloaded(self, row: WorkRow) -> _Job | None:
        candidate = next(
            (
                item
                for item in self.store.candidates(row.entry_id, history=True)
                if item.candidate_id == row.selected_candidate
            ),
            None,
        )
        if candidate is None:
            return None
        record = candidate.record
        files = self._pages(row.entry_id)
        base = self._base(row)
        return _Job(
            band="accepted",
            candidate_id=candidate.candidate_id,
            provider=record.provider,
            source_id=record.source_id,
            paths=tuple(self.store.root / row.folder / path for path, _ in files),
            files=files,
            reusable=self._reusable(
                row.entry_id, record.provider, record.source_id, base["sought"]
            ),
            **base,
        )

    def _review(self, row: WorkRow, item: tuple[str, str] | None) -> _Job | None:
        if item is None:
            return None
        candidate = next(
            (
                found
                for found in self.store.candidates(row.entry_id)
                if (found.record.provider, found.record.source_id) == item
            ),
            None,
        )
        if candidate is None:
            return None
        base = self._base(row)
        return _Job(
            band="review",
            candidate_id=candidate.candidate_id,
            provider=item[0],
            source_id=item[1],
            record=unmask(candidate.record),
            reusable=self._reusable(row.entry_id, *item, base["sought"]),
            **base,
        )

    def run(self, jobs: Sequence[_Job]) -> None:
        """Ask the checks on the worker pool and commit each on this thread."""
        pending: dict[Future[_Asked], _Job] = {
            self.services.pool.submit(
                functools.partial(
                    _check, self.services, self.asker, job, self.store.root
                )
            ): job
            for job in jobs
        }
        while pending:
            done, _ = wait(list(pending), POLL_S, FIRST_COMPLETED)
            for future in done:
                job = pending.pop(future)
                try:
                    asked = future.result()
                # The crash boundary of one check: a shadow check never ends
                # the run; KeyboardInterrupt is no Exception.
                except Exception as exc:  # noqa: BLE001
                    logger.exception("gate check of %s failed", job.entry_id)
                    asked = _Asked("failed", error=f"{type(exc).__name__}: {exc}")
                self.usage.add(asked)
                try:
                    self._commit(job, asked)
                # A store error (a busy timeout) fails this check only.
                except Exception as exc:  # noqa: BLE001
                    logger.exception("gate check of %s not stored", job.entry_id)
                    error = f"store: {type(exc).__name__}: {exc}"
                    self._result(job, "failed", replace(asked, error=error))

    def _current(self, job: _Job) -> bool:
        """Return True while the work still fits the check (under its lease)."""
        row = self.store.work(job.entry_id)
        if row is None or sought_hash(query_of(_work_input(row))) != job.sought:
            return False
        if job.band == "accepted":
            return (
                row.status in DOWNLOADED
                and row.selected_candidate == job.candidate_id
                and self._pages(job.entry_id) == job.files
            )
        return row.status == "needs_review" and any(
            item.candidate_id == job.candidate_id
            for item in self.store.candidates(job.entry_id)
        )

    def _result(self, job: _Job, outcome: Any, asked: _Asked | None) -> None:
        answers = asked.answers if asked is not None else None
        relation = (answers or {}).get("relation")
        note = asked.error if asked is not None else None
        if asked is not None and asked.status == "failed":
            logger.warning(
                "gate check of %s (%s:%s) failed: %s",
                job.entry_id,
                job.provider,
                job.source_id,
                note,
            )
        self.results.append(
            GateResult(
                job.entry_id,
                job.provider,
                job.source_id,
                job.band,
                outcome,
                same_work=(answers or {}).get("same_work"),
                relation=relation.get("choice")
                if isinstance(relation, Mapping)
                else None,
                note=note,
            )
        )

    def _commit(self, job: _Job, asked: _Asked) -> None:
        try:
            lease = self.store.acquire(
                WorkInput(job.entry_id, job.query.title),
                job.folder,
                select=lambda before: before is not None,
                update=False,
            )
        except NotSelectedError:
            self._result(job, "stale", None)
            return
        if lease is None:
            self._result(job, "blocked", None)
            return
        try:
            if not self._current(job):
                self._result(job, "stale", None)
                return
            self.store.record_page_check(lease, self._row(job, asked))
            self._result(job, asked.status, asked)
        except LeaseLostError:
            self._result(job, "blocked", None)
        finally:
            self.store.release(lease)

    def _row(self, job: _Job, asked: _Asked) -> GateCheck:
        target = self.asker.target
        usage = asked.usage
        return GateCheck(
            candidate_id=job.candidate_id,
            provider=job.provider,
            source_id=job.source_id,
            band=job.band,
            page_source="downloaded" if job.band == "accepted" else "sampled",
            sought_hash=job.sought,
            page_hashes=asked.page_hashes,
            model=target.model,
            dialect=target.dialect,
            endpoint=target.endpoint,
            question_version=GATE_QUESTION_VERSION,
            fingerprint=self.asker.fingerprint,
            status="ok" if asked.status == "reused" else asked.status,
            error=asked.error,
            answers=asked.answers,
            reused_from=asked.reused_from,
            usage=None
            if usage is None
            else {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_tokens": usage.cached_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "cost": usage.cost,
            },
        )


def _downloaded_works(store: Store, entry_ids: Sequence[str]) -> list[PlannedWork]:
    works: list[PlannedWork] = []
    for entry_id in entry_ids:
        row = store.work(entry_id)
        if row is not None:
            works.append(PlannedWork(_work_input(row), row.folder, str(row.status)))
    return works


def run_post_phase(
    store: Store,
    services: Services,
    decision: DownloadDecisions,
    log: StatusLog,
    *,
    stopped: bool,
) -> DownloadReport:
    """Run the gate and ``--classify`` over the works *log* saw finish.

    *stopped* marks a run a size limit stopped: nothing runs then. The run's
    lease keeper must be running.
    """
    gate = GateReport(mode=decision.mode)
    if stopped or (decision.mode == "off" and not decision.classify):
        return DownloadReport(gate)
    settings = services.settings
    target = decision_target_of(settings, decision)
    classified: ClassifyReport | None = None
    # The crash boundary of each step: a post-phase step never changes the
    # run's outcome, so an error ends that step with a warning, not the run.
    if decision.mode == "shadow":
        loop: _Gate | None = None
        try:
            asker = GateAsker(
                services.network, settings, target, clock=services.wiring.clock
            )
            loop = _Gate(store, services, asker)
            loop.run(loop.jobs(log.statuses))
        except Exception as exc:  # noqa: BLE001
            _stopped(log, "gate", exc)
        gate = GateReport(
            mode=decision.mode,
            checks=() if loop is None else tuple(loop.results),
            model=target.model,
            usage=DecisionTotals() if loop is None else loop.usage.totals(),
        )
    if decision.classify:
        downloaded = sorted(
            entry_id
            for entry_id, (status, _reason) in log.statuses.items()
            if status in DOWNLOADED
        )
        try:
            classified = classify_stored(
                store, services, target, _downloaded_works(store, downloaded)
            )
        except Exception as exc:  # noqa: BLE001
            _stopped(log, "classify", exc)
    return DownloadReport(gate, classified)


def _stopped(log: StatusLog, step: str, exc: Exception) -> None:
    logger.warning("the download's %s step stopped: %s", step, exc)
    log.warning(f"post-phase {step} stopped: {redact_text(str(exc))}")
