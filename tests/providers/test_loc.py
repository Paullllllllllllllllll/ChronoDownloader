"""The Library of Congress adapter: search parsing, item JSON, fallbacks.

Ported from v1.30.3 ``tests/integration/test_providers.py``
(``TestLocProvider``, ``test_loc_fallback_handles_list_image_url``,
``test_loc_uses_shared_loop_with_loc_key``),
``tests/unit/test_provider_parsing.py`` (``TestLocSearchParsing``,
``test_loc_string_item_and_resources_do_not_raise``) and
``tests/unit/test_loc_url.py``. The representative-image fallback is now a
preview that never completes the download (v1.30.3 returned True for it).
The fixtures are synthetic: loc.gov answered HTTP 403 on 19.10.2026.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chronodownloader.providers.base import (
    DownloadRequest,
    DownloadResult,
    ItemNotFound,
    ProviderBlocked,
    SearchQuery,
    SearchRecord,
)
from chronodownloader.providers.loc import KEY, LOC_API_BASE_URL, LocProvider
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import Settings
from tests.iiif.support import service_canvas, v2_manifest
from tests.net.support import FakeCoordinator

from .routed import (
    RoutedSession,
    fixture_route,
    jpeg_route,
    json_route,
    pdf_route,
    routed_network,
    slot_for,
    status_route,
)

BOOKS = f"{LOC_API_BASE_URL}books/"
SEARCH = f"{LOC_API_BASE_URL}search/"
ITEM_1 = "https://www.loc.gov/item/1/"
QUEEN = "https://www.loc.gov/item/65078367/"
QUEEN_MANIFEST = "https://www.loc.gov/item/65078367/manifest.json"
TILES = "https://tile.loc.gov/"


def _search(
    routes: dict[str, Any], title: str = "cookbook"
) -> tuple[list[SearchRecord], RoutedSession]:
    session = RoutedSession(routes)
    with routed_network(KEY, session) as network:
        records = LocProvider().search(SearchQuery(title), network=network, limit=3)
    return records, session


def _download(
    record: SearchRecord | str,
    session: RoutedSession,
    tmp_path: Path,
    *,
    coordinator: FakeCoordinator | None = None,
    **request: Any,
) -> DownloadResult:
    with routed_network(KEY, session, coordinator) as network:
        return LocProvider().download(
            record,
            network=network,
            staging_dir=tmp_path / "staging",
            request=DownloadRequest(slot_for(KEY), **request),
        )


def _record(item_id: str, item_url: str) -> SearchRecord:
    return SearchRecord(KEY, item_id, "Test", item_url=item_url)


# Search


def test_search_returns_results() -> None:
    response = {
        "results": [
            {
                "id": "http://www.loc.gov/item/12345/",
                "title": "American Cookbook",
                "contributor": ["Chef Smith"],
                "date": "1900",
            }
        ]
    }
    records, session = _search({BOOKS: json_route(response)})
    assert len(records) >= 1
    assert all(isinstance(record, SearchRecord) for record in records)
    assert records[0].source_id == "12345"
    assert session.params_of() == {"q": "cookbook", "fo": "json", "c": "3"}
    assert session.calls[0]["headers"]["Accept"] == "application/json"


def test_search_contributor_names_as_string() -> None:
    """A string contributor_names must not be indexed (its first character)."""
    response = {
        "results": [
            {
                "id": "http://www.loc.gov/item/12345/",
                "title": "American Cookbook",
                "contributor_names": "Chef Smith",
            }
        ]
    }
    records, _session = _search({BOOKS: json_route(response)})
    assert records[0].raw["creator"] == "Chef Smith"


def test_search_malformed_record_does_not_discard_others() -> None:
    response = {
        "results": [
            {"id": ["http://www.loc.gov/item/1/"], "title": "Bad"},
            {"id": "http://www.loc.gov/item/2/", "title": "Good"},
        ]
    }
    records, _session = _search({BOOKS: json_route(response)})
    assert len(records) == 1
    assert records[0].raw["id"] == "2"


def test_date_is_carried_into_the_result() -> None:
    response = {
        "results": [
            {
                "id": "https://www.loc.gov/item/12345/",
                "title": "The Cook's Oracle",
                "contributor_names": ["Kitchiner, William"],
                "url": "https://www.loc.gov/item/12345/",
                "date": "1822",
            }
        ]
    }
    records, _session = _search({BOOKS: json_route(response)}, "The Cook's Oracle")
    assert records[0].date == "1822"
    assert records[0].year == 1822


def test_dates_list_is_used_when_date_is_absent() -> None:
    response = {
        "results": [
            {
                "id": "https://www.loc.gov/item/6789/",
                "title": "Le cuisinier royal",
                "url": "https://www.loc.gov/item/6789/",
                "dates": ["1817-01-01T00:00:00Z"],
            }
        ]
    }
    records, _session = _search({BOOKS: json_route(response)})
    assert records[0].date == "1817-01-01T00:00:00Z"


def test_non_dict_content_does_not_raise() -> None:
    """LoC sometimes answers with "content" as a string; the general search
    is tried next."""
    records, session = _search(
        {LOC_API_BASE_URL: json_route({"content": "no results found"})}
    )
    assert records == []
    assert session.urls() == [BOOKS, SEARCH]


def test_a_refused_search_raises_instead_of_returning_nothing() -> None:
    """The 19.10.2026 baseline: loc.gov answered 403 to the search."""
    session = RoutedSession({BOOKS: status_route(403)})
    with (
        routed_network(KEY, session) as network,
        pytest.raises(ProviderBlocked) as excinfo,
    ):
        LocProvider().search(SearchQuery("x"), network=network, limit=3)
    assert excinfo.value.cause == "blocked"
    assert session.urls() == [BOOKS]


def test_recorded_search_response_parses() -> None:
    """Synthetic response (shapes of the v1.30.3 mocks, baseline LCCNs)."""
    records, _session = _search({BOOKS: fixture_route(KEY, "search.json")})
    assert [record.source_id for record in records] == ["65078367", "75320945"]
    queen, lady = records
    assert queen.iiif_manifest == QUEEN_MANIFEST
    assert queen.creators == ("Woolley, Hannah, b. 1623",)
    assert (queen.year, lady.year) == (1672, 1675)
    assert lady.item_url == "https://www.loc.gov/item/75320945/"
    assert lady.iiif_manifest is None


# Item JSON URL


def _fetched_url(item_url: str, tmp_path: Path) -> str:
    session = RoutedSession()
    with pytest.raises(ItemNotFound):
        _download(_record("abc123", item_url), session, tmp_path)
    return session.urls()[0]


def test_plain_item_url_gets_question_mark(tmp_path: Path) -> None:
    url = _fetched_url("https://www.loc.gov/item/abc123/", tmp_path)
    assert url == "https://www.loc.gov/item/abc123/?fo=json"


def test_item_url_with_query_string_gets_ampersand(tmp_path: Path) -> None:
    url = _fetched_url("https://www.loc.gov/item/abc123/?st=gallery", tmp_path)
    assert url == "https://www.loc.gov/item/abc123/?st=gallery&fo=json"


def test_item_url_already_json_unchanged(tmp_path: Path) -> None:
    url = _fetched_url("https://www.loc.gov/item/abc123/?fo=json", tmp_path)
    assert url == "https://www.loc.gov/item/abc123/?fo=json"


def test_a_bare_id_is_looked_up_at_its_item_page(tmp_path: Path) -> None:
    session = RoutedSession()
    with pytest.raises(ItemNotFound):
        _download("65078367", session, tmp_path)
    assert session.urls() == [f"{QUEEN}?fo=json"]


# Download


def test_rendering_survives_failed_image_fallback(tmp_path: Path) -> None:
    """A manifest-level PDF rendering completes the download although the
    manifest has no images and the sample image fails."""
    item_json = {
        "item": {
            "resources": [{"iiif_manifest": "https://example.org/manifest.json"}],
            "image_url": "https://example.org/sample.jpg",
        }
    }
    manifest = v2_manifest(
        rendering={"@id": "https://example.org/book.pdf", "format": "application/pdf"}
    )
    session = RoutedSession(
        {
            f"{ITEM_1}?fo=json": json_route(item_json),
            "https://example.org/manifest.json": json_route(manifest),
            "https://example.org/book.pdf": pdf_route(),
        }
    )
    result = _download(_record("1", ITEM_1), session, tmp_path)
    assert result.complete
    assert "https://example.org/sample.jpg" not in session.urls()


def test_fallback_handles_list_image_url(tmp_path: Path) -> None:
    """``image_url`` is a list by increasing resolution: the last is used.
    The image is a preview; the download is not complete."""
    item_json = {
        "item": {
            "image_url": [
                "//tile.loc.gov/image/small.jpg",
                "//tile.loc.gov/image/large.jpg",
            ]
        }
    }
    session = RoutedSession(
        {f"{ITEM_1}?fo=json": json_route(item_json), TILES: jpeg_route()}
    )
    result = _download(_record("1", ITEM_1), session, tmp_path)
    assert session.urls()[-1] == "https://tile.loc.gov/image/large.jpg"
    assert not result.complete
    assert result.files[-1].name == "metadata/preview.jpg"


def test_string_item_and_resources_do_not_raise(tmp_path: Path) -> None:
    envelope = {"item": "https://www.loc.gov/item/abc123/", "resources": "none"}
    session = RoutedSession({LOC_API_BASE_URL: json_route(envelope)})
    result = _download(
        _record("abc123", "https://www.loc.gov/item/abc123/"), session, tmp_path
    )
    assert not result.complete
    assert len(session.calls) == 1


def test_pages_go_through_the_shared_loop_under_the_loc_key(tmp_path: Path) -> None:
    item_json = {
        "item": {"resources": [{"iiif_manifest": "https://example.org/manifest.json"}]}
    }
    manifest = v2_manifest(
        service_canvas("https://svc.example/1"), service_canvas("https://svc.example/2")
    )
    coordinator = FakeCoordinator()
    session = RoutedSession(
        {
            f"{ITEM_1}?fo=json": json_route(item_json),
            "https://example.org/manifest.json": json_route(manifest),
            "https://svc.example/": jpeg_route(),
        }
    )
    result = _download(
        _record("LOC1", ITEM_1), session, tmp_path, coordinator=coordinator
    )
    assert result.complete
    assert result.source_id == "LOC1"
    assert {reservation.provider for reservation in coordinator.reservations} == {KEY}


def test_recorded_item_and_manifest_download(tmp_path: Path) -> None:
    """Synthetic item JSON and manifest: a bare id finds the manifest in the
    item's resources and stages both pages."""
    session = RoutedSession(
        {
            f"{QUEEN}?fo=json": fixture_route(KEY, "item.json"),
            QUEEN_MANIFEST: fixture_route(KEY, "manifest.json"),
            TILES: jpeg_route(),
        }
    )
    result = _download("65078367", session, tmp_path, format="images")
    assert result.complete
    assert [file.name for file in result.files] == [
        "metadata/item.json",
        "metadata/manifest.json",
        "p00001.jpg",
        "p00002.jpg",
    ]


def test_pages_of_a_candidate_use_its_manifest_without_the_item_json() -> None:
    record = SearchRecord(
        KEY,
        "65078367",
        "The queen-like closet",
        item_url=QUEEN,
        iiif_manifest=QUEEN_MANIFEST,
    )
    session = RoutedSession({QUEEN_MANIFEST: fixture_route(KEY, "manifest.json")})
    with routed_network(KEY, session) as network:
        refs = LocProvider().pages(record, network=network, first_n=1, max_px=500)
    assert session.urls() == [QUEEN_MANIFEST]
    assert [(ref.item, ref.label) for ref in refs] == [("p00001", "Title page")]


def test_the_registry_builds_the_adapter() -> None:
    provider = Registry(Settings()).get(KEY)
    assert isinstance(provider, LocProvider)
    assert provider.capabilities.api_key is None
    assert provider.capabilities.iiif
