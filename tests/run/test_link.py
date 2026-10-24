"""End to end: works CSV rows with a ``link`` skip the search."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chronodownloader import run as core
from chronodownloader.output.store import read_work_candidates, read_work_status
from chronodownloader.settings import Settings
from chronodownloader.spec import resolve_download
from tests.providers.routed import RoutedSession, jpeg_route, json_route
from tests.run.support import (
    PROVIDERS,
    FakeProvider,
    Recorder,
    StubJudge,
    wiring,
    write_csv,
)

HOST = "https://api.digitale-sammlungen.de"
MANIFEST = f"{HOST}/iiif/presentation/v2/bsb00012345/manifest"


def manifest(pages: int = 2) -> dict[str, Any]:
    canvases = [
        {
            "@id": f"{HOST}/canvas/{index}",
            "@type": "sc:Canvas",
            "images": [
                {
                    "resource": {
                        "@id": f"{HOST}/img/{index}.jpg",
                        "@type": "dctypes:Image",
                        "format": "image/jpeg",
                    }
                }
            ],
        }
        for index in range(1, pages + 1)
    ]
    return {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "@id": MANIFEST,
        "@type": "sc:Manifest",
        "label": "Le cuisinier françois",
        "metadata": [
            {"label": "Author", "value": "La Varenne"},
            {"label": "Date", "value": "1651"},
        ],
        "sequences": [{"canvases": canvases}],
    }


def run_link(
    tmp_path: Path, settings: Settings, out: Path, judge: StubJudge
) -> tuple[core.RunResult, FakeProvider, RoutedSession]:
    csv = write_csv(
        tmp_path / "works.csv",
        [{"entry_id": "L1", "title": "Le cuisinier francois", "link": MANIFEST}],
    )
    session = RoutedSession(
        {MANIFEST: json_route(manifest()), f"{HOST}/img/": jpeg_route()}
    )
    mdz = FakeProvider("mdz")
    resolution = resolve_download({"input": csv, "output": out, "providers": PROVIDERS})
    result = core.run(
        core.plan(resolution.spec, settings),
        Recorder(),
        wiring=wiring([mdz], judge, session_factory=lambda: session),
    )
    return result, mdz, session


def test_a_link_row_is_judged_and_downloaded_from_its_manifest(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    judge = StubJudge({"mdz:bsb00012345": 0.97})

    result, mdz, session = run_link(tmp_path, settings, out, judge)

    assert result.counts.complete == 1
    assert mdz.searches == []
    assert judge.calls == ["mdz:bsb00012345"]
    (row,) = read_work_status(out, ["L1"])
    assert (row["status"], row["pages_present"]) == ("complete", 2)
    (candidate,) = read_work_candidates(out, "L1")
    assert candidate["origin"] == "link"
    assert candidate["title"] == "Le cuisinier françois"
    assert candidate["year"] == 1651
    assert session.urls().count(MANIFEST) == 2


def test_a_link_the_judge_rejects_goes_to_review(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    judge = StubJudge({"mdz:bsb00012345": 0.03})

    result, mdz, session = run_link(tmp_path, settings, out, judge)

    assert result.counts.needs_review == 1
    (row,) = read_work_status(out, ["L1"])
    assert row["status"] == "needs_review"
    assert row["status_reason"].startswith("link:")
    assert row["file_count"] == 0
    assert session.urls() == [MANIFEST]
