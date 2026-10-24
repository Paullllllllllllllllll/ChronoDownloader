"""The DDB adapter: search parsing, authors, holding-library manifests.

Ported from v1.30.3 ``tests/integration/test_providers.py``
(``TestDdbProvider``, ``test_ddb_strips_quotes_from_title_and_creator``,
``test_ddb_uses_shared_loop_with_ddb_key``),
``tests/unit/test_provider_parsing.py`` (``TestDdbManifestPatterns``, the DDB
cases of ``TestPerRecordGuards``) and ``tests/unit/test_provider_metadata.py``
(``TestDdbCreatorFromView``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chronodownloader.providers.base import (
    DownloadRequest,
    DownloadResult,
    MissingApiKey,
    SearchQuery,
    SearchRecord,
)
from chronodownloader.providers.ddb import (
    API_BASE_URL,
    KEY,
    DdbProvider,
    creator_from_view,
    manifest_url_from_link,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import Settings
from tests.iiif.support import service_canvas, v2_manifest
from tests.net.support import FakeCoordinator

from .routed import (
    Route,
    RoutedSession,
    fixture_bytes,
    fixture_route,
    jpeg_route,
    json_route,
    routed_network,
    slot_for,
)

SEARCH_URL = f"{API_BASE_URL}search"
REGISTER = "EVZECNILXXNF2QLAQNBPEECKDKFYG5AZ"
KOCHBUCH = "O4JTHPWDL5LKMMXXO4R5236ZLTECHF5H"
MDZ_MANIFEST = (
    "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb10923113/manifest"
)


def _search(
    routes: dict[str, Any], query: SearchQuery | None = None
) -> tuple[list[SearchRecord], RoutedSession]:
    session = RoutedSession(routes)
    with routed_network(KEY, session) as network:
        records = DdbProvider("KEY").search(
            query or SearchQuery("cookbook"), network=network, limit=5
        )
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
        return DdbProvider("KEY").download(
            record,
            network=network,
            staging_dir=tmp_path / "staging",
            request=DownloadRequest(slot_for(KEY), **request),
        )


def _item_url(item: str) -> str:
    return f"{API_BASE_URL}items/{item}"


# Search


def test_search_handles_non_list_view() -> None:
    """A dict-valued or short "view" gives no creator, not an error."""
    response = {
        "results": [
            {
                "docs": [
                    {"id": "abc", "label": "Kochbuch", "view": {"unexpected": 1}},
                    {"id": "def", "label": "Backbuch", "view": ["a", "b"]},
                ]
            }
        ]
    }
    records, _session = _search({SEARCH_URL: json_route(response)})
    assert len(records) == 2
    assert records[0].raw["title"] == "Kochbuch"
    assert records[0].raw["creator"] is None
    assert records[1].raw["creator"] is None


def test_search_handles_list_label() -> None:
    response = {
        "results": [
            {
                "docs": [
                    {"id": "abc", "label": ["<match>Koch</match>buch"]},
                    {"id": "def", "label": [], "title": "Backbuch"},
                ]
            }
        ]
    }
    records, _session = _search({SEARCH_URL: json_route(response)})
    assert len(records) == 2
    assert records[0].raw["title"] == "Kochbuch"
    assert records[1].raw["title"] == "Backbuch"


def test_search_strips_quotes_from_title_and_creator() -> None:
    _records, session = _search(
        {SEARCH_URL: json_route({"results": []})},
        SearchQuery('Der "wahre" Koch', creator='Jean "le" Bon'),
    )
    query = session.params_of()["query"]
    assert '"Der  wahre  Koch"' in query
    assert 'creator:"Jean  le  Bon"' in query
    assert session.params_of()["rows"] == 5


def test_search_skips_only_the_bad_doc() -> None:
    response = {
        "results": [
            {
                "docs": [
                    "not-a-dict",
                    {
                        "id": "ABCDEF1234567890",
                        "label": "Neues <match>Kochbuch</match>",
                        "view": ["Braun, Emmy (Verfasser*in)"],
                    },
                ]
            }
        ]
    }
    records, _session = _search({SEARCH_URL: json_route(response)})
    assert [record.source_id for record in records] == ["ABCDEF1234567890"]
    assert records[0].title == "Neues Kochbuch"
    assert records[0].creators == ("Braun, Emmy",)


def test_search_tolerates_a_null_docs_list() -> None:
    response = {
        "results": [
            {"docs": None},
            {"docs": [{"id": "ABC", "label": "Kochbuch"}]},
            "not-a-dict",
        ]
    }
    records, _session = _search({SEARCH_URL: json_route(response)})
    assert [record.source_id for record in records] == ["ABC"]


def test_search_without_a_key_raises_missing_api_key_and_sends_nothing() -> None:
    session = RoutedSession()
    with (
        routed_network(KEY, session) as network,
        pytest.raises(MissingApiKey) as excinfo,
    ):
        DdbProvider(None).search(SearchQuery("x"), network=network, limit=3)
    assert (excinfo.value.provider, excinfo.value.env_var) == (KEY, "DDB_API_KEY")
    assert session.calls == []


def test_recorded_search_response_parses() -> None:
    """Live response of 19.10.2026 for baseline work 6776."""
    records, _session = _search({SEARCH_URL: fixture_route(KEY, "search.json")})
    assert [record.source_id for record in records] == [
        REGISTER,
        KOCHBUCH,
        "WSV64L3SR675P5VXUWMWHYFJTLROXVU7",
        "H4GPK4BE233LL2BSTWKBKVGAJFZ5KJIA",
    ]
    assert records[0].title == "Register Dieser Koch- und Kellermeisterey"
    assert all("<match>" not in record.title for record in records)
    assert all(record.creators == () for record in records)
    assert records[1].item_url == (
        f"https://www.deutsche-digitale-bibliothek.de/item/{KOCHBUCH}"
    )


# Authors


def test_role_marked_author_is_recovered() -> None:
    view = [
        "Braun, Emmy (Verfasser*in)",
        "SLUB Dresden (Besitzer*in)",
        "Monografie",
        "Achte verbesserte und vermehrte Auflage",
        "Grünstadt : J. Schäffer's Buchhandlung , [circa 1900]",
        "Deutsch",
        "XXIV, 400 Seiten",
    ]
    assert creator_from_view(view) == "Braun, Emmy"


def test_non_author_roles_are_not_creators() -> None:
    for entry in (
        "Kaven, Johann Heinrich (Drucker*in)",
        "SLUB Dresden (Besitzer*in)",
        "Strahowsky, Bartholomäus (Gravierer*in)",
    ):
        assert creator_from_view([entry]) is None, entry


def test_unlabeled_entries_yield_nothing() -> None:
    """Better no author than the publisher or the page count."""
    view = [
        "",
        "Neues Kochbuch",
        "Bauer, Josefine",
        "Veröffentlichung",
        "Stuttgart",
        "Fleischhauer & Spohn",
        "1951",
    ]
    assert creator_from_view(view) is None


def test_odd_view_shapes_are_tolerated() -> None:
    assert creator_from_view(None) is None
    assert creator_from_view({"not": "a list"}) is None
    assert creator_from_view([]) is None
    assert creator_from_view([None, 42, "Monografie"]) is None
    assert creator_from_view(["   (Verfasser*in)"]) is None


# Manifest URLs from holding-library links


def test_every_bsb_url_shape_yields_the_manifest() -> None:
    expected = (
        "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb10301321/manifest"
    )
    for url in (
        "https://www.digitale-sammlungen.de/view/bsb10301321",
        "https://www.digitale-sammlungen.de/en/view/bsb10301321",
        "https://www.digitale-sammlungen.de/de/view/bsb10301321?page=7",
        "https://mdz-nbn-resolving.de/urn:nbn:de:bvb:12-bsb10301321-4",
        "https://mdz-nbn-resolving.de/details:bsb10301321",
    ):
        assert manifest_url_from_link(url) == expected, url


def test_heidelberg_query_string_stays_out_of_the_manifest_url() -> None:
    assert manifest_url_from_link(
        "https://digi.ub.uni-heidelberg.de/diglit/rumohr1822?sid=abc123"
    ) == ("https://digi.ub.uni-heidelberg.de/diglit/iiif/rumohr1822/manifest.json")


def test_unknown_host_yields_nothing() -> None:
    assert manifest_url_from_link("https://example.org/item/1") is None
    assert manifest_url_from_link("") is None


# Download


def test_pages_go_through_the_shared_loop_under_the_ddb_key(tmp_path: Path) -> None:
    item_meta = {"iiifManifest": "https://example.org/manifest.json"}
    manifest = v2_manifest(
        service_canvas("https://svc.example/1"), service_canvas("https://svc.example/2")
    )
    coordinator = FakeCoordinator()
    session = RoutedSession(
        {
            _item_url("DDB1"): json_route(item_meta),
            "https://example.org/manifest.json": json_route(manifest),
            "https://svc.example/": jpeg_route(),
        }
    )
    result = _download("DDB1", session, tmp_path, coordinator=coordinator)
    assert result.complete
    assert result.source_id == "DDB1"
    assert session.params_of() == {"oauth_consumer_key": "KEY"}
    assert {reservation.provider for reservation in coordinator.reservations} == {KEY}


def _challenge() -> Route:
    return (
        200,
        fixture_bytes(KEY, "isshownby_challenge.html"),
        {"Content-Type": "text/html; charset=utf-8"},
    )


def test_a_record_without_a_manifest_downloads_nothing_of_another_record(
    tmp_path: Path,
) -> None:
    """Baseline 6776: the selected register record has no manifest and its
    SLUB ``isShownBy`` answers a bot challenge. The download stays with that
    record and reports the work missing; v1.30.3's pipeline went on to the
    next candidate's MDZ copy."""
    session = RoutedSession(
        {
            _item_url(REGISTER): fixture_route(KEY, f"item_{REGISTER}.json"),
            "https://digital.slub-dresden.de/": _challenge(),
        }
    )
    result = _download(REGISTER, session, tmp_path)
    assert session.urls() == [
        _item_url(REGISTER),
        "https://digital.slub-dresden.de/data/kitodo/kochunke_313665893/"
        "kochunke_313665893_tif/jpegs/00000130.tif.medium.jpg",
    ]
    assert result.source_id == REGISTER
    assert not result.complete
    assert [file.item for file in result.files] == ["item"]


def test_the_holding_librarys_manifest_is_built_for_the_same_record(
    tmp_path: Path,
) -> None:
    """Baseline 6776's second record links MDZ ``bsb10923113`` in
    ``isShownAt``; the MDZ manifest of that record is downloaded."""
    session = RoutedSession(
        {
            _item_url(KOCHBUCH): fixture_route(KEY, f"item_{KOCHBUCH}.json"),
            MDZ_MANIFEST: fixture_route(KEY, "manifest_bsb10923113.json"),
            "https://api.digitale-sammlungen.de/iiif/image/": jpeg_route(),
        }
    )
    result = _download(KOCHBUCH, session, tmp_path, format="images", max_pages=2)
    assert session.urls()[:2] == [_item_url(KOCHBUCH), MDZ_MANIFEST]
    images = result.part("images")
    assert images is not None
    assert images.present == ("p00001", "p00002")
    assert images.available == 200
    assert images.truncated
    assert result.complete
    assert [file.name for file in result.files] == [
        "metadata/item.json",
        "metadata/manifest.json",
        "p00001.jpg",
        "p00002.jpg",
    ]


def test_recorded_manifest_pages() -> None:
    session = RoutedSession(
        {
            _item_url(KOCHBUCH): fixture_route(KEY, f"item_{KOCHBUCH}.json"),
            MDZ_MANIFEST: fixture_route(KEY, "manifest_bsb10923113.json"),
        }
    )
    with routed_network(KEY, session) as network:
        refs = DdbProvider("KEY").pages(
            KOCHBUCH, network=network, first_n=2, max_px=500
        )
    assert [(ref.item, ref.label) for ref in refs] == [
        ("p00001", "(0001)"),
        ("p00002", "(0002)"),
    ]
    assert refs[0].service == (
        "https://api.digitale-sammlungen.de/iiif/image/v2/bsb10923113_00001"
    )


def test_the_registry_builds_the_adapter_with_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DDB_API_KEY", "FROM-ENV")
    provider = Registry(Settings()).get(KEY)
    assert isinstance(provider, DdbProvider)
    assert provider.capabilities.api_key == KEY
    session = RoutedSession({SEARCH_URL: json_route({"results": []})})
    with routed_network(KEY, session) as network:
        assert provider.search(SearchQuery("x"), network=network, limit=1) == []
    assert session.params_of()["oauth_consumer_key"] == "FROM-ENV"
