"""Regressions of the phase 6A review: one test per finding."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from chronodownloader import run as core
from chronodownloader._runtime import Services
from chronodownloader._worker import link_record
from chronodownloader.matching.judge import catalog_record, work_request
from chronodownloader.net import Network
from chronodownloader.output.store import (
    DATABASE_NAME,
    Store,
    read_work_candidates,
    read_work_status,
)
from chronodownloader.providers.base import (
    DownloadRequest,
    DownloadResult,
    SearchQuery,
    SearchRecord,
    manifest_url,
)
from chronodownloader.providers.europeana import EuropeanaProvider
from chronodownloader.settings import Settings
from chronodownloader.spec import resolve_download, resolve_fetch
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
EUROPEANA_MANIFEST = "https://iiif.europeana.eu/presentation/9200/abc/manifest"


def fakes() -> tuple[FakeProvider, FakeProvider]:
    mdz = FakeProvider("mdz", records={"cuisinier": [COOK], "kochbuch": [KOCH]})
    ia = FakeProvider("internet_archive", records={"cuisinier": [COOK_IA]})
    return mdz, ia


def csv_of(tmp_path: Path, **e1: str) -> Path:
    rows = [
        {"entry_id": "E1", "title": "Le cuisinier francois", "creator": "", **e1},
        {"entry_id": "E2", "title": "Ein new Kochbuch", "creator": ""},
    ]
    return write_csv(tmp_path / "works.csv", rows)


def plan(settings: Settings, csv: Path, out: Path, **flags: Any) -> core.RunPlan:
    given: dict[str, Any] = {"input": csv, "output": out, "providers": PROVIDERS}
    resolution = resolve_download({**given, **flags}, settings.defaults)
    return core.plan(resolution.spec, settings, sources=resolution.sources)


def run(
    settings: Settings,
    csv: Path,
    out: Path,
    adapters: tuple[FakeProvider, ...],
    judge: StubJudge,
    **flags: Any,
) -> core.RunResult:
    return core.run(
        plan(settings, csv, out, **flags), Recorder(), wiring=wiring(adapters, judge)
    )


def status(out: Path, entry_id: str = "E1") -> dict[str, Any]:
    (row,) = read_work_status(out, [entry_id])
    return row


def test_1_a_changed_input_row_is_searched_and_judged_anew(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = fakes()
    mdz.failing = {"p00002"}
    judge = StubJudge({"mdz:bsb1": 0.97, "internet_archive:ia1": 0.5})
    run(settings, csv_of(tmp_path), out, (mdz, ia), judge, entry_ids=("E1",))
    assert status(out)["status"] == "partial"

    changed = csv_of(tmp_path, creator="La Varenne")
    planned = plan(settings, changed, out, entry_ids=("E1",))
    assert planned.works[0].changed
    assert any("changed in the input" in note for note in planned.warnings)
    assert not plan(settings, csv_of(tmp_path), out).works[0].changed

    mdz.failing = set()
    core.run(planned, Recorder(), wiring=wiring((mdz, ia), judge))

    assert mdz.searches == ["Le cuisinier francois"] * 2
    assert judge.calls.count("mdz:bsb1") == 2
    current = [row["current"] for row in read_work_candidates(out, "E1")]
    assert sorted(current) == [0, 0, 1, 1]
    assert status(out)["creator"] == "La Varenne"


def test_2_another_accepted_copy_needs_review_unless_forced(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = fakes()
    csv = csv_of(tmp_path)
    first = StubJudge({"mdz:bsb1": 0.97, "internet_archive:ia1": 0.5})
    run(settings, csv, out, (mdz, ia), first, entry_ids=("E1",))
    folder = out / status(out)["folder"]
    old = sorted((folder / "objects").glob("*_mdz_*"))
    assert len(old) == 3

    second = StubJudge({"mdz:bsb1": 0.02, "internet_archive:ia1": 0.97})
    result = run(
        settings, csv, out, (mdz, ia), second, entry_ids=("E1",), research=True
    )

    assert result.counts.needs_review == 1
    row = status(out)
    assert row["status"] == "needs_review"
    assert "mdz:bsb1" in row["status_reason"]
    assert "fetch --force" in row["status_reason"]
    assert all(path.is_file() for path in old)
    assert ia.downloads == []

    forced = run(
        settings,
        csv,
        out,
        (mdz, ia),
        second,
        entry_ids=("E1",),
        research=True,
        force=True,
        only=("needs_review",),
    )

    assert forced.counts.complete == 1
    assert not any(path.exists() for path in old)
    assert len(list((folder / "objects").glob("*_ia_*"))) == 3
    row = status(out)
    assert (row["selected_provider"], row["file_count"]) == ("internet_archive", 3)


def test_3_credentials_are_masked_at_rest(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    link = "https://www.digitale-sammlungen.de/view/bsb1?token=SECRET123"
    csv = csv_of(tmp_path, link=link)
    run(settings, csv, out, fakes(), StubJudge(), entry_ids=("E1",))
    with Store(out) as store:
        store.begin_run("fetch", spec={"item.url": f"{EUROPEANA_MANIFEST}?wskey=K9"})

    with sqlite3.connect(out / DATABASE_NAME) as connection:
        links = connection.execute("SELECT link FROM works").fetchall()
        specs = connection.execute("SELECT spec FROM runs").fetchall()
    dump = json.dumps([links, specs])
    assert "SECRET123" not in dump
    assert "K9" not in dump
    assert "token=***" in dump
    assert status(out)["status"] == "needs_review"


class _Interrupted(FakeProvider):
    """Stages its first page, then Ctrl+C arrives."""

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "p00001.jpg").write_bytes(b"\xff\xd8\xff partial")
        raise KeyboardInterrupt


def test_4_an_interrupt_releases_leases_without_touching_staging(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    csv = csv_of(tmp_path)
    judge = StubJudge({"mdz:bsb1": 0.97})
    stuck = _Interrupted("mdz", records={"cuisinier": [COOK]})

    with pytest.raises(KeyboardInterrupt):
        run(settings, csv, out, (stuck,), judge, entry_ids=("E1",), providers=("mdz",))

    row = status(out)
    assert row["lease_holder"] is None
    staged = list((out / row["folder"]).glob("objects/.staging/*/p00001.jpg"))
    assert len(staged) == 1

    mdz = FakeProvider("mdz", records={"cuisinier": [COOK]})
    result = run(
        settings, csv, out, (mdz,), judge, entry_ids=("E1",), providers=("mdz",)
    )

    assert result.counts.complete == 1
    assert not list((out / row["folder"]).glob("objects/.staging/*"))


def test_5_a_worker_crash_fails_only_its_work(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = fakes()
    ia.records = {}
    mdz.search_error = None
    broken = FakeProvider("internet_archive", search_error=RuntimeError("boom"))
    judge = StubJudge({"mdz:bsb1": 0.97, "mdz:bsb2": 0.97})
    csv = write_csv(
        tmp_path / "two.csv",
        [
            {"entry_id": "E1", "title": "Le cuisinier francois"},
            {"entry_id": "E2", "title": "Ein new Kochbuch"},
        ],
    )
    first = core.run(
        plan(settings, csv, out, providers=("internet_archive",), entry_ids=("E1",)),
        Recorder(),
        wiring=wiring((broken,), judge),
    )
    assert first.counts.failed == 1
    assert "RuntimeError: boom" in status(out)["status_reason"]
    assert status(out)["lease_holder"] is None

    second = run(settings, csv, out, (mdz, ia), judge)
    assert second.counts.complete == 2

    with Services.open(settings, wiring=wiring(())) as services:
        reason = link_record(services, "http://[bad/iiif/x/manifest")
    assert isinstance(reason, str)
    assert reason.startswith("link:")


def test_6_research_reopens_complete_no_match_and_review(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = fakes()
    judge = StubJudge({"mdz:bsb1": 0.97, "mdz:bsb2": 0.5})
    csv = csv_of(tmp_path)
    run(settings, csv, out, (mdz, ia), judge)
    assert {status(out, e)["status"] for e in ("E1", "E2")} == {
        "complete",
        "needs_review",
    }

    assert plan(settings, csv, out).works == ()
    reopened = plan(settings, csv, out, research=True)

    assert [work.work.entry_id for work in reopened.works] == ["E1", "E2"]


def test_7_fetch_url_takes_manifests_and_item_pages_the_provider_resolves(
    settings: Settings, out: Path
) -> None:
    """Live round D3 re-opened ``fetch --url`` for item pages: the provider's
    ``item_id_from_url`` hook must resolve the URL, else exit 2."""
    flags = {"output": out, "entry_id": "N1", "title": "X"}
    page = resolve_fetch(
        {**flags, "url": "https://www.digitale-sammlungen.de/view/bsb1"}
    )
    manifest = resolve_fetch(
        {
            **flags,
            "url": "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb1/manifest",
        }
    )
    search_page = resolve_fetch(
        {**flags, "url": "https://www.digitale-sammlungen.de/en/search?query=koch"}
    )

    with pytest.raises(core.PlanError) as caught:
        core.plan(search_page.spec, settings)
    assert caught.value.exit_code == 2
    assert "--provider KEY and --id ID" in str(caught.value)
    target = core.plan(page.spec, settings).fetch
    assert target is not None
    assert (target.provider, target.item_id, target.manifest) == ("mdz", "bsb1", False)
    target = core.plan(manifest.spec, settings).fetch
    assert target is not None
    assert (target.provider, target.item_id, target.manifest) == ("mdz", "bsb1", True)


def test_8_europeana_manifests_carry_the_key_on_its_own_host(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    europeana = EuropeanaProvider("K9")
    assert manifest_url(europeana, EUROPEANA_MANIFEST).endswith("?wskey=K9")
    other = "https://iiif.example.org/europeana.eu/manifest"
    assert manifest_url(europeana, other) == other
    assert manifest_url(object(), EUROPEANA_MANIFEST) == EUROPEANA_MANIFEST

    monkeypatch.setenv("EUROPEANA_API_KEY", "K9")
    session = RoutedSession(
        {EUROPEANA_MANIFEST: json_route({"@id": EUROPEANA_MANIFEST, "label": "Koch"})}
    )
    linked = wiring((), session_factory=lambda: session)
    with Services.open(settings, wiring=linked) as services:
        found = link_record(services, EUROPEANA_MANIFEST)

    assert isinstance(found, SearchRecord)
    assert session.urls() == [f"{EUROPEANA_MANIFEST}?wskey=K9"]
    assert found.iiif_manifest == EUROPEANA_MANIFEST


def test_9_the_judge_sees_the_linked_manifest_metadata(settings: Settings) -> None:
    url = "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb1/manifest"
    document = {
        "@id": url,
        "label": "Le cuisinier françois",
        "metadata": [
            {"label": "Publisher", "value": "Pierre David"},
            {"label": "Place of publication", "value": "Paris"},
            {"label": "Language", "value": "fre"},
            {"label": "Edition", "value": "Seconde edition"},
        ],
        "attribution": "BSB",
    }
    session = RoutedSession({url: json_route(document)})
    with Services.open(
        settings, wiring=wiring((), session_factory=lambda: session)
    ) as services:
        found = link_record(services, url)
    assert isinstance(found, SearchRecord)
    assert list(found.raw)[0] == "label"

    seen = catalog_record(found)
    request = json.dumps(work_request("m", SearchQuery("Le cuisinier"), found))

    for key, value in (
        ("Publisher", "Pierre David"),
        ("Place of publication", "Paris"),
        ("Language", "fre"),
        ("Edition", "Seconde edition"),
    ):
        assert seen[key] == value
        assert value in request
