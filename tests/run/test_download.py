"""End to end: ``download`` against fake providers and a stub judge."""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest

from chronodownloader import run as core
from chronodownloader.events import CandidateJudged, Downloaded
from chronodownloader.matching.judge import JudgeUnavailable
from chronodownloader.output.index import INDEX_NAME
from chronodownloader.output.store import (
    Store,
    WorkInput,
    read_work_candidates,
    read_work_status,
)
from chronodownloader.process.heartbeat import ENV_VAR
from chronodownloader.settings import Settings
from chronodownloader.spec import resolve_download
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
COOK_IA = record("internet_archive", "ia1", "Le cuisinier francois")
KOCH = record("mdz", "bsb2", "Ein new Kochbuch")


def plan(
    settings: Settings, csv: Path, out: Path, *, now: float | None = None, **flags: Any
) -> core.RunPlan:
    given: dict[str, Any] = {"input": csv, "output": out, "providers": PROVIDERS}
    given.update(flags)
    resolution = resolve_download(given, settings.defaults)
    return core.plan(resolution.spec, settings, sources=resolution.sources, now=now)


def works_csv(tmp_path: Path, *extra: dict[str, str]) -> Path:
    rows = [
        {"entry_id": "E1", "title": "Le cuisinier francois", "creator": "La Varenne"},
        {"entry_id": "E2", "title": "Ein new Kochbuch", "year": "1581"},
        *extra,
    ]
    return write_csv(tmp_path / "works.csv", rows)


def providers() -> tuple[FakeProvider, FakeProvider]:
    mdz = FakeProvider(
        "mdz",
        records={"cuisinier": [COOK], "kochbuch": [KOCH]},
        page_counts={"bsb1": 4},
    )
    ia = FakeProvider("internet_archive", records={"cuisinier": [COOK_IA]})
    return mdz, ia


def accepting() -> StubJudge:
    return StubJudge({"mdz:bsb1": 0.97, "internet_archive:ia1": 0.95, "mdz:bsb2": 0.96})


def statuses(out: Path) -> dict[str, dict[str, Any]]:
    return {row["entry_id"]: row for row in read_work_status(out)}


def test_a_download_searches_judges_and_downloads_every_work(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    events = Recorder()

    result = core.run(
        plan(settings, works_csv(tmp_path), out),
        events,
        wiring=wiring([mdz, ia], accepting()),
    )

    assert result.counts == core.WorkCounts(total=2, complete=2)
    rows = statuses(out)
    assert {entry: row["status"] for entry, row in rows.items()} == {
        "E1": "complete",
        "E2": "complete",
    }
    assert rows["E1"]["selected_provider"] == "mdz"
    assert rows["E1"]["pages_present"] == 4
    folder = out / rows["E1"]["folder"]
    names = sorted(path.name for path in (folder / "objects").iterdir())
    assert names == [f"{rows['E1']['folder']}_mdz_image_00{n}.jpg" for n in range(1, 5)]
    assert (out / INDEX_NAME).is_file()
    assert events.statuses() == {"E1": "complete", "E2": "complete"}
    assert sum(isinstance(event, Downloaded) for event in events.events) == 2
    assert not list(folder.glob("objects/.staging/*"))


def test_the_heartbeat_counts_finished_works(
    tmp_path: Path, settings: Settings, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    beat = tmp_path / "beat.json"
    monkeypatch.setenv(ENV_VAR, str(beat))
    mdz, ia = providers()

    core.run(
        plan(settings, works_csv(tmp_path), out),
        Recorder(),
        wiring=wiring([mdz, ia], accepting()),
    )

    payload = json.loads(beat.read_text(encoding="utf-8"))
    assert (payload["units_done"], payload["units_total"]) == (2, 2)
    assert payload["unit_label"] == "work"


def test_format_and_page_limit_reach_the_download(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    mdz.pdf = {"bsb1"}
    csv = works_csv(tmp_path)

    core.run(
        plan(settings, csv, out, entry_ids=("E1",)),
        Recorder(),
        wiring=wiring([mdz, ia], accepting()),
    )
    row = statuses(out)["E1"]
    pdfs = [path.name for path in (out / row["folder"] / "objects").iterdir()]
    assert (row["status"], pdfs) == ("complete", [f"{row['folder']}_mdz.pdf"])

    core.run(
        plan(settings, csv, out, entry_ids=("E2",), format="images", max_pages=2),
        Recorder(),
        wiring=wiring([mdz, ia], accepting()),
    )
    row = statuses(out)["E2"]
    assert (row["status"], row["pages_expected"], row["file_count"]) == (
        "complete",
        2,
        2,
    )


def test_nothing_to_do_opens_no_store(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    result = core.run(
        plan(settings, works_csv(tmp_path), out, only=("failed",)),
        Recorder(),
        wiring=wiring(providers(), accepting()),
    )

    assert result.counts == core.WorkCounts(total=2, skipped=2)
    assert not out.exists()


def test_a_partial_work_exits_1_and_resumes_with_its_present_pages(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    mdz.failing = {"p00003"}
    judge = accepting()
    csv = works_csv(tmp_path)
    first = core.run(
        plan(settings, csv, out, entry_ids=("E1",)),
        Recorder(),
        wiring=wiring([mdz, ia], judge),
    )
    assert first.counts.partial == 1
    row = statuses(out)["E1"]
    assert row["status"] == "partial"
    assert "images: 1 of 4 missing (transient 1)" in row["status_reason"]

    mdz.failing = set()
    second = core.run(
        plan(settings, csv, out, entry_ids=("E1",)),
        Recorder(),
        wiring=wiring([mdz, ia], judge),
    )

    assert second.counts.complete == 1
    assert statuses(out)["E1"]["status"] == "complete"
    assert mdz.downloads[-1] == ("bsb1", frozenset({"p00001", "p00002", "p00004"}))
    assert mdz.searches == ["Le cuisinier francois"]
    assert judge.calls.count("mdz:bsb1") == 1


def test_an_interrupted_run_releases_its_leases_and_the_next_run_resumes(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    judge = accepting()
    csv = works_csv(tmp_path)

    def on_e2_judged(event: Any) -> bool:
        return isinstance(event, CandidateJudged) and event.entry_id == "E2"

    with pytest.raises(KeyboardInterrupt):
        core.run(
            plan(settings, csv, out, concurrency=1),
            Recorder(interrupt=on_e2_judged),
            wiring=wiring([mdz, ia], judge),
        )

    rows = statuses(out)
    assert rows["E1"]["status"] == "complete"
    assert rows["E2"]["status"] == "new"
    assert rows["E2"]["lease_holder"] is None
    assert len(read_work_candidates(out, "E2")) == 1

    result = core.run(
        plan(settings, csv, out), Recorder(), wiring=wiring([mdz, ia], judge)
    )

    assert result.counts == core.WorkCounts(total=2, complete=1, skipped=1)
    assert statuses(out)["E2"]["status"] == "complete"
    assert mdz.searches.count("Ein new Kochbuch") == 1
    assert judge.calls.count("mdz:bsb2") == 1


def test_a_work_another_run_holds_is_counted_blocked(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    csv = works_csv(tmp_path)
    folder = plan(settings, csv, out).works[0].folder
    with Store(out) as other:
        other.begin_run("download")
        assert other.acquire(WorkInput("E1", "Le cuisinier francois"), folder)
        events = Recorder()
        result = core.run(
            plan(settings, csv, out), events, wiring=wiring([mdz, ia], accepting())
        )

    assert result.counts.blocked == 1
    assert result.counts.complete == 1
    assert events.statuses()["E1"] == "blocked"
    assert mdz.searches == ["Ein new Kochbuch"]


def test_a_judge_outage_defers_the_work_until_retry_after(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    retry_after = time.time() + 3600
    judge = accepting()
    judge.answers["mdz:bsb1"] = JudgeUnavailable("endpoint down", retry_after)
    csv = works_csv(tmp_path)

    result = core.run(
        plan(settings, csv, out, entry_ids=("E1",)),
        Recorder(),
        wiring=wiring([mdz, ia], judge),
    )

    assert result.counts.deferred == 1
    assert result.counts.complete == 0
    row = statuses(out)["E1"]
    assert row["status"] == "deferred"
    assert "endpoint down" in row["status_reason"]
    assert row["file_count"] == 0

    waiting = plan(settings, csv, out, entry_ids=("E1",), now=time.time())
    assert waiting.works == ()
    assert waiting.counts.skipped == 1

    judge.answers["mdz:bsb1"] = 0.97
    later = plan(settings, csv, out, entry_ids=("E1",), now=retry_after + 1)
    # The run re-checks retry_after when it takes the lease: its clock must
    # have passed retry_after too.
    passed = dataclasses.replace(
        wiring([mdz, ia], judge), clock=lambda: time.time() + 3601
    )
    result = core.run(later, Recorder(), wiring=passed)

    assert result.counts.complete == 1
    assert statuses(out)["E1"]["status"] == "complete"
    assert mdz.searches == ["Le cuisinier francois"]


def test_research_searches_and_judges_again_keeping_the_old_candidates(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    judge = accepting()
    csv = works_csv(tmp_path)
    core.run(
        plan(settings, csv, out, entry_ids=("E1",)),
        Recorder(),
        wiring=wiring([mdz, ia], judge),
    )

    result = core.run(
        plan(settings, csv, out, entry_ids=("E1",), only=("complete",), research=True),
        Recorder(),
        wiring=wiring([mdz, ia], judge),
    )

    assert result.counts.complete == 1
    assert mdz.searches == ["Le cuisinier francois"] * 2
    assert judge.calls.count("mdz:bsb1") == 2
    rows = read_work_candidates(out, "E1")
    assert len(rows) == 4
    assert sorted(row["current"] for row in rows) == [0, 0, 1, 1]


def test_reviewed_and_rejected_works_take_their_open_statuses(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    judge = StubJudge({"mdz:bsb1": 0.5, "internet_archive:ia1": 0.02}, default=0.02)

    result = core.run(
        plan(settings, works_csv(tmp_path), out),
        Recorder(),
        wiring=wiring([mdz, ia], judge),
    )

    assert result.counts == core.WorkCounts(total=2, needs_review=1, no_match=1)
    rows = statuses(out)
    assert rows["E1"]["status"] == "needs_review"
    assert rows["E1"]["status_reason"] == "review_band: mdz:bsb1"
    assert rows["E2"]["status"] == "no_match"
    assert mdz.downloads == []


def test_a_failed_search_on_every_provider_fails_the_work(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    from chronodownloader.providers.base import ProviderBlocked

    mdz, ia = providers()
    mdz.search_error = ProviderBlocked("mdz", "transient")
    ia.search_error = ProviderBlocked("internet_archive", "blocked")
    events = Recorder()

    result = core.run(
        plan(settings, works_csv(tmp_path), out, entry_ids=("E1",)),
        events,
        wiring=wiring([mdz, ia]),
    )

    assert result.counts.failed == 1
    assert statuses(out)["E1"]["status"] == "failed"
    assert any("mdz search failed" in warning for warning in events.warnings)


@dataclasses.dataclass
class CreatorBlind(FakeProvider):
    """Finds nothing for a query with a creator, as Gallica for Tirel."""

    creators: list[str | None] = dataclasses.field(default_factory=list)

    def search(self, query: Any, *, network: Any, limit: int) -> list[Any]:
        self.creators.append(query.creator)
        if query.creator:
            return []
        return super().search(query, network=network, limit=limit)


def test_a_creator_search_without_records_is_repeated_without_the_creator(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz = CreatorBlind("mdz", records={"cuisinier": [COOK]}, page_counts={"bsb1": 4})
    _mdz, ia = providers()

    result = core.run(
        plan(settings, works_csv(tmp_path), out, entry_ids=("E1",)),
        Recorder(),
        wiring=wiring([mdz, ia], accepting()),
    )

    assert result.counts.complete == 1
    assert mdz.creators == ["La Varenne", None]
    assert statuses(out)["E1"]["selected_provider"] == "mdz"


def test_the_judge_key_is_required_under_judge_and_not_under_fuzzy(
    tmp_path: Path, settings: Settings, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    csv = works_csv(tmp_path)

    with pytest.raises(core.PlanError) as caught:
        plan(settings, csv, out)
    assert caught.value.exit_code == 2
    assert "--matching fuzzy" in str(caught.value)

    mdz, ia = providers()
    result = core.run(
        plan(settings, csv, out, matching="fuzzy"), Recorder(), wiring=wiring([mdz, ia])
    )

    assert result.counts.needs_review == 2
    assert mdz.downloads == []


def test_a_per_work_size_limit_leaves_the_work_partial(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    csv = works_csv(tmp_path)

    result = core.run(
        plan(settings, csv, out, entry_ids=("E1",), work_images_gb=1e-9),
        Recorder(),
        wiring=wiring([mdz, ia], accepting()),
    )

    assert result.counts.partial == 1
    row = statuses(out)["E1"]
    assert row["pages_present"] == 1
    assert "stopped: budget" in row["status_reason"]


def test_a_run_size_limit_under_stop_ends_the_run(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    events = Recorder()

    result = core.run(
        plan(
            settings,
            works_csv(tmp_path),
            out,
            total_images_gb=1e-9,
            on_exceed="stop",
            concurrency=1,
        ),
        events,
        wiring=wiring([mdz, ia], accepting()),
    )

    assert result.counts == core.WorkCounts(total=2, partial=1, skipped=1)
    assert any("size limit" in warning for warning in events.warnings)
    assert "E2" not in statuses(out)


def test_unstored_folders_are_skipped_unless_forced(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    csv = works_csv(tmp_path)
    folder = plan(settings, csv, out).works[0].folder
    (out / folder).mkdir(parents=True)

    skipped = plan(settings, csv, out)
    forced = plan(settings, csv, out, force=True)

    assert [work.work.entry_id for work in skipped.works] == ["E2"]
    assert any("unstored" in warning for warning in skipped.warnings)
    assert [work.work.entry_id for work in forced.works] == ["E1", "E2"]


def test_complete_works_are_skipped_and_force_downloads_them_again(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = providers()
    csv = works_csv(tmp_path)
    core.run(
        plan(settings, csv, out, entry_ids=("E1",)),
        Recorder(),
        wiring=wiring([mdz, ia], accepting()),
    )

    assert plan(settings, csv, out, entry_ids=("E1",)).works == ()
    result = core.run(
        plan(settings, csv, out, entry_ids=("E1",), force=True),
        Recorder(),
        wiring=wiring([mdz, ia], accepting()),
    )

    assert result.counts.complete == 1
    assert mdz.downloads[-1] == ("bsb1", frozenset())
