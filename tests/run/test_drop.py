"""End to end: ``drop`` deletes works by entry id, status or all of them."""

from __future__ import annotations

from pathlib import Path

from chronodownloader import run as core
from chronodownloader.output.index import INDEX_NAME
from chronodownloader.output.paths import inspect_dir
from chronodownloader.output.store import Store, WorkInput, read_work_status
from chronodownloader.settings import Settings
from chronodownloader.spec import resolve_download, resolve_drop
from tests.run.support import FakeProvider, StubJudge, record, run_command, write_csv


def stored(tmp_path: Path, settings: Settings, out: Path) -> None:
    """Store E1 complete (3 pages) and E2 in review."""
    csv = write_csv(
        tmp_path / "works.csv",
        [
            {"entry_id": "E1", "title": "Le cuisinier francois"},
            {"entry_id": "E2", "title": "Ein new Kochbuch"},
        ],
    )
    mdz = FakeProvider(
        "mdz",
        records={
            "cuisinier": [record("mdz", "bsb1", "Le cuisinier françois")],
            "kochbuch": [record("mdz", "bsb2", "Ein new Kochbuch")],
        },
    )
    judge = StubJudge({"mdz:bsb1": 0.97, "mdz:bsb2": 0.5})
    flags = {"input": csv, "output": out, "providers": ("mdz",), "format": "images"}
    run_command(resolve_download, settings, flags, [mdz], judge)
    (inspect_dir(out, "E1") / "mdz_bsb1").mkdir(parents=True)


def entry_ids(out: Path) -> list[str]:
    return [str(row["entry_id"]) for row in read_work_status(out)]


def snapshot(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_a_dry_run_lists_the_drop_and_changes_nothing(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    stored(tmp_path, settings, out)
    before = snapshot(out)
    resolution = resolve_drop({"output": out, "all": True, "dry_run": True})

    run_plan = core.plan(resolution.spec, settings)

    report = run_plan.report
    assert isinstance(report, core.DropReport)
    assert (report.dry_run, report.dropped) == (True, ("E1", "E2"))
    assert len(report.deleted_files) >= 3
    assert run_plan.counts == core.WorkCounts(total=2, complete=2)
    assert snapshot(out) == before


def test_drop_removes_rows_files_and_scratch_and_renders_the_index(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    stored(tmp_path, settings, out)
    (row,) = read_work_status(out, ["E1"])
    folder = out / str(row["folder"])

    result = run_command(resolve_drop, settings, {"output": out, "entry_ids": ("E1",)})

    assert result.counts == core.WorkCounts(total=1, complete=1)
    assert entry_ids(out) == ["E2"]
    assert not folder.exists()
    assert not inspect_dir(out, "E1").exists()
    index = (out / INDEX_NAME).read_text(encoding="utf-8")
    assert "E2" in index
    assert "E1" not in index


def test_drop_by_status_takes_only_those_works(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    stored(tmp_path, settings, out)

    result = run_command(
        resolve_drop, settings, {"output": out, "only": ("needs_review",)}
    )

    assert result.counts.complete == 1
    assert entry_ids(out) == ["E1"]


def test_drop_refuses_a_work_another_run_holds(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    stored(tmp_path, settings, out)
    other = Store(out)
    other.begin_run("download", run_id="other-run")
    (row,) = read_work_status(out, ["E2"])
    lease = other.acquire(WorkInput("E2", "Ein new Kochbuch"), str(row["folder"]))
    assert lease is not None
    try:
        result = run_command(resolve_drop, settings, {"output": out, "all": True})
    finally:
        other.close()

    assert result.counts == core.WorkCounts(total=2, complete=1, blocked=1)
    assert isinstance(result.report, core.DropReport)
    assert [refusal.entry_id for refusal in result.report.refused] == ["E2"]
    assert entry_ids(out) == ["E2"]


def test_unknown_entry_ids_are_skipped(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    stored(tmp_path, settings, out)

    result = run_command(
        resolve_drop, settings, {"output": out, "entry_ids": ("E1", "X9")}
    )

    assert result.counts == core.WorkCounts(total=2, complete=1, skipped=1)
