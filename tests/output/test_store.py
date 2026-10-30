"""The output root's store: schema, leases, commits and reconciliation.

Deterministic schedules open several stores (one per simulated process, each
its own run and connection) on one root with a shared :class:`FakeClock`.
The multi-process tests spawn ``tests/output/store_child.py``.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from chronodownloader import sqlite as sqlite_module
from chronodownloader.common import page_types
from chronodownloader.output import _promotion
from chronodownloader.output import store as store_module
from chronodownloader.output._liveness import own_creation, process_alive
from chronodownloader.output.paths import inspect_dir
from chronodownloader.output.store import (
    DATABASE_NAME,
    LEASE_TTL,
    SCHEMA_VERSION,
    CandidateInput,
    FileRow,
    FolderConflictError,
    Judgment,
    Lease,
    LeaseLostError,
    NewerSchemaError,
    PageLabel,
    Promotion,
    Store,
    StoreBusyError,
    StoreError,
    UnreadableDatabaseError,
    VersionlessDatabaseError,
    WorkInput,
    read_page_labels,
    read_title_page_picks,
    read_work_candidates,
    read_work_status,
    unstored_folders,
)
from chronodownloader.process.contract import ExitCode
from chronodownloader.providers.base import (
    Completeness,
    DownloadResult,
    SearchRecord,
    StagedFile,
)
from tests.core.coord_child import FakeClock
from tests.core.spawn import CHILD_TIMEOUT, child, expect, line, send
from tests.output.store_child import CRASH_EXIT, folder_of

CHILD = "../output/store_child.py"
"""The store child, relative to ``tests/core`` where :func:`child` looks."""


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "out"


@pytest.fixture
def opened(root: Path, clock: FakeClock) -> Iterator[Callable[..., Store]]:
    """Open stores on one root; each name is its own run."""
    stores: list[Store] = []

    def open_(name: str | None = None, **kwargs: Any) -> Store:
        store = Store(root, clock=kwargs.pop("clock", clock), **kwargs)
        stores.append(store)
        if name is not None:
            store.begin_run("download", run_id=name)
        return store

    yield open_
    for store in stores:
        store.close()


def _work(entry_id: str = "e1", **fields: str) -> WorkInput:
    return WorkInput(entry_id, fields.pop("title", f"Title {entry_id}"), **fields)


def _acquire(store: Store, entry_id: str = "e1") -> Lease:
    lease = store.acquire(_work(entry_id), folder_of(entry_id))
    assert lease is not None
    return lease


def _abandon(store: Store) -> None:
    """Drop a store as a killed process would: no release, no cleanup."""
    store._close_connections()


def _stage(
    store: Store,
    lease: Lease,
    pages: Mapping[str, bytes],
    *,
    expected: Sequence[str] | None = None,
    causes: Mapping[str, str] | None = None,
    renderings: Mapping[str, bytes] | None = None,
) -> tuple[DownloadResult, list[Promotion]]:
    """Stage page images and renderings; return the result and promotions."""
    staging = store.staging_dir(lease)
    files: list[StagedFile] = []
    for item, content in pages.items():
        (staging / f"{item}.jpg").write_bytes(content)
        url = f"https://example.org/{item}.jpg?wskey=secret"
        files.append(
            StagedFile(f"{item}.jpg", "image", item, len(content), "image/jpeg", url)
        )
    for item, content in (renderings or {}).items():
        (staging / f"{item}.pdf").write_bytes(content)
        files.append(StagedFile(f"{item}.pdf", "pdf", item, len(content), "", ""))
    parts = []
    if pages or expected:
        expected_pages = tuple(expected if expected is not None else pages)
        parts.append(
            Completeness(
                "images", expected_pages, tuple(pages), causes=dict(causes or {})
            )
        )
    if renderings:
        parts.append(Completeness("pdf", tuple(renderings), tuple(renderings)))
    result = DownloadResult("mdz", "bsb1", "both", tuple(parts), tuple(files))
    promotions = [Promotion(file, f"objects/{file.name}") for file in files]
    return result, promotions


def _final(root: Path, entry_id: str, name: str) -> Path:
    return root / folder_of(entry_id) / "objects" / name


def _staging_root(root: Path, entry_id: str) -> Path:
    return root / folder_of(entry_id) / "objects" / ".staging"


# Opening and schema


def test_opening_creates_schema_version_two_with_the_read_views(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened()
    assert store.path == root / DATABASE_NAME
    assert sqlite_module.table_names(store.path) == [
        "attempts",
        "candidates",
        "files",
        "meta",
        "page_labels",
        "runs",
        "works",
    ]
    views = sqlite_module.read(
        store.path,
        lambda c: [
            row[0]
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view' ORDER BY name"
            )
        ],
    )
    assert views == ["title_page_picks", "work_candidates", "work_status"]
    assert sqlite_module.read_rows(store.path, "meta") == [
        {"key": "schema_version", "value": str(SCHEMA_VERSION)}
    ]
    mode = store._connection().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "delete"
    assert store.reconciliation is not None
    assert store_module.check_schema(store.path) == SCHEMA_VERSION


def test_a_newer_database_is_refused_and_left_as_it_is(
    opened: Callable[..., Store], root: Path, clock: FakeClock
) -> None:
    opened().close()
    with contextlib.closing(sqlite3.connect(root / DATABASE_NAME)) as connection:
        connection.execute("UPDATE meta SET value = '3'")
        connection.commit()
    before = (root / DATABASE_NAME).read_bytes()

    with pytest.raises(NewerSchemaError, match="version 3") as raised:
        Store(root, clock=clock)
    assert raised.value.exit_code == ExitCode.USAGE == 2
    assert (root / DATABASE_NAME).read_bytes() == before
    with pytest.raises(NewerSchemaError):
        read_work_status(root)


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE works (entry_id TEXT)",
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
    ],
    ids=["no-meta", "meta-without-version"],
)
def test_a_versionless_database_is_refused(
    root: Path, clock: FakeClock, ddl: str
) -> None:
    root.mkdir()
    with contextlib.closing(sqlite3.connect(root / DATABASE_NAME)) as connection:
        connection.execute(ddl)
        connection.commit()
    before = (root / DATABASE_NAME).read_bytes()

    with pytest.raises(VersionlessDatabaseError) as raised:
        Store(root, clock=clock)
    assert isinstance(raised.value, StoreError)
    assert (root / DATABASE_NAME).read_bytes() == before


def test_a_file_that_is_no_database_is_refused(root: Path, clock: FakeClock) -> None:
    root.mkdir()
    (root / DATABASE_NAME).write_bytes(b"not a database, just some text" * 50)
    with pytest.raises(UnreadableDatabaseError):
        Store(root, clock=clock)


def test_an_empty_database_file_is_initialized(root: Path, clock: FakeClock) -> None:
    root.mkdir()
    (root / DATABASE_NAME).touch()
    with Store(root, clock=clock) as store:
        assert store_module.check_schema(store.path) == SCHEMA_VERSION


def test_a_closed_store_refuses_calls(opened: Callable[..., Store]) -> None:
    store = opened("run")
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.acquire(_work(), "folder")


# Leases


def test_acquisition_blocks_other_runs_and_counts_generations(
    opened: Callable[..., Store], clock: FakeClock
) -> None:
    first, second = opened("a"), opened("b")

    lease = _acquire(first)
    assert (lease.holder, lease.generation, lease.folder) == ("a", 1, "e1_work")
    assert lease.expires_at == clock() + LEASE_TTL
    assert second.acquire(_work(), folder_of("e1")) is None
    again = _acquire(first)
    assert again.generation == 2
    with pytest.raises(LeaseLostError, match="no longer"):
        first.set_status(lease, "failed")

    clock.advance(LEASE_TTL)
    taken = _acquire(second)
    assert (taken.holder, taken.generation) == ("b", 3)
    second.release(taken)
    assert first.acquire(_work(), folder_of("e1")) is not None


def test_acquisition_keeps_the_folder_and_updates_the_input_fields(
    opened: Callable[..., Store],
) -> None:
    store = opened("a")
    lease = store.acquire(_work(creator="Audot", year="1823"), "first_name")
    assert lease is not None
    store.release(lease)

    lease = store.acquire(_work(title="New title", year="1824"), "second_name")
    assert lease is not None and lease.folder == "first_name"
    work = store.work("e1")
    assert work is not None
    assert (work.title, work.creator, work.year) == ("New title", "Audot", "1824")
    assert work.status is None and work.first_run_id == "a"


def test_a_folder_taken_by_another_work_or_not_plain_is_refused(
    opened: Callable[..., Store],
) -> None:
    store = opened("a")
    assert store.acquire(_work("e1"), "Shared") is not None
    with pytest.raises(FolderConflictError):
        store.acquire(_work("e2"), "shared")
    for folder in ("", "..", "a/b", "a\\b", "_inspect", "C:x", " x"):
        with pytest.raises(ValueError):
            store.acquire(_work("e3"), folder)
    assert store.work("e2") is None


def test_lease_expiry_without_takeover_refuses_completion_and_renewal(
    opened: Callable[..., Store], root: Path, clock: FakeClock
) -> None:
    store = opened("a")
    lease = _acquire(store)
    result, promotions = _stage(store, lease, {"p00001": b"1"})
    clock.advance(LEASE_TTL - 1)
    lease = store.renew(lease)
    assert lease.expires_at == clock() + LEASE_TTL

    clock.advance(LEASE_TTL)
    with pytest.raises(LeaseLostError, match="cannot be renewed"):
        store.renew(lease)
    with pytest.raises(LeaseLostError, match="expired"):
        store.commit_download(lease, result, promotions, status="complete")
    with pytest.raises(LeaseLostError, match="expired"):
        store.set_status(lease, "failed")

    assert not _final(root, "e1", "p00001.jpg").exists()
    assert not _staging_root(root, "e1").exists()
    assert store.files("e1") == [] and store.attempts("e1") == []
    assert store.held_leases() == {}
    work = store.work("e1")
    assert work is not None and work.status is None
    assert _acquire(store).generation == 2


def test_a_write_that_waits_for_the_lock_past_expiry_is_refused(
    opened: Callable[..., Store], root: Path, clock: FakeClock
) -> None:
    store = opened("a")
    lease = _acquire(store)
    result, promotions = _stage(store, lease, {"p00001": b"1"})
    clock.advance(LEASE_TTL - 1)
    errors: list[BaseException] = []

    def commit() -> None:
        try:
            store.commit_download(lease, result, promotions, status="complete")
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    blocker = sqlite3.connect(store.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    writer = threading.Thread(target=commit)
    writer.start()
    try:
        time.sleep(0.3)
        assert writer.is_alive()
        clock.advance(2.0)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    writer.join(CHILD_TIMEOUT)

    assert len(errors) == 1 and isinstance(errors[0], LeaseLostError)
    assert not _final(root, "e1", "p00001.jpg").exists()
    assert store.files("e1") == []


def test_the_keeper_renews_and_reports_a_lease_lost_after_expiry(
    opened: Callable[..., Store], clock: FakeClock
) -> None:
    store, other = opened("a"), opened("b")
    lease = _acquire(store)
    lost: list[Lease] = []

    with store.keeper(interval=0.01, on_lost=lost.append) as keeper:
        clock.advance(LEASE_TTL - 1)
        deadline = time.monotonic() + 10.0
        while (
            store.held_leases()["e1"].expires_at < clock() + LEASE_TTL - 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert keeper.renewals >= 1
        assert other.acquire(_work(), folder_of("e1")) is None

        clock.advance(LEASE_TTL + 1)
        taken = _acquire(other)
        while not lost and time.monotonic() < deadline:
            time.sleep(0.01)

    assert taken.generation == 2
    assert [item.generation for item in lost] == [lease.generation]
    assert keeper.lost == lost
    assert store.held_leases() == {}


def test_a_released_lease_is_not_reported_lost(
    opened: Callable[..., Store], clock: FakeClock
) -> None:
    store = opened("a")
    lease = _acquire(store)
    keeper = store.keeper(interval=60.0)
    store.release(lease)
    clock.advance(LEASE_TTL + 1)
    keeper.renew_all()
    assert keeper.lost == [] and keeper.renewals == 0


# Commits


def test_a_commit_promotes_records_and_clears_the_staging(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    result, promotions = _stage(store, lease, {"p00001": b"one", "p00002": b"two"})

    attempt_id = store.commit_download(
        lease, result, promotions, status="complete", reason="matched", started_at=1.0
    )

    assert _final(root, "e1", "p00001.jpg").read_bytes() == b"one"
    assert not _staging_root(root, "e1").exists()
    files = store.files("e1")
    assert [(f.path, f.item, f.size, f.attempt_id) for f in files] == [
        ("objects/p00001.jpg", "p00001", 3, attempt_id),
        ("objects/p00002.jpg", "p00002", 3, attempt_id),
    ]
    assert files[0].source_url == "https://example.org/p00001.jpg?wskey=***"
    (attempt,) = store.attempts("e1")
    assert (attempt.outcome, attempt.lease_generation, attempt.run_id) == (
        "complete",
        1,
        "a",
    )
    assert attempt.started_at == "1970-01-01T00:00:01.000Z"
    work = store.work("e1")
    assert work is not None
    assert (work.status, work.status_reason) == ("complete", "matched")
    assert store.present_items("e1") == frozenset({"p00001", "p00002"})


def test_multi_file_rendering_items_are_recorded(
    opened: Callable[..., Store],
) -> None:
    store = opened("a")
    lease = _acquire(store)
    renderings = {
        "rendering": b"r",
        "rendering_00001": b"s",
        "rendering_p00001_00025": b"b1",
        "rendering_p00026_00050": b"b2",
    }
    result, promotions = _stage(store, lease, {"p00001": b"1"}, renderings=renderings)
    store.commit_download(lease, result, promotions)

    items = {(f.item, f.kind) for f in store.files("e1")}
    assert items == {("p00001", "image")} | {(item, "pdf") for item in renderings}
    assert store.present_items("e1") == frozenset({"p00001", *renderings})


def test_completeness_causes_are_stored_and_read_back(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    result, promotions = _stage(
        store,
        lease,
        {"p00001": b"1", "p00003": b"3"},
        expected=("p00001", "p00002", "p00003", "p00004"),
        causes={"p00002": "blocked", "p00004": "transient"},
    )
    result = DownloadResult(
        result.provider,
        result.source_id,
        "images",
        result.parts,
        result.files,
        stopped="budget",
        quota_charged=True,
    )
    store.commit_download(lease, result, promotions, status="partial", error="x" * 5000)

    (attempt,) = store.attempts("e1")
    assert attempt.parts == result.parts
    assert dict(attempt.parts[0].causes) == {"p00002": "blocked", "p00004": "transient"}
    assert (attempt.outcome, attempt.stopped, attempt.quota_charged) == (
        "partial",
        "budget",
        True,
    )
    assert attempt.error is not None and attempt.error.startswith("x" * 2000)
    assert attempt.error.endswith("[... 3,000 more characters]")
    (row,) = read_work_status(root)
    assert (row["pages_expected"], row["pages_present"]) == (4, 2)
    assert (row["file_count"], row["file_bytes"], row["last_outcome"]) == (
        2,
        2,
        "partial",
    )


def test_a_failed_attempt_without_files_is_recorded(
    opened: Callable[..., Store],
) -> None:
    store = opened("a")
    lease = _acquire(store)
    failed = DownloadResult("bnf_gallica", "ark:/1", "pdf", ())
    store.commit_download(
        lease,
        failed,
        status="failed",
        error="https://x.org/a?api_key=SECRET refused",
    )
    (attempt,) = store.attempts("e1")
    assert attempt.outcome == "failed" and attempt.parts == ()
    assert attempt.error == "https://x.org/a?api_key=*** refused"


def test_a_commit_replaces_a_recorded_file_and_updates_its_row(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    store.commit_download(lease, *_stage(store, lease, {"p00001": b"old"}))
    result, promotions = _stage(store, lease, {"p00001": b"newer"})
    second = store.commit_download(lease, result, promotions)

    assert _final(root, "e1", "p00001.jpg").read_bytes() == b"newer"
    (file,) = store.files("e1")
    assert (file.size, file.attempt_id) == (5, second)
    assert not _staging_root(root, "e1").exists()


def test_bad_promotions_are_refused_before_any_rename(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    result, promotions = _stage(store, lease, {"p00001": b"1", "p00002": b"2"})
    stray = StagedFile("p00009.jpg", "image", "p00009", 1, "", "")
    bad = [
        [Promotion(stray, "objects/p00009.jpg")],
        [promotions[0], Promotion(promotions[1].staged, "objects/P00001.JPG")],
        [promotions[0], Promotion(promotions[0].staged, "objects/again.jpg")],
        [Promotion(promotions[0].staged, "../escape.jpg")],
        [Promotion(promotions[0].staged, "objects/.staging/1/p00001.jpg")],
        [Promotion(promotions[0].staged, "C:/abs.jpg")],
    ]
    for promotion_set in bad:
        with pytest.raises(ValueError):
            store.commit_download(lease, result, promotion_set)
    with pytest.raises(ValueError, match="deferred"):
        store.commit_download(lease, result, promotions, status="deferred")
    assert store.attempts("e1") == []
    assert (_staging_root(root, "e1") / "1" / "p00001.jpg").exists()


def test_a_rename_failing_mid_commit_rolls_the_commit_back(
    opened: Callable[..., Store], root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = opened("a")
    lease = _acquire(store)
    store.commit_download(lease, *_stage(store, lease, {"p00001": b"old"}))
    result, promotions = _stage(
        store, lease, {"p00001": b"new", "p00002": b"2", "p00003": b"3"}
    )
    rename = _promotion.rename
    calls: list[Path] = []

    def failing(source: Path, target: Path) -> None:
        calls.append(target)
        if len(calls) == 4:
            raise PermissionError("locked by a scanner")
        rename(source, target)

    monkeypatch.setattr(_promotion, "rename", failing)
    with pytest.raises(PermissionError):
        store.commit_download(lease, result, promotions, status="complete")
    monkeypatch.undo()

    assert _final(root, "e1", "p00001.jpg").read_bytes() == b"old"
    assert not _final(root, "e1", "p00002.jpg").exists()
    assert [f.path for f in store.files("e1")] == ["objects/p00001.jpg"]
    assert len(store.attempts("e1")) == 1
    assert not _staging_root(root, "e1").exists()


def test_a_busy_commit_rolls_its_renames_back(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a", busy_timeout=0.3)
    lease = _acquire(store)
    result, promotions = _stage(store, lease, {"p00001": b"1", "p00002": b"2"})
    reader = sqlite3.connect(store.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM works").fetchone()
    try:
        with pytest.raises(sqlite_module.DatabaseBusyError):
            store.commit_download(lease, result, promotions, status="complete")
    finally:
        reader.execute("ROLLBACK")
        reader.close()

    assert not _final(root, "e1", "p00001.jpg").exists()
    assert store.files("e1") == [] and store.attempts("e1") == []
    assert not _staging_root(root, "e1").exists()


def test_a_replayed_promotion_resumes_its_renames(tmp_path: Path) -> None:
    gen_dir = tmp_path / "work" / "objects" / ".staging" / "1"
    gen_dir.mkdir(parents=True)
    moves = []
    for item in ("p00001", "p00002"):
        (gen_dir / f"{item}.jpg").write_bytes(item.encode())
        staged = StagedFile(f"{item}.jpg", "image", item, 6, "", "")
        target = tmp_path / "work" / "objects" / f"{item}.jpg"
        moves.append(
            _promotion.Move(
                staged, f"objects/{item}.jpg", gen_dir / staged.name, target
            )
        )
    journal = _promotion.journal_path(gen_dir, "c1")

    _promotion.promote(journal, "c1", moves)
    _promotion.promote(journal, "c1", moves)

    assert [move.target.read_bytes() for move in moves] == [b"p00001", b"p00002"]


# Takeover


def test_takeover_during_streaming_leaves_the_successors_files_alone(
    opened: Callable[..., Store], root: Path, clock: FakeClock
) -> None:
    stale, successor = opened("stale"), opened("successor")
    old = _acquire(stale)
    old_result, old_promotions = _stage(stale, old, {"p00001": b"stale"})
    clock.advance(LEASE_TTL + 1)
    new = _acquire(successor)
    new_result, new_promotions = _stage(
        successor, new, {"p00001": b"fresh", "p00002": b"fresh"}
    )
    assert not (_staging_root(root, "e1") / "1").exists()

    with pytest.raises(LeaseLostError, match="no longer"):
        stale.commit_download(old, old_result, old_promotions, status="complete")
    assert (_staging_root(root, "e1") / "2" / "p00001.jpg").read_bytes() == b"fresh"

    successor.commit_download(new, new_result, new_promotions, status="complete")
    assert _final(root, "e1", "p00001.jpg").read_bytes() == b"fresh"
    assert {f.path for f in successor.files("e1")} == {
        "objects/p00001.jpg",
        "objects/p00002.jpg",
    }


def test_takeover_right_before_promotion_neither_publishes_nor_deletes(
    opened: Callable[..., Store], root: Path, clock: FakeClock
) -> None:
    stale, successor = opened("stale"), opened("successor")
    old = _acquire(stale)
    old_result, old_promotions = _stage(
        stale, old, {"p00001": b"stale", "p00003": b"stale"}
    )
    clock.advance(LEASE_TTL + 1)
    new = _acquire(successor)
    gen_dir = successor._staging_path(new)
    gen_dir.mkdir(parents=True)
    (gen_dir / "p00001.jpg").write_bytes(b"fresh")
    staged = StagedFile("p00001.jpg", "image", "p00001", 5, "", "")
    new_result = DownloadResult(
        "mdz",
        "bsb1",
        "images",
        (Completeness("images", ("p00001",), ("p00001",)),),
        (staged,),
    )
    successor.commit_download(
        new, new_result, [Promotion(staged, "objects/p00001.jpg")], status="complete"
    )

    with pytest.raises(LeaseLostError):
        stale.commit_download(old, old_result, old_promotions, status="complete")

    assert _final(root, "e1", "p00001.jpg").read_bytes() == b"fresh"
    assert not _final(root, "e1", "p00003.jpg").exists()
    assert [f.path for f in successor.files("e1")] == ["objects/p00001.jpg"]
    (attempt,) = successor.attempts("e1")
    assert attempt.run_id == "successor"
    work = successor.work("e1")
    assert work is not None and work.lease_holder == "successor"


def test_a_stale_journal_spares_a_path_the_successor_recorded(
    opened: Callable[..., Store], root: Path, clock: FakeClock
) -> None:
    crashed, successor = opened("crashed"), opened("successor")
    old = _acquire(crashed)
    stale_dir = crashed.staging_dir(old)
    _final(root, "e1", "p00001.jpg").write_bytes(b"stale")
    _promotion.write_journal(
        _promotion.journal_path(stale_dir, "gone"),
        {
            "commit_id": "gone",
            "entries": [["p00001.jpg", "objects/p00001.jpg", False]],
        },
    )
    _abandon(crashed)
    clock.advance(LEASE_TTL + 1)
    new = _acquire(successor)
    gen_dir = successor._staging_path(new)
    gen_dir.mkdir(parents=True)
    (gen_dir / "p00001.jpg").write_bytes(b"mine")
    staged = StagedFile("p00001.jpg", "image", "p00001", 4, "", "")
    result = DownloadResult(
        "mdz",
        "bsb1",
        "images",
        (Completeness("images", ("p00001",), ("p00001",)),),
        (staged,),
    )
    successor.commit_download(new, result, [Promotion(staged, "objects/p00001.jpg")])
    assert stale_dir.exists()

    successor.staging_dir(new)

    assert not stale_dir.exists()
    assert _final(root, "e1", "p00001.jpg").read_bytes() == b"mine"
    assert [f.path for f in successor.files("e1")] == ["objects/p00001.jpg"]


def test_a_recovery_interrupted_after_a_restore_keeps_the_restored_file(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    gen_dir = store.staging_dir(lease)
    original = _final(root, "e1", "p00001.jpg")
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"original")
    (gen_dir / "p00001.jpg").write_bytes(b"new")
    staged = StagedFile("p00001.jpg", "image", "p00001", 3, "", "")
    move = _promotion.Move(
        staged, "objects/p00001.jpg", gen_dir / staged.name, original
    )
    _promotion.promote(_promotion.journal_path(gen_dir, "c1"), "c1", [move])
    assert original.read_bytes() == b"new"
    # The first recovery restores the original, then stops before the
    # journal is removed.
    paths = (move.source, original, gen_dir / ".replaced-c1" / "objects/p00001.jpg")
    _promotion._roll_back_entry(
        store._connection(), "e1", lease.generation, paths, move.final, True
    )

    store.release(lease)

    assert original.read_bytes() == b"original"
    assert not _staging_root(root, "e1").exists()


# Crash and reconciliation


def test_a_crash_between_promotion_and_record_is_repaired_at_open(
    root: Path, clock: FakeClock
) -> None:
    with Store(root) as store:
        store.begin_run("download", run_id="first")
        lease = _acquire(store)
        store.commit_download(
            lease, *_stage(store, lease, {"p00001": b"old"}), status="partial"
        )

    with child(CHILD, "crash", str(root), "crasher", "e1", "3") as process:
        expect(process, "READY")
        assert process.wait(CHILD_TIMEOUT) == CRASH_EXIT

    staging = _staging_root(root, "e1") / "2"
    assert _final(root, "e1", "p00001.jpg").read_bytes() == b"new"
    assert _final(root, "e1", "p00002.jpg").exists()
    assert (staging / "p00003.jpg").exists()
    assert any(staging.glob(".promote-*.json"))

    with Store(root, clock=lambda: time.time() + LEASE_TTL + 1) as reopened:
        report = reopened.reconciliation
        assert report is not None
        assert set(report.rolled_back) == {
            ("e1", "objects/p00001.jpg"),
            ("e1", "objects/p00002.jpg"),
        }
        assert report.staging_removed == (("e1", 2),)
        assert report.missing == ()
        assert [f.path for f in reopened.files("e1")] == ["objects/p00001.jpg"]
        assert len(reopened.attempts("e1")) == 1
    assert _final(root, "e1", "p00001.jpg").read_bytes() == b"old"
    assert not _final(root, "e1", "p00002.jpg").exists()
    assert not _final(root, "e1", "p00003.jpg").exists()
    assert not _staging_root(root, "e1").exists()


def test_a_dead_holders_crash_is_repaired_before_its_lease_expires(
    root: Path,
) -> None:
    """Codex review: reconciliation judged leases by expiry alone, so a dead
    holder's journal stayed unsettled until its lease expired, while the
    acquisition already freed the work and read its unrepaired state."""
    with Store(root) as store:
        store.begin_run("download", run_id="first")
        lease = _acquire(store)
        store.commit_download(
            lease, *_stage(store, lease, {"p00001": b"old"}), status="partial"
        )
    with child(CHILD, "crash", str(root), "crasher", "e1", "3") as process:
        expect(process, "READY")
        assert process.wait(CHILD_TIMEOUT) == CRASH_EXIT
    assert _final(root, "e1", "p00002.jpg").exists()

    with Store(root) as reopened:  # the real clock: the lease is unexpired
        report = reopened.reconciliation
        assert report is not None and report.live == ()
        assert set(report.rolled_back) == {
            ("e1", "objects/p00001.jpg"),
            ("e1", "objects/p00002.jpg"),
        }
        reopened.begin_run("download", run_id="resume")
        seen: list[Any] = []
        lease = reopened.acquire(
            _work("e1"), folder_of("e1"), select=lambda row: not seen.append(row)
        )
        assert lease is not None and lease.generation == 3
        assert seen[0]["stored_status"] == "partial"
        assert [f.path for f in reopened.files("e1")] == ["objects/p00001.jpg"]
    assert _final(root, "e1", "p00001.jpg").read_bytes() == b"old"
    assert not _final(root, "e1", "p00002.jpg").exists()


def test_a_live_lease_keeps_its_staging_through_reconciliation(
    opened: Callable[..., Store], root: Path
) -> None:
    holder = opened("a")
    lease = _acquire(holder)
    _stage(holder, lease, {"p00001": b"1"})
    report = opened().reconciliation
    assert report is not None and report.live == ("e1",)
    assert (_staging_root(root, "e1") / "1" / "p00001.jpg").exists()


def test_reconciliation_drops_missing_files_and_downgrades_complete_works(
    opened: Callable[..., Store], root: Path, clock: FakeClock
) -> None:
    store = opened("a")
    lease = _acquire(store)
    store.commit_download(
        lease,
        *_stage(store, lease, {"p00001": b"1", "p00002": b"2"}),
        status="complete",
    )
    store.release(lease)
    _final(root, "e1", "p00002.jpg").unlink()
    (root / folder_of("e1") / "notes.txt").write_text("mine", encoding="utf-8")

    report = opened().reconciliation
    assert report is not None
    assert report.missing == (("e1", "objects/p00002.jpg"),)
    assert report.downgraded == ("e1",)
    work = store.work("e1")
    assert work is not None and work.status == "partial"
    assert work.status_reason == "reconcile: 1 recorded files missing on disk"
    assert store.present_items("e1") == frozenset({"p00001"})
    assert (root / folder_of("e1") / "notes.txt").exists()


def test_resume_after_an_interrupt(root: Path, clock: FakeClock) -> None:
    first = Store(root, clock=clock)
    first.begin_run("download", run_id="first")
    lease = _acquire(first)
    expected = ("p00001", "p00002", "p00003")
    result, promotions = _stage(first, lease, {"p00001": b"1"}, expected=expected)
    first.commit_download(lease, result, promotions, status="partial")
    _stage(first, lease, {"p00002": b"half"}, expected=expected)
    _abandon(first)

    second = Store(root, clock=clock)
    second.begin_run("download", run_id="second")
    try:
        assert second.acquire(_work(), folder_of("e1")) is None
        clock.advance(LEASE_TTL + 1)
        lease = _acquire(second)
        assert lease.generation == 2
        present = second.present_items("e1")
        assert present == frozenset({"p00001"})
        staging = second.staging_dir(lease)
        assert not (_staging_root(root, "e1") / "1").exists()
        assert list(staging.iterdir()) == []
        result, promotions = _stage(
            second, lease, {"p00002": b"2", "p00003": b"3"}, expected=expected
        )
        result = DownloadResult(
            result.provider,
            result.source_id,
            "images",
            (Completeness("images", expected, expected),),
            result.files,
        )
        second.commit_download(lease, result, promotions, status="complete")
        assert second.present_items("e1") == frozenset(expected)
        assert [a.outcome for a in second.attempts("e1")] == ["partial", "complete"]
        work = second.work("e1")
        assert work is not None and work.status == "complete"
    finally:
        second.close()


# Candidates and statuses


def _record(source_id: str, **fields: Any) -> SearchRecord:
    return SearchRecord("mdz", source_id, fields.pop("title", "Le cuisinier"), **fields)


def test_candidates_are_upserted_judged_and_kept_as_history(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    judgment = Judgment({"same_work": 0.97}, "accept", "typesafe/jev-1.14", "2026-10")
    ids = store.add_candidates(
        lease,
        [
            CandidateInput(
                _record(
                    "b1",
                    creators=("Audot",),
                    year=1823,
                    item_url="https://api.x/r?wskey=abc",
                    raw={"link": "https://api.x/r.json?wskey=abc"},
                ),
                rank=1,
                text_score=0.8,
                judgment=judgment,
            ),
            CandidateInput(_record("b2"), origin="link", rank=2),
        ],
    )
    stored = store.candidates("e1")[0].record
    assert stored.creators == ("Audot",) and stored.year == 1823
    assert stored.item_url == "https://api.x/r?wskey=***"
    assert dict(stored.raw) == {"link": "https://api.x/r.json?wskey=***"}
    again = store.add_candidates(lease, [CandidateInput(_record("b1", title="New"))])
    assert again == ids[:1]
    store.record_judgment(lease, ids[1], Judgment({"same_work": 0.4}, "review", "m"))
    store.set_status(lease, "complete", selected=ids[0])

    first, second = store.candidates("e1")
    assert first.record.title == "New" and first.text_score == 0.8
    assert first.judgment == judgment and first.record.item_url is None
    assert second.origin == "link" and second.judgment is not None
    with pytest.raises(KeyError):
        store.record_judgment(lease, 999, judgment)

    store.add_candidates(lease, [CandidateInput(_record("b3"))], research=True)
    assert [c.record.source_id for c in store.candidates("e1")] == ["b3"]
    history = store.candidates("e1", history=True)
    assert [c.superseded_at is not None for c in history] == [True, True, False]
    work = store.work("e1")
    assert work is not None and work.selected_candidate is None
    rows = read_work_candidates(root, "e1")
    assert [(r["source_id"], r["current"], r["selected"]) for r in rows] == [
        ("b1", 0, 0),
        ("b2", 0, 0),
        ("b3", 1, 0),
    ]


def test_statuses_reasons_and_deferral(
    opened: Callable[..., Store], root: Path, clock: FakeClock
) -> None:
    store = opened("a")
    lease = _acquire(store)
    other = _acquire(store, "e2")
    with pytest.raises(ValueError):
        store.set_status(lease, "deferred")
    with pytest.raises(ValueError):
        store.set_status(lease, "failed", retry_after=clock() + 60)
    with pytest.raises(ValueError):
        store.set_status(lease, "blocked")  # type: ignore[arg-type]
    (candidate,) = store.add_candidates(other, [CandidateInput(_record("b9"))])
    with pytest.raises(ValueError, match="not a candidate"):
        store.set_status(lease, "complete", selected=candidate)

    store.set_status(lease, "deferred", retry_after=clock() + 3600, reason="judge down")
    store.set_status(other, "needs_review", reason="translation")
    store.set_status(other, "complete", reason="fetch: checked the title page")
    with pytest.raises(sqlite3.IntegrityError):
        store._transact(
            lambda c: c.execute(
                "UPDATE works SET retry_after = NULL WHERE entry_id = 'e1'"
            )
        )
    store.release(lease)
    store.release(other)

    rows = {row["entry_id"]: row for row in read_work_status(root)}
    assert rows["e1"]["status"] == "deferred"
    assert rows["e1"]["retry_after"] == "2027-01-15T09:00:00.000Z"
    assert rows["e2"]["status_reason"] == "fetch: checked the title page"
    assert read_work_status(root, ["e2"])[0]["status"] == "complete"


def test_the_status_view_reports_new_and_live_leases_as_blocked(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a", clock=time.time)
    lease = _acquire(store)
    assert read_work_status(root)[0]["status"] == "blocked"
    assert read_work_status(root)[0]["stored_status"] is None
    store.release(lease)
    assert read_work_status(root)[0]["status"] == "new"
    assert read_work_status(root / "missing") == []


def test_unstored_folders_are_the_work_folders_without_a_row(
    opened: Callable[..., Store], root: Path
) -> None:
    assert unstored_folders(root) == []
    store = opened("a")
    store.release(_acquire(store, "e1"))
    for name in ("old_work", "E1_WORK", "_inspect", ".hidden", "another"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "index.csv").write_text("", encoding="utf-8")
    assert unstored_folders(root) == ["another", "old_work"]


# Two processes


def test_two_processes_skip_each_others_works_as_blocked(root: Path) -> None:
    Store(root).close()
    with (
        child(CHILD, "serve", str(root), "run-a") as first,
        child(CHILD, "serve", str(root), "run-b") as second,
    ):
        expect(first, "READY")
        expect(second, "READY")
        for process in (first, second):
            send(process, "acquire shared")
        answers = [line(first), line(second)]
        assert sorted(answers) == ["BLOCKED", "LEASE 1"]
        winner, loser = (first, second) if answers[0] == "LEASE 1" else (second, first)
        assert _ask(winner, "acquire a") == "LEASE 1"
        assert _ask(loser, "acquire a") == "BLOCKED"
        assert _ask(loser, "acquire b") == "LEASE 1"
        assert _ask(winner, "acquire b") == "BLOCKED"

        statuses = {row["entry_id"]: row["status"] for row in read_work_status(root)}
        assert statuses == {"a": "blocked", "b": "blocked", "shared": "blocked"}

        assert _ask(loser, "release b") == "RELEASED"
        assert _ask(winner, "acquire b") == "LEASE 2"
        for process in (first, second):
            send(process, "exit")
        for process in (first, second):
            assert process.wait(CHILD_TIMEOUT) == 0

    statuses = {row["entry_id"]: row["status"] for row in read_work_status(root)}
    assert statuses == {"a": "new", "b": "new", "shared": "new"}


def _ask(process: subprocess.Popen[str], command: str) -> str:
    send(process, command)
    return line(process)


def _set_run(root: Path, run_id: str, column: str, value: object) -> None:
    with contextlib.closing(sqlite3.connect(root / DATABASE_NAME)) as connection:
        connection.execute(
            f"UPDATE runs SET {column} = ? WHERE run_id = ?", (value, run_id)
        )
        connection.commit()


def _set_host(root: Path, run_id: str, host: str) -> None:
    _set_run(root, run_id, "host", host)


def test_a_lease_of_a_killed_process_on_this_host_is_free(root: Path) -> None:
    """Live round N3: a stopped job's lease blocked its resume for the whole
    lease TTL, and the resumed job closed as done with the work open."""
    Store(root).close()
    with child(CHILD, "serve", str(root), "run-dead") as holder:
        expect(holder, "READY")
        assert _ask(holder, "acquire e1") == "LEASE 1"
        holder.kill()
        holder.wait(CHILD_TIMEOUT)
    with Store(root) as store:
        store.begin_run("download", run_id="resume")
        _set_host(root, "run-dead", "another-machine")
        assert store.acquire(_work("e1"), folder_of("e1")) is None
        _set_host(root, "run-dead", socket.gethostname())
        lease = store.acquire(_work("e1"), folder_of("e1"))
        assert lease is not None and lease.generation == 2


def test_a_drop_takes_a_work_whose_holder_died(root: Path) -> None:
    Store(root).close()
    with child(CHILD, "serve", str(root), "run-dead") as holder:
        expect(holder, "READY")
        assert _ask(holder, "acquire e1") == "LEASE 1"
        holder.kill()
        holder.wait(CHILD_TIMEOUT)
    with Store(root) as store:
        assert store.drop(["e1"]).dropped == ("e1",)


def test_a_lease_of_a_live_process_stays_its_own(opened: Callable[..., Store]) -> None:
    holder, other = opened("holder", clock=time.time), opened("other", clock=time.time)
    _acquire(holder)
    assert other.acquire(_work("e1"), folder_of("e1")) is None
    assert process_alive(os.getpid(), own_creation())
    assert process_alive(os.getpid(), None)  # unknown identity counts as alive


def test_a_holder_is_known_by_its_process_creation_not_its_run_start(
    root: Path,
) -> None:
    """Codex review: liveness compared the process's creation with the run's
    ``started_at``, which lies anywhere after it; a reused pid is a process
    with another creation time."""
    Store(root).close()
    with child(CHILD, "serve", str(root), "run-live") as holder:
        expect(holder, "READY")
        assert _ask(holder, "acquire e1") == "LEASE 1"
        with Store(root) as store:
            store.begin_run("download", run_id="seeker")
            for started in ("2000-01-01T00:00:00.000Z", "2100-01-01T00:00:00.000Z"):
                _set_run(root, "run-live", "started_at", started)
                assert store.acquire(_work("e1"), folder_of("e1")) is None
            if sys.platform == "win32":
                (row,) = store._query(
                    "SELECT process_created FROM runs WHERE run_id = 'run-live'"
                )
                created = row["process_created"]
                assert created is not None
                _set_run(root, "run-live", "process_created", created + 60)
                lease = store.acquire(_work("e1"), folder_of("e1"))
                assert lease is not None and lease.generation == 2
        holder.kill()


# Drop


def _downloaded(store: Store, entry_id: str) -> None:
    lease = _acquire(store, entry_id)
    result, promotions = _stage(store, lease, {"p00001": b"one"})
    store.commit_download(lease, result, promotions, status="complete")
    store.release(lease)


def test_a_dry_run_drop_reports_and_changes_nothing(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    _downloaded(store, "e1")
    note = root / folder_of("e1") / "notes.txt"
    note.write_text("mine", encoding="utf-8")

    report = store.drop(["e1", "nope"], dry_run=True)

    assert report.dry_run
    assert report.dropped == ("e1",)
    assert [(r.entry_id, r.reason) for r in report.refused] == [
        ("nope", "no such work")
    ]
    assert report.deleted_files == (_final(root, "e1", "p00001.jpg"),)
    assert report.kept_files == (note,)
    assert store.work("e1") is not None
    assert _final(root, "e1", "p00001.jpg").is_file()


def test_a_drop_deletes_rows_recorded_files_scratch_and_empty_folders(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    _downloaded(store, "e1")
    _downloaded(store, "e2")
    stray = _staging_root(root, "e1") / "7" / "p00002.jpg"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"left over")
    scratch = root / "_inspect" / "e1" / "mdz_bsb1" / "p00001.jpg"
    scratch.parent.mkdir(parents=True)
    scratch.write_bytes(b"page")

    report = store.drop(["e1"])

    assert (report.dropped, report.kept_files) == (("e1",), ())
    assert not (root / folder_of("e1")).exists()
    assert not (root / "_inspect" / "e1").exists()
    assert store.work("e1") is None
    assert (store.files("e1"), store.attempts("e1")) == ([], [])
    assert [row["entry_id"] for row in read_work_status(root)] == ["e2"]
    assert _final(root, "e2", "p00001.jpg").is_file()


def test_a_work_dropped_and_created_again_never_reuses_a_generation(
    opened: Callable[..., Store], root: Path, clock: FakeClock
) -> None:
    stale, successor = opened("stale"), opened("successor")
    old = _acquire(stale)
    result, promotions = _stage(stale, old, {"p00001": b"stale"})
    clock.advance(LEASE_TTL + 1)
    assert successor.drop(["e1"]).dropped == ("e1",)
    new = _acquire(successor)
    assert new.generation > old.generation
    mine = successor.staging_dir(new) / "p00001.jpg"
    mine.write_bytes(b"mine")

    with pytest.raises(LeaseLostError):
        stale.commit_download(old, result, promotions)

    assert mine.read_bytes() == b"mine"


def test_a_drop_by_status_refuses_a_work_whose_status_changed(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    _downloaded(store, "e1")

    report = store.drop(["e1"], statuses={"failed"})

    assert report.dropped == ()
    assert [(r.entry_id, r.reason) for r in report.refused] == [
        ("e1", "its status is now complete")
    ]
    assert _final(root, "e1", "p00001.jpg").is_file()


def test_distinct_entry_ids_get_distinct_scratch_folders(root: Path) -> None:
    names = {inspect_dir(root, entry_id).name for entry_id in ("E-1", "E_1", "e_1")}
    assert len(names) == 3
    assert inspect_dir(root, "e1") == root / "_inspect" / "e1"
    assert inspect_dir(root, "..").parent == root / "_inspect"


def test_a_scratch_folder_stays_inside_the_inspect_folder(
    opened: Callable[..., Store], root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    (outside / "keep").mkdir(parents=True)
    store = opened("a")
    lease = store.acquire(_work(str(outside)), "outside_work")
    assert lease is not None
    store.release(lease)

    store.drop([str(outside)])

    assert (outside / "keep").is_dir()


def test_a_drop_keeps_unrecorded_files_and_refuses_another_runs_lease(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    _downloaded(store, "e1")
    note = root / folder_of("e1") / "notes.txt"
    note.write_text("mine", encoding="utf-8")
    other = opened("b")
    _acquire(other, "e2")

    report = store.drop(["e1", "e2"])

    assert report.dropped == ("e1",)
    assert [r.entry_id for r in report.refused] == ["e2"]
    assert "leased by run b" in report.refused[0].reason
    assert report.kept_files == (note,)
    assert note.is_file()
    assert not _final(root, "e1", "p00001.jpg").exists()
    assert other.work("e2") is not None


# Page labels and the title-page view


def _label(file: FileRow, index: int, title_page: float) -> PageLabel:
    probabilities = {"title_page": title_page, "body_text": 1.0 - title_page}
    page_type = "title_page" if title_page >= 0.5 else "body_text"
    return PageLabel(
        file.path,
        file.attempt_id,
        index,
        page_type,
        probabilities,
        page_types.VERSION,
        "general",
        "typesafe/jev-1.14",
        "openrouter",
        "fp",
    )


def _pages(count: int) -> dict[str, bytes]:
    return {f"p{number:05d}": b"page" for number in range(1, count + 1)}


def _labelled(store: Store, lease: Lease, title_pages: Sequence[float]) -> None:
    """Commit one page image per probability and label each with it."""
    store.commit_download(lease, *_stage(store, lease, _pages(len(title_pages))))
    files = sorted(store.files(lease.entry_id), key=lambda row: row.item)
    labels = [
        _label(file, index, value)
        for index, (file, value) in enumerate(zip(files, title_pages, strict=True))
    ]
    assert store.record_page_labels(lease, labels) == (len(labels), 0)


def _pick(root: Path, entry_id: str = "e1") -> dict[str, Any]:
    (row,) = read_title_page_picks(root, [entry_id])
    return row


def test_a_version_one_store_is_migrated_in_place(root: Path, clock: FakeClock) -> None:
    root.mkdir()
    with contextlib.closing(sqlite3.connect(root / DATABASE_NAME)) as connection:
        for statement in store_module._SCHEMA_V1:
            connection.execute(statement)
        connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        connection.execute(
            "INSERT INTO works (entry_id, title, folder, created_at, updated_at) "
            "VALUES ('e1', 'Kochbuch', 'e1_kochbuch', 't', 't')"
        )
        connection.commit()

    assert read_page_labels(root, "e1") == []
    assert read_title_page_picks(root) == []
    with Store(root, clock=clock, reconcile=False, migrate=False):
        assert store_module.check_schema(root / DATABASE_NAME) == 1
    with Store(root, clock=clock) as store:
        assert store_module.check_schema(store.path) == SCHEMA_VERSION == 2
        assert "page_labels" in sqlite_module.table_names(store.path)
        assert [row["entry_id"] for row in read_work_status(root)] == ["e1"]
        assert store.page_labels("e1") == []


def test_a_version_one_store_under_an_older_runs_lease_is_not_migrated(
    root: Path, clock: FakeClock
) -> None:
    root.mkdir()
    with contextlib.closing(sqlite3.connect(root / DATABASE_NAME)) as connection:
        for statement in store_module._SCHEMA_V1:
            connection.execute(statement)
        connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        connection.execute(
            "INSERT INTO works (entry_id, title, folder, lease_holder, "
            "lease_expires_at, created_at, updated_at) "
            "VALUES ('e1', 'Kochbuch', 'e1_kochbuch', 'old-run', ?, 't', 't')",
            (clock() + 60,),
        )
        connection.commit()

    with pytest.raises(StoreBusyError, match="older chronodownloader") as raised:
        Store(root, clock=clock)
    assert raised.value.exit_code == ExitCode.USAGE
    assert store_module.check_schema(root / DATABASE_NAME) == 1

    clock.advance(61)
    with Store(root, clock=clock) as store:
        assert store_module.check_schema(store.path) == 2


def test_a_label_lands_only_while_its_file_keeps_the_attempt(
    opened: Callable[..., Store],
) -> None:
    store = opened("a")
    lease = _acquire(store)
    store.commit_download(lease, *_stage(store, lease, {"p00001": b"old"}))
    (asked,) = store.files("e1")
    # The file is replaced between the question and the answer's commit.
    store.commit_download(lease, *_stage(store, lease, {"p00001": b"new"}))
    unknown = replace(_label(asked, 1, 0.9), path="objects/p00009.jpg")

    assert store.record_page_labels(lease, [_label(asked, 0, 0.9), unknown]) == (0, 2)
    assert store.page_labels("e1") == []

    (current,) = store.files("e1")
    assert store.record_page_labels(lease, [_label(current, 0, 0.9)]) == (1, 0)
    (label,) = store.page_labels("e1")
    assert (label.attempt_id, label.run_id) == (current.attempt_id, "a")
    assert dict(label.probabilities) == pytest.approx(
        {"title_page": 0.9, "body_text": 0.1}
    )
    store.release(lease)
    with pytest.raises(LeaseLostError):
        store.record_page_labels(lease, [_label(current, 0, 0.9)])


def test_replacing_removing_missing_and_dropped_files_lose_their_labels(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    _labelled(store, lease, [0.9, 0.1, 0.1, 0.1])
    store.commit_download(lease, *_stage(store, lease, {"p00001": b"again"}))
    store.remove_files(lease, ["objects/p00002.jpg"])
    store.release(lease)
    _final(root, "e1", "p00003.jpg").unlink()

    opened()
    assert [label.path for label in store.page_labels("e1")] == ["objects/p00004.jpg"]
    store.drop(["e1"])
    assert store.page_labels("e1") == []


@pytest.mark.parametrize(
    "title_pages",
    [
        [0.1, 0.8, 0.3],
        [0.2, 0.7, 0.7, 0.1],
        [0.1, 0.5, 0.4999],
        [0.1, 0.49, 0.2],
        [0.0] * 10 + [0.99],
        [0.1] * 9 + [0.6, 0.95],
        [0.9],
    ],
    ids=["clear", "tie", "threshold", "below", "eleventh", "tenth", "single"],
)
def test_the_title_page_view_follows_the_shared_rule(
    opened: Callable[..., Store], root: Path, title_pages: list[float]
) -> None:
    assert store_module.PICK_THRESHOLD == page_types.TITLE_PAGE_THRESHOLD
    assert store_module.PICK_SEARCH == page_types.TITLE_PAGE_SEARCH
    store = opened("a")
    _labelled(store, _acquire(store), title_pages)

    expected = page_types.title_page_index([{"title_page": p} for p in title_pages])
    row = _pick(root)
    assert row["page_index"] == expected
    assert (row["pages_total"], row["pages_labelled"]) == (
        len(title_pages),
        min(len(title_pages), page_types.TITLE_PAGE_SEARCH),
    )
    if expected is None:
        assert (row["path"], row["probability"]) == (None, None)
    else:
        assert row["path"] == f"objects/p{expected + 1:05d}.jpg"
        assert row["probability"] == pytest.approx(title_pages[expected])


def test_a_partial_download_keeps_source_positions_and_shows_the_gap(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    items = ("p00001", "p00002", "p00004", "p00005")
    store.commit_download(lease, *_stage(store, lease, dict.fromkeys(items, b"page")))
    by_item = {row.item: row for row in store.files("e1")}
    labels = [
        _label(by_item[item], int(item[1:]) - 1, 0.9 if item == "p00004" else 0.1)
        for item in items
    ]
    store.record_page_labels(lease, labels)

    row = _pick(root)
    assert (row["pages_total"], row["pages_leading"], row["pages_labelled"]) == (
        4,
        5,
        4,
    )
    assert (row["path"], row["page_index"]) == ("objects/p00004.jpg", 3)


def test_items_not_of_page_form_are_numbered_in_item_order(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    store.commit_download(lease, *_stage(store, lease, {"p00002": b"1", "x": b"2"}))
    by_item = {row.item: row for row in store.files("e1")}
    store.record_page_labels(
        lease, [_label(by_item["p00002"], 0, 0.1), _label(by_item["x"], 1, 0.8)]
    )

    row = _pick(root)
    assert (row["pages_leading"], row["pages_labelled"], row["page_index"]) == (
        2,
        2,
        1,
    )


def test_the_view_counts_only_current_labels_and_shows_an_incomplete_run(
    opened: Callable[..., Store], root: Path
) -> None:
    store = opened("a")
    lease = _acquire(store)
    store.commit_download(lease, *_stage(store, lease, _pages(3)))
    files = sorted(store.files("e1"), key=lambda row: row.item)
    store.record_page_labels(lease, [_label(files[0], 0, 0.2)])
    assert (_pick(root)["pages_labelled"], _pick(root)["path"]) == (1, None)

    store.record_page_labels(lease, [_label(files[2], 2, 0.9)])
    assert _pick(root)["path"] == "objects/p00003.jpg"
    # A process of version 1 replaces the file without deleting its label.
    with contextlib.closing(sqlite3.connect(root / DATABASE_NAME)) as connection:
        connection.execute(
            "UPDATE files SET attempt_id = attempt_id + 100 WHERE path = ?",
            ("objects/p00003.jpg",),
        )
        connection.commit()

    row = _pick(root)
    assert (row["pages_total"], row["pages_labelled"], row["path"]) == (3, 1, None)
    assert len(store.page_labels("e1")) == 2
