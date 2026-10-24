"""The British Library adapter: SRU search, BNB SPARQL fallback, IIIF downloads.

Ported from v1.30.3 ``tests/integration/test_providers.py``
(``TestBritishLibraryProvider``). The patched ``make_request`` is replaced by
a local server (or a scripted session for DNS and timeout failures) behind
the real network client.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from chronodownloader.coord import Coordinator, SlotToken
from chronodownloader.net import Network
from chronodownloader.providers.base import (
    DownloadRequest,
    ItemNotFound,
    ProviderBlocked,
    SearchQuery,
)
from chronodownloader.providers.british_library import (
    BritishLibrary,
    build,
    build_bnb_query,
    parse_bnb_bindings,
    parse_sru_records,
)
from chronodownloader.settings import Settings
from tests.iiif.support import json_reply, serve_pages, service_canvas, v2_manifest
from tests.net.support import (
    Reply,
    ScriptedServer,
    ScriptedSession,
    response,
    settings_with,
)

KEY = "british_library"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers" / KEY

DNS_FAILURE = requests.exceptions.ConnectionError(
    "HTTPSConnectionPool(host='sru.bl.uk', port=443): Max retries exceeded "
    "(Caused by NameResolutionError: Failed to resolve 'sru.bl.uk')"
)
TIMEOUT = requests.exceptions.ConnectTimeout("connect timed out")


@pytest.fixture
def net(coord: Coordinator) -> Iterator[Network]:
    with Network(
        settings_with(KEY, max_attempts=1), coord, sleep=lambda _s: None
    ) as client:
        yield client


@pytest.fixture
def bl_slot(coord: Coordinator) -> SlotToken:
    token = coord.acquire_slot(KEY, 1)
    assert token is not None
    return token


def _sru_envelope(records: str, count: int = 1) -> str:
    """Wrap record elements in the SRU 1.2 searchRetrieveResponse envelope."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">'
        "<srw:version>1.2</srw:version>"
        f"<srw:numberOfRecords>{count}</srw:numberOfRecords>"
        f"<srw:records>{records}</srw:records>"
        "</srw:searchRetrieveResponse>"
    )


def _srw_dc_record(fields: str, position: int = 1) -> str:
    """One SRU record carrying the srw_dc container for recordSchema=dc."""
    return (
        "<srw:record>"
        "<srw:recordSchema>info:srw/schema/1/dc-v1.1</srw:recordSchema>"
        "<srw:recordPacking>xml</srw:recordPacking>"
        "<srw:recordData>"
        '<srw_dc:dc xmlns:srw_dc="info:srw/schema/1/dc-v1.1"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:schemaLocation="info:srw/schema/1/dc-schema.xsd">'
        f"{fields}"
        "</srw_dc:dc>"
        "</srw:recordData>"
        f"<srw:recordPosition>{position}</srw:recordPosition>"
        "</srw:record>"
    )


def _oai_dc_record(fields: str, position: int = 1) -> str:
    """One SRU record carrying the OAI-PMH oai_dc container instead."""
    return (
        "<srw:record>"
        "<srw:recordSchema>info:srw/schema/1/dc-v1.1</srw:recordSchema>"
        "<srw:recordPacking>xml</srw:recordPacking>"
        "<srw:recordData>"
        '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"{fields}"
        "</oai_dc:dc>"
        "</srw:recordData>"
        f"<srw:recordPosition>{position}</srw:recordPosition>"
        "</srw:record>"
    )


SRU_NO_ARK = _sru_envelope(
    _srw_dc_record(
        "<dc:title>Cookery</dc:title>"
        "<dc:creator>Anon</dc:creator>"
        "<dc:identifier>http://example.org/not-an-ark</dc:identifier>"
    )
)


def _xml(text: str) -> Reply:
    return Reply(200, text.encode("utf-8"), {"Content-Type": "application/xml"})


def _adapter(server: ScriptedServer) -> BritishLibrary:
    return BritishLibrary(
        sru_url=server.url("/SRU"),
        bnb_sparql_url=server.url("/sparql"),
        manifest_url=server.url("/iiif/{identifier}/manifest.json"),
        viewer_url=server.url("/viewer/{identifier}"),
    )


def _search(
    server: ScriptedServer,
    net: Network,
    sru: str,
    sparql: Any = None,
    *,
    title: str = "Cookery",
    creator: str | None = None,
    limit: int = 3,
) -> list[Any]:
    server.script("/SRU", _xml(sru))
    if sparql is not None:
        server.script("/sparql", json_reply(sparql))
    return _adapter(server).search(
        SearchQuery(title, creator=creator), network=net, limit=limit
    )


def _staging(tmp_path: Path) -> Path:
    return tmp_path / "work" / "objects" / ".staging" / "1"


# SRU parsing


def test_srw_dc_wrapped_records_are_parsed(
    server: ScriptedServer, net: Network
) -> None:
    """The container element sru.bl.uk sends: a ``{DCMES}dc`` wrapper does
    not exist, so looking for it returned nothing against any server."""
    xml = _sru_envelope(
        _srw_dc_record(
            "<dc:title>The Cook's Oracle</dc:title>"
            "<dc:creator>Kitchiner, William</dc:creator>"
            "<dc:date>1822</dc:date>"
            "<dc:identifier>ark:/81055/vdc_100022589074.0x000002</dc:identifier>"
        )
    )
    results = _search(server, net, xml, title="The Cook's Oracle")
    assert len(results) == 1
    assert results[0].title == "The Cook's Oracle"
    assert results[0].creators == ("Kitchiner, William",)
    assert results[0].date == "1822"
    assert results[0].source_id == "vdc_100022589074.0x000002"


def test_oai_dc_wrapped_records_are_parsed(
    server: ScriptedServer, net: Network
) -> None:
    """The other container in the wild: the OAI-PMH oai_dc profile."""
    xml = _sru_envelope(
        _oai_dc_record(
            "<dc:title>A New System of Domestic Cookery</dc:title>"
            "<dc:creator>Rundell, Maria Eliza Ketelby</dc:creator>"
            "<dc:date>1806</dc:date>"
            "<dc:identifier>ark:/81055/vdc_100023054321.0x000001</dc:identifier>"
        )
    )
    results = _search(server, net, xml, title="A New System of Domestic Cookery")
    assert len(results) == 1
    assert results[0].source_id == "vdc_100023054321.0x000001"
    assert results[0].date == "1806"


def test_ark_identifier_stops_at_the_first_separator(
    server: ScriptedServer, net: Network
) -> None:
    """``ark:/81055/(.*)`` swallowed a manifest path or a prose gloss."""
    xml = _sru_envelope(
        _srw_dc_record(
            "<dc:title>Modern Cookery</dc:title>"
            "<dc:identifier>https://api.bl.uk/metadata/iiif/"
            "ark:/81055/vdc_100000000795.0x000002/manifest.json"
            "</dc:identifier>",
            position=1,
        )
        + _srw_dc_record(
            "<dc:title>The Frugal Housewife</dc:title>"
            "<dc:identifier>ark:/81055/vdc_100000000796 "
            "(digitised copy)</dc:identifier>",
            position=2,
        ),
        count=2,
    )
    results = _search(server, net, xml, limit=2)
    assert [r.source_id for r in results] == [
        "vdc_100000000795.0x000002",
        "vdc_100000000796",
    ]


def test_sru_records_without_ark_fall_through_to_sparql(
    server: ScriptedServer, net: Network
) -> None:
    """An ARK-less SRU record is undownloadable and must not suppress the
    BNB SPARQL fallback."""
    sparql = {
        "results": {
            "bindings": [
                {
                    "title": {"value": "Cookery"},
                    "same": {"value": "http://bnb.example/ark:/81055/vdc_0001"},
                }
            ]
        }
    }
    results = _search(server, net, SRU_NO_ARK, sparql)
    assert len(results) == 1
    assert results[0].raw["source"] == "bnb_sparql"
    assert results[0].source_id == "vdc_0001"


def test_malformed_record_does_not_discard_later_results(
    server: ScriptedServer, net: Network
) -> None:
    """One bad record must not throw away the records around it.

    v1.30.3 forced an exception into the record conversion; the broad
    ``except`` that caught it is gone, so the bad record here is an empty
    ``recordData``.
    """
    good = _sru_envelope(
        "<srw:record><srw:recordData></srw:recordData></srw:record>"
        + _srw_dc_record(
            "<dc:title>B</dc:title><dc:identifier>ark:/81055/vdc_B</dc:identifier>",
            position=2,
        ),
        count=2,
    )
    results = _search(server, net, good)
    assert len(results) == 1
    assert results[0].source_id == "vdc_B"


def test_sru_record_without_creator_has_none_not_na(
    server: ScriptedServer, net: Network
) -> None:
    """No <dc:creator> gives raw["creator"] None, not the "N/A" sentinel."""
    xml = _sru_envelope(
        _srw_dc_record(
            "<dc:title>Cookery</dc:title><dc:identifier>ark:/81055/vdc_X</dc:identifier>"
        )
    )
    results = _search(server, net, xml)
    assert len(results) == 1
    assert results[0].raw["creator"] is None


# SPARQL fallback


def test_sparql_binding_without_a_title_does_not_borrow_the_query(
    server: ScriptedServer, net: Network
) -> None:
    """A binding with no dct:title was handed the searched-for title, a
    fabricated perfect score that let an arbitrary work be selected."""
    sparql = {
        "head": {"vars": ["work", "title", "creatorName", "same", "ident"]},
        "results": {
            "bindings": [
                {
                    "work": {
                        "type": "uri",
                        "value": "http://bnb.data.bl.uk/id/resource/009876543",
                    },
                    "same": {
                        "type": "uri",
                        "value": "http://api.bl.uk/metadata/iiif/"
                        "ark:/81055/vdc_100000000999",
                    },
                }
            ]
        },
    }
    results = _search(
        server,
        net,
        SRU_NO_ARK,
        sparql,
        title="The Cook's Oracle",
        creator="Kitchiner",
    )
    assert len(results) == 1
    assert results[0].title == ""
    # The query creator must not be fabricated either.
    assert results[0].creators == ()
    assert results[0].raw["creator"] is None


def test_sparql_binding_keeps_its_own_title_and_creator(
    server: ScriptedServer, net: Network
) -> None:
    """The fields the endpoint does bind are carried through."""
    sparql = json.loads((FIXTURES / "bnb_sparql.json").read_text(encoding="utf-8"))
    sparql["results"]["bindings"] = sparql["results"]["bindings"][:1]
    results = _search(
        server,
        net,
        SRU_NO_ARK,
        sparql,
        title="The Cook's Oracle",
        creator="Kitchiner",
    )
    assert len(results) == 1
    assert results[0].title == "The Cook's Oracle and Housekeeper's Manual"
    assert results[0].creators == ("Kitchiner, William",)
    assert results[0].source_id == "vdc_100000000998"


# Failures with their causes (sru.bl.uk NXDOMAIN, BNB SPARQL timeouts)


def _scripted(coord: Coordinator, *outcomes: Any) -> tuple[Network, ScriptedSession]:
    session = ScriptedSession(*outcomes)
    client = Network(
        settings_with(KEY, max_attempts=1),
        coord,
        session_factory=lambda: session,
        sleep=lambda _s: None,
    )
    return client, session


def test_dns_and_timeout_surface_with_their_causes(coord: Coordinator) -> None:
    client, session = _scripted(coord, DNS_FAILURE, TIMEOUT)
    with client, pytest.raises(ProviderBlocked) as excinfo:
        BritishLibrary().search(SearchQuery("Cookery"), network=client, limit=3)
    assert excinfo.value.cause == "dns"
    assert "BNB SPARQL" in excinfo.value.detail
    assert "transient" in excinfo.value.detail
    assert [call["url"] for call in session.calls] == [
        "https://sru.bl.uk/SRU",
        "https://bnb.data.bl.uk/sparql",
    ]


def test_an_unresolvable_sru_host_still_reaches_the_fallback(
    coord: Coordinator,
) -> None:
    body = (FIXTURES / "bnb_sparql.json").read_bytes()
    answer = response(200, [body], {"Content-Type": "application/json"})
    client, _session = _scripted(coord, DNS_FAILURE, answer)
    with client:
        results = BritishLibrary().search(
            SearchQuery("Cookery"), network=client, limit=3
        )
    assert [r.source_id for r in results] == ["vdc_100000000998", "vdc_100000000999"]


def test_an_empty_fallback_does_not_hide_the_sru_failure(coord: Coordinator) -> None:
    empty = response(
        200, [b'{"results": {"bindings": []}}'], {"Content-Type": "application/json"}
    )
    client, _session = _scripted(coord, DNS_FAILURE, empty)
    with client, pytest.raises(ProviderBlocked) as excinfo:
        BritishLibrary().search(SearchQuery("Cookery"), network=client, limit=3)
    assert excinfo.value.cause == "dns"


def test_a_timed_out_fallback_after_an_empty_sru_answer_is_transient(
    server: ScriptedServer, net: Network
) -> None:
    server.script("/SRU", _xml(_sru_envelope("", count=0)))
    server.script("/sparql", Reply(503))
    with pytest.raises(ProviderBlocked) as excinfo:
        _adapter(server).search(SearchQuery("Cookery"), network=net, limit=3)
    assert excinfo.value.cause == "transient"


# Downloads


def test_viewer_fallback_runs_when_manifest_endpoint_returns_html(
    server: ScriptedServer, net: Network, bl_slot: SlotToken, tmp_path: Path
) -> None:
    """api.bl.uk answers with an error page rather than a status code; a
    truthiness test on the body skipped the viewer fallback in that case."""
    server.script("/iiif/vdc_X/manifest.json", Reply(200, b"<html>unavailable</html>"))
    found = server.url("/x/manifest.json")
    server.script("/viewer/vdc_X", Reply(200, f'<html>var m = "{found}";'.encode()))
    server.script("/x/manifest.json", json_reply({"sequences": []}))
    _adapter(server).download(
        "vdc_X",
        network=net,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(bl_slot),
    )
    # Three requests: the failing manifest endpoint, the viewer page, and
    # the manifest discovered inside it.
    assert [a.path for a in server.arrivals] == [
        "/iiif/vdc_X/manifest.json",
        "/viewer/vdc_X",
        "/x/manifest.json",
    ]


def test_download_uses_the_ark_without_its_viewer_suffix(
    server: ScriptedServer, net: Network, bl_slot: SlotToken, tmp_path: Path
) -> None:
    manifest = v2_manifest(*(service_canvas(u) for u in serve_pages(server, "1", "2")))
    server.script("/iiif/vdc_1/manifest.json", json_reply(manifest))
    result = _adapter(server).download(
        "vdc_1.0x000002",
        network=net,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(bl_slot, format="images"),
    )
    assert result.source_id == "vdc_1.0x000002"
    assert result.complete
    assert [f.name for f in result.files] == [
        "metadata/manifest.json",
        "p00001.jpg",
        "p00002.jpg",
    ]


def test_a_missing_manifest_without_a_viewer_link_is_item_not_found(
    server: ScriptedServer, net: Network, bl_slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/viewer/vdc_gone", Reply(200, b"<html>no manifest</html>"))
    with pytest.raises(ItemNotFound):
        _adapter(server).download(
            "vdc_gone",
            network=net,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(bl_slot),
        )


# Recorded responses (synthetic, see the fixture README)


def test_fixture_sru_search_parses() -> None:
    records = parse_sru_records((FIXTURES / "sru_search.xml").read_bytes(), limit=5)
    assert [(r.source_id, r.year) for r in records] == [
        ("vdc_100022589074.0x000002", 1822),
        ("vdc_100023054321.0x000001", 1806),
    ]


def test_fixture_bnb_sparql_parses() -> None:
    document = json.loads((FIXTURES / "bnb_sparql.json").read_text(encoding="utf-8"))
    records = parse_bnb_bindings(document, limit=5)
    assert [(r.source_id, r.title) for r in records] == [
        ("vdc_100000000998", "The Cook's Oracle and Housekeeper's Manual"),
        ("vdc_100000000999", ""),
    ]


def test_fixture_manifest_gives_pages(server: ScriptedServer, net: Network) -> None:
    manifest = (FIXTURES / "manifest.json").read_bytes()
    server.script("/iiif/vdc_100022589074/manifest.json", Reply(200, manifest))
    refs = _adapter(server).pages(
        "vdc_100022589074", network=net, first_n=5, max_px=600
    )
    base = "https://api.bl.uk/image/iiif/ark:/81055/vdc_100022589075.0x000001"
    assert [(r.item, r.label) for r in refs] == [
        ("p00001", "Front cover"),
        ("p00002", "Title page"),
    ]
    assert refs[0].service == base
    assert refs[0].urls[0] == f"{base}/full/!600,600/0/default.jpg"


def test_bnb_query_escapes_double_quotes() -> None:
    query = build_bnb_query('The "Cook\'s" Oracle', "Kitchiner", 3)
    assert 'LCASE("The \\"Cook\\\'s\\" Oracle")' in query
    assert "LIMIT 9" in query


def test_build_returns_the_adapter() -> None:
    adapter = build(Settings())
    assert adapter.key == KEY
    assert adapter.capabilities.iiif
