"""End to end: ``verify`` checks recorded files and, with ``--pages``, the
first pages of each work."""

from __future__ import annotations

from pathlib import Path

from chronodownloader import run as core
from chronodownloader.matching.judge import JudgeUnavailable
from chronodownloader.output.store import read_files, read_work_status
from chronodownloader.settings import Settings
from chronodownloader.spec import resolve_download, resolve_verify
from tests.run.support import (
    PROVIDERS,
    FakeProvider,
    StubJudge,
    record,
    run_command,
    write_csv,
)

COOK = record("mdz", "bsb1", "Le cuisinier françois")
KOCH = record("mdz", "bsb2", "Ein new Kochbuch")


def downloaded(tmp_path: Path, settings: Settings, out: Path) -> FakeProvider:
    """Download E1 (4 pages) and E2 (3 pages) completely."""
    csv = write_csv(
        tmp_path / "works.csv",
        [
            {"entry_id": "E1", "title": "Le cuisinier francois"},
            {"entry_id": "E2", "title": "Ein new Kochbuch"},
        ],
    )
    mdz = FakeProvider(
        "mdz",
        records={"cuisinier": [COOK], "kochbuch": [KOCH]},
        page_counts={"bsb1": 4},
    )
    judge = StubJudge({"mdz:bsb1": 0.97, "mdz:bsb2": 0.97})
    flags = {
        "input": csv,
        "output": out,
        "providers": PROVIDERS,
        "format": "images",
        "include_metadata": False,
    }
    result = run_command(resolve_download, settings, flags, [mdz], judge)
    assert result.counts.complete == 2
    return mdz


def page_path(out: Path, entry_id: str, item: str) -> Path:
    (row,) = read_work_status(out, [entry_id])
    (file,) = [f for f in read_files(out, entry_id) if f.item == item]
    return out / str(row["folder"]) / file.path


def verify(
    settings: Settings, judge: StubJudge | None = None, **flags: object
) -> core.RunResult:
    return run_command(resolve_verify, settings, flags, (), judge)


def status(out: Path, entry_id: str) -> str:
    (row,) = read_work_status(out, [entry_id])
    return str(row["status"])


def test_intact_works_verify_complete(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)

    result = verify(settings, output=out)

    assert result.counts == core.WorkCounts(total=2, complete=2)
    assert isinstance(result.report, core.VerifyReport)
    assert [work.files for work in result.report.works] == [4, 3]


def test_a_missing_file_fails_the_work_and_reconciliation_makes_it_partial(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    gone = page_path(out, "E1", "p00002")
    gone.unlink()

    result = verify(settings, output=out, entry_ids=("E1",))

    assert result.counts == core.WorkCounts(total=1, failed=1)
    assert isinstance(result.report, core.VerifyReport)
    (work,) = result.report.works
    assert (work.status, work.downgraded) == ("partial", True)
    assert work.missing == (gone.relative_to(out / work.folder).as_posix(),)
    assert status(out, "E1") == "partial"
    assert len(read_files(out, "E1")) == 3
    assert status(out, "E2") == "complete"


def test_a_dry_run_reports_the_missing_file_and_changes_nothing(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    page_path(out, "E1", "p00001").unlink()
    database = out / "chronodownloader.sqlite"
    before = database.read_bytes()
    resolution = resolve_verify({"output": out, "dry_run": True})

    run_plan = core.plan(resolution.spec, settings)

    assert run_plan.counts == core.WorkCounts(total=2, complete=1, failed=1)
    assert isinstance(run_plan.report, core.VerifyReport)
    assert run_plan.report.dry_run
    assert run_plan.report.works[0].downgraded
    assert database.read_bytes() == before
    assert status(out, "E1") == "complete"


def test_a_file_of_another_size_is_reported_and_kept(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    changed = page_path(out, "E2", "p00003")
    changed.write_bytes(changed.read_bytes() + b"tail")

    result = verify(settings, output=out, entry_ids=("E2",))

    assert result.counts == core.WorkCounts(total=1, failed=1)
    assert isinstance(result.report, core.VerifyReport)
    (work,) = result.report.works
    assert work.mismatched == (changed.relative_to(out / work.folder).as_posix(),)
    assert work.missing == ()
    assert status(out, "E2") == "complete"


def test_pages_asks_the_page_check_over_the_first_page_files(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz = downloaded(tmp_path, settings, out)
    judge = StubJudge(pages={"Le cuisinier francois": 0.1})

    result = verify(settings, judge, output=out, pages=True, first=2)

    assert result.counts == core.WorkCounts(total=2, complete=2)
    assert sorted(judge.page_calls) == [
        ("Ein new Kochbuch", 2),
        ("Le cuisinier francois", 2),
    ]
    assert isinstance(result.report, core.VerifyReport)
    first = result.report.works[0].pages
    assert first is not None
    assert (first.outcome, first.provider, first.item_id) == ("checked", "mdz", "bsb1")
    assert first.answer is not None
    assert first.answer.title_page == 0.1
    assert status(out, "E1") == "complete"
    assert mdz.sampled == []


def test_a_judge_outage_defers_the_page_check(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    judge = StubJudge(pages={"Ein new Kochbuch": JudgeUnavailable("timeout", 9e9)})

    result = verify(settings, judge, output=out, pages=True)

    assert result.counts == core.WorkCounts(total=2, complete=1, deferred=1)
    assert status(out, "E2") == "complete"
