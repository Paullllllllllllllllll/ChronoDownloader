"""End to end: ``status`` lists works, the review queue, ``unstored`` folders
and ``inspect`` leftovers, and writes nothing."""

from __future__ import annotations

from pathlib import Path

from chronodownloader import run as core
from chronodownloader.output.paths import inspect_dir
from chronodownloader.settings import Settings
from chronodownloader.spec import resolve_download, resolve_status
from tests.run.support import (
    FakeProvider,
    StubJudge,
    record,
    run_command,
    write_csv,
)

ROWS = [
    {"entry_id": "E1", "title": "Le cuisinier francois"},
    {"entry_id": "E2", "title": "Ein new Kochbuch"},
    {"entry_id": "E3", "title": "Nichts zu finden"},
    {"entry_id": "E4", "title": "Le patissier"},
]


def stored(tmp_path: Path, settings: Settings, out: Path) -> Path:
    """Store E1 complete, E2 needs_review, E3 no_match and E4 failed; return
    the works CSV, which adds E5 without a store entry."""
    csv = write_csv(tmp_path / "works.csv", ROWS)
    mdz = FakeProvider(
        "mdz",
        records={
            "cuisinier": [record("mdz", "bsb1", "Le cuisinier françois")],
            "kochbuch": [record("mdz", "bsb2", "Ein new Kochbuch")],
            "patissier": [record("mdz", "bsb4", "Le patissier")],
        },
        failing={"p00001", "p00002", "p00003"},
        pdf={"bsb1"},
    )
    judge = StubJudge({"mdz:bsb1": 0.97, "mdz:bsb2": 0.5, "mdz:bsb4": 0.97})
    flags = {"input": csv, "output": out, "providers": ("mdz",)}
    run_command(resolve_download, settings, flags, [mdz], judge)
    return write_csv(tmp_path / "all.csv", [*ROWS, {"entry_id": "E5", "title": "Neu"}])


def listing(settings: Settings, **flags: object) -> core.StatusReport:
    result = run_command(resolve_status, settings, flags)
    assert isinstance(result.report, core.StatusReport)
    return result.report


def test_status_lists_every_status_the_review_queue_and_new_input_rows(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    csv = stored(tmp_path, settings, out)
    database = out / "chronodownloader.sqlite"
    before = database.read_bytes()

    result = run_command(resolve_status, settings, {"output": out, "input": csv})

    report = result.report
    assert isinstance(report, core.StatusReport)
    statuses = {row.entry_id: row.status for row in report.works}
    assert statuses == {
        "E1": "complete",
        "E2": "needs_review",
        "E3": "no_match",
        "E4": "failed",
        "E5": "new",
    }
    assert [row.entry_id for row in report.review] == ["E2"]
    assert report.review[0].reason
    assert result.counts == core.WorkCounts(
        total=5, complete=1, failed=1, no_match=1, needs_review=1
    )
    assert database.read_bytes() == before


def test_status_filters_by_status_and_entry_id(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    stored(tmp_path, settings, out)

    only = listing(settings, output=out, only=("failed", "no_match"))
    picked = listing(settings, output=out, entry_ids=("E2",))

    assert [row.entry_id for row in only.works] == ["E3", "E4"]
    assert [row.entry_id for row in picked.works] == ["E2"]


def test_status_lists_unstored_folders_and_inspect_leftovers(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    stored(tmp_path, settings, out)
    (out / "old_work").mkdir()
    (inspect_dir(out, "E2") / "mdz_bsb2").mkdir(parents=True)
    (inspect_dir(out, "X9") / "mdz_bsb9").mkdir(parents=True)

    report = listing(settings, output=out)

    assert report.unstored == ("old_work",)
    leftovers = {left.name: left for left in report.leftovers}
    assert leftovers["E2"].items == ("mdz_bsb2",)
    assert leftovers["E2"].stored
    # A folder no stored work names is listed by its folder name.
    assert not leftovers[inspect_dir(out, "X9").name].stored


def test_status_of_a_root_without_a_store_creates_nothing(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    report = listing(settings, output=out)

    assert report == core.StatusReport()
    assert not out.exists()
