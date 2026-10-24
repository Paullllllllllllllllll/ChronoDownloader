"""The MDZ adapter: JSON and HTML search, v2/v3 manifests, page sampling.

Ported from the v1.30.3 tests ``TestMdzProvider``
(tests/integration/test_providers.py), ``TestMdzHtmlFallback`` and
``TestQueryEchoSuppression`` (tests/unit/test_provider_parsing.py) and
``TestDatesReachTheSearchResult`` (tests/unit/test_provider_metadata.py).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chronodownloader.iiif import extract_canvases
from chronodownloader.net import Cause, Network, RequestFailed
from chronodownloader.providers.base import (
    DownloadRequest,
    ItemNotFound,
    SearchQuery,
)
from chronodownloader.providers.mdz import (
    HTML_SEARCH_URL,
    MANIFEST_V2_URL,
    MANIFEST_V3_URL,
    SEARCH_URL,
    Mdz,
    clean_text,
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
HTML_SEARCH = local_path(HTML_SEARCH_URL)
OBJECT_ID = "bsb00027043"
MANIFEST_V2 = local_path(MANIFEST_V2_URL.format(object_id=OBJECT_ID))
MANIFEST_V3 = local_path(MANIFEST_V3_URL.format(object_id=OBJECT_ID))


@pytest.fixture
def network(server: ScriptedServer) -> Iterator[Network]:
    with routed_network(server) as client:
        yield client


def _search(
    server: ScriptedServer,
    network: Network,
    api: Any,
    *,
    html: str | None = None,
    title: str = "Kochbuch",
    creator: str | None = None,
    limit: int = 5,
) -> list[Any]:
    server.script(SEARCH, json_reply(api))
    if html is not None:
        server.script(HTML_SEARCH, body_reply(html.encode(), "text/html"))
    return Mdz().search(
        SearchQuery(title, creator=creator), network=network, limit=limit
    )


def test_the_registry_builds_the_adapter() -> None:
    provider = Registry(Settings()).get("mdz")
    assert isinstance(provider, Mdz)
    assert provider.key == "mdz"
    assert provider.capabilities.iiif


def test_the_search_sends_the_api_parameters(
    server: ScriptedServer, network: Network
) -> None:
    _search(server, network, {"docs": [{"id": "bsb1"}]}, creator="Rumpolt", limit=3)
    assert query_of(server, SEARCH) == {
        "query": ["Kochbuch Rumpolt"],
        "handler": ["simple-metadata"],
        "pageSize": ["3"],
        "ocrContext": ["1"],
    }


def test_a_failed_search_raises_instead_of_returning_nothing(
    server: ScriptedServer, network: Network
) -> None:
    """v1.30.3 returned [] for a failed request (``make_request`` -> None)."""
    server.script(SEARCH, Reply(503, b"down"))
    with pytest.raises(RequestFailed) as excinfo:
        Mdz().search(SearchQuery("kochen"), network=network, limit=3)
    assert excinfo.value.cause is Cause.TRANSIENT


def test_search_handles_list_title(server: ScriptedServer, network: Network) -> None:
    """A list-valued (highlighted) title is coerced, not dropped."""
    response = {
        "docs": [{"id": "bsb123", "title": ["Kochbuch"], "iiifAvailable": True}]
    }
    results = _search(server, network, response)
    assert len(results) == 1
    assert results[0].title == "Kochbuch"


def test_items_without_iiif_or_id_are_skipped(
    server: ScriptedServer, network: Network
) -> None:
    response = {
        "docs": [
            {"id": "bsb1", "title": "A", "iiifAvailable": False},
            {"title": "B"},
            "not a doc",
            {"id": "bsb2", "title": "C"},
        ]
    }
    assert [r.source_id for r in _search(server, network, response)] == ["bsb2"]


def test_mdz_reads_publication_date(server: ScriptedServer, network: Network) -> None:
    response = {
        "docs": [
            {
                "id": "bsb10000001",
                "title": "Bayersches Kochbuch",
                "authors": ["Anon"],
                "publicationDate": "(1837)",
                "iiifAvailable": True,
            }
        ]
    }
    assert _search(server, network, response)[0].date == "(1837)"


def test_highlight_tags_are_stripped_from_creators_and_titles(
    server: ScriptedServer, network: Network
) -> None:
    """v1.30.3 stripped tags from titles only and decoded no entities."""
    response = {
        "docs": [
            {
                "id": "bsb10311854",
                "title": "La Cuisiniere Bourgeoise Suivi De L&apos;Office"
                " &amp; <em>Kochbuch</em>",
                "authors": ["<em>Mangolt</em>, <em>Gregor</em>, 1498-1584"],
            },
            {"id": "bsb2", "title": "B", "authors": "Samarow, <em>Gregor</em>"},
        ]
    }
    first, second = _search(server, network, response)
    assert first.title == "La Cuisiniere Bourgeoise Suivi De L'Office & Kochbuch"
    assert first.creators == ("Mangolt, Gregor, 1498-1584",)
    assert second.creators == ("Samarow, Gregor",)


def test_clean_text_strips_tags_before_decoding_entities() -> None:
    assert clean_text("a &lt;b&gt; <em>c</em>") == "a <b> c"


def test_unprefixed_view_href_is_matched(
    server: ScriptedServer, network: Network
) -> None:
    """The HTML fallback skipped every unprefixed /view/ link."""
    html = (
        "<html><body>"
        '<a href="/view/bsb10123456">Ein Koch-Buch</a>'
        '<a href="/en/view/bsb10999999">A Cookbook</a>'
        "</body></html>"
    )
    results = _search(server, network, {"docs": []}, html=html, title="Koch-Buch")
    ids = [r.source_id for r in results]
    assert "bsb10123456" in ids
    assert "bsb10999999" in ids
    assert query_of(server, HTML_SEARCH) == {"search": ["Koch-Buch"]}


def test_mdz_html_fallback_carries_no_creator(
    server: ScriptedServer, network: Network
) -> None:
    """A record never inherits the query's creator."""
    html = '<html><body><a href="/view/bsb10123456">Ein Koch-Buch</a></body></html>'
    results = _search(
        server, network, {"docs": []}, html=html, title="Koch-Buch", creator="Rumpolt"
    )
    assert results
    assert all(not r.creators for r in results)


def test_the_recorded_search_parses(server: ScriptedServer, network: Network) -> None:
    """Live answer of 19.10.2026 for "Fischbuch Gregor Mangolt"."""
    server.script(
        SEARCH,
        body_reply(fixture_bytes("mdz", "search_fischbuch.json"), "application/json"),
    )
    results = Mdz().search(
        SearchQuery("Fischbuch", creator="Gregor Mangolt"), network=network, limit=5
    )
    assert [r.source_id for r in results] == [
        "bsb11359226",
        "bsb00027043",
        "bsb11200266",
        "bsb11200270",
        "bsb11671421",
    ]
    known = results[1]
    assert known.title == "Fischbuch : von der Natur der Fische"
    assert known.creators == ("Mangolt, Gregor, 1498-1584",)
    assert known.date == "[1557]"
    assert known.year == 1557
    assert known.item_url == "https://www.digitale-sammlungen.de/view/bsb00027043"
    assert not any("<" in c or "&" in c for r in results for c in r.creators)
    assert "&amp;" not in results[2].title
    assert requested_paths(server) == [SEARCH]


def _serve_manifest(server: ScriptedServer, path: str, pages: int) -> dict[str, Any]:
    manifest: dict[str, Any] = fixture_json("mdz", "manifest_bsb00027043.json")
    server.script(path, json_reply(manifest))
    for canvas in extract_canvases(manifest)[:pages]:
        server.script(local_path(canvas.source.url + FULL), jpeg())
    return manifest


def test_the_recorded_manifest_downloads(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    manifest = _serve_manifest(server, MANIFEST_V2, 2)
    result = Mdz().download(
        OBJECT_ID,
        network=network,
        staging_dir=tmp_path,
        request=DownloadRequest(slot_of("mdz"), format="images", max_pages=2),
    )
    assert result.complete
    images = result.part("images")
    assert images is not None
    assert images.present == ("p00001", "p00002")
    assert images.available == len(extract_canvases(manifest)) == 80
    assert images.truncated
    assert sorted(f.name for f in result.files) == [
        "metadata/manifest.json",
        "p00001.jpg",
        "p00002.jpg",
    ]
    staged = json.loads((tmp_path / "metadata" / "manifest.json").read_text("utf-8"))
    assert staged["label"] == "Mangolt, Gregor: Fischbuch"
    assert server.hits(MANIFEST_V3) == 0


def test_the_v3_manifest_is_the_fallback(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    server.script(MANIFEST_V2, Reply(404, b"no"))
    _serve_manifest(server, MANIFEST_V3, 1)
    result = Mdz().download(
        OBJECT_ID,
        network=network,
        staging_dir=tmp_path,
        request=DownloadRequest(slot_of("mdz"), format="images", max_pages=1),
    )
    assert result.complete
    assert requested_paths(server)[:2] == [MANIFEST_V2, MANIFEST_V3]


def test_an_unknown_item_is_not_found(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    with pytest.raises(ItemNotFound):
        Mdz().download(
            OBJECT_ID,
            network=network,
            staging_dir=tmp_path,
            request=DownloadRequest(slot_of("mdz")),
        )
    assert requested_paths(server) == [MANIFEST_V2, MANIFEST_V3]


def test_pages_lists_the_first_canvases(
    server: ScriptedServer, network: Network
) -> None:
    manifest = _serve_manifest(server, MANIFEST_V2, 0)
    refs = Mdz().pages(OBJECT_ID, network=network, first_n=3, max_px=600)
    bases = [canvas.source.url for canvas in extract_canvases(manifest)[:3]]
    assert [ref.item for ref in refs] == ["p00001", "p00002", "p00003"]
    assert [ref.service for ref in refs] == bases
    assert refs[0].urls == (f"{bases[0]}/full/!600,600/0/default.jpg",)
