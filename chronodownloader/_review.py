"""The cores of ``inspect`` and ``verify``: page samples, page checks, files.

Inspect. ``--entry-ids`` takes the current stored candidates of these works
in ``--output`` (with ``--providers``, only those of the named providers);
``--provider`` with ``--id`` takes one item. Each item's first ``--first``
pages are sampled at most :data:`PAGE_MAX_PX` pixels
(:func:`~chronodownloader.pages.sample_pages`) under one of the provider's
download slots, into ``<output>/_inspect/<entry_id>/<provider>_<id>/``; a
folder sampled before is replaced. The slot is released before the page
check, which asks the page model one question over the sampled pages
together (``judge_pages``), with the work's input fields as the sought work.
The answer is reported and recorded nowhere: the store records nothing of an
inspect. ``fetch`` and ``drop`` remove a work's scratch folder; ``status``
lists what is left.

An item named by ``--provider``/``--id`` is checked against every work of
``--output`` whose current candidates include it, with its pages in that
work's scratch folder. When no stored work names it, the pages go to
:func:`scratch_root` (``%TEMP%\\chronodownloader\\inspect\\<provider>_<id>``)
and no check is asked (outcome ``sampled``): without a sought work there is
no question to ask.

A judge outage defers that item's check (``deferred``, with
``retry_after``); a refusal or a malformed answer fails it; the other items
go on. A metadata-only provider offers no pages (``skipped``).

Verify. The store's reconciliation, run when a store opens, compares every
``files`` row with the disk: a recorded file that is gone loses its row, and
a ``complete`` work that lost one becomes ``partial`` with the reason
recorded. ``verify`` opens the store for that and reports, per work, the
missing files, the files whose size differs from the record (reported, not
repaired) and the status that results. ``--dry-run`` makes the same
comparison through read-only queries, so nothing changes. Works another run
holds are reported ``blocked`` and left alone.

With ``--pages`` the page check runs over the first ``--first`` page images
the work's ``files`` rows record, read from disk; only the judge call uses
the network. A work without page images (a PDF only) gets no check
(``skipped``). The page check never changes a status: its answer is
reported for the agent to decide (``fetch --force`` another copy, or
``drop``), since a status change on a page answer would need thresholds the
settings do not hold.
"""

from __future__ import annotations

import functools
import logging
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from chronodownloader.coord import CoordinationError
from chronodownloader.matching.judge import (
    JudgeRefused,
    JudgeUnavailable,
    MalformedAnswer,
    MissingJudgeKey,
)
from chronodownloader.net import NetworkError
from chronodownloader.net.credentials import redact_text
from chronodownloader.options.options import Source
from chronodownloader.output.index import write_index
from chronodownloader.output.paths import inspect_dir, sanitize_filename
from chronodownloader.output.store import (
    CandidateRow,
    FileRow,
    Reconciliation,
    Store,
    StoreError,
    WorkInput,
    read_candidates,
    read_files,
    read_work_candidates,
)
from chronodownloader.pages import sample_pages
from chronodownloader.providers.base import ProviderError, UnsupportedCapability
from chronodownloader.settings import Settings
from chronodownloader.spec import InspectSpec, VerifySpec

from ._core import (
    CheckOutcome,
    InspectReport,
    InspectTarget,
    PageCheck,
    PlanError,
    RunPlan,
    RunResult,
    VerifiedWork,
    VerifyReport,
    WorkCounts,
    check_judge_key,
    check_store,
    read_statuses,
)
from ._runtime import Services, Wiring
from ._worker import query_of, unmask

__all__ = [
    "PAGE_MAX_PX",
    "plan_inspect",
    "plan_verify",
    "run_inspect",
    "run_verify",
    "scratch_root",
]

logger = logging.getLogger(__name__)

PAGE_MAX_PX: Final = 1500
"""The longest side of a sampled page in pixels, as in the jev_eval page run."""

POLL_S: Final = 0.25
"""Seconds the main thread waits for a worker before it looks again."""

CONCURRENCY: Final = 3
"""Items sampled or checked at once."""


type _Check = Callable[..., PageCheck]
"""Builds the page check of one item from its outcome and fields."""


def scratch_root() -> Path:
    """Return the folder of the pages of items no stored work names."""
    return Path(tempfile.gettempdir()) / "chronodownloader" / "inspect"


def _item_folder(provider: str, item_id: str) -> str:
    return sanitize_filename(f"{provider}_{item_id}")


def _work_of(row: dict[str, Any]) -> WorkInput:
    """Return the input fields of a ``work_status`` *row*."""
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


def _absent(wanted: Sequence[str], found: Sequence[str] | set[str]) -> list[str]:
    known = set(found)
    return [entry_id for entry_id in dict.fromkeys(wanted) if entry_id not in known]


def _read_candidates(root: Path, entry_id: str) -> list[CandidateRow]:
    try:
        return read_candidates(root, entry_id)
    except StoreError as exc:
        raise PlanError(str(exc)) from exc


def _open(settings: Settings, size: int, wiring: Wiring) -> Services:
    """Open the services of a page check; a missing judge key exits 2."""
    services = Services.open(settings, concurrency=max(1, size), wiring=wiring)
    try:
        services.judge()
    except MissingJudgeKey as exc:
        services.close()
        raise PlanError(str(exc)) from exc
    return services


def _gather(
    services: Services,
    jobs: Sequence[Callable[[], PageCheck]],
    crashed: Callable[[int, Exception], PageCheck],
) -> list[PageCheck]:
    """Run *jobs* on the pool; return their checks in order.

    A job that raises fails its own check (*crashed*); Ctrl+C propagates.
    """
    pending: dict[Future[PageCheck], int] = {
        services.pool.submit(job): index for index, job in enumerate(jobs)
    }
    results: dict[int, PageCheck] = {}
    while pending:
        done, _ = wait(list(pending), POLL_S, FIRST_COMPLETED)
        for future in done:
            index = pending.pop(future)
            try:
                results[index] = future.result()
            # The per-item crash boundary: an unexpected error fails this
            # item's check alone; KeyboardInterrupt is no Exception.
            except Exception as exc:  # noqa: BLE001
                logger.exception("page check %d failed", index)
                results[index] = crashed(index, exc)
    return [results[index] for index in range(len(jobs))]


def _ask(
    services: Services, work: WorkInput, images: list[bytes], check: _Check
) -> PageCheck:
    """Ask the page check over *images*; an outage defers, a refusal fails."""
    pages = len(images)
    try:
        answer = services.judge().judge_pages(query_of(work), images)
    except JudgeUnavailable as exc:
        return check(
            "deferred",
            pages=pages,
            note=f"judge: {exc.reason}",
            retry_after=exc.retry_after,
        )
    except (JudgeRefused, MalformedAnswer, ValueError) as exc:
        return check("failed", pages=pages, note=f"judge: {exc}")
    return check("checked", pages=pages, answer=answer)


def _checker(
    provider: str, item_id: str, entry_id: str | None, folder: Path | None
) -> _Check:
    def check(outcome: CheckOutcome, **fields: Any) -> PageCheck:
        return PageCheck(provider, item_id, entry_id, folder, outcome, **fields)

    return check


def _count(checks: Sequence[PageCheck], total: int | None = None) -> WorkCounts:
    outcomes = [check.outcome for check in checks]
    return WorkCounts(
        total=len(checks) if total is None else total,
        complete=outcomes.count("checked"),
        failed=outcomes.count("failed"),
        deferred=outcomes.count("deferred"),
        skipped=outcomes.count("skipped") + outcomes.count("sampled"),
    )


# -- inspect ------------------------------------------------------------------


def _stored_targets(
    spec: InspectSpec, root: Path, warnings: list[str]
) -> list[InspectTarget]:
    rows = read_statuses(root, spec.entry_ids)
    absent = _absent(spec.entry_ids, set(rows))
    if absent:
        warnings.append(f"entry ids not in the store: {', '.join(absent)}")
    targets: list[InspectTarget] = []
    for entry_id in dict.fromkeys(spec.entry_ids):
        row = rows.get(entry_id)
        if row is None:
            continue
        work = _work_of(row)
        found = 0
        for candidate in _read_candidates(root, entry_id):
            record = candidate.record
            if spec.providers is not None and record.provider not in spec.providers:
                continue
            found += 1
            folder = inspect_dir(root, entry_id) / _item_folder(
                record.provider, record.source_id
            )
            targets.append(
                InspectTarget(
                    record.provider, record.source_id, folder, unmask(record), work
                )
            )
        if not found:
            warnings.append(f"{entry_id}: no stored candidate to inspect")
    return targets


def _item_targets(spec: InspectSpec, root: Path | None) -> list[InspectTarget]:
    """Return the item of ``--provider``/``--id`` once per stored work naming
    it, else once in the temporary scratch folder without a work."""
    assert spec.provider is not None and spec.id is not None
    provider, item_id = spec.provider, spec.id
    targets: list[InspectTarget] = []
    if root is not None:
        try:
            rows = read_work_candidates(root)
        except StoreError as exc:
            raise PlanError(str(exc)) from exc
        entry_ids = dict.fromkeys(
            str(row["entry_id"])
            for row in rows
            if row["current"]
            and (row["provider"], row["source_id"]) == (provider, item_id)
        )
        statuses = read_statuses(root, list(entry_ids)) if entry_ids else {}
        for entry_id in entry_ids:
            candidate = next(
                (
                    row
                    for row in _read_candidates(root, entry_id)
                    if (row.record.provider, row.record.source_id)
                    == (provider, item_id)
                ),
                None,
            )
            if candidate is None or entry_id not in statuses:
                continue
            targets.append(
                InspectTarget(
                    provider,
                    item_id,
                    inspect_dir(root, entry_id) / _item_folder(provider, item_id),
                    unmask(candidate.record),
                    _work_of(statuses[entry_id]),
                )
            )
    if not targets:
        folder = scratch_root() / _item_folder(provider, item_id)
        targets.append(InspectTarget(provider, item_id, folder))
    return targets


def plan_inspect(
    spec: InspectSpec, settings: Settings, sources: Mapping[str, Source]
) -> RunPlan:
    """Plan an ``inspect``: the items to sample and the works to ask about.

    Raises:
        PlanError: The judge key is missing, or the store is refused.
    """
    settings.state_directory()
    check_judge_key("judge", settings)
    root = spec.output
    if root is not None:
        check_store(root)
    warnings: list[str] = []
    if spec.entry_ids:
        assert root is not None
        targets = _stored_targets(spec, root, warnings)
    else:
        targets = _item_targets(spec, root)
        if targets[0].work is None:
            warnings.append(
                f"{spec.provider}:{spec.id} is no stored candidate of a work"
                + ("" if root is None else f" in {root}")
                + "; its pages are sampled without the page check"
            )
    planned = tuple(
        PageCheck(
            target.provider,
            target.item_id,
            None if target.work is None else target.work.entry_id,
            target.folder,
            "planned",
        )
        for target in targets
    )
    return RunPlan(
        spec,
        settings,
        sources,
        WorkCounts(total=len(targets)),
        warnings=tuple(warnings),
        report=InspectReport(planned),
        targets=tuple(targets),
    )


def inspect_item(services: Services, target: InspectTarget, first: int) -> PageCheck:
    """Sample *target*'s first pages and ask the page check (worker side)."""
    entry_id = None if target.work is None else target.work.entry_id
    check = _checker(target.provider, target.item_id, entry_id, target.folder)
    try:
        provider = services.registry.get(target.provider)
        if target.folder.exists():
            shutil.rmtree(target.folder)
        with services.network.hold_slot(target.provider) as slot:
            sample = sample_pages(
                provider,
                target.record if target.record is not None else target.item_id,
                network=services.network,
                slot=slot,
                dest_dir=target.folder,
                first_n=first,
                max_px=PAGE_MAX_PX,
            )
        images = [
            (target.folder / page.file.name).read_bytes()
            for page in sample.sampled
            if page.file is not None
        ]
    except UnsupportedCapability as exc:
        return check("skipped", note=str(exc))
    except (ProviderError, NetworkError, CoordinationError, OSError, ValueError) as exc:
        return check("failed", note=redact_text(str(exc)))
    stopped = None if sample.stopped is None else f"sample stopped: {sample.stopped}"
    if not images:
        return check("failed", note=stopped or "no page could be sampled")
    if target.work is None:
        return check("sampled", pages=len(images), note="no stored work to ask about")
    return _ask(services, target.work, images, check)


def run_inspect(run_plan: RunPlan, wiring: Wiring) -> RunResult:
    """Sample and check the plan's items; report the answers."""
    spec = run_plan.spec
    assert isinstance(spec, InspectSpec)
    targets = run_plan.targets
    if not targets:
        return RunResult(run_plan.counts, report=InspectReport())
    with _open(run_plan.settings, min(CONCURRENCY, len(targets)), wiring) as services:
        checks = _gather(
            services,
            [
                functools.partial(inspect_item, services, target, spec.first)
                for target in targets
            ],
            lambda index, exc: _crashed(targets[index], exc),
        )
    return RunResult(_count(checks), report=InspectReport(tuple(checks)))


def _crashed(target: InspectTarget, exc: Exception) -> PageCheck:
    return PageCheck(
        target.provider,
        target.item_id,
        None if target.work is None else target.work.entry_id,
        target.folder,
        "failed",
        note=redact_text(f"error: {type(exc).__name__}: {exc}"),
    )


# -- verify -------------------------------------------------------------------


def _read_files(root: Path, entry_id: str) -> list[FileRow]:
    try:
        return read_files(root, entry_id)
    except StoreError as exc:
        raise PlanError(str(exc)) from exc


def _compare(
    work_dir: Path, files: Sequence[FileRow]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the recorded files missing on disk and those of another size."""
    missing: list[str] = []
    mismatched: list[str] = []
    for row in files:
        path = work_dir / row.path
        if not path.is_file():
            missing.append(row.path)
        elif path.stat().st_size != row.size:
            mismatched.append(row.path)
    return tuple(missing), tuple(mismatched)


def _verified(root: Path, row: dict[str, Any]) -> VerifiedWork:
    """Return the read-only comparison of a work's files with its record."""
    entry_id, folder = str(row["entry_id"]), str(row["folder"])
    files = _read_files(root, entry_id)
    missing, mismatched = _compare(root / folder, files)
    status = str(row["status"])
    return VerifiedWork(
        entry_id,
        folder,
        status,
        len(files),
        missing,
        mismatched,
        downgraded=bool(missing) and status == "complete",
    )


def _verify_counts(works: Sequence[VerifiedWork]) -> WorkCounts:
    tally = dict.fromkeys(("complete", "failed", "deferred", "blocked", "skipped"), 0)
    for work in works:
        outcome = None if work.pages is None else work.pages.outcome
        if work.status == "blocked":
            key = "blocked"
        elif not work.intact or outcome == "failed":
            key = "failed"
        elif outcome == "deferred":
            key = "deferred"
        elif work.files == 0:
            key = "skipped"
        else:
            key = "complete"
        tally[key] += 1
    return WorkCounts(total=len(works), **tally)


def plan_verify(
    spec: VerifySpec, settings: Settings, sources: Mapping[str, Source]
) -> RunPlan:
    """Plan a ``verify``: compare the works' files with their records.

    The comparison is read-only and is the report of a dry run.

    Raises:
        PlanError: The store is refused, or ``--pages`` lacks the judge key.
    """
    if spec.pages:
        settings.state_directory()
        check_judge_key("judge", settings)
    root = spec.output
    check_store(root)
    rows = read_statuses(root, spec.entry_ids or None)
    warnings: list[str] = []
    absent = _absent(spec.entry_ids, set(rows))
    if absent:
        warnings.append(f"entry ids not in the store: {', '.join(absent)}")
    works = tuple(_verified(root, row) for row in rows.values())
    return RunPlan(
        spec,
        settings,
        sources,
        _verify_counts(works),
        warnings=tuple(warnings),
        report=VerifyReport(works, dry_run=True),
    )


def _reconcile(root: Path, wiring: Wiring) -> Reconciliation:
    """Open the store, which reconciles it with the disk; return the repairs."""
    try:
        store = Store(root, clock=wiring.clock)
    except StoreError as exc:
        raise PlanError(str(exc)) from exc
    store.close()
    try:
        write_index(root)
    except (StoreError, OSError) as exc:
        logger.warning("index.csv not written in %s: %s", root, exc)
    return store.reconciliation or Reconciliation()


def _page_paths(root: Path, work: VerifiedWork, first: int) -> tuple[Path, ...]:
    """Return the work's first *first* recorded page images, by item."""
    images = sorted(
        (row for row in _read_files(root, work.entry_id) if row.kind == "image"),
        key=lambda row: row.item,
    )
    return tuple(root / work.folder / row.path for row in images[:first])


def verify_pages(
    services: Services,
    work: WorkInput,
    selected: tuple[str, str],
    folder: Path,
    paths: Sequence[Path],
) -> PageCheck:
    """Ask the page check over a work's page files (worker side)."""
    check = _checker(selected[0], selected[1], work.entry_id, folder)
    try:
        images = [path.read_bytes() for path in paths]
    except OSError as exc:
        return check("failed", note=str(exc))
    return _ask(services, work, images, check)


@dataclass(frozen=True)
class _PageJob:
    """The page check of one verified work: its sought fields and files."""

    index: int
    work: WorkInput
    selected: tuple[str, str]
    folder: Path
    paths: tuple[Path, ...]


def _with_pages(
    run_plan: RunPlan,
    works: list[VerifiedWork],
    rows: dict[str, dict[str, Any]],
    wiring: Wiring,
) -> list[VerifiedWork]:
    """Return *works* with the page check of each that has page images."""
    spec = run_plan.spec
    assert isinstance(spec, VerifySpec)
    root = spec.output
    jobs: list[_PageJob] = []
    for index, work in enumerate(works):
        if work.status == "blocked" or work.files == 0:
            continue
        row = rows[work.entry_id]
        selected = (
            str(row["selected_provider"] or row["last_provider"] or ""),
            str(row["selected_source_id"] or row["last_source_id"] or ""),
        )
        folder = root / work.folder
        paths = _page_paths(root, work, spec.first)
        if paths:
            jobs.append(_PageJob(index, _work_of(row), selected, folder, paths))
            continue
        skipped = PageCheck(
            *selected,
            work.entry_id,
            folder,
            "skipped",
            note="no page images recorded",
        )
        works[index] = replace(work, pages=skipped)
    if not jobs:
        return works
    with _open(run_plan.settings, min(CONCURRENCY, len(jobs)), wiring) as services:
        checks = _gather(
            services,
            [
                functools.partial(
                    verify_pages,
                    services,
                    job.work,
                    job.selected,
                    job.folder,
                    job.paths,
                )
                for job in jobs
            ],
            lambda position, exc: PageCheck(
                *jobs[position].selected,
                jobs[position].work.entry_id,
                jobs[position].folder,
                "failed",
                note=redact_text(f"error: {type(exc).__name__}: {exc}"),
            ),
        )
    for job, check in zip(jobs, checks, strict=True):
        works[job.index] = replace(works[job.index], pages=check)
    return works


def run_verify(run_plan: RunPlan, wiring: Wiring) -> RunResult:
    """Reconcile the store with the disk, then report the planned works.

    The missing files and the downgrades are those the reconciliation
    repaired; a work another run holds keeps the read-only comparison.
    """
    spec = run_plan.spec
    assert isinstance(spec, VerifySpec)
    planned = run_plan.report
    assert isinstance(planned, VerifyReport)
    if not planned.works:
        return RunResult(run_plan.counts, report=VerifyReport())
    root = spec.output
    files = {work.entry_id: work.files for work in planned.works}
    repaired = _reconcile(root, wiring)
    rows = read_statuses(root, list(files))
    works: list[VerifiedWork] = []
    for entry_id, count in files.items():
        row = rows.get(entry_id)
        if row is None:
            continue
        after = _verified(root, row)
        if after.status != "blocked":
            missing = tuple(path for key, path in repaired.missing if key == entry_id)
            after = replace(
                after,
                files=count,
                missing=missing,
                downgraded=entry_id in repaired.downgraded,
            )
        works.append(after)
    if spec.pages:
        works = _with_pages(run_plan, works, rows, wiring)
    return RunResult(_verify_counts(works), report=VerifyReport(tuple(works)))
