"""The e-rara adapter: SRU (MODS) search and IIIF downloads.

Ported from the v1.30.3 tests ``TestERaraProvider``
(tests/integration/test_providers.py).
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
from chronodownloader.providers.e_rara import (
    MANIFEST_URL,
    SRU_URL,
    ERara,
    build_query,
    parse_search,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import Settings
from tests.iiif.support import FULL, jpeg, json_reply
from tests.net.support import ScriptedServer

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

SRU = local_path(SRU_URL)
VLID = "29924315"
MANIFEST = local_path(MANIFEST_URL.format(vlid=VLID))


@pytest.fixture
def network(server: ScriptedServer) -> Iterator[Network]:
    with routed_network(server) as client:
        yield client


def _serve_manifest(server: ScriptedServer, path: str, pages: int) -> dict[str, Any]:
    manifest: dict[str, Any] = fixture_json("e_rara", "manifest_29924315.json")
    server.script(path, json_reply(manifest))
    for canvas in extract_canvases(manifest)[:pages]:
        server.script(local_path(canvas.source.url + FULL), jpeg())
    return manifest


def test_the_registry_builds_the_adapter() -> None:
    provider = Registry(Settings()).get("e_rara")
    assert isinstance(provider, ERara)
    assert provider.key == "e_rara"


def test_build_query_joins_terms_with_and() -> None:
    """Two quoted CQL terms separated by a bare space are invalid CQL."""
    assert build_query("Kochbuch", "Rumpolt") == '"Kochbuch" and "Rumpolt"'


def test_build_query_single_term_unchanged() -> None:
    assert build_query("Kochbuch", None) == '"Kochbuch"'


def test_an_empty_query_sends_nothing(server: ScriptedServer, network: Network) -> None:
    assert ERara().search(SearchQuery(""), network=network, limit=3) == []
    assert server.arrivals == []


def test_the_search_sends_the_sru_parameters(
    server: ScriptedServer, network: Network
) -> None:
    server.script(
        SRU,
        body_reply(
            b'<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/"/>',
            "text/xml",
        ),
    )
    ERara().search(SearchQuery("Kochbuch", creator="Rumpolt"), network=network, limit=2)
    assert query_of(server, SRU) == {
        "version": ["1.2"],
        "operation": ["searchRetrieve"],
        "query": ['"Kochbuch" and "Rumpolt"'],
        "maximumRecords": ["2"],
        "recordSchema": ["mods"],
    }


def test_an_answer_that_is_not_xml_is_invalid() -> None:
    with pytest.raises(InvalidResponse):
        parse_search(b"<html><body>Wartung")


def test_the_recorded_search_parses(server: ScriptedServer, network: Network) -> None:
    """Live answer of 19.10.2026 for "Berner Kochbuch"."""
    server.script(
        SRU,
        body_reply(fixture_bytes("e_rara", "search_berner_kochbuch.xml"), "text/xml"),
    )
    (record,) = ERara().search(SearchQuery("Berner Kochbuch"), network=network, limit=3)
    assert record.source_id == VLID
    assert record.title == "Berner Kochbuch"
    assert record.creators == ("Haller, Ludwig Albrecht",)
    assert record.date == "1803"
    assert record.year == 1803
    assert record.item_url == "https://www.e-rara.ch/bes_1/29924315"
    assert record.iiif_manifest == "https://www.e-rara.ch/i3f/v20/29924315/manifest"


def test_records_without_an_id_are_skipped() -> None:
    content = (
        b'<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">'
        b"<srw:records><srw:record><srw:recordData>"
        b'<mods xmlns="http://www.loc.gov/mods/v3"><titleInfo><title>T</title>'
        b"</titleInfo></mods></srw:recordData></srw:record></srw:records>"
        b"</srw:searchRetrieveResponse>"
    )
    assert parse_search(content) == []


def test_the_recorded_manifest_downloads(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    manifest = _serve_manifest(server, MANIFEST, 3)
    result = ERara().download(
        VLID,
        network=network,
        staging_dir=tmp_path,
        request=DownloadRequest(slot_of("e_rara"), format="images", max_pages=3),
    )
    images = result.part("images")
    assert images is not None
    assert result.complete
    assert images.present == ("p00001", "p00002", "p00003")
    assert images.available == len(extract_canvases(manifest))
    assert requested_paths(server)[0] == MANIFEST


def test_a_records_own_manifest_wins(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    own = "https://www.e-rara.ch/i3f/v21/29924315/manifest"
    _serve_manifest(server, local_path(own), 1)
    record = SearchRecord("e_rara", VLID, "Berner Kochbuch", iiif_manifest=own)
    result = ERara().download(
        record,
        network=network,
        staging_dir=tmp_path,
        request=DownloadRequest(slot_of("e_rara"), format="images", max_pages=1),
    )
    assert result.complete
    assert requested_paths(server)[0] == local_path(own)


def test_an_item_without_id_or_manifest_is_not_found(
    network: Network, tmp_path: Path
) -> None:
    with pytest.raises(ItemNotFound):
        ERara().download(
            "",
            network=network,
            staging_dir=tmp_path,
            request=DownloadRequest(slot_of("e_rara")),
        )


def test_pages_lists_the_first_canvases(
    server: ScriptedServer, network: Network
) -> None:
    manifest = _serve_manifest(server, MANIFEST, 0)
    refs = ERara().pages(VLID, network=network, first_n=2, max_px=400)
    assert [ref.item for ref in refs] == ["p00001", "p00002"]
    assert refs[0].service == extract_canvases(manifest)[0].source.url
