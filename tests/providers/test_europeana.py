"""The Europeana adapter: search parsing, manifest keys, downloads, fixtures.

Ported from v1.30.3 ``tests/integration/test_providers.py``
(``TestEuropeanaProvider``, the Europeana cases of ``TestQuotedQueryEscaping``
and ``TestPageImageLoopConsolidation``), ``tests/unit/test_provider_parsing.py``
(``test_europeana_title_is_never_built_into_a_manifest_url``) and
``tests/unit/test_provider_metadata.py``
(``test_europeana_reads_year_and_drops_the_provider_sentinel``). The record
fallback's previews no longer complete a download.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from chronodownloader.coord import Coordinator
from chronodownloader.net import Network
from chronodownloader.net.client import SLOT_POLL_S
from chronodownloader.providers.base import (
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    ItemNotFound,
    MissingApiKey,
    ProviderBlocked,
    SearchQuery,
    SearchRecord,
)
from chronodownloader.providers.europeana import (
    API_BASE_URL,
    KEY,
    EuropeanaProvider,
    append_wskey,
    record_media_urls,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import Settings
from tests.iiif.support import (
    direct_canvas,
    jpeg,
    json_reply,
    service_canvas,
    v2_manifest,
)
from tests.net.support import FakeCoordinator, ScriptedServer

from .routed import (
    RoutedSession,
    fixture_json,
    fixture_route,
    jpeg_route,
    json_route,
    routed_network,
    slot_for,
    staged_names,
    status_route,
)
from .routing import RoutingSession, fast_settings, local_path

MANIFEST_1X = "https://iiif.europeana.eu/presentation/1/x/manifest?format=3"
RECORD_1X = "https://api.europeana.eu/record/v2/1/x.json"
GALLICA_ID = "/794/ark__12148_bpt6k114423k"
GALLICA_MANIFEST = (
    "https://iiif.europeana.eu/presentation/794/ark__12148_bpt6k114423k/manifest"
)
GALLICA_RECORD = "https://api.europeana.eu/record/v2/794/ark__12148_bpt6k114423k.json"
THUMBNAIL = "https://api.europeana.eu/thumbnail/"
CONTRIBUTED_ID = "/9200365/item_GALLICA_PAGES"
"""A record without a Gallica ark whose Europeana manifest (the recorded one)
places its page on Gallica."""
CONTRIBUTED_RECORD = (
    "https://api.europeana.eu/record/v2/9200365/item_GALLICA_PAGES.json"
)
GALLICA_ARK_ID = "/794/ark__12148_bpt6k12345q"
GALLICA_OWN_MANIFEST = (
    "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k12345q/manifest.json"
)


def _search(
    routes: dict[str, Any], *, title: str = "cookbook", **kwargs: Any
) -> tuple[list[SearchRecord], RoutedSession]:
    session = RoutedSession(routes)
    with routed_network(KEY, session) as network:
        records = EuropeanaProvider("KEY").search(
            SearchQuery(title, **kwargs), network=network, limit=3
        )
    return records, session


def _download(
    provider: EuropeanaProvider,
    record: SearchRecord | str,
    session: RoutedSession,
    tmp_path: Path,
    *,
    coordinator: FakeCoordinator | None = None,
    breaker_threshold: int = 100,
    breaker_key: str = KEY,
    **request: Any,
) -> DownloadResult:
    with routed_network(
        breaker_key, session, coordinator, circuit_breaker_threshold=breaker_threshold
    ) as network:
        return provider.download(
            record,
            network=network,
            staging_dir=tmp_path / "staging",
            request=DownloadRequest(slot_for(KEY), **request),
        )


def _record(source_id: str, manifest: str) -> SearchRecord:
    return SearchRecord(KEY, source_id, "Kochbuch", iiif_manifest=manifest)


# Search


def test_search_handles_empty_title_list() -> None:
    """A present-but-empty ``title: []`` must not raise; a string dcCreator
    is used as-is."""
    response = {
        "success": True,
        "items": [
            {"id": "/1/x", "title": [], "dcCreator": "Solo Author"},
            {"id": "/1/y", "title": ["Good Title"]},
        ],
    }
    records, _session = _search({API_BASE_URL: json_route(response)})
    assert len(records) == 2
    # A titleless record carries an empty title, not a sentinel.
    assert records[0].raw["title"] == ""
    assert records[0].raw["creator"] == "Solo Author"
    assert records[1].raw["title"] == "Good Title"
    assert records[1].raw["creator"] is None


def test_search_result_manifest_url_carries_no_api_key() -> None:
    """The wskey must not be stored with the candidate."""
    response = {"success": True, "items": [{"id": "/1/x", "title": ["T"]}]}
    session = RoutedSession({API_BASE_URL: json_route(response)})
    with routed_network(KEY, session) as network:
        records = EuropeanaProvider("SECRET").search(
            SearchQuery("cookbook"), network=network, limit=3
        )
    manifest = records[0].raw["iiif_manifest"]
    assert manifest == MANIFEST_1X
    assert records[0].iiif_manifest == MANIFEST_1X
    assert "SECRET" not in manifest
    assert session.params_of()["wskey"] == "SECRET"


def test_search_strips_quotes_from_title_and_creator() -> None:
    _records, session = _search(
        {API_BASE_URL: json_route({"success": True, "items": []})},
        title='Der "wahre" Koch',
        creator='Jean "le" Bon',
    )
    query = session.params_of()["query"]
    assert 'title:"Der  wahre  Koch"' in query
    assert 'who:"Jean  le  Bon"' in query
    assert session.params_of()["rows"] == "9"


def test_search_reads_year_strips_the_key_and_drops_the_provider_sentinel() -> None:
    response = {
        "success": True,
        "items": [
            {
                "id": "/123/abc",
                "title": ["Kookboek"],
                "guid": "https://www.europeana.eu/item/123/abc?utm_source=api"
                "&utm_medium=api&utm_campaign=secret-key",
                "year": ["1783"],
            }
        ],
    }
    records, _session = _search({API_BASE_URL: json_route(response)})
    assert records[0].date == "1783"
    assert records[0].year == 1783
    assert records[0].item_url == "https://www.europeana.eu/item/123/abc"
    assert records[0].raw["item_url"] == "https://www.europeana.eu/item/123/abc"
    assert records[0].raw["provider"] is None


def test_search_without_a_key_raises_missing_api_key_and_sends_nothing() -> None:
    session = RoutedSession()
    with (
        routed_network(KEY, session) as network,
        pytest.raises(MissingApiKey) as excinfo,
    ):
        EuropeanaProvider(None).search(
            SearchQuery("cookbook"), network=network, limit=3
        )
    assert excinfo.value.provider == KEY
    assert excinfo.value.env_var == "EUROPEANA_API_KEY"
    assert "EUROPEANA_API_KEY" in str(excinfo.value)
    assert session.calls == []


def test_a_refused_search_raises_instead_of_returning_nothing() -> None:
    session = RoutedSession({API_BASE_URL: status_route(403)})
    with (
        routed_network(KEY, session) as network,
        pytest.raises(ProviderBlocked) as excinfo,
    ):
        EuropeanaProvider("KEY").search(
            SearchQuery("cookbook"), network=network, limit=3
        )
    assert excinfo.value.cause == "blocked"


def test_recorded_search_response_parses() -> None:
    """Live response of 19.10.2026 for the title clause of baseline work 6684
    (the baseline's title-and-creator query returned no records)."""
    response = fixture_json(KEY, "search.json")
    assert response["apikey"] == "REDACTED"
    records, _session = _search(
        {API_BASE_URL: fixture_route(KEY, "search.json")},
        title="Le cuisinier françois",
    )
    assert len(records) == 3
    first = records[0]
    assert first.source_id == "/794/ark__12148_bpt6k62887921"
    assert first.title.startswith("Le Cuisinier françois")
    assert first.creators == ("La Varenne (1618-1678). Auteur du texte",)
    assert (first.date, first.year) == ("1654", 1654)
    assert first.raw["provider"] == "National Library of France"
    assert first.iiif_manifest == (
        "https://iiif.europeana.eu/presentation/794/ark__12148_bpt6k62887921/"
        "manifest?format=3"
    )
    assert [record.source_id for record in records][1:] == [
        "/1493/item_67YSA3S4KB57L2WHIZLH2E4Z5A5I2VKB",
        "/358/item_67YSA3S4KB57L2WHIZLH2E4Z5A5I2VKB",
    ]


# Download


def test_download_appends_api_key_at_fetch_time(tmp_path: Path) -> None:
    """The key-less stored manifest URL is authenticated when fetched."""
    session = RoutedSession()
    with pytest.raises(ItemNotFound):
        _download(
            EuropeanaProvider("SECRET"), _record("/1/x", MANIFEST_1X), session, tmp_path
        )
    assert session.urls()[0] == f"{MANIFEST_1X}&wskey=SECRET"


@pytest.mark.parametrize(
    "url",
    [
        "https://iiif.europeana.eu.attacker.example/presentation/1/x/manifest",
        "https://iiif.europeana.eu@attacker.example/presentation/1/x/manifest",
        "https://user@iiif.europeana.eu/presentation/1/x/manifest",
        "https://iiif.europeana.eu:8443/presentation/1/x/manifest",
        "http://iiif.europeana.eu/presentation/1/x/manifest",
    ],
)
def test_the_key_goes_only_to_europeanas_exact_https_host(url: str) -> None:
    assert append_wskey(url, "SECRET") == url


def test_the_key_is_added_to_the_parsed_query() -> None:
    url = "https://iiif.europeana.eu/presentation/1/x/manifest?format=3#top"
    assert append_wskey(url, "S&T") == (
        "https://iiif.europeana.eu/presentation/1/x/manifest?format=3&wskey=S%26T#top"
    )


def test_download_leaves_foreign_manifest_urls_untouched(tmp_path: Path) -> None:
    """A manifest on a contributing library's host neither needs nor
    understands wskey."""
    foreign = "https://example.org/iiif/1/manifest"
    session = RoutedSession()
    with pytest.raises(ItemNotFound):
        _download(
            EuropeanaProvider("SECRET"), _record("/1/x", foreign), session, tmp_path
        )
    assert session.urls()[0] == foreign


def test_a_title_is_never_built_into_a_manifest_url(tmp_path: Path) -> None:
    """Without a record id there is no manifest URL and no request."""
    session = RoutedSession()
    record = SearchRecord(KEY, "", "Le Cuisinier / Royal")
    with pytest.raises(ItemNotFound):
        _download(EuropeanaProvider("KEY"), record, session, tmp_path)
    assert session.calls == []


def test_an_unparsable_manifest_is_reported_without_the_key(tmp_path: Path) -> None:
    session = RoutedSession({MANIFEST_1X: (200, b"<html>", {})})
    with pytest.raises(InvalidResponse) as excinfo:
        _download(
            EuropeanaProvider("SECRET"), _record("/1/x", MANIFEST_1X), session, tmp_path
        )
    assert "SECRET" not in str(excinfo.value)
    assert "wskey=" in excinfo.value.url


def test_pages_go_through_the_shared_loop_under_the_europeana_key(
    tmp_path: Path,
) -> None:
    manifest = v2_manifest(
        service_canvas("https://svc.example/1"), service_canvas("https://svc.example/2")
    )
    session = RoutedSession(
        {MANIFEST_1X: json_route(manifest), "https://svc.example/": jpeg_route()}
    )
    coordinator = FakeCoordinator()
    result = _download(
        EuropeanaProvider("KEY"),
        _record("EU1", MANIFEST_1X),
        session,
        tmp_path,
        coordinator=coordinator,
        format="images",
    )
    assert result.complete
    assert result.source_id == "EU1"
    assert [file.name for file in result.files] == [
        "metadata/manifest.json",
        "p00001.jpg",
        "p00002.jpg",
    ]
    assert {reservation.provider for reservation in coordinator.reservations} == {KEY}


def test_direct_image_urls_go_through_the_shared_loop(tmp_path: Path) -> None:
    """No Image API service: the whole-image URLs are downloaded instead."""
    manifest = v2_manifest(
        direct_canvas("https://img.example/1.jpg"),
        direct_canvas("https://img.example/2.jpg"),
    )
    session = RoutedSession(
        {MANIFEST_1X: json_route(manifest), "https://img.example/": jpeg_route()}
    )
    result = _download(
        EuropeanaProvider("KEY"),
        _record("EU1", MANIFEST_1X),
        session,
        tmp_path,
        format="images",
    )
    assert result.complete
    assert "https://img.example/2.jpg" in session.urls()


def _empty_manifest_routes(record: dict[str, Any]) -> dict[str, Any]:
    return {
        MANIFEST_1X: json_route({"sequences": []}),
        RECORD_1X: json_route(record),
    }


def test_record_api_fallback_reads_the_nested_media_links(tmp_path: Path) -> None:
    """Record API v2 nests the media links under "object"; the full media
    comes before the thumbnail. The preview does not complete the work."""
    record = {
        "success": True,
        "object": {
            "about": "/1/x",
            "aggregations": [
                {
                    "edmIsShownBy": "https://media.example.org/full.tif",
                    "edmIsShownAt": "https://example.org/page",
                }
            ],
            "europeanaAggregation": {
                "edmPreview": "https://api.europeana.eu/thumbnail/x"
            },
        },
    }
    routes = _empty_manifest_routes(record)
    routes["https://media.example.org/"] = jpeg_route()
    session = RoutedSession(routes)
    result = _download(
        EuropeanaProvider("KEY"), _record("/1/x", MANIFEST_1X), session, tmp_path
    )
    assert session.urls()[2] == "https://media.example.org/full.tif"
    assert session.params_of(1) == {"wskey": "KEY"}
    assert not result.complete
    assert [(file.name, file.kind) for file in result.files][-1] == (
        "metadata/preview.jpg",
        "metadata",
    )


def test_record_api_fallback_falls_through_to_the_preview(tmp_path: Path) -> None:
    """A dead edmIsShownBy still leaves the thumbnail as a candidate."""
    record = {
        "object": {
            "aggregations": [{"edmIsShownBy": "https://media.example.org/dead.tif"}],
            "europeanaAggregation": {
                "edmPreview": "https://api.europeana.eu/thumbnail/x"
            },
        }
    }
    routes = _empty_manifest_routes(record)
    routes[THUMBNAIL] = jpeg_route()
    session = RoutedSession(routes)
    result = _download(
        EuropeanaProvider("KEY"), _record("/1/x", MANIFEST_1X), session, tmp_path
    )
    assert session.urls()[2:] == [
        "https://media.example.org/dead.tif",
        "https://api.europeana.eu/thumbnail/x",
    ]
    assert not result.complete
    assert result.files[-1].item == "preview"


def _gallica_routes() -> dict[str, Any]:
    return {
        GALLICA_MANIFEST: fixture_route(KEY, "manifest.json"),
        CONTRIBUTED_RECORD: fixture_route(KEY, "record.json"),
        "http://gallica.bnf.fr/": status_route(403),
        "https://gallica.bnf.fr/": status_route(403),
        THUMBNAIL: jpeg_route(),
    }


CONTRIBUTED = SearchRecord(
    KEY, CONTRIBUTED_ID, "Kochbuch", iiif_manifest=GALLICA_MANIFEST
)


def test_a_gallica_record_downloads_through_gallicas_own_manifest(
    tmp_path: Path,
) -> None:
    """Europeana's manifest of a Gallica record lists one canvas on a dead
    Gallica path; the adapter reads Gallica's manifest instead, paced and
    sent under Gallica's key."""
    coordinator = FakeCoordinator()
    session = RoutedSession(
        {
            GALLICA_OWN_MANIFEST: fixture_route(
                "bnf_gallica", "manifest_bpt6k12345q.json"
            ),
            "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k12345q/": jpeg_route(),
        }
    )
    result = _download(
        EuropeanaProvider("KEY"),
        GALLICA_ARK_ID,
        session,
        tmp_path,
        coordinator=coordinator,
    )
    assert result.complete
    assert session.urls()[0] == GALLICA_OWN_MANIFEST
    assert not any("iiif.europeana.eu" in url for url in session.urls())
    assert {r.provider for r in coordinator.reservations} == {"bnf_gallica"}


def test_a_linked_gallica_record_resolves_to_gallicas_manifest() -> None:
    provider = EuropeanaProvider("KEY")
    assert provider.item_manifest_url(GALLICA_ARK_ID) == GALLICA_OWN_MANIFEST
    assert provider.item_manifest_url("/1/x") == MANIFEST_1X


def test_recorded_gallica_backed_manifest_leaves_the_work_missing(
    tmp_path: Path,
) -> None:
    """Baseline 6684 before the Gallica route: the Europeana manifest's page
    is served by Gallica, which refuses it (403). The refusal is counted as
    ``blocked`` on Gallica's breaker and the page is missing with cause
    ``blocked``; the record's thumbnail is only a preview."""
    coordinator = FakeCoordinator()
    session = RoutedSession(_gallica_routes())
    result = _download(
        EuropeanaProvider("KEY"),
        CONTRIBUTED,
        session,
        tmp_path,
        coordinator=coordinator,
    )
    images = result.part("images")
    assert images is not None
    assert images.expected == ("p00001",)
    assert images.missing == ("p00001",)
    assert dict(images.causes) == {"p00001": "blocked"}
    assert not result.complete
    assert result.stopped is None
    assert ("bnf_gallica", "blocked") in coordinator.failures
    assert (KEY, "blocked") not in coordinator.failures
    assert staged_names(tmp_path / "staging") == [
        "metadata/manifest.json",
        "metadata/preview.jpg",
    ]


def test_pages_on_gallica_are_paced_under_gallica_with_the_europeana_slot(
    tmp_path: Path,
) -> None:
    coordinator = FakeCoordinator()
    session = RoutedSession(_gallica_routes())
    _download(
        EuropeanaProvider("KEY"),
        CONTRIBUTED,
        session,
        tmp_path,
        coordinator=coordinator,
    )
    paced = [(r.provider, r.slot) for r in coordinator.reservations]
    gallica = [
        index
        for index, url in enumerate(session.urls())
        if urlsplit(url).hostname == "gallica.bnf.fr"
    ]
    assert gallica
    assert {paced[index][0] for index in gallica} == {"bnf_gallica"}
    assert paced[0][0] == KEY
    assert {slot for _key, slot in paced} == {slot_for(KEY)}


def test_gallica_refusals_open_gallicas_breaker_not_europeanas(
    tmp_path: Path,
) -> None:
    coordinator = FakeCoordinator()
    session = RoutedSession(_gallica_routes())
    result = _download(
        EuropeanaProvider("KEY"),
        CONTRIBUTED,
        session,
        tmp_path,
        coordinator=coordinator,
        breaker_threshold=1,
        breaker_key="bnf_gallica",
    )
    assert result.stopped == "breaker_open"
    assert not result.complete
    assert set(coordinator.breakers) == {"bnf_gallica"}
    sent = len(session.calls)
    again = _download(
        EuropeanaProvider("KEY"),
        CONTRIBUTED,
        session,
        tmp_path / "again",
        coordinator=coordinator,
        breaker_threshold=1,
        breaker_key="bnf_gallica",
    )
    assert again.stopped == "breaker_open"
    hosts = {urlsplit(url).hostname for url in session.urls()[sent:]}
    assert "gallica.bnf.fr" not in hosts


def test_gallica_pages_wait_for_gallicas_only_slot(
    server: ScriptedServer, coord: Coordinator, tmp_path: Path
) -> None:
    """A direct Gallica download holds Gallica's one slot; a Europeana
    download of Gallica pages sends nothing to Gallica until it is free."""
    page = "https://gallica.bnf.fr/ark:/12148/x/f1.jpg"
    server.script(
        "/iiif.europeana.eu/presentation/1/x/manifest",
        json_reply(v2_manifest(direct_canvas(page))),
    )
    server.script(local_path(page), jpeg())
    direct = coord.acquire_slot("bnf_gallica", 1)
    own = coord.acquire_slot(KEY, 1)
    assert direct is not None and own is not None
    results: list[DownloadResult] = []
    with Network(
        fast_settings(),
        coord,
        session_factory=lambda: RoutingSession(server),
        sleep=time.sleep,
    ) as network:
        worker = threading.Thread(
            target=lambda: results.append(
                EuropeanaProvider("KEY").download(
                    _record("/1/x", MANIFEST_1X),
                    network=network,
                    staging_dir=tmp_path / "staging",
                    request=DownloadRequest(
                        own, format="images", include_metadata=False
                    ),
                )
            )
        )
        worker.start()
        time.sleep(3 * SLOT_POLL_S)
        assert server.hits(local_path(page)) == 0
        assert coord.acquire_slot("bnf_gallica", 1) is None
        coord.release_slot(direct)
        worker.join(timeout=30)
    assert not worker.is_alive()
    assert results[0].complete
    assert server.hits(local_path(page)) == 1
    assert coord.acquire_slot("bnf_gallica", 1) is not None


def test_recorded_record_response_lists_its_media() -> None:
    record = fixture_json(KEY, "record.json")
    assert record["apikey"] == "REDACTED"
    urls = record_media_urls(record)
    assert urls[0] == (
        "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k114423k/f1/full/full/0/native.jpg"
    )
    assert urls[-1].startswith(THUMBNAIL)


def test_recorded_manifest_pages(tmp_path: Path) -> None:
    session = RoutedSession({GALLICA_MANIFEST: fixture_route(KEY, "manifest.json")})
    with routed_network(KEY, session) as network:
        refs = EuropeanaProvider(None).pages(
            CONTRIBUTED, network=network, first_n=3, max_px=500
        )
    assert [ref.item for ref in refs] == ["p00001"]
    assert refs[0].label == "p. 1"
    assert refs[0].service == (
        "http://gallica.bnf.fr/view3if/ga/ark:/12148/bpt6k114423k/f1"
    )
    assert session.urls() == [GALLICA_MANIFEST]


def test_the_registry_builds_the_adapter_with_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EUROPEANA_API_KEY", "FROM-ENV")
    provider = Registry(Settings()).get(KEY)
    assert isinstance(provider, EuropeanaProvider)
    assert provider.capabilities.api_key == KEY
    assert provider.capabilities.iiif
    session = RoutedSession({API_BASE_URL: json_route({"success": True})})
    with routed_network(KEY, session) as network:
        assert provider.search(SearchQuery("x"), network=network, limit=1) == []
    assert session.params_of()["wskey"] == "FROM-ENV"
