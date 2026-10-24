"""End to end: ``inspect`` samples first pages and asks the page check."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from chronodownloader import run as core
from chronodownloader.matching.judge import JudgeUnavailable
from chronodownloader.output.paths import inspect_dir
from chronodownloader.output.store import read_work_status
from chronodownloader.providers.base import Capabilities
from chronodownloader.settings import Settings
from chronodownloader.spec import (
    resolve_download,
    resolve_drop,
    resolve_fetch,
    resolve_inspect,
    resolve_status,
)
from tests.run.support import (
    PROVIDERS,
    FakeProvider,
    StubJudge,
    record,
    run_command,
    write_csv,
)

COOK = record("mdz", "bsb1", "Le cuisinier françois", year=1651)
COOK_IA = record("internet_archive", "ia1", "Cuisinier francois")
KOCH = record("mdz", "bsb2", "Ein new Kochbuch")


def fakes() -> list[FakeProvider]:
    return [
        FakeProvider(
            "mdz",
            records={"cuisinier": [COOK], "kochbuch": [KOCH]},
            page_counts={"bsb1": 5},
        ),
        FakeProvider("internet_archive", records={"cuisinier": [COOK_IA]}),
    ]


def reviewed(tmp_path: Path, settings: Settings, out: Path) -> list[FakeProvider]:
    """Leave E1 and E2 in the review band with their candidates stored."""
    csv = write_csv(
        tmp_path / "works.csv",
        [
            {"entry_id": "E1", "title": "Le cuisinier francois", "year": "1651"},
            {"entry_id": "E2", "title": "Ein new Kochbuch"},
        ],
    )
    providers = fakes()
    judge = StubJudge({"mdz:bsb1": 0.5, "internet_archive:ia1": 0.4, "mdz:bsb2": 0.5})
    flags = {"input": csv, "output": out, "providers": PROVIDERS}
    result = run_command(resolve_download, settings, flags, providers, judge)
    assert result.counts.needs_review == 2
    return providers


def inspect(
    settings: Settings,
    adapters: list[FakeProvider],
    judge: StubJudge,
    **flags: object,
) -> core.RunResult:
    return run_command(resolve_inspect, settings, flags, adapters, judge)


def test_inspect_samples_each_stored_candidate_and_asks_once_over_its_pages(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    providers = reviewed(tmp_path, settings, out)
    judge = StubJudge(pages={"Le cuisinier francois": 0.95})

    result = inspect(settings, providers, judge, entry_ids=("E1",), output=out, first=4)

    assert result.counts == core.WorkCounts(total=2, complete=2)
    assert isinstance(result.report, core.InspectReport)
    checks = {check.item_id: check for check in result.report.checks}
    assert checks["bsb1"].pages == 4
    assert checks["bsb1"].answer is not None
    assert checks["bsb1"].answer.title_page == 0.95
    assert checks["ia1"].pages == 3
    assert sorted(judge.page_calls) == [
        ("Le cuisinier francois", 3),
        ("Le cuisinier francois", 4),
    ]
    scratch = inspect_dir(out, "E1")
    assert sorted(path.name for path in (scratch / "mdz_bsb1").iterdir()) == [
        "p00001.jpg",
        "p00002.jpg",
        "p00003.jpg",
        "p00004.jpg",
    ]
    assert (scratch / "internet_archive_ia1" / "p00003.jpg").is_file()
    assert providers[0].sampled[-1] == ("bsb1", 4)
    (row,) = read_work_status(out, ["E1"])
    assert row["status"] == "needs_review"


def test_inspect_takes_only_the_named_providers(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    providers = reviewed(tmp_path, settings, out)

    result = inspect(
        settings,
        providers,
        StubJudge(),
        entry_ids=("E1",),
        output=out,
        providers=("internet_archive",),
    )

    assert isinstance(result.report, core.InspectReport)
    assert [check.provider for check in result.report.checks] == ["internet_archive"]
    assert not (inspect_dir(out, "E1") / "mdz_bsb1").exists()


def test_a_judge_outage_defers_one_check_and_the_others_go_on(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    providers = reviewed(tmp_path, settings, out)
    judge = StubJudge(pages={"Ein new Kochbuch": JudgeUnavailable("timeout", 9e9)})

    result = inspect(settings, providers, judge, entry_ids=("E1", "E2"), output=out)

    assert result.counts == core.WorkCounts(total=3, complete=2, deferred=1)
    assert isinstance(result.report, core.InspectReport)
    (deferred,) = [c for c in result.report.checks if c.outcome == "deferred"]
    assert (deferred.entry_id, deferred.retry_after) == ("E2", 9e9)


def test_fetch_and_drop_clear_the_scratch_and_status_lists_what_is_left(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    providers = reviewed(tmp_path, settings, out)
    inspect(settings, providers, StubJudge(), entry_ids=("E1", "E2"), output=out)

    listing = run_command(resolve_status, settings, {"output": out})
    assert isinstance(listing.report, core.StatusReport)
    assert [left.name for left in listing.report.leftovers] == ["E1", "E2"]
    assert listing.report.leftovers[0].items == ("internet_archive_ia1", "mdz_bsb1")

    fetched = run_command(
        resolve_fetch,
        settings,
        {
            "output": out,
            "entry_id": "E1",
            "provider": "mdz",
            "id": "bsb1",
            "reason": "title page matches",
        },
        providers,
    )
    assert fetched.counts.complete == 1
    assert not inspect_dir(out, "E1").exists()

    dropped = run_command(resolve_drop, settings, {"output": out, "entry_ids": ("E2",)})
    assert dropped.counts.complete == 1
    assert not inspect_dir(out, "E2").exists()

    listing = run_command(resolve_status, settings, {"output": out})
    assert isinstance(listing.report, core.StatusReport)
    assert listing.report.leftovers == ()


def test_an_item_no_work_names_is_sampled_into_the_temporary_scratch(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    judge = StubJudge()

    result = inspect(settings, fakes(), judge, provider="mdz", id="bsb9", first=2)

    assert isinstance(result.report, core.InspectReport)
    (check,) = result.report.checks
    assert (check.outcome, check.pages, check.entry_id) == ("sampled", 2, None)
    folder = tmp_path / "temp" / "chronodownloader" / "inspect" / "mdz_bsb9"
    assert check.folder == folder
    assert (folder / "p00002.jpg").is_file()
    assert judge.page_calls == []
    assert result.counts == core.WorkCounts(total=1, skipped=1)


def test_an_item_a_stored_work_names_is_checked_against_that_work(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    providers = reviewed(tmp_path, settings, out)
    judge = StubJudge()

    result = inspect(settings, providers, judge, provider="mdz", id="bsb2", output=out)

    assert isinstance(result.report, core.InspectReport)
    (check,) = result.report.checks
    assert (check.outcome, check.entry_id) == ("checked", "E2")
    assert judge.page_calls == [("Ein new Kochbuch", 3)]
    assert (inspect_dir(out, "E2") / "mdz_bsb2" / "p00001.jpg").is_file()


def test_a_metadata_only_provider_is_skipped(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    mdz = FakeProvider("mdz", capabilities=Capabilities(downloads=False))

    result = inspect(settings, [mdz], StubJudge(), provider="mdz", id="b1")

    assert result.counts == core.WorkCounts(total=1, skipped=1)
    assert isinstance(result.report, core.InspectReport)
    assert "not supported" in (result.report.checks[0].note or "")


def test_inspect_without_the_judge_key_exits_2(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    resolution = resolve_inspect({"provider": "mdz", "id": "b1"})

    with pytest.raises(core.PlanError) as caught:
        core.plan(resolution.spec, settings)

    assert caught.value.exit_code == 2
    assert "OPENROUTER_API_KEY" in str(caught.value)


def test_a_dry_run_plans_the_items_and_writes_nothing(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    reviewed(tmp_path, settings, out)
    resolution = resolve_inspect({"entry_ids": ("E1",), "output": out, "dry_run": True})

    run_plan = core.plan(resolution.spec, settings)

    assert run_plan.counts.total == 2
    assert isinstance(run_plan.report, core.InspectReport)
    assert {check.outcome for check in run_plan.report.checks} == {"planned"}
    assert not inspect_dir(out, "E1").exists()
