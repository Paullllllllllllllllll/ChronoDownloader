"""Regressions of the v1.0.0 live round (``docs/live_round_v1_0_0.md``):
one test per defect the run core fixes (D1, D3, D4, F2, N1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chronodownloader import run as core
from chronodownloader._loop import describe_result
from chronodownloader._runtime import Services
from chronodownloader._worker import link_record
from chronodownloader.coord import Coordinator, Holder
from chronodownloader.net import Network
from chronodownloader.output.store import Store, read_work_status
from chronodownloader.providers.base import (
    Completeness,
    DownloadRequest,
    DownloadResult,
    SearchRecord,
    page_item,
)
from chronodownloader.settings import Settings
from chronodownloader.spec import resolve_download, resolve_fetch
from tests.core.spawn import CHILD_TIMEOUT, child, expect, line, send
from tests.iiif.support import v2_manifest
from tests.providers.routed import RoutedSession, json_route
from tests.run.support import (
    PROVIDERS,
    FakeProvider,
    Recorder,
    StubJudge,
    record,
    wiring,
    write_csv,
)

COOK = record("mdz", "bsb1", "Le cuisinier françois")
COOK_IA = record("internet_archive", "ia1", "Le cuisinier francois")
KOCH = record("mdz", "bsb2", "Ein new Kochbuch")


def download_plan(settings: Settings, tmp_path: Path, out: Path) -> core.RunPlan:
    csv = write_csv(
        tmp_path / "works.csv",
        [
            {"entry_id": "E1", "title": "Le cuisinier francois"},
            {"entry_id": "E2", "title": "Ein new Kochbuch"},
        ],
    )
    flags: dict[str, Any] = {"input": csv, "output": out, "providers": PROVIDERS}
    resolution = resolve_download(flags, settings.defaults)
    return core.plan(resolution.spec, settings, sources=resolution.sources)


def status(out: Path, entry_id: str) -> dict[str, Any]:
    (row,) = read_work_status(out, [entry_id])
    return row


def test_d1_a_download_without_files_falls_back_to_the_next_acceptable_candidate(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    """DDB 6776 under Luna: the preferred copy offered no images; the next
    accepted copy downloads. A work whose only copy yields nothing fails
    with that copy's reason."""
    mdz = FakeProvider(
        "mdz",
        records={"cuisinier": [COOK], "kochbuch": [KOCH]},
        page_counts={"bsb1": 0, "bsb2": 0},
    )
    ia = FakeProvider("internet_archive", records={"cuisinier": [COOK_IA]})
    judge = StubJudge(
        {"mdz:bsb1": 0.97, "internet_archive:ia1": 0.95, "mdz:bsb2": 0.97}
    )
    events = Recorder()

    result = core.run(
        download_plan(settings, tmp_path, out), events, wiring=wiring([mdz, ia], judge)
    )

    assert result.counts == core.WorkCounts(total=2, complete=1, failed=1)
    first = status(out, "E1")
    assert (first["status"], first["selected_provider"]) == (
        "complete",
        "internet_archive",
    )
    assert [item for item, _present in mdz.downloads] == ["bsb1", "bsb2"]
    assert [item for item, _present in ia.downloads] == ["ia1"]
    with Store(out) as store:
        tried = [(a.source_id, a.outcome) for a in store.attempts("E1")]
    assert tried == [("bsb1", "failed"), ("ia1", "complete")]
    assert any("trying internet_archive:ia1" in note for note in events.warnings)
    second = status(out, "E2")
    assert second["status"] == "failed"
    assert second["status_reason"] == "images: the item offers no page images"


def test_n1_a_work_another_run_finished_since_planning_is_skipped(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    """Item 4: of two concurrent downloads, one took works the other had
    completed since planning, made a new attempt and staged their metadata
    again. A plan made before another run finished its works now skips them
    at the lease without a write."""
    mdz = FakeProvider("mdz", records={"cuisinier": [COOK], "kochbuch": [KOCH]})
    ia = FakeProvider("internet_archive", records={"cuisinier": [COOK_IA]})
    judge = StubJudge({"mdz:bsb1": 0.97, "internet_archive:ia1": 0.5, "mdz:bsb2": 0.97})
    stale = download_plan(settings, tmp_path, out)
    other = core.run(
        download_plan(settings, tmp_path, out),
        Recorder(),
        wiring=wiring([mdz, ia], judge),
    )
    assert other.counts.complete == 2
    before = {entry_id: status(out, entry_id) for entry_id in ("E1", "E2")}
    searches, downloads, judged = len(mdz.searches), len(mdz.downloads), judge.calls[:]
    events = Recorder()

    late = core.run(stale, events, wiring=wiring([mdz, ia], judge))

    assert late.counts == core.WorkCounts(total=2, skipped=2)
    assert (len(mdz.searches), len(mdz.downloads), judge.calls) == (
        searches,
        downloads,
        judged,
    )
    for entry_id in ("E1", "E2"):
        assert status(out, entry_id) == before[entry_id]
    with Store(out) as store:
        assert [a.outcome for a in store.attempts("E1")] == ["complete"]
    assert sum("left it complete" in note for note in events.warnings) == 2


def test_n3_only_blocked_takes_a_killed_holders_unfinished_work(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    """Codex review: a work planned as ``blocked`` was re-checked at its
    lease against ``--only blocked`` with its stored status (partial), so a
    resume with ``--only blocked`` after a killed run skipped it."""
    mdz = FakeProvider("mdz", records={"cuisinier": [COOK]})
    mdz.failing = {"p00002"}
    judge = StubJudge({"mdz:bsb1": 0.97})
    csv = write_csv(tmp_path / "works.csv", [{"entry_id": "E1", "title": COOK.title}])

    def plan(**flags: Any) -> core.RunPlan:
        given = {"input": csv, "output": out, "providers": ("mdz",), **flags}
        resolution = resolve_download(given, settings.defaults)
        return core.plan(resolution.spec, settings, sources=resolution.sources)

    core.run(plan(), Recorder(), wiring=wiring([mdz], judge))
    assert status(out, "E1")["status"] == "partial"
    with child("../output/store_child.py", "serve", str(out), "killed") as holder:
        expect(holder, "READY")
        send(holder, "acquire E1")
        assert line(holder) == "LEASE 2"
        resume = plan(only=("blocked",))
        holder.kill()
        holder.wait(CHILD_TIMEOUT)
    assert [work.status for work in resume.works] == ["blocked"]
    mdz.failing = set()

    result = core.run(resume, Recorder(), wiring=wiring([mdz], judge))

    assert result.counts.complete == 1
    assert status(out, "E1")["status"] == "complete"


def test_d3_a_link_to_an_item_page_reads_the_items_manifest(settings: Settings) -> None:
    """Row 6689 linked the MDZ viewer page; the link now resolves through
    the adapter's hooks to the item's manifest, which names the record."""
    viewer = "https://www.digitale-sammlungen.de/view/bsb00027043"
    manifest = (
        "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb00027043/manifest"
    )
    session = RoutedSession({manifest: json_route(v2_manifest(label="Fischbuch"))})
    with Services.open(
        settings, wiring=wiring((), session_factory=lambda: session)
    ) as services:
        found = link_record(services, viewer)
        unresolved = link_record(services, "https://www.digitale-sammlungen.de/en/")

    assert isinstance(found, SearchRecord)
    assert (found.provider, found.source_id, found.title) == (
        "mdz",
        "bsb00027043",
        "Fischbuch",
    )
    assert (found.iiif_manifest, found.item_url) == (manifest, viewer)
    assert session.urls() == [manifest]
    assert isinstance(unresolved, str)
    assert "names no mdz item" in unresolved


class _BreakerStopped(FakeProvider):
    """Stops every download at an open breaker before any page."""

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        expected = (page_item(1), page_item(2))
        return DownloadResult(
            self.key,
            "bsb2",
            request.format,
            (Completeness("images", expected, (), available=2),),
            stopped="breaker_open",
        )


def test_d4_reasons_name_the_missing_items_and_the_open_breaker(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    """DDB's first fetch stopped on sbb_digital's breaker without naming it;
    an item without images said only "incomplete"."""
    nothing = DownloadResult(
        "ddb", "WSV64", "images", (Completeness("images", (), ()),)
    )
    assert describe_result(nothing) == "images: the item offers no page images"
    with Coordinator(
        settings.state_directory(), settings.coordination, Holder.current("other")
    ) as coordinator:
        coordinator.record_failure(
            "sbb_digital", threshold=1, cooldown_s=600.0, cause="transient"
        )
    mdz = _BreakerStopped("mdz", records={"kochbuch": [KOCH]})
    judge = StubJudge({"mdz:bsb2": 0.97})

    core.run(
        download_plan(settings, tmp_path, out),
        Recorder(),
        wiring=wiring([mdz, FakeProvider("internet_archive")], judge),
    )

    reason = status(out, "E2")["status_reason"]
    assert reason.startswith("images: 2 of 2 missing; stopped: breaker_open; ")
    assert "circuit breaker open for sbb_digital (transient) until 20" in reason


def test_f2_a_metadata_only_fetch_needs_review_and_exits_0(
    settings: Settings, out: Path
) -> None:
    """HathiTrust's unsupported download ended as failed with exit 1."""
    resolution = resolve_fetch(
        {
            "output": out,
            "entry_id": "H1",
            "title": "The compleat housewife",
            "provider": "hathitrust",
            "id": "ucbk.ark:/28722/h2rf5m094",
        },
        settings.defaults,
    )

    result = core.run(
        core.plan(resolution.spec, settings), Recorder(), wiring=wiring(())
    )

    assert result.counts == core.WorkCounts(total=1, needs_review=1)
    row = status(out, "H1")
    assert row["status"] == "needs_review"
    assert row["status_reason"] == (
        "metadata_only: hathitrust: download is not supported"
    )
