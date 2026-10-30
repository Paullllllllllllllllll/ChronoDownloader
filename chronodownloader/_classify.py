"""The core of ``classify``: the page type of every stored page image.

Plan. The works of ``--output`` (narrowed by ``--entry-ids``) are read from
the store's views without writing. A work's pages are its ``files`` rows of
kind ``image`` by source position (the store's Page index); a work without
one (a PDF only) is skipped. A
page whose label carries the same attempt as its files row and the request
fingerprint of this run (:func:`~chronodownloader.matching.classifier.
fingerprint`) is current and is not asked again; ``--force`` asks every
page. Planning resolves the decision model (a flag wins over the settings'
``decisions`` block) and checks its key; ``--dry-run`` reports the pages it
would ask.

Run. A work is classified under its lease, taken without writing input
fields, which the run's lease keeper renews while the work's requests are
out; a work another run holds is counted ``blocked``. The request context is
the works row read under the lease. Before a work's pages are asked, its
labels of another request fingerprint are deleted, so an interrupted
reclassification with other settings shows as incomplete rather than as a
mix of two requests. At most ``judge.network.slots`` works are in flight.
Each work's pages to ask are split into consecutive groups of up to
``decisions.pages_per_request``; a worker reads a group's files, downscales
each page to ``decisions.image_max_px`` and asks the page types in one
request (:class:`~chronodownloader.matching.classifier.PageClassifier`).
The main thread writes each group's labels in one short lease-checked
transaction; a label whose files row changed since it was asked is dropped
and counted stale (:meth:`~chronodownloader.output.store.Store.
record_page_labels`). A page that cannot be read or decoded, a refused
request, a malformed answer and a declined question fail their pages; an
outage of the decision model defers them. The work's status is never
changed. The report closes with the title page that the store's
``title_page_picks`` view picks for each work.
"""

from __future__ import annotations

import functools
import logging
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from chronodownloader.common.decisions import DecisionUsage, group_indices
from chronodownloader.common.page_types import VERSION
from chronodownloader.matching.classifier import (
    CONTEXT_FIELDS,
    DecisionTarget,
    MissingDecisionKey,
    PageClassifier,
    decision_key,
    decision_target,
    downscale,
    fingerprint,
)
from chronodownloader.matching.judge import (
    JudgeRefused,
    JudgeUnavailable,
    MalformedAnswer,
)
from chronodownloader.net.credentials import redact_text
from chronodownloader.options.options import Source
from chronodownloader.output.store import (
    FileRow,
    Lease,
    LeaseLostError,
    NotSelectedError,
    PageLabel,
    Store,
    StoreError,
    WorkInput,
    read_files,
    read_page_labels,
    read_title_page_picks,
)
from chronodownloader.process.contract import ExitCode
from chronodownloader.settings import Settings
from chronodownloader.spec import ClassifySpec, effective_spec

from ._core import (
    ClassifiedWork,
    ClassifyReport,
    DecisionTotals,
    PlanError,
    PlannedWork,
    RunPlan,
    RunResult,
    WorkCounts,
    check_store,
    close_store,
    open_store,
    read_statuses,
    tool_version,
)
from ._runtime import Services, Wiring

__all__ = ["plan_classify", "run_classify"]

logger = logging.getLogger(__name__)

POLL_S: Final = 0.25
"""Seconds the main thread waits for a worker before it looks again."""

NO_IMAGES: Final = "no page images recorded"


_SOURCE_ITEM: Final = re.compile(r"p([0-9]+)")


@dataclass(frozen=True)
class _Page:
    """A page image of a work: its position in the source, its row."""

    index: int
    file: FileRow


def _pages(files: Sequence[FileRow]) -> list[_Page]:
    """Return the work's page images by source position, as the store's view.

    Item ``pNNNNN`` is position NNNNN - 1; when any item is not of that form,
    the images are numbered in item order instead.
    """
    images = sorted(
        (row for row in files if row.kind == "image"),
        key=lambda row: (row.item, row.path),
    )
    matches = [_SOURCE_ITEM.fullmatch(row.item) for row in images]
    numbers = [int(match[1]) if match else 0 for match in matches]
    if images and min(numbers) > 0:
        pages = [
            _Page(number - 1, row) for number, row in zip(numbers, images, strict=True)
        ]
        return sorted(pages, key=lambda page: (page.index, page.file.path))
    return [_Page(index, row) for index, row in enumerate(images)]


def _to_ask(
    pages: Sequence[_Page], labels: Sequence[PageLabel], key: str, force: bool
) -> list[_Page]:
    """Return the pages without a current label of request fingerprint *key*."""
    if force:
        return list(pages)
    current = {
        (label.path, label.attempt_id) for label in labels if label.fingerprint == key
    }
    return [
        page for page in pages if (page.file.path, page.file.attempt_id) not in current
    ]


def _work_input(row: Mapping[str, Any]) -> WorkInput:
    """Return the stored input fields of a ``work_status`` *row*."""
    return WorkInput(
        entry_id=str(row["entry_id"]),
        title=str(row["title"]),
        creator=row["creator"],
        year=row["year"],
        place=row["place"],
        publisher=row["publisher"],
        language=row["language"],
        edition=row["edition"],
        link=row["link"],
    )


def _read[T](read: Callable[[], T]) -> T:
    try:
        return read()
    except StoreError as exc:
        raise PlanError(str(exc)) from exc


def _target(spec: ClassifySpec, settings: Settings) -> DecisionTarget:
    decision = spec.decision
    try:
        return decision_target(
            settings, decision.model, decision.provider, decision.endpoint
        )
    except ValueError as exc:
        raise PlanError(str(exc)) from exc


def _with_picks(
    root: Path, works: Sequence[ClassifiedWork]
) -> tuple[ClassifiedWork, ...]:
    """Return *works* with the title page the store's view picks for each."""
    wanted = [work.entry_id for work in works if work.pages]
    picks = {
        str(row["entry_id"]): row
        for row in (
            _read(lambda: read_title_page_picks(root, wanted)) if wanted else []
        )
    }
    picked: list[ClassifiedWork] = []
    for work in works:
        row = picks.get(work.entry_id)
        if row is not None:
            work = replace(
                work,
                title_page=row["path"],
                probability=row["probability"],
                labelled_leading=int(row["pages_labelled"] or 0),
            )
        picked.append(work)
    return tuple(picked)


def plan_classify(
    spec: ClassifySpec, settings: Settings, sources: Mapping[str, Source]
) -> RunPlan:
    """Plan a ``classify``: the works and the pages each would ask.

    The plan is read-only and is the report of a dry run.

    Raises:
        PlanError: The decision model's key is missing, its provider is
            unknown, or the store is refused.
    """
    settings.state_directory()
    target = _target(spec, settings)
    try:
        decision_key(settings, target.dialect)
    except MissingDecisionKey as exc:
        raise PlanError(str(exc)) from exc
    root = spec.output
    check_store(root)
    rows = read_statuses(root, spec.entry_ids or None)
    warnings: list[str] = []
    absent = [
        entry_id for entry_id in dict.fromkeys(spec.entry_ids) if entry_id not in rows
    ]
    if absent:
        warnings.append(f"entry ids not in the store: {', '.join(absent)}")
    key = fingerprint(target, settings.decisions)
    works: list[ClassifiedWork] = []
    planned: list[PlannedWork] = []
    for entry_id, row in rows.items():
        folder = str(row["folder"])
        pages = _pages(_read(functools.partial(read_files, root, entry_id)))
        if not pages:
            works.append(ClassifiedWork(entry_id, folder, "skipped", note=NO_IMAGES))
            continue
        labels = _read(functools.partial(read_page_labels, root, entry_id))
        asked = len(_to_ask(pages, labels, key, spec.force))
        works.append(
            ClassifiedWork(
                entry_id,
                folder,
                "planned",
                pages=len(pages),
                asked=asked,
                current=len(pages) - asked,
            )
        )
        planned.append(PlannedWork(_work_input(row), folder, str(row["status"])))
    report = ClassifyReport(
        _with_picks(root, works),
        model=target.model,
        dialect=target.dialect,
        endpoint=target.endpoint,
        profile=settings.decisions.profile,
        fingerprint=key,
        dry_run=True,
    )
    skipped = sum(1 for work in works if work.outcome == "skipped")
    return RunPlan(
        spec,
        settings,
        sources,
        WorkCounts(total=len(works), skipped=skipped),
        works=tuple(planned),
        warnings=tuple(warnings),
        report=report,
    )


# -- run ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Answered:
    """The outcome of one group: labels, failed pages, usage, a deferral.

    *sent* marks a request the endpoint answered, whether or not the answer
    parsed; *usage* is known only for a parsed one.
    """

    labels: tuple[PageLabel, ...] = ()
    failed: tuple[str, ...] = ()
    deferred: int = 0
    note: str | None = None
    usage: DecisionUsage | None = None
    sent: bool = False


def _ask_group(
    classifier: PageClassifier,
    work_dir: Path,
    context: Mapping[str, Any],
    group: Sequence[_Page],
) -> _Answered:
    """Read, downscale and classify one group of pages (worker side)."""
    images: list[bytes] = []
    sent: list[_Page] = []
    failed: list[str] = []
    for page in group:
        try:
            data = (work_dir / page.file.path).read_bytes()
            images.append(downscale(data, classifier.image_max_px))
        except (OSError, ValueError) as exc:
            failed.append(f"{page.file.path}: {exc}")
            continue
        sent.append(page)
    if not sent:
        return _Answered(failed=tuple(failed))
    try:
        types = classifier.classify(context, images)
    except JudgeUnavailable as exc:
        note = f"decision model: {exc.reason}"
        return _Answered(failed=tuple(failed), deferred=len(sent), note=note)
    except (JudgeRefused, MalformedAnswer, ValueError) as exc:
        reason = f"decision model: {exc}"
        return _Answered(
            failed=(*failed, *(f"{page.file.path}: {reason}" for page in sent)),
            note=reason,
            sent=isinstance(exc, MalformedAnswer)
            or (isinstance(exc, JudgeRefused) and exc.status is not None),
        )
    target = classifier.target
    labels: list[PageLabel] = []
    for page, answer in zip(sent, types.answers, strict=True):
        if answer is None:
            failed.append(f"{page.file.path}: the model declined the question")
            continue
        labels.append(
            PageLabel(
                path=page.file.path,
                attempt_id=page.file.attempt_id,
                page_index=page.index,
                page_type=answer.choice,
                probabilities=dict(answer.probabilities),
                taxonomy_version=VERSION,
                profile=classifier.profile,
                model=target.model,
                dialect=target.dialect,
                fingerprint=classifier.fingerprint,
            )
        )
    return _Answered(tuple(labels), tuple(failed), usage=types.usage, sent=True)


@dataclass
class _InFlight:
    """A work whose groups are out: its lease and what came back so far."""

    planned: PlannedWork
    lease: Lease
    pages: int
    asked: int
    pending: int = 0
    labelled: int = 0
    stale: int = 0
    failed: int = 0
    deferred: int = 0
    lost: bool = False
    notes: list[str] = field(default_factory=list)

    def finished(self) -> ClassifiedWork:
        """Return the work's outcome once every group came back."""
        work = ClassifiedWork(
            self.planned.work.entry_id,
            self.lease.folder,
            "complete",
            pages=self.pages,
            asked=self.asked,
            current=self.pages - self.asked,
            labelled=self.labelled,
            stale=self.stale,
            failed=self.failed + self.deferred,
        )
        if self.stale:
            self.notes.append(f"{self.stale} label(s) stale: their files changed")
        note = "; ".join(dict.fromkeys(self.notes)) or None
        if self.lost:
            return replace(
                work, outcome="blocked", note="the lease passed to another run"
            )
        if self.failed or self.stale:
            return replace(work, outcome="failed", note=note)
        if self.deferred:
            return replace(work, outcome="deferred", note=note)
        return work


@dataclass
class _Usage:
    """The summed usage of the run's requests."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float | None = None

    def add(self, usage: DecisionUsage) -> None:
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


class _Classifier:
    """The main-thread loop of a ``classify`` run over one store."""

    def __init__(
        self,
        store: Store,
        services: Services,
        classifier: PageClassifier,
        *,
        force: bool,
        window: int,
    ) -> None:
        self.store = store
        self.services = services
        self.classifier = classifier
        self.force = force
        self.window = window
        self.usage = _Usage()
        self._pending: dict[Future[_Answered], tuple[_InFlight, int]] = {}
        self._active: dict[str, _InFlight] = {}
        self._done: dict[str, ClassifiedWork] = {}

    def run(self, works: Sequence[PlannedWork]) -> list[ClassifiedWork]:
        """Classify *works*; return their outcomes in order."""
        queue = deque(works)
        while queue or self._pending:
            while queue and len(self._active) < self.window:
                self._start(queue.popleft())
            if not self._pending:
                continue
            done, _ = wait(list(self._pending), POLL_S, FIRST_COMPLETED)
            for future in done:
                job, size = self._pending.pop(future)
                self._settle(job, size, future)
                job.pending -= 1
                if job.pending == 0:
                    self._finish(job)
        return [self._done[planned.work.entry_id] for planned in works]

    def _start(self, planned: PlannedWork) -> None:
        entry_id = planned.work.entry_id
        try:
            lease = self.store.acquire(
                planned.work,
                planned.folder,
                select=lambda before: before is not None,
                update=False,
            )
        except NotSelectedError:
            self._done[entry_id] = ClassifiedWork(
                entry_id, planned.folder, "skipped", note="no longer in the store"
            )
            return
        if lease is None:
            self._done[entry_id] = ClassifiedWork(
                entry_id, planned.folder, "blocked", note="another run holds the work"
            )
            return
        # An error here ends the run, whose store close releases the lease.
        pages = _pages(self.store.files(entry_id))
        key = self.classifier.fingerprint
        ask = _to_ask(pages, self.store.page_labels(entry_id), key, self.force)
        job = _InFlight(planned, lease, len(pages), len(ask))
        stored = self.store.work(entry_id)
        if pages and ask and stored is not None:
            try:
                self.store.drop_page_labels(lease, key)
            except LeaseLostError:
                job.lost = True
                self._done[entry_id] = job.finished()
                return
            context = {name: getattr(stored, name) for name in CONTEXT_FIELDS}
            self._submit(job, ask, context)
            return
        self.store.release(lease)
        if pages:
            self._done[entry_id] = job.finished()
        else:
            self._done[entry_id] = ClassifiedWork(
                entry_id, lease.folder, "skipped", note=NO_IMAGES
            )

    def _submit(
        self, job: _InFlight, ask: Sequence[_Page], context: Mapping[str, Any]
    ) -> None:
        work_dir = self.store.root / job.lease.folder
        groups = group_indices(len(ask), per_group=self.classifier.pages_per_request)
        self._active[job.planned.work.entry_id] = job
        for indices in groups:
            group = [ask[index] for index in indices]
            future = self.services.pool.submit(
                functools.partial(_ask_group, self.classifier, work_dir, context, group)
            )
            self._pending[future] = (job, len(group))
            job.pending += 1

    def _settle(self, job: _InFlight, size: int, future: Future[_Answered]) -> None:
        try:
            answered = future.result()
        # The per-group crash boundary: an unexpected error fails this
        # group's pages alone; KeyboardInterrupt is no Exception.
        except Exception as exc:  # noqa: BLE001
            logger.exception("classify of %s failed", job.planned.work.entry_id)
            note = redact_text(f"error: {type(exc).__name__}: {exc}")
            answered = _Answered(failed=(note,) * size, note=note)
        self.usage.requests += answered.sent
        if answered.usage is not None:
            self.usage.add(answered.usage)
        job.failed += len(answered.failed)
        job.deferred += answered.deferred
        if answered.note:
            job.notes.append(answered.note)
        elif answered.failed:
            job.notes.append(answered.failed[0])
        if job.lost or not answered.labels:
            return
        try:
            written, stale = self.store.record_page_labels(job.lease, answered.labels)
        except LeaseLostError:
            job.lost = True
            return
        job.labelled += written
        job.stale += stale

    def _finish(self, job: _InFlight) -> None:
        entry_id = job.planned.work.entry_id
        del self._active[entry_id]
        if not job.lost:
            self.store.release(job.lease)
        self._done[entry_id] = job.finished()


def _lost(lease: Lease) -> None:
    logger.warning("The lease on work %s expired or passed to another run.", lease)


def _counts(works: Sequence[ClassifiedWork]) -> WorkCounts:
    outcomes = [work.outcome for work in works]
    return WorkCounts(
        total=len(works),
        complete=outcomes.count("complete"),
        failed=outcomes.count("failed"),
        deferred=outcomes.count("deferred"),
        blocked=outcomes.count("blocked"),
        skipped=outcomes.count("skipped"),
    )


def run_classify(run_plan: RunPlan, wiring: Wiring) -> RunResult:
    """Classify the plan's works; report each with its title-page pick."""
    spec = run_plan.spec
    assert isinstance(spec, ClassifySpec)
    planned = run_plan.report
    assert isinstance(planned, ClassifyReport)
    if not run_plan.works:
        return RunResult(run_plan.counts, report=replace(planned, dry_run=False))
    settings = run_plan.settings
    target = _target(spec, settings)
    window = settings.judge.network.slots
    with Services.open(settings, concurrency=window, wiring=wiring) as services:
        try:
            classifier = PageClassifier(
                services.network, settings, target, clock=wiring.clock
            )
        except MissingDecisionKey as exc:
            raise PlanError(str(exc)) from exc
        store = open_store(spec.output, wiring.clock)
        exit_code: int = ExitCode.FAILURE
        settle = False
        try:
            store.begin_run(
                "classify",
                run_id=services.run_id,
                spec=effective_spec(spec, run_plan.sources),
                tool_version=tool_version(),
            )
            loop = _Classifier(
                store, services, classifier, force=spec.force, window=window
            )
            with store.keeper(on_lost=_lost):
                classified = {work.entry_id: work for work in loop.run(run_plan.works)}
            settle = True
            failed = any(work.outcome == "failed" for work in classified.values())
            exit_code = ExitCode.FAILURE if failed else ExitCode.OK
        except KeyboardInterrupt:
            exit_code = ExitCode.INTERRUPTED
            raise
        finally:
            close_store(store, exit_code, settle=settle)
    works = [classified.get(work.entry_id, work) for work in planned.works]
    report = replace(
        planned,
        works=_with_picks(spec.output, works),
        usage=loop.usage.totals(),
        dry_run=False,
    )
    return RunResult(_counts(report.works), report=report)
