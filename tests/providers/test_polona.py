"""The Polona adapter: gateway search, search-service manifests, size fallback.

Polona serves an Angular single-page application whose Spring gateway at
``https://polona.pl/api`` answers searches (``/search-service/search/simple``)
and serves manifests (``/search-service/search/iiif/{objectId}/manifest.json``);
the old Cantaloupe route ``/iiif/item/{id}/manifest.json`` answers 404.

Ported from the v1.30.3 tests in tests/unit/test_polona.py and
``test_polona_uses_shared_loop_with_polona_key``
(tests/integration/test_providers.py). The search and manifest snippets
recorded on 01.08.2026 for those tests now live in
``tests/fixtures/providers/polona/``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chronodownloader.iiif import extract_canvases
from chronodownloader.net import Cause, Network, RequestFailed
from chronodownloader.providers.base import (
    DownloadRequest,
    InvalidResponse,
    ItemNotFound,
    ProviderBlocked,
    SearchQuery,
    SearchRecord,
)
from chronodownloader.providers.polona import (
    API_BASE_URL,
    MANIFEST_URL,
    SEARCH_URL,
    Polona,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import Settings
from tests.iiif.support import FULL, jpeg, json_reply
from tests.net.support import Reply, ScriptedServer

from .routing import (
    body_reply,
    fixture_bytes,
    fixture_json,
    local_path,
    query_of,
    requested_paths,
    routed_network,
    slot_of,
)

SEARCH = local_path(SEARCH_URL)
SEARCH_RESPONSE: dict[str, Any] = fixture_json(
    "polona", "search_kucharz_doskonaly.json"
)
SPARSE_RESPONSE: dict[str, Any] = fixture_json(
    "polona", "search_compendium_ferculorum.json"
)
EMPTY_RESPONSE: dict[str, Any] = {"totalElements": 0, "hits": []}
CZERNIECKI_ID = "b11c20f5-9ff1-4b16-995e-a8451a8b954c"
TOM_2_ID = "7ca593d6-2d0d-497f-98f0-a4b4f5723a28"
GELATINE_ID = "52c7d7fd-e857-4cd8-95dc-eea728738df5"


@pytest.fixture
def network(server: ScriptedServer) -> Iterator[Network]:
    with routed_network(server) as client:
        yield client


def _search(
    server: ScriptedServer,
    network: Network,
    payload: Any = SEARCH_RESPONSE,
    *,
    creator: str | None = None,
    limit: int = 3,
    title: str = "kucharz doskonały",
) -> list[SearchRecord]:
    server.script(SEARCH, json_reply(payload))
    return Polona().search(
        SearchQuery(title, creator=creator), network=network, limit=limit
    )


def _download(network: Network, tmp_path: Path, item: str, **request: Any) -> Any:
    return Polona().download(
        item,
        network=network,
        staging_dir=tmp_path,
        request=DownloadRequest(slot_of("polona"), **request),
    )


def test_the_registry_builds_the_adapter() -> None:
    provider = Registry(Settings()).get("polona")
    assert isinstance(provider, Polona)
    assert provider.key == "polona"


# Request construction: the gateway rejects an incomplete query with 400.


def test_targets_the_json_gateway_not_the_spa_shell(
    server: ScriptedServer, network: Network
) -> None:
    _search(server, network)
    assert requested_paths(server) == [SEARCH]
    assert SEARCH_URL.startswith(f"{API_BASE_URL}/search-service/")
    assert "/search/?query=" not in server.arrivals[0].path


def test_sends_every_mandatory_parameter(
    server: ScriptedServer, network: Network
) -> None:
    """query, page, pageSize and sort are all required; a missing sort turns
    the whole call into a 400 Bad Request."""
    _search(server, network, limit=3)
    params = query_of(server, SEARCH)
    assert set(params) == {"query", "page", "pageSize", "sort"}
    assert params["query"] == ["kucharz doskonały"]
    assert params["page"] == ["0"]
    assert params["pageSize"] == ["3"]
    assert params["sort"] == ["RELEVANCE"]


def test_creator_is_appended_to_the_query(
    server: ScriptedServer, network: Network
) -> None:
    _search(server, network, creator="Szytler")
    assert query_of(server, SEARCH)["query"] == ["kucharz doskonały Szytler"]


@pytest.mark.parametrize("limit", [0, -5])
def test_page_size_never_drops_below_one(
    server: ScriptedServer, network: Network, limit: int
) -> None:
    _search(server, network, limit=limit)
    assert query_of(server, SEARCH)["pageSize"] == ["1"]


# Search results


def test_source_id_is_the_object_uuid(server: ScriptedServer, network: Network) -> None:
    results = _search(server, network, limit=3)
    assert [r.source_id for r in results] == [
        "6e15cb64-c546-4b38-9f3f-2eccf761f57d",
        TOM_2_ID,
        "2a7cfd9e-273a-47bc-92ee-f7f563413f25",
    ]


def test_metadata_is_unwrapped_from_the_field_envelope(
    server: ScriptedServer, network: Network
) -> None:
    results = _search(server, network, limit=3)
    assert results[1].title == "Kucharz doskonały. T. 2"
    assert results[1].creators == ("Szytler, Jan (1763-1850)",)
    assert results[1].date == "1834"


def test_item_url_uses_the_live_preview_route(
    server: ScriptedServer, network: Network
) -> None:
    """The SPA routes records to /preview/<id>; /item/<id> is retired."""
    results = _search(server, network, limit=3)
    for result in results:
        assert result.item_url == f"https://polona.pl/preview/{result.source_id}"
    assert not any("/item/" in (r.item_url or "") for r in results)


def test_thumbnail_is_absolutized_against_the_gateway(
    server: ScriptedServer, network: Network
) -> None:
    """The record has no thumbnail field; the URL is kept in ``raw``."""
    results = _search(server, network, limit=2)
    assert results[0].raw["thumbnail"] == (
        f"{API_BASE_URL}/download/digital-content/5b045498-17df-4035-bd56-6ec0181cb5da"
    )


def test_missing_thumbnail_is_tolerated(
    server: ScriptedServer, network: Network
) -> None:
    results = _search(server, network, SPARSE_RESPONSE, limit=2)
    assert "thumbnail" not in results[0].raw
    assert results[1].raw["thumbnail"] is not None


def test_missing_creator_and_date_are_tolerated(
    server: ScriptedServer, network: Network
) -> None:
    results = _search(server, network, SPARSE_RESPONSE, limit=2)
    assert results[0].creators == ()
    assert results[0].date is None
    assert results[1].date == "1755"


def test_max_results_is_respected(server: ScriptedServer, network: Network) -> None:
    assert len(_search(server, network, limit=2)) == 2


def test_hits_without_an_object_id_are_dropped(
    server: ScriptedServer, network: Network
) -> None:
    payload = {"hits": [{"basicFields": {}}, SEARCH_RESPONSE["hits"][1]]}
    results = _search(server, network, payload, limit=5)
    assert [r.source_id for r in results] == [TOM_2_ID]


def test_empty_result_set_returns_no_results(
    server: ScriptedServer, network: Network
) -> None:
    assert _search(server, network, EMPTY_RESPONSE, title="nonexistent work") == []


def test_html_shell_response_is_invalid(
    server: ScriptedServer, network: Network
) -> None:
    """v1.30.3 returned [] for the SPA shell; it is not a search answer."""
    server.script(SEARCH, body_reply(b"<html>...</html>", "text/html"))
    with pytest.raises(InvalidResponse):
        Polona().search(SearchQuery("kucharz doskonały"), network=network, limit=3)


def test_failed_request_raises(server: ScriptedServer, network: Network) -> None:
    """v1.30.3 returned [] when the request failed."""
    server.script(SEARCH, Reply(503, b"down"))
    with pytest.raises(RequestFailed) as excinfo:
        Polona().search(SearchQuery("kucharz doskonały"), network=network, limit=3)
    assert excinfo.value.cause is Cause.TRANSIENT


def test_the_recorded_search_parses(server: ScriptedServer, network: Network) -> None:
    """Live answer of 19.10.2026 for "Essai sur la Gélatine des Os"."""
    server.script(
        SEARCH,
        body_reply(
            fixture_bytes("polona", "search_essai_gelatine.json"), "application/json"
        ),
    )
    results = Polona().search(
        SearchQuery("Essai sur la Gélatine des Os"), network=network, limit=3
    )
    assert len(results) == 3
    known = results[0]
    assert known.source_id == GELATINE_ID
    assert known.title == "Essai sur la gelatine des os"
    assert known.creators == ("Cadet de Vaux, Antoine-Alexis (1743-1828)",)
    assert known.date == "1803"
    assert known.year == 1803


# Downloads address the search-service manifest, not Cantaloupe.


def test_manifest_url_is_the_search_service_route() -> None:
    assert MANIFEST_URL.format(item_id=CZERNIECKI_ID) == (
        f"https://polona.pl/api/search-service/search/iiif/{CZERNIECKI_ID}/manifest.json"
    )


def test_fetches_the_search_service_manifest(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    manifest = fixture_json("polona", f"manifest_{TOM_2_ID}.json")
    manifest_path = local_path(MANIFEST_URL.format(item_id=TOM_2_ID))
    server.script(manifest_path, json_reply(manifest))
    service = "/polona.pl/iiif/3/ce686a7b-e0c2-4ee0-85b0-903a96eeb6dc"
    server.script(f"{service}{FULL}", jpeg())
    result = _download(network, tmp_path, TOM_2_ID, format="images")
    paths = requested_paths(server)
    assert paths[0] == manifest_path
    assert paths[0].startswith("/polona.pl/api/search-service/search/iiif/")
    assert not any("/iiif/item/" in path for path in paths)
    assert paths[1] == f"{service}{FULL}"
    assert result.complete


def test_polona_uses_the_shared_page_loop_with_its_key(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    manifest = fixture_json("polona", f"manifest_{TOM_2_ID}.json")
    server.script(
        local_path(MANIFEST_URL.format(item_id=TOM_2_ID)), json_reply(manifest)
    )
    server.script(
        f"/polona.pl/iiif/3/ce686a7b-e0c2-4ee0-85b0-903a96eeb6dc{FULL}", jpeg()
    )
    result = _download(network, tmp_path, TOM_2_ID, format="images")
    assert result.provider == "polona"
    assert [f.name for f in result.files] == ["metadata/manifest.json", "p00001.jpg"]


def test_missing_identifier_fails_cleanly(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    with pytest.raises(ItemNotFound):
        _download(network, tmp_path, "")
    assert server.arrivals == []


def test_unreachable_manifest_fails_cleanly(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    server.script(local_path(MANIFEST_URL.format(item_id=CZERNIECKI_ID)), Reply(503))
    with pytest.raises(ProviderBlocked) as excinfo:
        _download(network, tmp_path, CZERNIECKI_ID)
    assert excinfo.value.cause == "transient"


def test_pages_fall_back_from_full_to_max_after_a_400(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    """Every page answered 400 at full/full in the 19.10.2026 baseline."""
    manifest = fixture_json("polona", f"manifest_{GELATINE_ID}.json")
    server.script(
        local_path(MANIFEST_URL.format(item_id=GELATINE_ID)), json_reply(manifest)
    )
    bases = [local_path(c.source.url) for c in extract_canvases(manifest)[:2]]
    for base in bases:
        server.script(f"{base}/full/full/0/default.jpg", Reply(400, b"bad size"))
        server.script(f"{base}/full/max/0/default.jpg", jpeg())
    result = _download(network, tmp_path, GELATINE_ID, format="images", max_pages=2)
    images = result.part("images")
    assert result.complete
    assert images is not None and images.available == 44
    assert sorted(p.name for p in tmp_path.glob("p*.jpg")) == [
        "p00001.jpg",
        "p00002.jpg",
    ]
    assert {
        f.source_url.rsplit("/", 4)[-3] for f in result.files if f.kind == "image"
    } == {"max"}


def test_pages_lists_the_first_canvases(
    server: ScriptedServer, network: Network
) -> None:
    manifest = fixture_json("polona", f"manifest_{GELATINE_ID}.json")
    server.script(
        local_path(MANIFEST_URL.format(item_id=GELATINE_ID)), json_reply(manifest)
    )
    refs = Polona().pages(GELATINE_ID, network=network, first_n=2, max_px=500)
    assert [ref.service for ref in refs] == [
        c.source.url for c in extract_canvases(manifest)[:2]
    ]
