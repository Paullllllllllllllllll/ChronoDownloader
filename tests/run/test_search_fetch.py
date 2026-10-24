"""End to end: ``search`` and ``fetch`` against fake providers and a stub judge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chronodownloader import run as core
from chronodownloader.output.paths import inspect_dir
from chronodownloader.output.store import read_work_candidates, read_work_status
from chronodownloader.settings import Settings
from chronodownloader.spec import resolve_download, resolve_fetch, resolve_search
from tests.run.support import (
    PROVIDERS,
    FakeProvider,
    Recorder,
    StubJudge,
    record,
    wiring,
    write_csv,
)

COOK = record("mdz", "bsb1", "Le cuisinier françois", year=1651)
COOK_IA = record("internet_archive", "ia1", "Cuisinier francois")


def fakes() -> list[FakeProvider]:
    return [
        FakeProvider("mdz", records={"cuisinier": [COOK]}),
        FakeProvider("internet_archive", records={"cuisinier": [COOK_IA]}),
    ]


def search_plan(settings: Settings, **flags: Any) -> core.RunPlan:
    given: dict[str, Any] = {"providers": PROVIDERS, **flags}
    resolution = resolve_search(given, settings.defaults)
    return core.plan(resolution.spec, settings, sources=resolution.sources)


def fetch_plan(settings: Settings, out: Path, **flags: Any) -> core.RunPlan:
    resolution = resolve_fetch({"output": out, **flags}, settings.defaults)
    return core.plan(resolution.spec, settings, sources=resolution.sources)


def status_of(out: Path, entry_id: str) -> dict[str, Any]:
    (row,) = read_work_status(out, [entry_id])
    return row


def test_a_search_without_output_returns_the_table_and_writes_nothing(
    tmp_path: Path, settings: Settings
) -> None:
    judge = StubJudge({"mdz:bsb1": 0.97, "internet_archive:ia1": 0.4})

    result = core.run(
        search_plan(settings, title="Le cuisinier francois"),
        Recorder(),
        wiring=wiring(fakes(), judge),
    )

    assert result.counts == core.WorkCounts(total=1, accepted=1)
    rows = {row.item_id: row for row in result.rows}
    assert rows["bsb1"].chosen
    assert rows["bsb1"].decision == "accept"
    assert rows["ia1"].decision == "review"
    assert not rows["ia1"].chosen
    assert sorted(path.name for path in tmp_path.iterdir()) == ["home", "state"]


def test_a_search_with_output_stores_candidates_judgments_and_status(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    csv = write_csv(
        tmp_path / "works.csv",
        [
            {"entry_id": "E1", "title": "Le cuisinier francois"},
            {"entry_id": "E2", "title": "Cuisinier francois oder so"},
        ],
    )
    judge = StubJudge({"mdz:bsb1": 0.97, "internet_archive:ia1": 0.5})
    providers = fakes()
    providers[0].records = {"le cuisinier": [COOK], "oder": []}

    result = core.run(
        search_plan(settings, input=csv, output=out),
        Recorder(),
        wiring=wiring(providers, judge),
    )

    assert result.counts == core.WorkCounts(total=2, accepted=1, needs_review=1)
    first, second = status_of(out, "E1"), status_of(out, "E2")
    assert (first["status"], first["selected_provider"]) == ("new", "mdz")
    assert first["file_count"] == 0
    assert second["status"] == "needs_review"
    judged = [row for row in read_work_candidates(out, "E1") if row["judged_at"]]
    assert {row["source_id"] for row in judged} == {"bsb1", "ia1"}

    download = resolve_download(
        {"input": csv, "output": out, "providers": PROVIDERS, "entry_ids": ("E1",)}
    )
    taken = core.plan(download.spec, settings)
    assert [work.work.entry_id for work in taken.works] == ["E1"]
    done = core.run(taken, Recorder(), wiring=wiring(providers, judge))
    assert done.counts.complete == 1
    assert judge.calls.count("mdz:bsb1") == 1


def needs_review(tmp_path: Path, settings: Settings, out: Path) -> FakeProvider:
    """Leave work E1 in the review band; return the mdz fake."""
    csv = write_csv(
        tmp_path / "works.csv", [{"entry_id": "E1", "title": "Le cuisinier francois"}]
    )
    providers = fakes()
    judge = StubJudge({"mdz:bsb1": 0.5, "internet_archive:ia1": 0.02})
    resolution = resolve_download({"input": csv, "output": out, "providers": PROVIDERS})
    core.run(
        core.plan(resolution.spec, settings),
        Recorder(),
        wiring=wiring(providers, judge),
    )
    assert status_of(out, "E1")["status"] == "needs_review"
    return providers[0]


def test_fetch_closes_the_review_band_with_its_reason(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz = needs_review(tmp_path, settings, out)
    with pytest.raises(core.PlanError) as caught:
        fetch_plan(settings, out, entry_id="E1", provider="mdz", id="bsb1")
    assert caught.value.exit_code == 2
    assert "--reason" in str(caught.value)
    scratch = inspect_dir(out, "E1") / "mdz_bsb1"
    scratch.mkdir(parents=True)
    (scratch / "p00001.jpg").write_bytes(b"x")

    reason = "title page matches the 1651 edition"
    result = core.run(
        fetch_plan(
            settings, out, entry_id="E1", provider="mdz", id="bsb1", reason=reason
        ),
        Recorder(),
        wiring=wiring([mdz, *fakes()[1:]]),
    )

    assert result.counts == core.WorkCounts(total=1, complete=1)
    row = status_of(out, "E1")
    assert row["status"] == "complete"
    assert row["status_reason"] == f"fetch: {reason}"
    assert row["selected_source_id"] == "bsb1"
    assert not inspect_dir(out, "E1").exists()
    origins = {r["source_id"]: r["origin"] for r in read_work_candidates(out, "E1")}
    assert origins["bsb1"] == "search"


def test_fetch_of_a_new_work_needs_its_title(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    with pytest.raises(core.PlanError, match="--title"):
        fetch_plan(settings, out, entry_id="N1", provider="mdz", id="bsb7")

    mdz = FakeProvider("mdz")
    result = core.run(
        fetch_plan(
            settings,
            out,
            entry_id="N1",
            title="Ein Kochbuch",
            provider="mdz",
            id="bsb7",
        ),
        Recorder(),
        wiring=wiring([mdz]),
    )

    assert result.counts.complete == 1
    row = status_of(out, "N1")
    assert (row["status"], row["title"], row["file_count"]) == (
        "complete",
        "Ein Kochbuch",
        3,
    )
    (candidate,) = read_work_candidates(out, "N1")
    assert candidate["origin"] == "fetch"


def test_fetch_of_a_complete_work_needs_force(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz = FakeProvider("mdz")
    flags = {"entry_id": "N1", "title": "Ein Kochbuch", "provider": "mdz", "id": "bsb7"}
    core.run(fetch_plan(settings, out, **flags), Recorder(), wiring=wiring([mdz]))

    again = fetch_plan(settings, out, **flags)
    assert again.counts == core.WorkCounts(total=1, skipped=1)
    assert again.fetch is None
    assert core.run(again, Recorder(), wiring=wiring([mdz])).counts.skipped == 1

    forced = fetch_plan(settings, out, force=True, **flags)
    assert core.run(forced, Recorder(), wiring=wiring([mdz])).counts.complete == 1
    assert mdz.downloads[-1] == ("bsb7", frozenset())


def test_fetch_of_a_url_no_provider_serves_is_a_usage_error(
    settings: Settings, out: Path
) -> None:
    with pytest.raises(core.PlanError) as caught:
        fetch_plan(
            settings,
            out,
            entry_id="N1",
            title="X",
            url="https://example.org/a/manifest",
        )
    assert caught.value.exit_code == 2


def test_a_failed_fetch_is_recorded_as_a_failed_attempt(
    settings: Settings, out: Path
) -> None:
    mdz = FakeProvider("mdz", page_counts={"bsb7": 0})
    flags = {"entry_id": "N1", "title": "Ein Kochbuch", "provider": "mdz", "id": "bsb7"}

    result = core.run(
        fetch_plan(settings, out, **flags), Recorder(), wiring=wiring([mdz])
    )

    assert result.counts.failed == 1
    row = status_of(out, "N1")
    assert row["status"] == "failed"
    assert row["last_outcome"] == "failed"
