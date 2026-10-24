"""The DPLA adapter: search parsing, dates, manifest discovery, fallbacks.

Ported from v1.30.3 ``tests/integration/test_providers.py``
(``TestDplaProvider`` and ``test_dpla_uses_shared_loop_with_dpla_key``) and
``tests/unit/test_provider_metadata.py`` (``TestDplaDisplayDate``). A
record without a IIIF manifest is not downloadable through DPLA (v1.30.3
returned True for its thumbnail); a fallback file after an empty manifest
is a preview that never completes the download.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chronodownloader.providers.base import (
    DownloadRequest,
    DownloadResult,
    MissingApiKey,
    SearchQuery,
    SearchRecord,
    UnsupportedCapability,
)
from chronodownloader.providers.dpla import (
    API_BASE_URL,
    KEY,
    DplaProvider,
    display_date,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import Settings
from tests.iiif.support import service_canvas, v2_manifest
from tests.net.support import FakeCoordinator

from .routed import (
    RoutedSession,
    fixture_json,
    fixture_route,
    jpeg_route,
    json_route,
    routed_network,
    slot_for,
    staged_names,
)

SEARCH_URL = f"{API_BASE_URL}items"
ITEM_URL = f"{API_BASE_URL}items/DPLA1"
MANIFEST = "https://iiif.example.org/manifest.json"


def _search(
    routes: dict[str, Any], query: SearchQuery | None = None
) -> tuple[list[SearchRecord], RoutedSession]:
    session = RoutedSession(routes)
    with routed_network(KEY, session) as network:
        records = DplaProvider("KEY").search(
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
        return DplaProvider(None).download(
            record,
            network=network,
            staging_dir=tmp_path / "staging",
            request=DownloadRequest(slot_for(KEY), **request),
        )


# Search


def test_search_malformed_record_does_not_discard_others() -> None:
    """One malformed record must not discard the parsed results."""
    response = {
        "docs": [
            "not-a-record",
            {"id": None, "sourceResource": {"title": "No id"}},
            {"id": "good", "sourceResource": {"title": "Good"}},
        ]
    }
    records, _session = _search({SEARCH_URL: json_route(response)})
    assert len(records) == 1
    assert records[0].raw["id"] == "good"


def test_search_empty_creators_yields_none_not_empty_string() -> None:
    response = {
        "docs": [{"id": "x1", "sourceResource": {"title": "T1", "creator": []}}]
    }
    records, _session = _search({SEARCH_URL: json_route(response)})
    assert records[0].raw["creators"] == []
    assert records[0].creators == ()


def test_search_sends_title_creator_and_page_size() -> None:
    _records, session = _search(
        {SEARCH_URL: json_route({"docs": []})},
        SearchQuery("The Compleat Housewife", creator="E. Smith"),
    )
    assert session.params_of() == {
        "q": "The Compleat Housewife",
        "api_key": "KEY",
        "page_size": 5,
        "sourceResource.creator": "E. Smith",
    }


def test_search_without_a_key_raises_missing_api_key_and_sends_nothing() -> None:
    session = RoutedSession()
    with (
        routed_network(KEY, session) as network,
        pytest.raises(MissingApiKey) as excinfo,
    ):
        DplaProvider(None).search(SearchQuery("x"), network=network, limit=3)
    assert (excinfo.value.provider, excinfo.value.env_var) == (KEY, "DPLA_API_KEY")
    assert session.calls == []


def test_recorded_search_response_parses() -> None:
    """Live response of 19.10.2026 for baseline work 3240."""
    records, _session = _search({SEARCH_URL: fixture_route(KEY, "search.json")})
    assert [record.source_id for record in records] == [
        "4727cb2d032c186ddbae1caed16a6221",
        "d7f16d75efaef227fe7aa942ba675931",
        "895ca128ff39946aa357b040205c493a",
        "5c7a04f479ecfdae716e58c1dcb1e7e1",
        "508d5d6ee58650960c805dd259c5d4c8",
    ]
    by_id = {record.source_id: record for record in records}
    first = by_id["5c7a04f479ecfdae716e58c1dcb1e7e1"]
    assert (first.date, first.year) == ("1729", 1729)
    assert first.creators == ("Smith, E. (Eliza), -approximately 1732",)
    assert first.item_url == "http://catalog.hathitrust.org/Record/100158800"
    later = by_id["508d5d6ee58650960c805dd259c5d4c8"]
    assert later.year == 1730
    # The thumbnail in "object" is no manifest; v1.30.3 reads no other field.
    assert later.iiif_manifest is None
    assert later.raw["object"] == "https://archive.org/services/img/b30510168"


# Dates


def test_display_date_unwraps_the_list_and_the_object() -> None:
    assert display_date([{"displayDate": "between 1958-1961"}]) == "between 1958-1961"
    assert display_date({"displayDate": "1897"}) == "1897"


def test_display_date_falls_back_to_begin() -> None:
    assert display_date({"begin": "1897-01-01"}) == "1897-01-01"


def test_display_date_bare_string_and_empties() -> None:
    assert display_date("1789") == "1789"
    assert display_date([]) is None
    assert display_date(None) is None
    assert display_date({"displayDate": "   "}) is None


# Download


def test_download_reads_fields_from_the_docs_envelope(tmp_path: Path) -> None:
    """The item endpoint answers {"count": 1, "docs": [{...}]}; the fields
    are read off docs[0] and the whole envelope is staged as metadata."""
    item_details = {
        "count": 1,
        "docs": [
            {
                "id": "DPLA1",
                "object": MANIFEST,
                "isShownBy": "https://media.example.org/full.jpg",
            }
        ],
    }
    session = RoutedSession(
        {
            ITEM_URL: json_route(item_details),
            MANIFEST: json_route({"sequences": []}),
            "https://media.example.org/": jpeg_route(),
        }
    )
    result = _download("DPLA1", session, tmp_path)
    staged = tmp_path / "staging" / "metadata" / "item.json"
    assert json.loads(staged.read_text(encoding="utf-8")) == item_details
    # isShownBy came from docs[0], not the envelope.
    assert session.urls()[2] == "https://media.example.org/full.jpg"
    assert not result.complete
    assert result.files[-1].name == "metadata/preview.jpg"


def test_download_still_accepts_an_unwrapped_dict(tmp_path: Path) -> None:
    item_details = {
        "object": MANIFEST,
        "isShownBy": "https://media.example.org/full.jpg",
    }
    session = RoutedSession(
        {
            ITEM_URL: json_route(item_details),
            MANIFEST: json_route({"sequences": []}),
            "https://media.example.org/": jpeg_route(),
        }
    )
    result = _download("DPLA1", session, tmp_path)
    assert session.urls()[2] == "https://media.example.org/full.jpg"
    assert not result.complete


def test_pages_go_through_the_shared_loop_under_the_dpla_key(tmp_path: Path) -> None:
    item_details = {"count": 1, "docs": [{"object": MANIFEST}]}
    manifest = v2_manifest(
        service_canvas("https://svc.example/1"), service_canvas("https://svc.example/2")
    )
    coordinator = FakeCoordinator()
    session = RoutedSession(
        {
            ITEM_URL: json_route(item_details),
            MANIFEST: json_route(manifest),
            "https://svc.example/": jpeg_route(),
        }
    )
    result = _download("DPLA1", session, tmp_path, coordinator=coordinator)
    assert result.complete
    images = result.part("images")
    assert images is not None
    assert images.present == ("p00001", "p00002")
    assert {reservation.provider for reservation in coordinator.reservations} == {KEY}


def test_a_record_without_a_manifest_is_unsupported_and_names_its_library(
    tmp_path: Path,
) -> None:
    """Live round D2, baseline 3240: the 1730 record holds only the
    archive.org thumbnail in ``object`` (v1.30.3 recorded it as completed).
    DPLA cannot download it; the error names where the item is held."""
    item = "508d5d6ee58650960c805dd259c5d4c8"
    details = fixture_json(KEY, f"item_{item}.json")
    shown_at = details["docs"][0]["isShownAt"]
    session = RoutedSession(
        {f"{API_BASE_URL}items/{item}": fixture_route(KEY, f"item_{item}.json")}
    )
    with pytest.raises(UnsupportedCapability) as caught:
        _download(item, session, tmp_path)
    assert caught.value.capability == "download"
    assert "no IIIF manifest" in str(caught.value)
    assert shown_at in str(caught.value)
    assert session.urls() == [f"{API_BASE_URL}items/{item}"]
    assert staged_names(tmp_path / "staging") == []


def test_recorded_record_without_any_link_is_unsupported(tmp_path: Path) -> None:
    """Baseline 3240's selected 1729 record points at a HathiTrust catalog
    page: no manifest, no media, nothing of the work."""
    item = "5c7a04f479ecfdae716e58c1dcb1e7e1"
    details = fixture_json(KEY, f"item_{item}.json")
    assert details["docs"][0]["id"] == item
    session = RoutedSession(
        {f"{API_BASE_URL}items/{item}": fixture_route(KEY, f"item_{item}.json")}
    )
    with pytest.raises(UnsupportedCapability) as caught:
        _download(item, session, tmp_path)
    assert "catalog.hathitrust.org/Record/100158800" in str(caught.value)
    assert len(session.calls) == 1


def test_pages_without_a_manifest_are_none() -> None:
    session = RoutedSession({ITEM_URL: json_route({"docs": [{"id": "DPLA1"}]})})
    with routed_network(KEY, session) as network:
        refs = DplaProvider(None).pages("DPLA1", network=network, first_n=2, max_px=500)
    assert refs == []


def test_pages_come_from_the_discovered_manifest() -> None:
    manifest = v2_manifest(*(service_canvas(f"https://svc.example/{n}") for n in "123"))
    session = RoutedSession(
        {
            ITEM_URL: json_route({"docs": [{"hasView": [{"@id": MANIFEST}]}]}),
            MANIFEST: json_route(manifest),
        }
    )
    with routed_network(KEY, session) as network:
        refs = DplaProvider(None).pages("DPLA1", network=network, first_n=2, max_px=500)
    assert [ref.service for ref in refs] == [
        "https://svc.example/1",
        "https://svc.example/2",
    ]


def test_the_registry_builds_the_adapter_with_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DPLA_API_KEY", "FROM-ENV")
    provider = Registry(Settings()).get(KEY)
    assert isinstance(provider, DplaProvider)
    assert provider.capabilities.api_key == KEY
    session = RoutedSession({SEARCH_URL: json_route({"docs": []})})
    with routed_network(KEY, session) as network:
        assert provider.search(SearchQuery("x"), network=network, limit=1) == []
    assert session.params_of()["api_key"] == "FROM-ENV"
