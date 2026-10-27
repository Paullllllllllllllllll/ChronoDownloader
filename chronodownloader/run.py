"""The core of every command: plan a run, then run it.

:func:`plan` checks a resolved spec against the settings and the output root
and lists what the run would do; :func:`run` carries the plan out and reports
through :class:`~chronodownloader.events.RunEvents`. Neither prints, prompts
or exits. A run that cannot be planned raises :class:`PlanError`, whose
``exit_code`` is the process exit code.

Planning never sends a request and never writes: it reads the works CSV
(:mod:`chronodownloader.inputs`), checks the decision model's key under
``--matching judge`` and ``hybrid``, and reads the store of the output root
through its read-only views. ``--dry-run`` reports the plan.

Work selection (``download``, ``search``). The works of the CSV, narrowed by
``--entry-ids``, are taken by their status: ``new`` (no store entry or no
outcome yet), ``blocked`` (another run's unexpired lease) or the stored
status, which must be one of ``--only`` (``complete`` too under
``--force``; ``complete``, ``no_match`` and ``needs_review`` too under
``--research``). A ``deferred`` work waits until its ``retry_after``. A
work whose matching fields in the CSV differ from the stored ones is
searched and judged anew, as under ``--research``. A new
work whose folder exists without a store entry (``unstored``) is skipped
unless ``--force`` takes the folder over. ``--limit`` applies last. The
loop applies the status rules again when it takes a work's lease
(:func:`reselect`): a work another run finished meanwhile is skipped.

Runs. ``download``, ``search`` and ``fetch`` run the loop of
:mod:`chronodownloader._loop` over the services of
:mod:`chronodownloader._runtime`: one coordinator, network, registry and
worker pool per run, and one store with a lease keeper per output root.
After a run that wrote a store, ``index.csv`` is rendered from it.

Checks and listings. ``inspect`` and ``verify`` are planned and run by
:mod:`chronodownloader._review` (page samples, page checks, the files of
the store); ``status``, ``drop`` and ``providers`` by
:mod:`chronodownloader._listing`. Their results carry a report
(:data:`Report`) beside the counts; the plan of ``status`` is its report,
and the plans of ``inspect``, ``verify`` and ``drop`` hold what their dry
runs report.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from chronodownloader._budget import SizeBudget
from chronodownloader._core import (
    FetchTarget,
    InspectReport,
    Leftover,
    PageCheck,
    PlanError,
    PlannedWork,
    ProvidersReport,
    ProviderState,
    Report,
    RunPlan,
    RunResult,
    SearchRow,
    StatusReport,
    StatusRow,
    VerifiedWork,
    VerifyReport,
    WorkCounts,
    check_judge_key,
    check_store,
    read_statuses,
)
from chronodownloader._listing import plan_drop, plan_status, run_drop, run_providers
from chronodownloader._loop import DownloadOptions, MatchOptions, Tally, WorkLoop
from chronodownloader._review import plan_inspect, plan_verify, run_inspect, run_verify
from chronodownloader._runtime import Services, Wiring
from chronodownloader.events import RunEvents
from chronodownloader.iiif.urls import extract_item_id_from_url, is_iiif_manifest_url
from chronodownloader.inputs import InputError, read_works
from chronodownloader.matching.judge import MissingJudgeKey
from chronodownloader.matching.policy import (
    EDITION_POLICIES,
    MODES,
)
from chronodownloader.net.credentials import redact_text
from chronodownloader.options.options import Source
from chronodownloader.output.index import write_index
from chronodownloader.output.paths import work_folder_name
from chronodownloader.output.store import (
    DropReport,
    Lease,
    Store,
    StoreError,
    WorkInput,
    unstored_folders,
)
from chronodownloader.process.contract import ExitCode
from chronodownloader.process.heartbeat import Heartbeat
from chronodownloader.providers.base import FORMATS, item_id_from_url
from chronodownloader.providers.registry import Registry, host_key
from chronodownloader.settings import Naming, Settings
from chronodownloader.spec import (
    CommandSpec,
    DownloadSpec,
    DropSpec,
    FetchSpec,
    FilesSpec,
    InspectSpec,
    MatchingSpec,
    ProvidersSpec,
    SearchSpec,
    SizeLimits,
    StatusSpec,
    VerifySpec,
    WorkSelection,
    command_of,
    effective_spec,
)

__all__ = [
    "DropReport",
    "FetchTarget",
    "InspectReport",
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
    "plan",
    "read_statuses",
    "reselect",
    "run",
    "select_works",
]

logger = logging.getLogger(__name__)

NEW = "new"


def _tool_version() -> str | None:
    try:
        return version("chronodownloader")
    except PackageNotFoundError:
        return None


# -- planning -------------------------------------------------------------------


def _epoch(text: str | None) -> float | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _unstored(root: Path | None) -> set[str]:
    if root is None:
        return set()
    try:
        return {name.casefold() for name in unstored_folders(root)}
    except StoreError as exc:
        raise PlanError(str(exc)) from exc


REOPENED = ("complete", "no_match", "needs_review")
"""The statuses ``--research`` reopens (decision D7)."""

BLOCKED = "blocked"
UNFINISHED = (NEW, "partial", "failed", "deferred")
"""Stored outcomes a work planned as ``blocked`` is taken with at its lease
(a deferred one only past its ``retry_after``)."""

MATCHING_FIELDS = (
    "title",
    "creator",
    "year",
    "place",
    "publisher",
    "language",
    "edition",
    "link",
)
"""The input fields the search and the judge read."""


def _input_changed(work: WorkInput, row: Mapping[str, Any]) -> bool:
    """Return True when the input names the work otherwise than the store.

    An empty optional field keeps its stored value (as the lease
    acquisition does), so only a given value that differs counts; the link
    is compared as stored, credentials masked.
    """
    for name in MATCHING_FIELDS:
        value = getattr(work, name)
        if name == "link" and value is not None:
            value = redact_text(value)
        if value is not None and value != row.get(name):
            return True
    return False


def _filter_entry_ids(
    works: Sequence[WorkInput], entry_ids: Sequence[str]
) -> tuple[Sequence[WorkInput], list[str]]:
    """Keep the works *entry_ids* names (all when empty); warn on absent ids."""
    if not entry_ids:
        return works, []
    wanted = set(entry_ids)
    known = {work.entry_id for work in works}
    absent = [entry_id for entry_id in entry_ids if entry_id not in known]
    warnings = [f"entry ids not in the input: {', '.join(absent)}"] if absent else []
    return [work for work in works if work.entry_id in wanted], warnings


def _open_statuses(selection: WorkSelection, *, force: bool) -> set[str]:
    """Return the statuses a run takes: ``--only``, ``--force``, ``--research``."""
    only = set(selection.only) | ({"complete"} if force else set())
    if selection.research:
        only |= set(REOPENED)
    return only


def _folder_and_status(
    work: WorkInput, row: Mapping[str, Any] | None, naming: Naming
) -> tuple[str, str]:
    """Return the work's stored folder and status, or a new folder name."""
    if row is not None:
        return str(row["folder"]), str(row["status"])
    folder = work_folder_name(
        work.entry_id,
        work.title,
        creator=work.creator,
        year=work.year,
        naming=naming,
    )
    return folder, NEW


def _waiting(status: str, row: Mapping[str, Any] | None, now: float) -> bool:
    """Return True for a deferred work whose ``retry_after`` lies after *now*."""
    retry_after = _epoch(row.get("retry_after")) if row is not None else None
    return status == "deferred" and retry_after is not None and retry_after > now


def _selection_warnings(changed: Sequence[str], held_folders: int) -> list[str]:
    """Return the warnings on changed inputs and on unstored folders."""
    warnings: list[str] = []
    if changed:
        warnings.append(
            f"{len(changed)} work(s) changed in the input since their last run; "
            f"their candidates are searched and judged anew: {', '.join(changed)}"
        )
    if held_folders:
        warnings.append(
            f"{held_folders} work(s) skipped: their folders exist without a store "
            "entry (unstored); --force takes them over"
        )
    return warnings


def select_works(
    works: Sequence[WorkInput],
    root: Path | None,
    selection: WorkSelection,
    *,
    force: bool,
    naming: Naming,
    now: float,
) -> tuple[tuple[PlannedWork, ...], WorkCounts, tuple[str, ...]]:
    """Return the works a run takes, the planned counts and warnings.

    *works* are the input's works in order; *root* the output root (None:
    no store, every work is new). See the module docstring for the rules.

    Raises:
        PlanError: The store's database is refused.
    """
    works, warnings = _filter_entry_ids(works, selection.entry_ids)
    rows = read_statuses(root) if root is not None else {}
    unstored = _unstored(root)
    only = _open_statuses(selection, force=force)
    taken: list[PlannedWork] = []
    changed: list[str] = []
    skipped = 0
    held_folders = 0
    for work in works:
        row = rows.get(work.entry_id)
        folder, status = _folder_and_status(work, row, naming)
        if row is None and folder.casefold() in unstored and not force:
            held_folders += 1
            skipped += 1
            continue
        if status not in only or _waiting(status, row, now):
            skipped += 1
            continue
        renewed = row is not None and _input_changed(work, row)
        if renewed:
            changed.append(work.entry_id)
        taken.append(PlannedWork(work, folder, status, changed=renewed))
    warnings.extend(_selection_warnings(changed, held_folders))
    if selection.limit is not None and len(taken) > selection.limit:
        skipped += len(taken) - selection.limit
        taken = taken[: selection.limit]
    counts = WorkCounts(total=len(works), skipped=skipped)
    return tuple(taken), counts, tuple(warnings)


def reselect(
    planned: PlannedWork,
    row: Mapping[str, Any] | None,
    *,
    selection: WorkSelection,
    force: bool,
    clock: Callable[[], float],
) -> PlannedWork | None:
    """Return *planned* as the run takes it now, or None if it left the selection.

    The run loop asks while it takes the work's lease: *row* is the work's
    ``work_status`` row read inside that acquisition (None: not stored). The
    rules of :func:`select_works` apply to the stored status, so a work
    another run finished since planning is no longer taken; a work taken
    again has its ``changed`` mark read anew. A work planned as ``blocked``
    is also taken while its stored outcome is unfinished (:data:`UNFINISHED`).
    """
    stored = row.get("stored_status") if row is not None else None
    status = str(stored) if stored else NEW
    statuses = _open_statuses(selection, force=force)
    if planned.status == BLOCKED:
        statuses |= set(UNFINISHED)
    if status not in statuses:
        return None
    if _waiting(status, row, clock()):
        return None
    changed = row is not None and _input_changed(planned.work, row)
    return replace(planned, status=status, changed=changed)


def _read_input(path: Path) -> tuple[tuple[WorkInput, ...], tuple[str, ...]]:
    try:
        works = read_works(path)
    except InputError as exc:
        raise PlanError(str(exc)) from exc
    return works.works, works.warnings


def _plan_works(
    works: Sequence[WorkInput],
    warnings: Sequence[str],
    root: Path | None,
    selection: WorkSelection,
    settings: Settings,
    *,
    force: bool,
    now: float,
) -> tuple[tuple[PlannedWork, ...], WorkCounts, tuple[str, ...]]:
    if root is not None:
        check_store(root)
    planned, counts, notes = select_works(
        works, root, selection, force=force, naming=settings.naming, now=now
    )
    return planned, counts, (*warnings, *notes)


def _plan_download(
    spec: DownloadSpec, settings: Settings, sources: Mapping[str, Source], now: float
) -> RunPlan:
    settings.state_directory()
    check_judge_key(spec.matching.mode, settings)
    works, warnings = _read_input(spec.input)
    planned, counts, notes = _plan_works(
        works,
        warnings,
        spec.output,
        spec.selection,
        settings,
        force=spec.run.force,
        now=now,
    )
    return RunPlan(spec, settings, sources, counts, planned, notes)


def _plan_search(
    spec: SearchSpec, settings: Settings, sources: Mapping[str, Source], now: float
) -> RunPlan:
    settings.state_directory()
    check_judge_key(spec.matching.mode, settings)
    query = spec.query
    if query.input is not None:
        works, warnings = _read_input(query.input)
    else:
        assert query.title is not None
        year = None if query.year is None else str(query.year)
        works = (WorkInput(query.entry_id, query.title, query.creator, year),)
        warnings = ()
    planned, counts, notes = _plan_works(
        works, warnings, spec.output, spec.selection, settings, force=False, now=now
    )
    return RunPlan(spec, settings, sources, counts, planned, notes)


def _fetch_work(spec: FetchSpec, settings: Settings) -> tuple[PlannedWork, str | None]:
    """Return the work of a fetch and its stored status.

    Raises:
        PlanError: A new work without ``--title``, or one whose folder
            exists without a store entry and no ``--force``.
    """
    item = spec.item
    row = read_statuses(spec.output, [item.entry_id]).get(item.entry_id)
    if row is not None:
        work = WorkInput(
            entry_id=item.entry_id,
            title=item.title or str(row["title"]),
            creator=row["creator"],
            year=row["year"],
            place=row["place"],
            publisher=row["publisher"],
            language=row["language"],
            edition=row["edition"],
            link=row["link"],
        )
        planned = PlannedWork(work, str(row["folder"]), str(row["status"]))
        return planned, row["stored_status"]
    if item.title is None:
        raise PlanError(
            f"work {item.entry_id!r} is not in the store at {spec.output}; "
            "give its title with --title TEXT"
        )
    folder = work_folder_name(item.entry_id, item.title, naming=settings.naming)
    if folder.casefold() in _unstored(spec.output) and not spec.run.force:
        raise PlanError(
            f"folder {folder!r} exists in {spec.output} without a store entry "
            "(unstored); --force takes it over"
        )
    return PlannedWork(WorkInput(item.entry_id, item.title), folder, NEW), None


def _fetch_url(url: str, settings: Settings) -> tuple[str, str, bool]:
    """Return the provider, item id and manifest flag of a ``fetch --url``.

    A IIIF manifest URL names its item; any other URL must be one the
    serving provider's ``item_id_from_url`` hook resolves.

    Raises:
        PlanError: No provider serves the host, or its hook does not
            resolve the URL (exit 2).
    """
    provider = host_key(url)
    if provider is None:
        raise PlanError(
            f"--url {redact_text(url)} names no provider; name the item with "
            "--provider KEY and --id ID",
            items_total=1,
        )
    if is_iiif_manifest_url(url):
        return provider, extract_item_id_from_url(url), True
    item_id = item_id_from_url(Registry(settings).get(provider), url)
    if item_id is None:
        raise PlanError(
            f"--url {redact_text(url)} names no {provider} item this tool can "
            "resolve; give the item's IIIF manifest URL or page URL, or name it "
            "with --provider KEY and --id ID",
            items_total=1,
        )
    return provider, item_id, False


def _plan_fetch(
    spec: FetchSpec, settings: Settings, sources: Mapping[str, Source]
) -> RunPlan:
    settings.state_directory()
    check_store(spec.output)
    item = spec.item
    planned, stored = _fetch_work(spec, settings)
    if stored == "needs_review" and item.reason is None:
        raise PlanError(
            f"work {item.entry_id!r} needs review; record why this item is the "
            "right one with --reason TEXT",
            items_total=1,
        )
    if item.url is not None:
        provider, item_id, manifest = _fetch_url(item.url, settings)
    else:
        assert item.provider is not None and item.id is not None
        provider, item_id, manifest = item.provider, item.id, False
    target = FetchTarget(
        planned, provider, item_id, item.url, manifest, stored, item.reason
    )
    if stored == "complete" and not spec.run.force:
        return RunPlan(
            spec,
            settings,
            sources,
            WorkCounts(total=1, skipped=1),
            warnings=(
                f"work {item.entry_id!r} is complete; --force fetches the item again",
            ),
        )
    return RunPlan(
        spec, settings, sources, WorkCounts(total=1), (planned,), fetch=target
    )


def plan(
    spec: CommandSpec,
    settings: Settings,
    *,
    sources: Mapping[str, Source] | None = None,
    now: float | None = None,
) -> RunPlan:
    """Plan a run of *spec* under *settings*.

    *sources* holds the source of each spec field; *now* (epoch s) dates the
    deferred works' retry.

    Raises:
        PlanError: The run cannot be planned.
        SettingsError: ``state_dir`` is not absolute.
    """
    sources = sources or {}
    moment = datetime.now().timestamp() if now is None else now
    if isinstance(spec, DownloadSpec):
        return _plan_download(spec, settings, sources, moment)
    if isinstance(spec, SearchSpec):
        return _plan_search(spec, settings, sources, moment)
    if isinstance(spec, FetchSpec):
        return _plan_fetch(spec, settings, sources)
    if isinstance(spec, InspectSpec):
        return plan_inspect(spec, settings, sources)
    if isinstance(spec, VerifySpec):
        return plan_verify(spec, settings, sources)
    if isinstance(spec, StatusSpec):
        return plan_status(spec, settings, sources)
    if isinstance(spec, DropSpec):
        return plan_drop(spec, settings, sources)
    if isinstance(spec, ProvidersSpec):
        settings.state_directory()
        return RunPlan(spec, settings, sources)
    raise PlanError(f"{command_of(spec)}: not implemented")


# -- running ----------------------------------------------------------------------


def _open_store(root: Path, wiring: Wiring) -> Store:
    try:
        return Store(root, clock=wiring.clock)
    except StoreError as exc:
        raise PlanError(str(exc)) from exc


def _lost(lease: Lease) -> None:
    logger.warning("The lease on work %s expired or passed to another run.", lease)


def _close_store(store: Store, exit_code: int, *, settle: bool) -> None:
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


def _matching(
    spec: MatchingSpec,
    selection: WorkSelection,
    *,
    force: bool,
    clock: Callable[[], float],
) -> MatchOptions:
    if spec.mode not in MODES or spec.edition_policy not in EDITION_POLICIES:
        raise PlanError(f"unknown matching {spec.mode!r}/{spec.edition_policy!r}")
    return MatchOptions(
        mode=spec.mode,
        edition_policy=spec.edition_policy,
        providers=spec.providers,
        max_candidates=spec.max_candidates,
        search_timeout=spec.search_timeout,
        research=selection.research,
        reselect=functools.partial(
            reselect, selection=selection, force=force, clock=clock
        ),
    )


def _download(
    files: FilesSpec, limits: SizeLimits, force: bool, settings: Settings
) -> DownloadOptions:
    if files.format not in FORMATS:
        raise PlanError(f"unknown format {files.format!r}")
    return DownloadOptions(
        format=files.format,
        max_pages=files.max_pages,
        include_metadata=files.include_metadata,
        force=force,
        budget=SizeBudget.of(limits, settings.download_limits),
    )


def _counts(run_plan: RunPlan, tally: Tally) -> WorkCounts:
    return tally.counts(run_plan.counts.total, run_plan.counts.skipped)


def _exit(tally: Tally) -> ExitCode:
    return ExitCode.FAILURE if tally.failed or tally.partial else ExitCode.OK


def _open(run_plan: RunPlan, concurrency: int, wiring: Wiring, mode: str) -> Services:
    """Open the run's services; a missing judge key closes them (exit 2)."""
    services = Services.open(run_plan.settings, concurrency=concurrency, wiring=wiring)
    if mode != "fuzzy":
        try:
            services.judge()
        except MissingJudgeKey as exc:
            services.close()
            raise PlanError(str(exc)) from exc
    return services


def _loop_over(
    run_plan: RunPlan,
    events: RunEvents,
    *,
    command: str,
    root: Path | None,
    services: Services,
    build: Callable[[Store | None], WorkLoop],
) -> Tally:
    """Run *build*'s loop under a store of *root* (None: no store)."""
    if root is None:
        return build(None).run(run_plan.works)
    store = _open_store(root, services.wiring)
    exit_code: int = ExitCode.FAILURE
    settle = False
    try:
        store.begin_run(
            command,
            run_id=services.run_id,
            spec=effective_spec(run_plan.spec, run_plan.sources),
            tool_version=_tool_version(),
        )
        with store.keeper(on_lost=_lost):
            loop = build(store)
            if run_plan.fetch is not None:
                tally = loop.run_fetch(run_plan.fetch)
            else:
                tally = loop.run(run_plan.works)
        exit_code = _exit(tally)
        settle = True
        return tally
    except KeyboardInterrupt:
        exit_code = ExitCode.INTERRUPTED
        raise
    finally:
        # An interrupt or an escaping error may leave workers writing into
        # staging folders: release the leases without touching those files.
        _close_store(store, exit_code, settle=settle)


def _run_download(run_plan: RunPlan, events: RunEvents, wiring: Wiring) -> Tally:
    spec = run_plan.spec
    assert isinstance(spec, DownloadSpec)
    download = _download(spec.files, spec.limits, spec.run.force, run_plan.settings)
    services = _open(run_plan, spec.run.concurrency, wiring, spec.matching.mode)
    with services:

        def build(store: Store | None) -> WorkLoop:
            return WorkLoop(
                services,
                store,
                events,
                command="download",
                matching=_matching(
                    spec.matching,
                    spec.selection,
                    force=spec.run.force,
                    clock=services.wiring.clock,
                ),
                download=download,
                concurrency=spec.run.concurrency,
                heartbeat=Heartbeat.from_env(),
            )

        return _loop_over(
            run_plan,
            events,
            command="download",
            root=spec.output,
            services=services,
            build=build,
        )


def _run_search(run_plan: RunPlan, events: RunEvents, wiring: Wiring) -> Tally:
    spec = run_plan.spec
    assert isinstance(spec, SearchSpec)
    concurrency = min(3, max(1, len(run_plan.works)))
    services = _open(run_plan, concurrency, wiring, spec.matching.mode)
    with services:

        def build(store: Store | None) -> WorkLoop:
            return WorkLoop(
                services,
                store,
                events,
                command="search",
                matching=_matching(
                    spec.matching,
                    spec.selection,
                    force=False,
                    clock=services.wiring.clock,
                ),
                download=None,
                concurrency=concurrency,
                heartbeat=Heartbeat.from_env(),
            )

        return _loop_over(
            run_plan,
            events,
            command="search",
            root=spec.output,
            services=services,
            build=build,
        )


def _run_fetch(run_plan: RunPlan, events: RunEvents, wiring: Wiring) -> Tally:
    spec = run_plan.spec
    assert isinstance(spec, FetchSpec)
    if run_plan.fetch is None:
        return Tally()
    download = _download(spec.files, spec.limits, spec.run.force, run_plan.settings)
    services = _open(run_plan, 1, wiring, "fuzzy")
    with services:

        def build(store: Store | None) -> WorkLoop:
            return WorkLoop(
                services,
                store,
                events,
                command="fetch",
                matching=None,
                download=download,
                concurrency=1,
                heartbeat=Heartbeat.from_env(),
            )

        return _loop_over(
            run_plan,
            events,
            command="fetch",
            root=spec.output,
            services=services,
            build=build,
        )


def run(
    run_plan: RunPlan, events: RunEvents, *, wiring: Wiring | None = None
) -> RunResult:
    """Carry out *run_plan*, reporting through *events*.

    *wiring* replaces providers, judge, session, clock and sleep (tests).
    Ctrl+C propagates after the run released its leases and recorded its
    end. A plan without works opens nothing and writes nothing.

    Raises:
        PlanError: The run cannot proceed (a missing judge key, a store
            refused after planning).
        ValueError: The plan is a dry run.
    """
    spec = run_plan.spec
    command = command_of(spec)
    wired = wiring or Wiring()
    if _dry_run(spec):
        raise ValueError("a dry-run plan is reported, not run")
    reported: dict[str, Callable[[], RunResult]] = {
        "inspect": lambda: run_inspect(run_plan, wired),
        "verify": lambda: run_verify(run_plan, wired),
        "status": lambda: RunResult(run_plan.counts, report=run_plan.report),
        "drop": lambda: run_drop(run_plan, wired),
        "providers": lambda: run_providers(run_plan, events, wired),
    }
    if command in reported:
        return reported[command]()
    runner = {
        "download": _run_download,
        "search": _run_search,
        "fetch": _run_fetch,
    }.get(command)
    if runner is None:
        raise PlanError(f"{command}: not implemented")
    if not run_plan.works and run_plan.fetch is None:
        return RunResult(run_plan.counts)
    tally = runner(run_plan, events, wired)
    return RunResult(_counts(run_plan, tally), tuple(tally.rows))


def _dry_run(spec: CommandSpec) -> bool:
    """Return True for a spec with ``--dry-run`` on.

    ``drop`` keeps the flag at the top level, the other commands under ``run``.
    """
    control = getattr(spec, "run", spec)
    return bool(getattr(control, "dry_run", False))
