"""HathiTrust: identifier lookups in the Bibliographic API; metadata only.

Lookups go through the real :class:`~chronodownloader.net.Network` and the
real coordination database to a local server that stands in for the
catalog. Ported from v1.30.3 ``tests/integration/test_providers.py``
(``TestHathiTrustProvider`` and the HathiTrust download cases) and
``tests/unit/test_provider_parsing.py``
(``test_hathitrust_untitled_bib_record_gets_empty_title``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chronodownloader.coord import Coordinator, SlotToken
from chronodownloader.net import Network
from chronodownloader.providers import hathitrust
from chronodownloader.providers.base import (
    DownloadRequest,
    ProviderBlocked,
    SearchQuery,
    SearchRecord,
    UnsupportedCapability,
)
from chronodownloader.providers.hathitrust import (
    KEY,
    HathiTrust,
    bib_url,
    parse_bib_response,
    parse_identifiers,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import Settings
from tests.iiif.support import json_reply
from tests.net.support import Reply, ScriptedServer, settings_with

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / KEY
BIB = "/api/volumes/brief/"
HTID = "ucbk.ark:/28722/h2rf5m094"


def _fixture(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return document


@pytest.fixture
def hathi_network(coord: Coordinator) -> Iterator[Network]:
    with Network(settings_with(KEY), coord, sleep=lambda _seconds: None) as client:
        yield client


@pytest.fixture
def hathi_slot(coord: Coordinator) -> SlotToken:
    token = coord.acquire_slot(KEY, 1)
    assert token is not None
    return token


def _provider(server: ScriptedServer) -> HathiTrust:
    return HathiTrust(server.url(BIB))


# v1.30.3 TestHathiTrustProvider and parsing


def test_search_extracts_htid_from_top_level_items(
    server: ScriptedServer, hathi_network: Network
) -> None:
    """The Bibliographic API returns "records" and "items" as sibling
    top-level keys; items link back via "fromRecord". htid/item_url must be
    read from the top-level items, not from a per-record "items" list, so
    the identifier is the htid and not the useless record number."""
    response = {
        "records": {
            "000123456": {
                "recordURL": "https://catalog.hathitrust.org/Record/000123456",
                "titles": ["The Art of Cooking"],
                "publishDates": ["1850"],
            }
        },
        "items": [
            {
                "orig": "University of California",
                "fromRecord": "000123456",
                "htid": "uc1.b1234567",
                "itemURL": "https://babel.hathitrust.org/cgi/pt?id=uc1.b1234567",
            }
        ],
    }
    server.script(f"{BIB}oclc/12345.json", json_reply(response))
    results = _provider(server).search(
        SearchQuery("Cookery oclc:12345"), network=hathi_network, limit=3
    )
    assert len(results) == 1
    first = results[0]
    assert first.raw["htid"] == "uc1.b1234567"
    assert (
        first.raw["item_url"] == "https://babel.hathitrust.org/cgi/pt?id=uc1.b1234567"
    )
    # Identifier must be the htid, not the 9-digit record number.
    assert first.source_id == "uc1.b1234567"


def test_hathitrust_untitled_bib_record_gets_empty_title(
    server: ScriptedServer, hathi_network: Network
) -> None:
    data = {
        "records": {"123": {"publishDates": ["1651"]}},
        "items": [{"fromRecord": "123", "htid": "x1"}],
    }
    server.script(f"{BIB}oclc/12345.json", json_reply(data))
    results = _provider(server).search(
        SearchQuery("Le Cuisinier oclc:12345"), network=hathi_network, limit=3
    )
    assert results
    assert not results[0].title


def test_a_record_without_items_falls_back_to_its_record_number() -> None:
    data = {"records": {"777": {"titles": ["Kochbuch"], "items": []}}}
    (record,) = parse_bib_response(data, 3)
    assert record.source_id == "777"
    assert record.item_url is None


def test_malformed_records_are_skipped() -> None:
    data = {"records": {"1": "broken", "2": {"titles": "Kochbuch"}}, "items": "x"}
    (record,) = parse_bib_response(data, 3)
    assert (record.source_id, record.title) == ("2", "Kochbuch")
    assert parse_bib_response({"records": []}, 3) == []


# The recorded response (live, 19.10.2026)


def test_the_live_fixture_parses_into_the_baseline_record() -> None:
    (record,) = parse_bib_response(_fixture("oclc_21726609.json"), 5)
    assert record.provider == KEY
    assert record.source_id == HTID
    assert record.title == "Tonkin ; ossia, Il credenziere cinese"
    assert record.creators == ()
    assert (record.date, record.year) == ("1827", 1827)
    assert record.item_url == f"https://babel.hathitrust.org/cgi/pt?id={HTID}"
    assert record.raw["record_id"] == "103004126"
    assert record.raw["bib"]["oclcs"] == ["21726609"]


def test_an_oclc_hint_retrieves_the_metadata_through_the_api(
    server: ScriptedServer, hathi_network: Network
) -> None:
    server.script(
        f"{BIB}oclc/21726609.json", json_reply(_fixture("oclc_21726609.json"))
    )
    query = SearchQuery(
        "Tonkin ossia Il Credenziere Cinese oclc:21726609",
        creator="Francesco Leonardi",
    )
    (record,) = _provider(server).search(query, network=hathi_network, limit=5)
    assert record.source_id == HTID
    assert server.hits(f"{BIB}oclc/21726609.json") == 1


# Search without a keyword API


def test_a_query_without_an_identifier_hint_sends_no_request(
    server: ScriptedServer, hathi_network: Network
) -> None:
    results = _provider(server).search(
        SearchQuery("Tonkin ossia Il Credenziere Cinese"),
        network=hathi_network,
        limit=5,
    )
    assert results == []
    assert server.arrivals == []


def test_an_htid_hint_becomes_a_record_without_a_request(
    server: ScriptedServer, hathi_network: Network
) -> None:
    query = SearchQuery(f"htid:{HTID}", creator="Leonardi")
    (record,) = _provider(server).search(query, network=hathi_network, limit=5)
    assert (record.source_id, record.title) == (HTID, query.title)
    assert record.creators == ("Leonardi",)
    assert record.raw["htid"] == HTID
    assert server.arrivals == []


def test_identifier_hints_are_read_from_title_and_creator(
    server: ScriptedServer, hathi_network: Network
) -> None:
    assert parse_identifiers("x isbn:978-3-16-148410-0 OCLC: 42 lccn:a/1") == {
        "oclc": ["42"],
        "isbn": ["9783161484100"],
        "lccn": ["a/1"],
    }
    assert parse_identifiers(None) == {}
    server.script(f"{BIB}isbn/123X.json", json_reply({"records": {}, "items": []}))
    _provider(server).search(
        SearchQuery("Kochbuch", creator="isbn:123-X"), network=hathi_network, limit=3
    )
    assert server.hits(f"{BIB}isbn/123X.json") == 1
    assert bib_url(" OCLC ", "1") == f"{hathitrust.BIB_BASE_URL}oclc/1.json"


def test_the_limit_caps_the_records(
    server: ScriptedServer, hathi_network: Network
) -> None:
    server.script(
        f"{BIB}oclc/21726609.json", json_reply(_fixture("oclc_21726609.json"))
    )
    query = SearchQuery("htid:a htid:b oclc:21726609")
    results = _provider(server).search(query, network=hathi_network, limit=2)
    assert [r.source_id for r in results] == ["a", "b"]
    assert server.arrivals == []


def test_an_unknown_identifier_is_no_result(
    server: ScriptedServer, hathi_network: Network
) -> None:
    results = _provider(server).search(
        SearchQuery("Kochbuch oclc:999"), network=hathi_network, limit=3
    )
    assert results == []
    assert server.hits(f"{BIB}oclc/999.json") == 1


def test_a_failed_lookup_raises_instead_of_returning_nothing(
    server: ScriptedServer, coord: Coordinator
) -> None:
    server.script(f"{BIB}oclc/1.json", Reply(503))
    settings = settings_with(KEY, max_attempts=1)
    with (
        Network(settings, coord, sleep=lambda _s: None) as network,
        pytest.raises(ProviderBlocked) as excinfo,
    ):
        _provider(server).search(SearchQuery("oclc:1"), network=network, limit=3)
    assert excinfo.value.cause == "transient"


# Metadata only (v1.30.3 download cases; plan phase 3)


def test_hathitrust_is_declared_metadata_only() -> None:
    provider = Registry(Settings()).get(KEY)
    assert isinstance(provider, HathiTrust)
    assert provider.capabilities.downloads is False
    assert provider.capabilities.iiif is False


def test_hathitrust_download_without_api_key_is_unsupported(
    server: ScriptedServer,
    hathi_network: Network,
    hathi_slot: SlotToken,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-1: no page image downloaded is never a completed work; the
    adapter now declares downloads unsupported instead of returning False."""
    monkeypatch.delenv("HATHI_API_KEY", raising=False)
    record = SearchRecord(KEY, "abc", "Kochbuch", raw={"htid": "abc", "bib": {"x": 1}})
    with pytest.raises(UnsupportedCapability) as excinfo:
        hathitrust.build(Settings()).download(
            record,
            network=hathi_network,
            staging_dir=tmp_path / "staging",
            request=DownloadRequest(hathi_slot),
        )
    assert excinfo.value.capability == "download"
    assert not (tmp_path / "staging").exists()


def test_hathitrust_download_with_api_key_fires_no_request(
    server: ScriptedServer,
    hathi_network: Network,
    hathi_slot: SlotToken,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old Data-API branch matched no real htd endpoint.

    Page bytes are served only by the OAuth 1.0a-signed Data API, so with a
    key set the adapter stays metadata-only rather than issuing a request
    that can never succeed.
    """
    monkeypatch.setenv("HATHI_API_KEY", "KEY")
    provider = hathitrust.build(Settings())
    with pytest.raises(UnsupportedCapability):
        provider.download(
            "abc",
            network=hathi_network,
            staging_dir=tmp_path / "staging",
            request=DownloadRequest(hathi_slot),
        )
    with pytest.raises(UnsupportedCapability) as excinfo:
        provider.pages("abc", network=hathi_network, first_n=3, max_px=800)
    assert excinfo.value.capability == "pages"
    assert server.arrivals == []


def test_hathitrust_module_has_no_data_api_constant() -> None:
    """The doomed endpoint constant is gone, not merely unused."""
    assert not hasattr(hathitrust, "DATA_API_URL")
