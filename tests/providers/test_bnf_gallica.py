"""The Gallica adapter: SRU search, IIIF downloads, 403 reported as blocked.

Ported from the v1.30.3 tests ``TestGallicaProvider``, the Gallica cases of
``TestDownloadFunctions`` and ``TestPageImageLoopConsolidation``
(tests/integration/test_providers.py) and ``TestGallicaSearchParsing``
(tests/unit/test_provider_parsing.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chronodownloader.net import Cause, Network, RequestFailed
from chronodownloader.providers.base import (
    DownloadRequest,
    InvalidResponse,
    ProviderBlocked,
    SearchQuery,
    SearchRecord,
)
from chronodownloader.providers.bnf_gallica import (
    MANIFEST_URL,
    SRU_URL,
    BnfGallica,
    build_query,
    parse_search,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import Settings
from tests.iiif.support import FULL, jpeg, json_reply, pdf, v2_manifest
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

SRU = local_path(SRU_URL)
ARK = "bpt6k123"
MANIFEST = local_path(MANIFEST_URL.format(ark_id=ARK))
EMPTY_SRU = (
    b'<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">'
    b"<srw:numberOfRecords>0</srw:numberOfRecords>"
    b"</srw:searchRetrieveResponse>"
)


@pytest.fixture
def network(server: ScriptedServer) -> Iterator[Network]:
    with routed_network(server) as client:
        yield client


def _download(
    network: Network, tmp_path: Path, format: Any = "pdf", ark: str = ARK
) -> Any:
    return BnfGallica().download(
        ark,
        network=network,
        staging_dir=tmp_path,
        request=DownloadRequest(slot_of("bnf_gallica"), format=format),
    )


def test_the_registry_builds_the_adapter() -> None:
    provider = Registry(Settings()).get("bnf_gallica")
    assert isinstance(provider, BnfGallica)
    assert provider.key == "bnf_gallica"


# Search


def test_the_search_sends_the_sru_query(
    server: ScriptedServer, network: Network
) -> None:
    server.script(SRU, body_reply(EMPTY_SRU, "text/xml"))
    BnfGallica().search(
        SearchQuery('Le "cuisinier"', creator="Viard"), network=network, limit=3
    )
    assert query_of(server, SRU) == {
        "version": ["1.2"],
        "operation": ["searchRetrieve"],
        "query": [r'dc.title all "Le \"cuisinier\"" and dc.creator all "Viard"'],
        "maximumRecords": ["3"],
        "recordSchema": ["oai_dc"],
    }


def test_build_query_without_a_creator() -> None:
    assert build_query("cuisine", None) == 'dc.title all "cuisine"'


def test_search_empty_response(server: ScriptedServer, network: Network) -> None:
    server.script(SRU, body_reply(EMPTY_SRU, "text/xml"))
    assert (
        BnfGallica().search(SearchQuery("nonexistent"), network=network, limit=3) == []
    )


def test_a_403_search_is_blocked_not_empty(
    server: ScriptedServer, network: Network
) -> None:
    """v1.30.3 logged Gallica's 403 as "no valid XML" and returned []."""
    challenge = fixture_bytes("bnf_gallica", "sru_403_challenge.html")
    server.script(SRU, body_reply(challenge, "text/html; charset=UTF-8", status=403))
    with pytest.raises(RequestFailed) as excinfo:
        BnfGallica().search(SearchQuery("cuisine"), network=network, limit=3)
    assert excinfo.value.cause is Cause.BLOCKED
    assert excinfo.value.status == 403
    assert requested_paths(server) == [SRU]


def test_an_answer_that_is_not_xml_is_invalid() -> None:
    with pytest.raises(InvalidResponse):
        parse_search(b"<html><body>maintenance")


def _sru_response(identifier: str, creators: list[str]) -> bytes:
    creator_xml = "".join(f"<dc:creator>{c}</dc:creator>" for c in creators)
    return (
        '<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">'
        "<srw:records><srw:record><srw:recordData>"
        '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Le cuisinier royal</dc:title>"
        f"{creator_xml}"
        "<dc:date>1817</dc:date>"
        f"<dc:identifier>{identifier}</dc:identifier>"
        "</oai_dc:dc>"
        "</srw:recordData></srw:record></srw:records>"
        "</srw:searchRetrieveResponse>"
    ).encode()


def test_ark_capture_stops_at_qualifier_and_query() -> None:
    """Dot-qualifiers, query strings and prose stay out of the ark."""
    identifier = (
        "Disponible sur https://gallica.bnf.fr/ark:/12148/bpt6k12345q"
        ".texteBrut?rk=21459;2 (consulté le 01.08.2026)"
    )
    results = parse_search(_sru_response(identifier, ["Viard, Alexandre"]))
    assert [r.source_id for r in results] == ["bpt6k12345q"]


def test_every_creator_is_carried() -> None:
    """Multi-author records keep all dc:creator entries for scoring."""
    results = parse_search(
        _sru_response(
            "https://gallica.bnf.fr/ark:/12148/bpt6k12345q",
            ["Viard, Alexandre", "Fouret, Léon"],
        )
    )
    assert results[0].creators == ("Viard, Alexandre", "Fouret, Léon")


def test_item_url_is_the_landing_page() -> None:
    """The result carries the landing URL for the CSV link column."""
    results = parse_search(
        _sru_response("https://gallica.bnf.fr/ark:/12148/bpt6k12345q", ["Viard"])
    )
    assert results[0].item_url == "https://gallica.bnf.fr/ark:/12148/bpt6k12345q"


def test_the_recorded_search_parses(server: ScriptedServer, network: Network) -> None:
    """Synthetic SRU answer (Gallica answered 403 when recording)."""
    server.script(
        SRU,
        body_reply(fixture_bytes("bnf_gallica", "sru_cuisinier_royal.xml"), "text/xml"),
    )
    results = BnfGallica().search(
        SearchQuery("cuisinier royal"), network=network, limit=3
    )
    assert [r.source_id for r in results] == ["bpt6k12345q", "bpt6k67890r"]
    first, second = results
    assert first.title == "Le cuisinier royal"
    assert first.creators == ("Viard, Alexandre", "Fouret, Léon")
    assert first.date == "1817"
    assert second.creators == ("Viard, Alexandre",)
    assert all(r.provider == "bnf_gallica" for r in results)


# Download


def test_a_403_manifest_is_blocked(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    server.script(MANIFEST, Reply(403, b"Forbidden"))
    with pytest.raises(ProviderBlocked) as excinfo:
        _download(network, tmp_path)
    assert excinfo.value.cause == "blocked"
    with pytest.raises(ProviderBlocked):
        BnfGallica().pages(ARK, network=network, first_n=1, max_px=500)


def test_gallica_download_no_content_is_not_complete(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    """BUG-1: a manifest with no renderings and no images is not a success."""
    server.script(MANIFEST, json_reply({"@id": "m"}))
    result = _download(network, tmp_path)
    assert not result.complete


def test_gallica_download_renderings_only_is_complete(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    """A downloaded rendering with no image services still counts as success."""
    rendering = {
        "@id": "https://gallica.bnf.fr/bpt6k123.pdf",
        "format": "application/pdf",
    }
    server.script(MANIFEST, json_reply(v2_manifest(rendering=rendering)))
    server.script("/gallica.bnf.fr/bpt6k123.pdf", pdf())
    result = _download(network, tmp_path)
    assert result.complete
    assert "rendering.pdf" in {f.name for f in result.files}


def test_gallica_rendering_survives_failed_image_downloads(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    """A staged rendering is kept when every page image fails afterwards.

    v1.30.3 reported this as success; the work is now partial, with the
    rendering part complete and the images part missing both pages.
    """
    rendering = {
        "@id": "https://gallica.bnf.fr/bpt6k123.pdf",
        "format": "application/pdf",
    }
    canvases = [
        {"images": [{"resource": {"service": {"@id": "https://svc.example/1"}}}]},
        {"images": [{"resource": {"service": {"@id": "https://svc.example/2"}}}]},
    ]
    server.script(MANIFEST, json_reply(v2_manifest(*canvases, rendering=rendering)))
    server.script("/gallica.bnf.fr/bpt6k123.pdf", pdf())
    result = _download(network, tmp_path, format="both")
    rendering_part = result.part("pdf")
    images = result.part("images")
    assert rendering_part is not None and rendering_part.complete
    assert images is not None and images.missing == ("p00001", "p00002")
    assert (tmp_path / "rendering.pdf").is_file()
    assert not result.complete


def test_gallica_uses_the_shared_page_loop_with_its_key(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    """Page images are staged by canvas position under the Gallica key."""
    manifest = fixture_json("bnf_gallica", "manifest_bpt6k12345q.json")
    server.script(
        local_path(MANIFEST_URL.format(ark_id="bpt6k12345q")), json_reply(manifest)
    )
    for page in ("f1", "f2"):
        server.script(
            f"/gallica.bnf.fr/iiif/ark:/12148/bpt6k12345q/{page}{FULL}", jpeg()
        )
    record = SearchRecord("bnf_gallica", "bpt6k12345q", "Le cuisinier royal")
    result = BnfGallica().download(
        record,
        network=network,
        staging_dir=tmp_path,
        request=DownloadRequest(slot_of("bnf_gallica"), format="images"),
    )
    assert result.provider == "bnf_gallica"
    assert result.source_id == "bpt6k12345q"
    assert result.complete
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == [
        "p00001.jpg",
        "p00002.jpg",
    ]
    assert requested_paths(server)[0].endswith("/bpt6k12345q/manifest.json")


def test_pages_lists_the_first_canvases(
    server: ScriptedServer, network: Network
) -> None:
    manifest = fixture_json("bnf_gallica", "manifest_bpt6k12345q.json")
    server.script(
        local_path(MANIFEST_URL.format(ark_id="bpt6k12345q")), json_reply(manifest)
    )
    refs = BnfGallica().pages("bpt6k12345q", network=network, first_n=5, max_px=800)
    assert [(ref.item, ref.label) for ref in refs] == [
        ("p00001", "NP"),
        ("p00002", "1"),
    ]
    assert refs[0].service == "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k12345q/f1"
