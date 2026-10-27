"""The cores of ``status``, ``drop`` and ``providers``.

Status. The works of ``work_status`` in ``--only`` (every status by
default), narrowed by ``--entry-ids``; with ``--input``, the CSV's works
without a store entry are listed as ``new``. The listing names the review
queue (the ``needs_review`` works with their reasons), the ``unstored``
folders and the ``inspect`` scratch folders left under ``<output>/_inspect/``.
``status`` sends nothing and writes nothing; it is planned, and the plan is
its report.

Drop. Exactly one of ``--entry-ids``, ``--only`` and ``--all`` selects the
works, by their ``work_status`` rows. :meth:`~chronodownloader.output.store.
Store.drop` deletes their rows, recorded files, staging folders and
``inspect`` scratch, and refuses works another run holds (counted
``blocked``); ``index.csv`` is rendered afterwards. A dry run lists the same
through the store's dry run, which only reads.

Providers. The 17 providers in the settings order, each with its
capabilities, its key's environment variable and whether it is set, and the
breaker and quota state the coordination database holds, read without
writing (:func:`~chronodownloader.coord.read_snapshot`).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from chronodownloader.coord import (
    BreakerState,
    CoordinationError,
    DatabaseBusyError,
    QuotaStatus,
    Snapshot,
    read_snapshot,
)
from chronodownloader.events import RunEvents
from chronodownloader.inputs import InputError, read_works
from chronodownloader.options.options import Source
from chronodownloader.output.index import write_index
from chronodownloader.output.paths import (
    INSPECT_DIR,
    inspect_dir,
    work_folder_name,
)
from chronodownloader.output.store import (
    DATABASE_NAME,
    DropReport,
    Store,
    StoreError,
    WorkInput,
    unstored_folders,
)
from chronodownloader.providers.base import ProviderError
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import AnnasArchiveSettings, Settings, SettingsError
from chronodownloader.spec import NEW, DropSpec, StatusSpec

from ._core import (
    Leftover,
    PlanError,
    PlannedWork,
    ProvidersReport,
    ProviderState,
    RunPlan,
    RunResult,
    StatusReport,
    StatusRow,
    WorkCounts,
    check_store,
    read_statuses,
)
from ._runtime import Wiring

__all__ = ["plan_drop", "plan_status", "run_drop", "run_providers"]

logger = logging.getLogger(__name__)

_COUNTED = ("complete", "partial", "failed", "no_match", "needs_review", "deferred")


# -- status -------------------------------------------------------------------


def _status_row(row: Mapping[str, Any]) -> StatusRow:
    selected = None
    if row["selected_provider"] is not None:
        selected = f"{row['selected_provider']}:{row['selected_source_id']}"
    return StatusRow(
        entry_id=str(row["entry_id"]),
        title=str(row["title"]),
        status=str(row["status"]),
        folder=str(row["folder"]),
        reason=row["status_reason"],
        retry_after=row["retry_after"],
        selected=selected,
        files=int(row["file_count"] or 0),
        pages_present=row["pages_present"],
        pages_expected=row["pages_expected"],
    )


def _input_rows(
    spec: StatusSpec, settings: Settings, stored: Mapping[str, Any]
) -> tuple[list[StatusRow], tuple[str, ...]]:
    """Return the CSV's works without a store entry as ``new`` rows."""
    if spec.input is None:
        return [], ()
    try:
        works = read_works(spec.input)
    except InputError as exc:
        raise PlanError(str(exc)) from exc
    wanted = set(spec.entry_ids)
    rows = [
        StatusRow(
            entry_id=work.entry_id,
            title=work.title,
            status=NEW,
            folder=work_folder_name(
                work.entry_id,
                work.title,
                creator=work.creator,
                year=work.year,
                naming=settings.naming,
            ),
        )
        for work in works.works
        if work.entry_id not in stored and (not wanted or work.entry_id in wanted)
    ]
    return rows, works.warnings


def _leftovers(
    root: Path, stored: Sequence[str], entry_ids: Sequence[str]
) -> tuple[Leftover, ...]:
    """Return the scratch folders under ``<root>/_inspect/``."""
    base = root / INSPECT_DIR
    if not base.is_dir():
        return ()
    # Scratch folder name to entry id, for the ids the store or the
    # selection knows; any other folder is listed by its name.
    known = {
        inspect_dir(root, entry_id).name: entry_id for entry_id in (*stored, *entry_ids)
    }
    names = {inspect_dir(root, entry_id).name for entry_id in stored}
    wanted = {inspect_dir(root, entry_id).name for entry_id in entry_ids}
    found: list[Leftover] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or (wanted and entry.name not in wanted):
            continue
        items = tuple(sorted(child.name for child in entry.iterdir() if child.is_dir()))
        label = known.get(entry.name, entry.name)
        found.append(Leftover(label, entry, items, entry.name in names))
    return tuple(found)


def _status_counts(rows: Sequence[StatusRow]) -> WorkCounts:
    statuses = [row.status for row in rows]
    counted = {status: statuses.count(status) for status in _COUNTED}
    return WorkCounts(total=len(rows), blocked=statuses.count("blocked"), **counted)


def plan_status(
    spec: StatusSpec, settings: Settings, sources: Mapping[str, Source]
) -> RunPlan:
    """Plan (and so list) a ``status`` of ``--output``; nothing is written.

    Raises:
        PlanError: The store is refused, or the works CSV cannot be read.
    """
    root = spec.output
    check_store(root)
    every = read_statuses(root)
    wanted = set(spec.entry_ids)
    only = set(spec.only)
    rows = [
        _status_row(row)
        for entry_id, row in every.items()
        if (not wanted or entry_id in wanted) and row["status"] in only
    ]
    added, warnings = _input_rows(spec, settings, every)
    if NEW in only:
        rows += added
    known = set(every) | {row.entry_id for row in added}
    absent = [
        entry_id for entry_id in dict.fromkeys(spec.entry_ids) if entry_id not in known
    ]
    notes = list(warnings)
    if absent:
        notes.append(f"entry ids not in the store: {', '.join(absent)}")
    try:
        unstored = tuple(unstored_folders(root))
    except StoreError as exc:
        raise PlanError(str(exc)) from exc
    report = StatusReport(
        works=tuple(rows),
        unstored=unstored,
        leftovers=_leftovers(root, list(every), spec.entry_ids),
    )
    return RunPlan(
        spec,
        settings,
        sources,
        _status_counts(rows),
        warnings=tuple(notes),
        report=report,
    )


# -- drop -----------------------------------------------------------------------


def _drop_counts(report: DropReport, total: int, skipped: int) -> WorkCounts:
    leased = sum(1 for refusal in report.refused if refusal.reason.startswith("leased"))
    return WorkCounts(
        total=total,
        complete=len(report.dropped),
        blocked=leased,
        skipped=skipped + len(report.refused) - leased,
    )


def plan_drop(
    spec: DropSpec, settings: Settings, sources: Mapping[str, Source]
) -> RunPlan:
    """Plan a ``drop``: the works its selection names in the store.

    A dry run lists what the store's drop would delete and keep.

    Raises:
        PlanError: The store is refused.
    """
    root = spec.output
    check_store(root)
    rows = read_statuses(root)
    warnings: list[str] = []
    absent: list[str] = []
    if spec.all:
        chosen = list(rows)
    elif spec.only:
        chosen = [key for key, row in rows.items() if row["status"] in spec.only]
    else:
        wanted = list(dict.fromkeys(spec.entry_ids))
        chosen = [entry_id for entry_id in wanted if entry_id in rows]
        absent = [entry_id for entry_id in wanted if entry_id not in rows]
        if absent:
            warnings.append(f"entry ids not in the store: {', '.join(absent)}")
    works = tuple(
        PlannedWork(
            WorkInput(entry_id, str(rows[entry_id]["title"])),
            str(rows[entry_id]["folder"]),
            str(rows[entry_id]["status"]),
        )
        for entry_id in chosen
    )
    skipped = len(absent)
    total = len(works) + skipped
    counts = WorkCounts(total=total, skipped=skipped)
    report: DropReport | None = None
    if spec.dry_run:
        report = DropReport(dry_run=True)
        if works and (root / DATABASE_NAME).is_file():
            report = _preview(root, chosen, set(spec.only) if spec.only else None)
        counts = _drop_counts(report, total, skipped)
    return RunPlan(
        spec,
        settings,
        sources,
        counts,
        works=works,
        warnings=tuple(warnings),
        report=report,
    )


def _preview(
    root: Path, entry_ids: Sequence[str], statuses: set[str] | None
) -> DropReport:
    """Return the store's dry run of the drop.

    The store opens without reconciliation; on an existing database that
    reads and writes nothing, and the dry run itself only reads.
    """
    try:
        with Store(root, reconcile=False) as store:
            return store.drop(entry_ids, dry_run=True, statuses=statuses)
    except StoreError as exc:
        raise PlanError(str(exc)) from exc


def run_drop(run_plan: RunPlan, wiring: Wiring) -> RunResult:
    """Drop the planned works; render ``index.csv`` afterwards."""
    spec = run_plan.spec
    assert isinstance(spec, DropSpec)
    entry_ids = [planned.work.entry_id for planned in run_plan.works]
    if not entry_ids:
        return RunResult(run_plan.counts, report=DropReport(dry_run=False))
    try:
        store = Store(spec.output, clock=wiring.clock)
    except StoreError as exc:
        raise PlanError(str(exc)) from exc
    # --only is checked again where the works are deleted: a work may have
    # changed status since planning.
    statuses = set(spec.only) if spec.only else None
    try:
        report = store.drop(entry_ids, statuses=statuses)
    finally:
        store.close()
    try:
        write_index(spec.output)
    except (StoreError, sqlite3.Error, OSError) as exc:
        logger.warning("index.csv not written in %s: %s", spec.output, exc)
    counts = _drop_counts(report, run_plan.counts.total, run_plan.counts.skipped)
    return RunResult(counts, report=report)


# -- providers --------------------------------------------------------------------


def _quota_limit(settings: Settings, key: str) -> int | None:
    block = settings.providers.get(key)
    if isinstance(block, AnnasArchiveSettings) and block.quota.enabled:
        return block.quota.daily_limit
    return None


def _key_of(settings: Settings, service: str | None) -> tuple[str | None, bool | None]:
    if service is None:
        return None, None
    try:
        variable = settings.api_key_env(service)
    except SettingsError:
        return None, False
    return variable, bool(os.environ.get(variable, "").strip())


def provider_state(
    key: str, registry: Registry, settings: Settings, snapshot: Snapshot
) -> ProviderState:
    """Return *key*'s capabilities, key and coordination state."""
    try:
        adapter = registry.get(key)
    except (ProviderError, TypeError, ValueError) as exc:
        return ProviderState(
            key, False, False, False, None, None, None, available=False, note=str(exc)
        )
    capabilities = adapter.capabilities
    variable, key_set = _key_of(settings, capabilities.api_key)
    available = bool(getattr(adapter, "available", True))
    state = ProviderState(
        key=key,
        iiif=capabilities.iiif,
        quota=capabilities.quota,
        downloads=capabilities.downloads,
        image_max_px=capabilities.image_max_px,
        api_key=variable,
        key_set=key_set,
        available=available,
        note=None if available else "not configured in the settings",
    )
    breaker = snapshot.breakers.get(key)
    if breaker is not None:
        state = _with_breaker(state, breaker)
    if capabilities.quota:
        window = snapshot.quotas.get(key)
        limit = _quota_limit(settings, key)
        if window is not None and window.daily_limit is not None:
            limit = window.daily_limit
        state = _with_quota(state, window, limit)
    return state


def _with_breaker(state: ProviderState, breaker: BreakerState) -> ProviderState:
    return replace(
        state,
        breaker=str(breaker.status),
        failures=breaker.failures,
        breaker_until=breaker.until,
        breaker_cause=breaker.cause,
    )


def _with_quota(
    state: ProviderState, window: QuotaStatus | None, limit: int | None
) -> ProviderState:
    return replace(
        state,
        quota_used=0 if window is None else window.used,
        quota_limit=limit,
        quota_reset_at=None if window is None else window.reset_at,
    )


def run_providers(run_plan: RunPlan, events: RunEvents, wiring: Wiring) -> RunResult:
    """List the providers with their breaker state.

    A coordination database that cannot be read is reported as a warning, and
    the breakers then show closed.
    """
    settings = run_plan.settings
    try:
        snapshot = read_snapshot(
            settings.state_directory(),
            clock=wiring.clock,
            busy_timeout=settings.coordination.busy_timeout_s,
        )
    except (CoordinationError, DatabaseBusyError, sqlite3.Error) as exc:
        events.warning(f"coordination state not read: {exc}")
        snapshot = Snapshot()
    registry = Registry(settings, factories=wiring.providers)
    states = tuple(
        provider_state(key, registry, settings, snapshot) for key in registry.keys
    )
    return RunResult(WorkCounts(total=len(states)), report=ProvidersReport(states))
