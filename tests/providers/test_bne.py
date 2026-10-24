"""The BNE adapter: identifier namespaces, SPARQL search, PDF page ranges.

BNE splits its holdings across two services with incompatible identifier
namespaces: the linked-data catalogue at ``datos.bne.es`` (resource ids such
as ``bimo0001244675``, ``bima0000020829`` and authority ids such as
``XX3469244``) and BNE Digital at ``bnedigital.bne.es``, whose objects are
UUID-addressed. An earlier revision spliced the catalogue id into
``https://iiif.bne.es/{id}/manifest``: wrong namespace, and a host that does
not resolve.

Ported from v1.30.3 ``tests/unit/test_bne.py`` and ``TestBudgetGuards`` of
``tests/integration/test_providers.py``. The patched ``make_request`` and
``download_file`` are replaced by a local server behind the real network
client; ``get_max_pages`` and ``budget_exhausted`` become the request's
``max_pages`` and ``budget``. The SPARQL documents are the recorded fixtures
(live responses of 01.08.2026).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from chronodownloader.coord import Coordinator, SlotToken
from chronodownloader.net import Network
from chronodownloader.providers.base import (
    DownloadRequest,
    DownloadResult,
    ItemNotFound,
    ProviderBlocked,
    SearchQuery,
    UnsupportedCapability,
)
from chronodownloader.providers.bne import (
    ACCENT_FOLDING,
    CHUNK_LIMIT,
    DIGITAL_BASE_URL,
    DIGITAL_PDF_URL,
    END_UNVERIFIED,
    HISTORICAL_YEAR_PATTERN,
    PDF_PAGE_CHUNK,
    SEARCH_API_URL,
    Bne,
    build,
    build_search_query,
    catalog_resource_id,
    chunk_item,
    extract_digital_id,
    fold_accents,
    parse_search_bindings,
)
from chronodownloader.settings import Settings
from tests.iiif.support import json_reply, pdf
from tests.net.support import Reply, ScriptedServer, settings_with

from .fakes import FileBudget

KEY = "bne"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers" / KEY


def _fixture(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return document


SPARQL_SEARCH_RESPONSE = _fixture("sparql_search.json")
SPARQL_SEE_ALSO_RESPONSE = _fixture("sparql_see_also.json")

# Recorded from the *unconstrained* query the connector used to send: the
# endpoint mixed authority records (XX...) into the bibliographic ones, and no
# binding carries a digitisation link.
SPARQL_UNCONSTRAINED_RESPONSE: dict[str, Any] = {
    "head": {"link": [], "vars": ["id", "title"]},
    "results": {
        "distinct": False,
        "ordered": True,
        "bindings": [
            {
                "id": {
                    "type": "uri",
                    "value": "https://datos.bne.es/resource/bimo0000251156",
                },
                "title": {"type": "literal", "value": "El arte de cocinar."},
            },
            {
                "id": {
                    "type": "uri",
                    "value": "https://datos.bne.es/resource/XX3469244",
                },
                "title": {"type": "literal", "value": "Arte de cocina"},
            },
        ],
    },
}

EMPTY_SPARQL_RESPONSE: dict[str, Any] = {
    "head": {"link": [], "vars": ["digital"]},
    "results": {"distinct": False, "ordered": True, "bindings": []},
}

COOKBOOK_UUID = "e7a1d9e6-51ce-4eea-a25f-11f35ffa6d41"
PDF_PATH = "/bd/es/pdf"


@pytest.fixture
def net(coord: Coordinator) -> Iterator[Network]:
    with Network(
        settings_with(KEY, max_attempts=1), coord, sleep=lambda _s: None
    ) as client:
        yield client


@pytest.fixture
def bne_slot(coord: Coordinator) -> SlotToken:
    token = coord.acquire_slot(KEY, 1)
    assert token is not None
    return token


def _adapter(server: ScriptedServer) -> Bne:
    return Bne(sparql_url=server.url("/sparql"), pdf_url=server.url(PDF_PATH))


def _search(
    server: ScriptedServer, net: Network, document: Any, title: str, limit: int
) -> list[Any]:
    server.script("/sparql", json_reply(document))
    return _adapter(server).search(SearchQuery(title), network=net, limit=limit)


def _sent_queries(server: ScriptedServer) -> list[str]:
    return [
        parse_qs(urlsplit(a.path).query)["query"][0]
        for a in server.arrivals
        if a.path.startswith("/sparql")
    ]


def _pdf_requests(server: ScriptedServer) -> list[str]:
    return [a.path for a in server.arrivals if a.path.startswith(PDF_PATH)]


def _download(
    server: ScriptedServer,
    net: Network,
    slot: SlotToken,
    tmp_path: Path,
    item: str = COOKBOOK_UUID,
    **request: Any,
) -> DownloadResult:
    return _adapter(server).download(
        item,
        network=net,
        staging_dir=tmp_path / "work" / "objects" / ".staging" / "1",
        request=DownloadRequest(slot, **request),
    )


def _range(start: int, end: int, uuid: str = COOKBOOK_UUID) -> str:
    return f"{PDF_PATH}?id={uuid}&page={start}-{end}"


# Query construction


class TestSearchQuery:
    """The SPARQL query must select bibliographic, digitised records only."""

    def test_constrains_to_manifestation_class(self) -> None:
        query = build_search_query("arte de cocina", 8)
        assert "<https://datos.bne.es/def/C1003>" in query

    def test_requires_a_bne_digital_link(self) -> None:
        query = build_search_query("arte de cocina", 8)
        assert "rdf-schema#seeAlso" in query
        assert f"STRSTARTS(STR(?digital), '{DIGITAL_BASE_URL}/')" in query

    def test_drops_the_dbpedia_author_property(self) -> None:
        """datos.bne.es carries no dbo:author; the OPTIONAL never bound."""
        query = build_search_query("arte de cocina", 8)
        assert "dbpedia.org/ontology/author" not in query
        assert "<https://datos.bne.es/def/P1011>" in query

    def test_escapes_quotes_in_the_title(self) -> None:
        query = build_search_query("l'art de cocina", 8)
        assert "l\\'art de cocina" in query


class TestHistoricalDateFilter:
    """Modern reprints must not consume the result budget.

    The date property is free text, so the filter is a membership test that
    deliberately fails open on undated records and on records whose date
    holds no four-digit year.
    """

    def test_the_filter_is_emitted(self) -> None:
        query = build_search_query("cocina", 12)
        assert "!BOUND(?date)" in query
        assert "!REGEX(STR(?date), '[0-9]{4}')" in query
        assert "REGEX(STR(?date), '(1[0-8][0-9][0-9]|1900)')" in query

    def test_the_pattern_admits_only_historical_years(self) -> None:
        """The cutoff lives in the character classes, not in a comparison."""
        pattern = re.compile(HISTORICAL_YEAR_PATTERN)
        for literal in (
            "1845",
            "[1770]",
            "1886-1945",
            "[entre 1645 y 1668?]",
            "Anno 1762",
            "1899",
            "1900",
            "1000",
        ):
            assert pattern.search(literal), literal
        for literal in ("1901", "1913", "2019", "2026"):
            assert not pattern.search(literal), literal

    def test_undated_and_unparseable_records_fail_open(self) -> None:
        """Century notation and Roman numerals carry no four-digit year and
        skew old; without the second disjunct the oldest material drops."""
        no_year = re.compile(r"[0-9]{4}")
        for literal in ("S.XVIII", "S.XVII", "A. MDCCLXII", "Anno Domini"):
            assert not no_year.search(literal), literal


# Accent folding


class TestAccentFolding:
    """Unaccented queries must reach accented Spanish titles: the endpoint
    has no accent-insensitive comparison, so both sides of the filter are
    folded onto ASCII with one shared table."""

    @pytest.mark.parametrize(
        ("raw", "folded"),
        [
            ("Novísimo arte de cocina", "novisimo arte de cocina"),
            ("La cocina española antigua", "la cocina espanola antigua"),
            ("Pardo Bazán", "pardo bazan"),
            ("Martínez Montiño", "martinez montino"),
            ("MENÚ", "menu"),
            ("cigüeña", "ciguena"),
            ("arte de cocina", "arte de cocina"),
            ("", ""),
        ],
    )
    def test_query_side_is_lowercased_and_folded(self, raw: str, folded: str) -> None:
        assert fold_accents(raw) == folded

    @pytest.mark.parametrize(("accented", "plain"), ACCENT_FOLDING)
    def test_every_table_entry_folds_on_both_sides(
        self, accented: str, plain: str
    ) -> None:
        """The Python fold and the SPARQL REPLACE chain share one table."""
        assert fold_accents(accented) == plain
        query = build_search_query("cocina", 8)
        assert f"'{accented}', '{plain}'" in query

    def test_title_variable_is_wrapped_in_the_folding_construct(self) -> None:
        query = build_search_query("cocina", 8)
        assert "REPLACE(LCASE(?title)" in query
        assert query.count("REPLACE(") == len(ACCENT_FOLDING)
        assert "CONTAINS(REPLACE(" in query

    def test_accented_query_is_folded_before_embedding(self) -> None:
        """The literal handed to CONTAINS carries no accents of its own."""
        query = build_search_query("Novísimo Arte de Cocina", 8)
        assert "'novisimo arte de cocina'))" in query
        assert "novísimo" not in query

    def test_the_digital_copy_is_read_from_op3001_or_see_also(self) -> None:
        """Current records link their BNE Digital card through OP3001 only."""
        query = build_search_query("cocina", 8)
        assert (
            "?id <https://datos.bne.es/def/OP3001>|"
            "<http://www.w3.org/2000/01/rdf-schema#seeAlso> ?digital" in query
        )

    def test_unaccented_query_is_left_alone(self) -> None:
        query = build_search_query("novisimo arte de cocina", 8)
        assert "'novisimo arte de cocina'))" in query

    def test_folding_does_not_break_quote_escaping(self) -> None:
        """Folding runs before escaping and must not swallow the guard."""
        query = build_search_query("L'Art de Cocína", 8)
        assert "l\\'art de cocina" in query
        assert "'l'art" not in query

    def test_backslashes_stay_escaped(self) -> None:
        query = build_search_query("cocina\\española", 8)
        assert "cocina\\\\espanola" in query

    def test_injection_attempt_cannot_close_the_literal(self) -> None:
        query = build_search_query("x')) . ?s ?p ?o #", 8)
        assert "x\\')) . ?s ?p ?o #" in query
        assert "'x'))" not in query
        # Still one CONTAINS, one STRSTARTS, one date filter, two OPTIONALs.
        assert query.count("FILTER(") == 3
        assert query.count("OPTIONAL") == 2

    def test_search_sends_the_folded_query(
        self, server: ScriptedServer, net: Network
    ) -> None:
        _search(server, net, SPARQL_SEARCH_RESPONSE, "Novísimo", 1)
        (query,) = _sent_queries(server)
        assert "'novisimo'))" in query


# Search


class TestSearchBne:
    """Search results must carry the downloadable BNE Digital identifier."""

    def test_source_id_is_the_bne_digital_uuid(
        self, server: ScriptedServer, net: Network
    ) -> None:
        results = _search(server, net, SPARQL_SEARCH_RESPONSE, "arte de cocina", 4)
        assert [r.source_id for r in results] == [
            "e7a1d9e6-51ce-4eea-a25f-11f35ffa6d41",
            "ad28ecee-2fb2-45af-9fe2-6fdb3c6a5c90",
            "a984ca89-2da2-4d68-b979-a996cf9b5eac",
            "23b896a6-7075-4579-b4fd-1f47c22bf9ac",
        ]

    def test_catalogue_id_is_kept_but_not_used_as_identifier(
        self, server: ScriptedServer, net: Network
    ) -> None:
        results = _search(server, net, SPARQL_SEARCH_RESPONSE, "arte de cocina", 1)
        first = results[0]
        assert first.raw["catalog_id"] == "https://datos.bne.es/resource/bima0000020829"
        assert first.source_id != "bima0000020829"

    def test_item_url_points_at_bne_digital(
        self, server: ScriptedServer, net: Network
    ) -> None:
        results = _search(server, net, SPARQL_SEARCH_RESPONSE, "arte de cocina", 4)
        for result in results:
            assert result.item_url is not None
            assert result.item_url.startswith(DIGITAL_BASE_URL)
        # No result may point at the retired IIIF host.
        assert not any("iiif.bne.es" in (r.item_url or "") for r in results)

    def test_creator_and_date_are_carried_through(
        self, server: ScriptedServer, net: Network
    ) -> None:
        results = _search(server, net, SPARQL_SEARCH_RESPONSE, "arte de cocina", 2)
        assert results[0].creators == ("Altamiras, Juan",)
        assert results[0].date == "1758"
        assert results[1].creators == ("Martínez Montiño, Francisco",)

    def test_max_results_is_respected(
        self, server: ScriptedServer, net: Network
    ) -> None:
        results = _search(server, net, SPARQL_SEARCH_RESPONSE, "arte de cocina", 2)
        assert len(results) == 2

    def test_over_fetches_to_survive_optional_duplicates(
        self, server: ScriptedServer, net: Network
    ) -> None:
        """OPTIONAL clauses repeat a record per binding, so LIMIT must exceed
        max_results or duplicates would eat the result slots."""
        _search(server, net, SPARQL_SEARCH_RESPONSE, "arte de cocina", 3)
        (query,) = _sent_queries(server)
        assert "LIMIT 12" in query

    def test_duplicate_bindings_are_de_duplicated(
        self, server: ScriptedServer, net: Network
    ) -> None:
        bindings = SPARQL_SEARCH_RESPONSE["results"]["bindings"]
        doubled = {
            "head": SPARQL_SEARCH_RESPONSE["head"],
            "results": {"bindings": [bindings[0], bindings[0], bindings[1]]},
        }
        results = _search(server, net, doubled, "arte de cocina", 5)
        assert len(results) == 2

    def test_bindings_without_a_digital_link_are_dropped(
        self, server: ScriptedServer, net: Network
    ) -> None:
        """Defence in depth: pre-fix responses mixed in authority records."""
        results = _search(
            server, net, SPARQL_UNCONSTRAINED_RESPONSE, "arte de cocina", 5
        )
        assert results == []

    def test_blocked_endpoint_is_reported_not_empty(
        self, server: ScriptedServer, net: Network
    ) -> None:
        """The bot-filter 403 of datos.bne.es is ``blocked``, not "no results"
        (v1.30.3 returned an empty list)."""
        server.script("/sparql", Reply(403, b"<html>Forbidden</html>"))
        with pytest.raises(ProviderBlocked) as excinfo:
            _adapter(server).search(SearchQuery("arte de cocina"), network=net, limit=3)
        assert excinfo.value.cause == "blocked"


# Identifier extraction


class TestExtractDigitalId:
    """UUID extraction across the URL shapes BNE Digital serves."""

    @pytest.mark.parametrize(
        "value",
        [
            "a984ca89-2da2-4d68-b979-a996cf9b5eac",
            "https://bnedigital.bne.es/bd/card?id=a984ca89-2da2-4d68-b979-a996cf9b5eac",
            "https://bnedigital.bne.es/bd/es/card?id=a984ca89-2da2-4d68-b979-a996cf9b5eac",
            "https://bnedigital.bne.es/bd/es/viewer"
            "?id=a984ca89-2da2-4d68-b979-a996cf9b5eac&page=1",
            "A984CA89-2DA2-4D68-B979-A996CF9B5EAC",
        ],
    )
    def test_accepts_uuid_shapes(self, value: str) -> None:
        assert extract_digital_id(value) == "a984ca89-2da2-4d68-b979-a996cf9b5eac"

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "bimo0001244675",
            "XX3469244",
            "https://datos.bne.es/resource/bima0000020829",
        ],
    )
    def test_rejects_catalogue_namespace(self, value: str | None) -> None:
        assert extract_digital_id(value) is None


class TestCatalogResourceId:
    """Catalogue ids feed an unescaped SPARQL lookup, so the guard is narrow,
    yet admits every shape search stores in ``raw["catalog_id"]``."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("bimo0001244675", "bimo0001244675"),
            ("XX3469244", "XX3469244"),
            ("https://datos.bne.es/resource/bima0000020829", "bima0000020829"),
            ("(CaPaEBR)a6232137", "(CaPaEBR)a6232137"),
            (
                "https://datos.bne.es/resource/(CaPaEBR)a6232137",
                "(CaPaEBR)a6232137",
            ),
        ],
    )
    def test_accepts_live_id_shapes(self, value: str, expected: str) -> None:
        assert catalog_resource_id(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "(CaPaEBR)",
            "(Ca')x1",
            "a1' } UNION { ?s ?p ?o ",
            "<https://evil.example/>",
            "a1 OR 1=1",
            "a1\\x",
            "(OrganizationCodeFarTooLong)a1",
            "https://example.com/resource/a6232137",
        ],
    )
    def test_rejects_unsafe_or_foreign_ids(self, value: str) -> None:
        assert catalog_resource_id(value) is None


# Downloads


class TestDownloadBneWork:
    """Downloads go to BNE Digital, in chunked PDF page ranges."""

    def test_downloads_chunked_pdf_ranges(
        self,
        server: ScriptedServer,
        net: Network,
        bne_slot: SlotToken,
        tmp_path: Path,
    ) -> None:
        # Two ranges, then the probable end-of-work HTTP 500.
        server.script(PDF_PATH, pdf(), pdf(), Reply(500))
        result = _download(server, net, bne_slot, tmp_path)
        part = result.part("pdf")
        assert part is not None
        assert part.present == (chunk_item(1, 25), chunk_item(26, 50))
        assert _pdf_requests(server) == [
            _range(1, 25),
            _range(26, 50),
            _range(51, 75),
        ]

    def test_chunk_never_exceeds_the_server_page_limit(
        self,
        server: ScriptedServer,
        net: Network,
        bne_slot: SlotToken,
        tmp_path: Path,
    ) -> None:
        """BNE Digital answers ranges above 25 pages with HTTP 500."""
        server.script(PDF_PATH, pdf(), Reply(500))
        _download(server, net, bne_slot, tmp_path)
        first_url = _pdf_requests(server)[0]
        start, _, end = first_url.rsplit("page=", 1)[1].partition("-")
        assert int(end) - int(start) + 1 == PDF_PAGE_CHUNK

    def test_never_contacts_the_retired_iiif_host(
        self,
        server: ScriptedServer,
        net: Network,
        bne_slot: SlotToken,
        tmp_path: Path,
    ) -> None:
        """iiif.bne.es has no DNS record; nothing may address it."""
        server.script(PDF_PATH, pdf(), Reply(500))
        _download(server, net, bne_slot, tmp_path)
        assert all("iiif.bne.es" not in a.path for a in server.arrivals)
        assert DIGITAL_PDF_URL == "https://bnedigital.bne.es/bd/es/pdf"

    def test_stops_at_the_configured_page_ceiling(
        self,
        server: ScriptedServer,
        net: Network,
        bne_slot: SlotToken,
        tmp_path: Path,
    ) -> None:
        server.script(PDF_PATH, pdf())
        result = _download(server, net, bne_slot, tmp_path, max_pages=10)
        assert result.complete
        assert _pdf_requests(server) == [_range(1, 10)]

    def test_respects_an_exhausted_download_budget(
        self,
        server: ScriptedServer,
        net: Network,
        bne_slot: SlotToken,
        tmp_path: Path,
    ) -> None:
        server.script(PDF_PATH, pdf())
        result = _download(
            server, net, bne_slot, tmp_path, budget=FileBudget({"pdf": 0})
        )
        assert not result.complete
        assert result.stopped == "budget"
        assert _pdf_requests(server) == []

    def test_resolves_a_catalogue_id_through_see_also(
        self,
        server: ScriptedServer,
        net: Network,
        bne_slot: SlotToken,
        tmp_path: Path,
    ) -> None:
        """A datos.bne.es record is translated into the digital namespace."""
        server.script("/sparql", json_reply(SPARQL_SEE_ALSO_RESPONSE))
        server.script(PDF_PATH, pdf(), Reply(500))
        result = _download(server, net, bne_slot, tmp_path, item="bimo0001244675")
        part = result.part("pdf")
        assert part is not None and part.present == (chunk_item(1, 25),)
        (query,) = _sent_queries(server)
        assert "https://datos.bne.es/resource/bimo0001244675" in query
        assert "id=a984ca89-2da2-4d68-b979-a996cf9b5eac" in _pdf_requests(server)[0]

    def test_authority_record_without_a_digital_copy_fails_cleanly(
        self,
        server: ScriptedServer,
        net: Network,
        bne_slot: SlotToken,
        tmp_path: Path,
    ) -> None:
        server.script("/sparql", json_reply(EMPTY_SPARQL_RESPONSE))
        with pytest.raises(ItemNotFound):
            _download(server, net, bne_slot, tmp_path, item="XX3469244")
        assert _pdf_requests(server) == []

    def test_missing_identifier_fails_cleanly(
        self,
        server: ScriptedServer,
        net: Network,
        bne_slot: SlotToken,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(ItemNotFound):
            _download(server, net, bne_slot, tmp_path, item="")
        assert server.arrivals == []


def test_bne_pdf_loop_stops_on_exhausted_budget(
    server: ScriptedServer, net: Network, bne_slot: SlotToken, tmp_path: Path
) -> None:
    """v1.30.3 ``TestBudgetGuards``: the PDF range loop respects the budget."""
    result = _download(
        server,
        net,
        bne_slot,
        tmp_path,
        item="a984ca89-2da2-4d68-b979-a996cf9b5eac",
        budget=FileBudget({"pdf": 0}),
    )
    assert not result.complete
    assert _pdf_requests(server) == []


# Completeness of the range loop (new contract)


def test_a_500_after_a_range_leaves_the_end_unverified(
    server: ScriptedServer, net: Network, bne_slot: SlotToken, tmp_path: Path
) -> None:
    """BNE publishes no page count, so a 500 is no proof of the end: the
    range stays expected and missing, and the work partial."""
    server.script(PDF_PATH, pdf(), pdf(), Reply(500))
    result = _download(server, net, bne_slot, tmp_path)
    part = result.part("pdf")
    assert part is not None
    assert part.expected == (chunk_item(1, 25), chunk_item(26, 50), chunk_item(51, 75))
    assert part.missing == (chunk_item(51, 75),)
    assert dict(part.causes) == {chunk_item(51, 75): END_UNVERIFIED}
    assert not result.complete
    assert [f.name for f in result.files] == [
        "metadata/item.json",
        "rendering_p00001_00025.pdf",
        "rendering_p00026_00050.pdf",
    ]


def test_exhausting_the_chunk_limit_is_partial(
    server: ScriptedServer,
    net: Network,
    bne_slot: SlotToken,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("chronodownloader.providers.bne.MAX_PDF_CHUNKS", 2)
    server.script(PDF_PATH, pdf())
    result = _download(server, net, bne_slot, tmp_path)
    part = result.part("pdf")
    assert part is not None
    assert part.missing == (chunk_item(51, 75),)
    assert dict(part.causes) == {chunk_item(51, 75): CHUNK_LIMIT}
    assert len(_pdf_requests(server)) == 2


def test_the_end_of_work_500_is_neither_retried_nor_a_breaker_failure(
    server: ScriptedServer, coord: Coordinator, bne_slot: SlotToken, tmp_path: Path
) -> None:
    server.script(PDF_PATH, pdf(), Reply(500))
    settings = settings_with(KEY, max_attempts=5, circuit_breaker_threshold=1)
    with Network(settings, coord, sleep=lambda _s: None) as client:
        _download(server, client, bne_slot, tmp_path)
    assert _pdf_requests(server) == [_range(1, 25), _range(26, 50)]
    assert coord.breaker_check(KEY).allows


def test_a_500_on_the_first_range_leaves_nothing_complete(
    server: ScriptedServer, net: Network, bne_slot: SlotToken, tmp_path: Path
) -> None:
    server.script(PDF_PATH, Reply(500))
    result = _download(server, net, bne_slot, tmp_path)
    assert not result.complete


def test_another_failure_leaves_the_range_missing(
    server: ScriptedServer, net: Network, bne_slot: SlotToken, tmp_path: Path
) -> None:
    server.script(PDF_PATH, pdf(), Reply(503))
    result = _download(server, net, bne_slot, tmp_path)
    part = result.part("pdf")
    assert part is not None
    assert part.missing == (chunk_item(26, 50),)
    assert not result.complete
    assert result.stopped is None


def test_resume_skips_present_ranges(
    server: ScriptedServer, net: Network, bne_slot: SlotToken, tmp_path: Path
) -> None:
    server.script(PDF_PATH, pdf(), Reply(500))
    result = _download(
        server,
        net,
        bne_slot,
        tmp_path,
        include_metadata=False,
        present=frozenset({chunk_item(1, 25)}),
    )
    part = result.part("pdf")
    assert part is not None
    assert part.present == (chunk_item(1, 25), chunk_item(26, 50))
    assert _pdf_requests(server) == [_range(26, 50), _range(51, 75)]


def test_page_images_are_not_offered(
    server: ScriptedServer, net: Network, bne_slot: SlotToken, tmp_path: Path
) -> None:
    with pytest.raises(UnsupportedCapability) as excinfo:
        _download(server, net, bne_slot, tmp_path, format="images")
    assert excinfo.value.capability == "images"
    with pytest.raises(UnsupportedCapability):
        Bne().pages(COOKBOOK_UUID, network=net, first_n=3, max_px=800)
    assert server.arrivals == []


# Recorded responses


def test_fixture_search_parses() -> None:
    records = parse_search_bindings(SPARQL_SEARCH_RESPONSE, limit=10)
    assert len(records) == 4
    assert [r.year for r in records] == [1758, 1763, 1845, 1848]
    assert records[2].creators == ()


def test_fixture_see_also_resolves(
    server: ScriptedServer, net: Network, bne_slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/sparql", json_reply(SPARQL_SEE_ALSO_RESPONSE))
    server.script(PDF_PATH, Reply(500))
    result = _download(server, net, bne_slot, tmp_path, item="bimo0001244675")
    assert result.source_id == "a984ca89-2da2-4d68-b979-a996cf9b5eac"


def test_fixture_pdf_range_is_staged(
    server: ScriptedServer, net: Network, bne_slot: SlotToken, tmp_path: Path
) -> None:
    body = (FIXTURES / "pdf_range.pdf").read_bytes()
    server.script(
        PDF_PATH, Reply(200, body, {"Content-Type": "application/pdf"}), Reply(500)
    )
    result = _download(server, net, bne_slot, tmp_path, include_metadata=False)
    (staged,) = result.files
    assert (staged.kind, staged.size) == ("pdf", len(body))


def test_build_returns_the_adapter() -> None:
    adapter = build(Settings())
    assert adapter.key == KEY
    assert adapter.sparql_url == SEARCH_API_URL
