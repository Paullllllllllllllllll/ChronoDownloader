"""The SLUB adapter: LOD search, source record, PPN with check digit, manifest.

Ported from the v1.30.3 tests ``TestSlubPpnCheckDigit``
(tests/unit/test_provider_parsing.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chronodownloader.iiif import extract_canvases
from chronodownloader.net import Network
from chronodownloader.providers.base import (
    DownloadRequest,
    InvalidResponse,
    ItemNotFound,
    SearchQuery,
    SearchRecord,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.providers.slub import (
    MANIFEST_URL,
    SEARCH_URL,
    SOURCE_URL,
    Slub,
    extract_ppn,
)
from chronodownloader.settings import Settings
from tests.iiif.support import FULL, jpeg, json_reply
from tests.net.support import ScriptedServer

from .fakes import FileBudget
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
RECORD_ID = "1648519962"
PPN = "313701709"
SOURCE = local_path(SOURCE_URL.format(record_id=RECORD_ID))
MANIFEST = local_path(MANIFEST_URL.format(ppn=PPN))


@pytest.fixture
def network(server: ScriptedServer) -> Iterator[Network]:
    with routed_network(server) as client:
        yield client


def _serve_item(server: ScriptedServer, pages: int) -> dict[str, Any]:
    server.script(SOURCE, json_reply(fixture_json("slub", "source_1648519962.json")))
    manifest: dict[str, Any] = fixture_json("slub", "manifest_313701709.json")
    server.script(MANIFEST, json_reply(manifest))
    for canvas in extract_canvases(manifest)[:pages]:
        server.script(local_path(canvas.source.url + FULL), jpeg())
    return manifest


def _download(
    network: Network, tmp_path: Path, item: SearchRecord | str = RECORD_ID, **kw: Any
) -> Any:
    return Slub().download(
        item,
        network=network,
        staging_dir=tmp_path,
        request=DownloadRequest(slot_of("slub"), **kw),
    )


def test_the_registry_builds_the_adapter() -> None:
    provider = Registry(Settings()).get("slub")
    assert isinstance(provider, Slub)
    assert provider.key == "slub"


# PPN check digit: K10plus PPNs end in a modulo-11 check digit that may be "X".


def test_trailing_check_digit_survives() -> None:
    """``\\d+`` truncated the X for about one record in eleven, and the
    manifest built from the short PPN 404s."""
    assert extract_ppn("https://digital.slub-dresden.de/id33299526X") == "33299526X"
    assert extract_ppn("http://digital.slub-dresden.de/ppn33299526X") == "33299526X"


def test_numeric_ppns_are_unchanged() -> None:
    assert extract_ppn("https://digital.slub-dresden.de/id403708982") == "403708982"
    assert extract_ppn("https://example.org/no-ppn-here") is None
    assert extract_ppn(None) is None


def test_check_digit_reaches_the_manifest_url(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    """End to end: the source record's 856 link builds the manifest URL."""
    source_record = {
        "856": [
            {
                "__": [
                    {
                        "u": "https://digital.slub-dresden.de/id33299526X",
                        "x": "Digitalisat",
                    }
                ]
            }
        ]
    }
    server.script(
        local_path(SOURCE_URL.format(record_id="kxp-de14-1")), json_reply(source_record)
    )
    manifest_path = local_path(MANIFEST_URL.format(ppn="33299526X"))
    server.script(manifest_path, json_reply({"@id": "m"}))
    _download(network, tmp_path, "kxp-de14-1")
    assert manifest_path == "/iiif.slub-dresden.de/iiif/2/33299526X/manifest.json"
    assert requested_paths(server)[-1] == manifest_path


# Search


def test_the_search_sends_the_lod_parameters(
    server: ScriptedServer, network: Network
) -> None:
    server.script(SEARCH, json_reply([]))
    assert (
        Slub().search(
            SearchQuery('Koch "Buch"', creator="Rumpolt"), network=network, limit=4
        )
        == []
    )
    assert query_of(server, SEARCH) == {
        "q": [r"Koch \"Buch\" Rumpolt"],
        "size": ["4"],
        "format": ["json"],
        "filter": ["@type:http://schema.org/CreativeWork"],
    }


def test_an_answer_that_is_not_a_list_is_invalid(
    server: ScriptedServer, network: Network
) -> None:
    server.script(SEARCH, json_reply({"error": "index offline"}))
    with pytest.raises(InvalidResponse):
        Slub().search(SearchQuery("Kochbuch"), network=network, limit=3)


def test_the_recorded_search_parses(server: ScriptedServer, network: Network) -> None:
    """Live answer of 19.10.2026 for "Ein Koch- und Artzney-Buch"; the print
    record (accessMode "local") is left out."""
    server.script(
        SEARCH,
        body_reply(
            fixture_bytes("slub", "search_koch_und_artzney_buch.json"),
            "application/json",
        ),
    )
    results = Slub().search(
        SearchQuery("Ein Koch- und Artzney-Buch"), network=network, limit=3
    )
    assert results[0].source_id == RECORD_ID
    assert results[0].title == "Ein Koch- Und Artzney-Buch"
    assert results[0].creators == ()
    assert "1335597840" not in [r.source_id for r in results]


# Download


def test_the_recorded_item_downloads(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    """Live source record and the baseline manifest of 19.10.2026."""
    manifest = _serve_item(server, 2)
    result = _download(network, tmp_path, format="images", max_pages=2)
    images = result.part("images")
    assert result.complete
    assert result.source_id == RECORD_ID
    assert images is not None and images.available == len(extract_canvases(manifest))
    assert [f.name for f in result.files] == [
        "metadata/source.json",
        "metadata/manifest.json",
        "p00001.jpg",
        "p00002.jpg",
    ]
    assert requested_paths(server)[:2] == [SOURCE, MANIFEST]


def test_the_source_record_respects_the_metadata_budget(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    _serve_item(server, 1)
    budget = FileBudget({"metadata": 1})
    result = _download(network, tmp_path, format="images", max_pages=1, budget=budget)
    assert [f.name for f in result.files] == ["metadata/source.json", "p00001.jpg"]
    assert [kind for kind, _size in budget.charged] == ["metadata", "image"]


def test_a_records_own_manifest_needs_no_source(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    _serve_item(server, 1)
    record = SearchRecord(
        "slub",
        RECORD_ID,
        "Ein Koch- Und Artzney-Buch",
        iiif_manifest=MANIFEST_URL.format(ppn=PPN),
    )
    result = _download(network, tmp_path, record, format="images", max_pages=1)
    assert result.complete
    assert SOURCE not in requested_paths(server)


def test_a_source_without_a_ppn_is_not_found(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    server.script(SOURCE, json_reply({"001": RECORD_ID, "856": []}))
    with pytest.raises(ItemNotFound):
        _download(network, tmp_path)
    assert requested_paths(server) == [SOURCE]


def test_pages_lists_the_first_canvases(
    server: ScriptedServer, network: Network
) -> None:
    manifest = _serve_item(server, 0)
    refs = Slub().pages(RECORD_ID, network=network, first_n=2, max_px=500)
    assert [ref.service for ref in refs] == [
        c.source.url for c in extract_canvases(manifest)[:2]
    ]
