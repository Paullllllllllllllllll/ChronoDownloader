"""The Wellcome Collection adapter over the real network client and a local server.

Ported from v1.30.3 ``tests/unit/test_wellcome.py`` and the Wellcome case of
``tests/unit/test_provider_parsing.py``. The catalogue snippets are trimmed
but verbatim parts of a live response recorded on 01.08.2026: Wellcome now
serves ``iiif-presentation`` manifest URLs and no ``iiif-image`` locations,
and filtering on the old type discarded every hit. New: the 1,024 px image
cap, the page limit that replaced ``max_images``, and the recorded
responses.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from chronodownloader.coord import Coordinator, SlotToken
from chronodownloader.net import Network
from chronodownloader.options.settings_loader import SettingsError
from chronodownloader.providers.base import (
    DownloadRequest,
    SearchQuery,
    SearchRecord,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.providers.wellcome import (
    IMAGE_MAX_PX,
    Wellcome,
    extract_image_services,
    extract_manifest_urls,
)
from chronodownloader.settings import Settings
from tests.iiif.support import jpeg, json_reply, pdf, service_canvas, v2_manifest
from tests.net.support import ScriptedServer, settings_with

KEY = "wellcome"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / KEY

MANIFEST_URL = "https://iiif.wellcomecollection.org/presentation/v2/b21528391"
CAPPED = "/full/!1024,1024/0/default.jpg"
"""The first speculative candidate under Wellcome's cap."""

# Current shape: an iiif-presentation location, alongside physical holdings.
PRESENTATION_WORK: dict[str, Any] = {
    "id": "asd4849u",
    "title": "Cookery book ; and, General axioms for plain cookery",
    "thumbnail": {"url": "https://iiif.wellcomecollection.org/thumb/b21528391"},
    "items": [
        {
            "locations": [
                {"locationType": {"id": "closed-stores"}, "label": "Closed stores"}
            ]
        },
        {
            "locations": [
                {"locationType": {"id": "iiif-presentation"}, "url": MANIFEST_URL}
            ]
        },
    ],
}

# Legacy shape, kept working for any endpoint still serving it.
IMAGE_WORK: dict[str, Any] = {
    "id": "legacy01",
    "title": "A legacy image work",
    "items": [
        {
            "locations": [
                {
                    "locationType": {"id": "iiif-image"},
                    "url": "https://iiif.wellcomecollection.org/image/b111_0001.jp2/info.json",
                }
            ]
        }
    ],
}

# Physical-only holding: no digitised content of any kind.
PHYSICAL_ONLY_WORK: dict[str, Any] = {
    "id": "physical1",
    "title": "Only on the shelf",
    "items": [{"locations": [{"locationType": {"id": "open-shelves"}}]}],
}


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def network(coord: Coordinator) -> Iterator[Network]:
    with Network(settings_with(KEY), coord, sleep=lambda _seconds: None) as client:
        yield client


@pytest.fixture
def slot(coord: Coordinator) -> SlotToken:
    token = coord.acquire_slot(KEY, 1)
    assert token is not None
    return token


def _adapter(server: ScriptedServer) -> Wellcome:
    return Wellcome(works_url=server.url("/catalogue/v2/works"))


def _search(
    server: ScriptedServer, network: Network, payload: Any, *, limit: int = 3
) -> list[SearchRecord]:
    server.script("/catalogue/v2/works", json_reply(payload))
    return _adapter(server).search(SearchQuery("cookery"), network=network, limit=limit)


def _paths(server: ScriptedServer) -> list[str]:
    return [urlsplit(arrival.path).path for arrival in server.arrivals]


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _presentation_work(manifest_url: str) -> dict[str, Any]:
    work = json.loads(json.dumps(PRESENTATION_WORK))
    work["items"][1]["locations"][0]["url"] = manifest_url
    return work


def _serve_manifest(server: ScriptedServer, pages: int = 2) -> str:
    names = [f"page{index}" for index in range(1, pages + 1)]
    for name in names:
        server.script(f"/image/{name}{CAPPED}", jpeg())
    manifest = v2_manifest(*(service_canvas(server.url(f"/image/{n}")) for n in names))
    return server.script("/presentation/v2/b21528391", json_reply(manifest))


# Location extraction


def test_presentation_manifest_is_extracted() -> None:
    assert extract_manifest_urls(PRESENTATION_WORK) == [MANIFEST_URL]


def test_image_service_base_strips_info_json() -> None:
    assert extract_image_services(IMAGE_WORK) == [
        "https://iiif.wellcomecollection.org/image/b111_0001.jp2"
    ]


def test_presentation_work_has_no_image_services() -> None:
    """The regression itself: the old filter sees nothing here."""
    assert extract_image_services(PRESENTATION_WORK) == []


def test_physical_only_work_yields_neither() -> None:
    assert extract_manifest_urls(PHYSICAL_ONLY_WORK) == []
    assert extract_image_services(PHYSICAL_ONLY_WORK) == []


def test_duplicate_manifest_urls_are_collapsed() -> None:
    location = {"locationType": {"id": "iiif-presentation"}, "url": MANIFEST_URL}
    work = {"items": [{"locations": [location]}, {"locations": [location]}]}
    assert extract_manifest_urls(work) == [MANIFEST_URL]


# Search


def test_presentation_only_work_is_returned(
    server: ScriptedServer, network: Network
) -> None:
    results = _search(server, network, {"results": [PRESENTATION_WORK]})

    assert len(results) == 1
    assert results[0].source_id == "asd4849u"
    assert results[0].iiif_manifest == MANIFEST_URL


def test_legacy_image_work_still_carries_its_services(
    server: ScriptedServer, network: Network
) -> None:
    results = _search(server, network, {"results": [IMAGE_WORK]})

    assert len(results) == 1
    assert results[0].raw["image_services"]
    assert results[0].raw["iiif_manifest"] is None


def test_works_without_digitised_content_are_skipped(
    server: ScriptedServer, network: Network
) -> None:
    assert _search(server, network, {"results": [PHYSICAL_ONLY_WORK]}) == []


def test_max_results_is_respected(server: ScriptedServer, network: Network) -> None:
    payload = {"results": [PRESENTATION_WORK, IMAGE_WORK, PRESENTATION_WORK]}
    assert len(_search(server, network, payload, limit=2)) == 2


def test_non_dict_response_returns_no_results(
    server: ScriptedServer, network: Network
) -> None:
    """A body without a result list yields no results; a failed request now
    raises instead (v1.30.3 read both as None)."""
    assert _search(server, network, {"totalResults": 0}) == []


def test_contributors_and_production_are_requested(
    server: ScriptedServer, network: Network
) -> None:
    """Both ride along on the same request, so they cost nothing."""
    _search(server, network, {"results": []})

    include = parse_qs(urlsplit(server.arrivals[-1].path).query)["include"][0]
    assert "contributors" in include
    assert "production" in include
    assert "items" in include


def test_creator_and_date_reach_the_result(
    server: ScriptedServer, network: Network
) -> None:
    """creator was once hard-coded None and production never requested."""
    work = dict(PRESENTATION_WORK)
    work["contributors"] = [
        {"agent": {"label": "Briggs, Emily.", "type": "Person"}, "primary": True}
    ]
    work["production"] = [
        {
            "label": "London : School Board for London, [1890].",
            "dates": [{"label": "[1890]", "type": "Period"}],
        }
    ]
    result = _search(server, network, {"results": [work]})[0]

    assert list(result.creators) == ["Briggs, Emily."]
    assert result.date == "[1890]"
    assert result.year == 1890


def test_a_work_naming_nobody_stays_empty(
    server: ScriptedServer, network: Network
) -> None:
    work = dict(PRESENTATION_WORK)
    work["contributors"] = [{"agent": {}}, {"not": "a contributor"}]
    work["production"] = [{"label": "London", "dates": []}]
    result = _search(server, network, {"results": [work]})[0]

    assert list(result.creators) == []
    assert result.date is None


def test_search_skips_only_the_bad_work(
    server: ScriptedServer, network: Network
) -> None:
    payload = {
        "results": [
            "not-a-dict",
            {
                "id": "w1",
                "title": "A Booke of Cookerie",
                "items": [
                    {
                        "locations": [
                            {
                                "locationType": {"id": "iiif-image"},
                                "url": "https://iiif.wellcomecollection.org/"
                                "image/w1/info.json",
                            }
                        ]
                    }
                ],
            },
        ]
    }
    results = _search(server, network, payload)

    assert [r.source_id for r in results] == ["w1"]


# Download


def test_manifest_is_routed_to_the_shared_strategy(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """The manifest downloads through the shared IIIF strategy, page images
    capped at 1,024 px."""
    manifest_url = _serve_manifest(server)
    results = _search(server, network, {"results": [_presentation_work(manifest_url)]})

    result = _adapter(server).download(
        results[0],
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot, format="images"),
    )

    assert result.complete
    assert result.source_id == "asd4849u"
    assert _files(tmp_path / "staging") == [
        "metadata/manifest.json",
        "p00001.jpg",
        "p00002.jpg",
    ]
    assert f"/image/page1{CAPPED}" in _paths(server)


def test_refetch_recovers_a_manifest_for_a_bare_id(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    manifest_url = _serve_manifest(server, pages=1)
    server.script(
        "/catalogue/v2/works/asd4849u", json_reply(_presentation_work(manifest_url))
    )

    result = _adapter(server).download(
        "asd4849u",
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot, format="images"),
    )

    assert result.complete
    assert "/presentation/v2/b21528391" in _paths(server)


def test_no_digitised_content_fails_cleanly(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/catalogue/v2/works/physical1", json_reply(PHYSICAL_ONLY_WORK))

    result = _adapter(server).download(
        "physical1",
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot),
    )

    assert result.complete is False
    assert result.files == ()


def test_legacy_image_services_are_capped(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script(f"/image/b111{CAPPED}", jpeg())
    record = SearchRecord(
        KEY,
        "legacy01",
        "A legacy image work",
        raw={"image_services": [server.url("/image/b111")]},
    )

    result = _adapter(server).download(
        record,
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot),
    )

    assert result.complete
    assert _files(tmp_path / "staging") == ["metadata/work.json", "p00001.jpg"]


def test_pdf_takes_the_manifest_rendering(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    rendering = [{"@id": server.url("/pdf/b21528391"), "format": "application/pdf"}]
    manifest = v2_manifest(
        service_canvas(server.url("/image/page1")), rendering=rendering
    )
    manifest_url = server.script("/presentation/v2/b21528391", json_reply(manifest))
    server.script("/pdf/b21528391", pdf())
    record = SearchRecord(KEY, "asd4849u", "Cookery", iiif_manifest=manifest_url)

    result = _adapter(server).download(
        record,
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot),
    )

    assert result.complete
    assert [part.part for part in result.parts] == ["pdf"]
    assert "rendering.pdf" in _files(tmp_path / "staging")


@pytest.mark.parametrize(("max_pages", "expected"), [(1, 1), (2, 2), (0, 3)])
def test_the_page_limit_binds_the_pages(
    server: ScriptedServer,
    network: Network,
    slot: SlotToken,
    tmp_path: Path,
    max_pages: int,
    expected: int,
) -> None:
    """``max_pages`` replaced ``max_images``; 0 sets no limit."""
    manifest_url = _serve_manifest(server, pages=3)
    record = SearchRecord(KEY, "asd4849u", "Cookery", iiif_manifest=manifest_url)

    result = _adapter(server).download(
        record,
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot, format="images", max_pages=max_pages),
    )

    images = result.part("images")
    assert images is not None
    assert len(images.expected) == expected
    assert images.complete


def test_a_non_integer_page_limit_is_refused_at_load() -> None:
    """v1.30.3 coerced "50" and 50.0 at use; the typed settings refuse them."""
    with pytest.raises(SettingsError):
        Settings.from_mapping({"providers": {KEY: {"max_pages": "50"}}})
    settings = Settings.from_mapping({"providers": {KEY: {"max_pages": 50}}})
    assert settings.providers[KEY].max_pages == 50


# Pages and recorded responses


def test_pages_are_capped_at_1024(server: ScriptedServer, network: Network) -> None:
    manifest_url = _serve_manifest(server, pages=3)
    record = SearchRecord(KEY, "asd4849u", "Cookery", iiif_manifest=manifest_url)

    refs = _adapter(server).pages(record, network=network, first_n=2, max_px=4000)

    assert [ref.item for ref in refs] == ["p00001", "p00002"]
    assert refs[0].urls[0].endswith(CAPPED)
    assert _adapter(server).capabilities.image_max_px == IMAGE_MAX_PX


def test_recorded_search_response_parses(
    server: ScriptedServer, network: Network
) -> None:
    results = _search(server, network, _fixture("search.json"))

    assert [r.source_id for r in results] == ["k967axqt", "nk9erjtd"]
    first = results[0]
    assert first.iiif_manifest is not None
    assert first.iiif_manifest.startswith("https://iiif.wellcomecollection.org/")
    assert first.year == 1858


def test_recorded_manifest_offers_a_pdf_and_capped_pages(
    server: ScriptedServer, network: Network
) -> None:
    manifest_url = server.script(
        "/presentation/v2/b28136469", json_reply(_fixture("manifest_b28136469.json"))
    )
    record = SearchRecord(KEY, "k967axqt", "Il cuciniere", iiif_manifest=manifest_url)

    refs = _adapter(server).pages(record, network=network, first_n=5, max_px=2000)

    assert [ref.item for ref in refs] == ["p00001", "p00002", "p00003"]
    assert (
        refs[0].service
        == "https://iiif.wellcomecollection.org/image/b28136469_0001.jp2"
    )
    assert refs[0].urls[0].endswith(CAPPED)


def test_the_registry_builds_the_adapter() -> None:
    adapter = Registry(Settings()).get(KEY)

    assert isinstance(adapter, Wellcome)
    assert adapter.capabilities.iiif
