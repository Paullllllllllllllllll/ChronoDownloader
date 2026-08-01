"""Unit tests for the BNE connector's identifier namespaces.

BNE splits its holdings across two services with incompatible identifier
namespaces: the linked-data catalogue at ``datos.bne.es`` (resource ids such
as ``bimo0001244675``, ``bima0000020829``, and authority ids such as
``XX3469244``) and the BNE Digital platform at ``bnedigital.bne.es``, whose
objects are UUID-addressed. An earlier revision spliced the catalogue id into
``https://iiif.bne.es/{id}/manifest``: wrong namespace, and a host that does
not resolve. These tests pin the corrected behaviour.

Every fixture below is a trimmed but verbatim snippet of a live response
recorded on 01.08.2026; the tests themselves never touch the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from api.providers.bne import (
    _ACCENT_FOLDING,
    DIGITAL_BASE_URL,
    PDF_PAGE_CHUNK,
    _build_search_query,
    _catalog_resource_id,
    download_bne_work,
    extract_digital_id,
    fold_accents,
    search_bne,
)

# ---------------------------------------------------------------------------
# Recorded fixtures
# ---------------------------------------------------------------------------

# Recorded from https://datos.bne.es/sparql with the corrected query for
# "arte de cocina": four digitised editions, each carrying an rdfs:seeAlso
# link into BNE Digital. Note that ?id spans several catalogue prefixes
# (bima, bimo, and a bare Alma MMS id) -- none of them usable for download.
SPARQL_SEARCH_RESPONSE: dict[str, Any] = {
    "head": {"link": [], "vars": ["id", "title", "digital", "creator", "date"]},
    "results": {
        "distinct": False,
        "ordered": True,
        "bindings": [
            {
                "id": {
                    "type": "uri",
                    "value": "https://datos.bne.es/resource/bima0000020829",
                },
                "title": {"type": "literal", "value": "Nuevo arte de cocina"},
                "digital": {
                    "type": "uri",
                    "value": (
                        "https://bnedigital.bne.es/bd/card"
                        "?id=e7a1d9e6-51ce-4eea-a25f-11f35ffa6d41"
                    ),
                },
                "creator": {"type": "literal", "value": "Altamiras, Juan"},
                "date": {"type": "literal", "value": "1758"},
            },
            {
                "id": {
                    "type": "uri",
                    "value": "https://datos.bne.es/resource/bima0000070134",
                },
                "title": {
                    "type": "literal",
                    "value": "Arte de cocina, pastelería, vizcochería, y conservería",
                },
                # Language-prefixed card URL: BNE Digital serves both shapes.
                "digital": {
                    "type": "uri",
                    "value": (
                        "https://bnedigital.bne.es/bd/es/card"
                        "?id=ad28ecee-2fb2-45af-9fe2-6fdb3c6a5c90"
                    ),
                },
                "creator": {
                    "type": "literal",
                    "value": "Martínez Montiño, Francisco",
                },
                "date": {"type": "literal", "value": "1763"},
            },
            {
                "id": {
                    "type": "uri",
                    "value": "https://datos.bne.es/resource/bimo0001244675",
                },
                "title": {
                    "type": "literal",
                    "value": "Novísimo arte de cocina o aviso a las cocineras",
                },
                "digital": {
                    "type": "uri",
                    "value": (
                        "https://bnedigital.bne.es/bd/card"
                        "?id=a984ca89-2da2-4d68-b979-a996cf9b5eac"
                    ),
                },
                "date": {"type": "literal", "value": "1845"},
            },
            {
                "id": {
                    "type": "uri",
                    "value": "https://datos.bne.es/resource/991047329279708606",
                },
                "title": {
                    "type": "literal",
                    "value": "Manual popular, o Arte de cocina y medicina doméstica",
                },
                "digital": {
                    "type": "uri",
                    "value": (
                        "https://bnedigital.bne.es/bd/card"
                        "?id=23b896a6-7075-4579-b4fd-1f47c22bf9ac"
                    ),
                },
                "date": {"type": "literal", "value": "1848"},
            },
        ],
    },
}

# Recorded from the *unconstrained* query the connector used to send. Without
# an rdf:type constraint the endpoint mixes authority records (XX...) into the
# bibliographic ones, and no binding carries a digitisation link at all.
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

# Recorded rdfs:seeAlso lookup for the catalogue record bimo0001244675.
SPARQL_SEE_ALSO_RESPONSE: dict[str, Any] = {
    "head": {"link": [], "vars": ["digital"]},
    "results": {
        "distinct": False,
        "ordered": True,
        "bindings": [
            {
                "digital": {
                    "type": "uri",
                    "value": (
                        "https://bnedigital.bne.es/bd/card"
                        "?id=a984ca89-2da2-4d68-b979-a996cf9b5eac"
                    ),
                }
            }
        ],
    },
}

EMPTY_SPARQL_RESPONSE: dict[str, Any] = {
    "head": {"link": [], "vars": ["digital"]},
    "results": {"distinct": False, "ordered": True, "bindings": []},
}

COOKBOOK_UUID = "e7a1d9e6-51ce-4eea-a25f-11f35ffa6d41"


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


class TestSearchQuery:
    """The SPARQL query must select bibliographic, digitised records only."""

    def test_constrains_to_manifestation_class(self) -> None:
        query = _build_search_query("arte de cocina", 8)
        assert "<https://datos.bne.es/def/C1003>" in query

    def test_requires_a_bne_digital_link(self) -> None:
        query = _build_search_query("arte de cocina", 8)
        assert "rdf-schema#seeAlso" in query
        assert f"STRSTARTS(STR(?digital), '{DIGITAL_BASE_URL}/')" in query

    def test_drops_the_dbpedia_author_property(self) -> None:
        """datos.bne.es carries no dbo:author; the OPTIONAL never bound."""
        query = _build_search_query("arte de cocina", 8)
        assert "dbpedia.org/ontology/author" not in query
        assert "<https://datos.bne.es/def/P1011>" in query

    def test_escapes_quotes_in_the_title(self) -> None:
        query = _build_search_query("l'art de cocina", 8)
        assert "l\\'art de cocina" in query


# ---------------------------------------------------------------------------
# Accent folding
# ---------------------------------------------------------------------------


class TestAccentFolding:
    """Unaccented queries must reach accented Spanish titles.

    datos.bne.es offers no accent-insensitive comparison, so a plain
    ``CONTAINS(LCASE(?title), 'novisimo')`` misses "Novísimo arte de cocina"
    (verified live on 01.08.2026: 0 bindings unaccented, 1 accented). Both
    sides of the filter are folded onto ASCII with one shared table.
    """

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

    @pytest.mark.parametrize(("accented", "plain"), _ACCENT_FOLDING)
    def test_every_table_entry_folds_on_both_sides(
        self, accented: str, plain: str
    ) -> None:
        """The Python fold and the SPARQL REPLACE chain share one table."""
        assert fold_accents(accented) == plain
        query = _build_search_query("cocina", 8)
        assert f"'{accented}', '{plain}'" in query

    def test_title_variable_is_wrapped_in_the_folding_construct(self) -> None:
        query = _build_search_query("cocina", 8)
        assert "REPLACE(LCASE(?title)" in query
        assert query.count("REPLACE(") == len(_ACCENT_FOLDING)
        assert "CONTAINS(REPLACE(" in query

    def test_accented_query_is_folded_before_embedding(self) -> None:
        """The literal handed to CONTAINS carries no accents of its own."""
        query = _build_search_query("Novísimo Arte de Cocina", 8)
        assert "'novisimo arte de cocina'))" in query
        assert "novísimo" not in query

    def test_unaccented_query_is_left_alone(self) -> None:
        query = _build_search_query("novisimo arte de cocina", 8)
        assert "'novisimo arte de cocina'))" in query

    def test_folding_does_not_break_quote_escaping(self) -> None:
        """Folding runs before escaping and must not swallow the guard."""
        query = _build_search_query("L'Art de Cocína", 8)
        assert "l\\'art de cocina" in query
        assert "'l'art" not in query

    def test_backslashes_stay_escaped(self) -> None:
        query = _build_search_query("cocina\\española", 8)
        assert "cocina\\\\espanola" in query

    def test_injection_attempt_cannot_close_the_literal(self) -> None:
        query = _build_search_query("x')) . ?s ?p ?o #", 8)
        assert "x\\')) . ?s ?p ?o #" in query
        assert "'x'))" not in query
        # The FILTER/OPTIONAL skeleton is unchanged: still one CONTAINS,
        # one STRSTARTS, two OPTIONAL clauses.
        assert query.count("FILTER(") == 2
        assert query.count("OPTIONAL") == 2

    def test_search_sends_the_folded_query(self) -> None:
        with patch(
            "api.providers.bne.make_request", return_value=SPARQL_SEARCH_RESPONSE
        ) as mock_request:
            search_bne("Novísimo", max_results=1)

        query = mock_request.call_args.kwargs["params"]["query"]
        assert "'novisimo'))" in query


# ---------------------------------------------------------------------------
# search_bne
# ---------------------------------------------------------------------------


class TestSearchBne:
    """Search results must carry the downloadable BNE Digital identifier."""

    def test_source_id_is_the_bne_digital_uuid(self) -> None:
        with patch(
            "api.providers.bne.make_request", return_value=SPARQL_SEARCH_RESPONSE
        ):
            results = search_bne("arte de cocina", max_results=4)

        assert [r.source_id for r in results] == [
            "e7a1d9e6-51ce-4eea-a25f-11f35ffa6d41",
            "ad28ecee-2fb2-45af-9fe2-6fdb3c6a5c90",
            "a984ca89-2da2-4d68-b979-a996cf9b5eac",
            "23b896a6-7075-4579-b4fd-1f47c22bf9ac",
        ]

    def test_catalogue_id_is_kept_but_not_used_as_identifier(self) -> None:
        with patch(
            "api.providers.bne.make_request", return_value=SPARQL_SEARCH_RESPONSE
        ):
            results = search_bne("arte de cocina", max_results=1)

        first = results[0]
        assert first.raw["catalog_id"] == "https://datos.bne.es/resource/bima0000020829"
        assert first.source_id != "bima0000020829"

    def test_item_url_points_at_bne_digital(self) -> None:
        with patch(
            "api.providers.bne.make_request", return_value=SPARQL_SEARCH_RESPONSE
        ):
            results = search_bne("arte de cocina", max_results=4)

        for result in results:
            assert result.item_url is not None
            assert result.item_url.startswith(DIGITAL_BASE_URL)
        # No result may point at the retired IIIF host.
        assert not any("iiif.bne.es" in (r.item_url or "") for r in results)

    def test_creator_and_date_are_carried_through(self) -> None:
        with patch(
            "api.providers.bne.make_request", return_value=SPARQL_SEARCH_RESPONSE
        ):
            results = search_bne("arte de cocina", max_results=2)

        assert results[0].creators == ["Altamiras, Juan"]
        assert results[0].date == "1758"
        assert results[1].creators == ["Martínez Montiño, Francisco"]

    def test_max_results_is_respected(self) -> None:
        with patch(
            "api.providers.bne.make_request", return_value=SPARQL_SEARCH_RESPONSE
        ):
            results = search_bne("arte de cocina", max_results=2)

        assert len(results) == 2

    def test_over_fetches_to_survive_optional_duplicates(self) -> None:
        """OPTIONAL clauses repeat a record per binding, so LIMIT must exceed
        max_results or duplicates would eat the result slots."""
        with patch(
            "api.providers.bne.make_request", return_value=SPARQL_SEARCH_RESPONSE
        ) as mock_request:
            search_bne("arte de cocina", max_results=3)

        query = mock_request.call_args.kwargs["params"]["query"]
        assert "LIMIT 12" in query

    def test_duplicate_bindings_are_de_duplicated(self) -> None:
        doubled = {
            "head": SPARQL_SEARCH_RESPONSE["head"],
            "results": {
                "bindings": [
                    SPARQL_SEARCH_RESPONSE["results"]["bindings"][0],
                    SPARQL_SEARCH_RESPONSE["results"]["bindings"][0],
                    SPARQL_SEARCH_RESPONSE["results"]["bindings"][1],
                ]
            },
        }
        with patch("api.providers.bne.make_request", return_value=doubled):
            results = search_bne("arte de cocina", max_results=5)

        assert len(results) == 2

    def test_bindings_without_a_digital_link_are_dropped(self) -> None:
        """Defence in depth: pre-fix responses mixed in authority records."""
        with patch(
            "api.providers.bne.make_request",
            return_value=SPARQL_UNCONSTRAINED_RESPONSE,
        ):
            results = search_bne("arte de cocina", max_results=5)

        assert results == []

    def test_blocked_endpoint_returns_no_results(self) -> None:
        """A bot-filter 403 reaches the connector as ``None``, not a dict."""
        with patch("api.providers.bne.make_request", return_value=None):
            assert search_bne("arte de cocina") == []


# ---------------------------------------------------------------------------
# Identifier extraction
# ---------------------------------------------------------------------------


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
    """Catalogue ids feed an unescaped SPARQL lookup, so the guard is narrow.

    It must still admit every shape ``search_bne`` actually stores in
    ``raw["catalog_id"]`` -- including the parenthesised MARC organization
    code that datos.bne.es emits for Alma-sourced records.
    """

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
        assert _catalog_resource_id(value) == expected

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
        assert _catalog_resource_id(value) is None


# ---------------------------------------------------------------------------
# download_bne_work
# ---------------------------------------------------------------------------


@pytest.fixture
def _download_env() -> Any:
    """Patch the download surface of the BNE connector."""

    with (
        patch("api.providers.bne.save_json", return_value=None),
        patch("api.providers.bne.budget_exhausted", return_value=False),
        patch("api.providers.bne.get_max_pages", return_value=0),
        patch("api.providers.bne.download_file") as mock_download,
    ):
        yield mock_download


class TestDownloadBneWork:
    """Downloads must go to BNE Digital, in chunked PDF page ranges."""

    def test_downloads_chunked_pdf_ranges(
        self, _download_env: Any, temp_output_dir: str
    ) -> None:
        # Two successful chunks, then the end-of-work HTTP 500 (None).
        _download_env.side_effect = ["/tmp/a.pdf", "/tmp/b.pdf", None]

        assert download_bne_work({"id": COOKBOOK_UUID}, temp_output_dir) is True

        urls = [call.args[0] for call in _download_env.call_args_list]
        assert urls == [
            f"{DIGITAL_BASE_URL}/bd/es/pdf?id={COOKBOOK_UUID}&page=1-25",
            f"{DIGITAL_BASE_URL}/bd/es/pdf?id={COOKBOOK_UUID}&page=26-50",
            f"{DIGITAL_BASE_URL}/bd/es/pdf?id={COOKBOOK_UUID}&page=51-75",
        ]

    def test_chunk_never_exceeds_the_server_page_limit(
        self, _download_env: Any, temp_output_dir: str
    ) -> None:
        """BNE Digital answers ranges above 25 pages with HTTP 500."""
        _download_env.side_effect = ["/tmp/a.pdf", None]

        download_bne_work({"id": COOKBOOK_UUID}, temp_output_dir)

        first_url = _download_env.call_args_list[0].args[0]
        start, _, end = first_url.rsplit("page=", 1)[1].partition("-")
        assert int(end) - int(start) + 1 == PDF_PAGE_CHUNK

    def test_never_contacts_the_retired_iiif_host(
        self, _download_env: Any, temp_output_dir: str
    ) -> None:
        """iiif.bne.es has no DNS record; nothing may address it."""
        _download_env.side_effect = ["/tmp/a.pdf", None]

        download_bne_work({"id": COOKBOOK_UUID}, temp_output_dir)

        urls = [call.args[0] for call in _download_env.call_args_list]
        assert all("iiif.bne.es" not in url for url in urls)

    def test_stops_at_the_configured_page_ceiling(self, temp_output_dir: str) -> None:
        with (
            patch("api.providers.bne.save_json", return_value=None),
            patch("api.providers.bne.budget_exhausted", return_value=False),
            patch("api.providers.bne.get_max_pages", return_value=10),
            patch(
                "api.providers.bne.download_file", return_value="/tmp/a.pdf"
            ) as mock_download,
        ):
            assert download_bne_work({"id": COOKBOOK_UUID}, temp_output_dir) is True

        urls = [call.args[0] for call in mock_download.call_args_list]
        assert urls == [f"{DIGITAL_BASE_URL}/bd/es/pdf?id={COOKBOOK_UUID}&page=1-10"]

    def test_respects_an_exhausted_download_budget(self, temp_output_dir: str) -> None:
        with (
            patch("api.providers.bne.save_json", return_value=None),
            patch("api.providers.bne.budget_exhausted", return_value=True),
            patch("api.providers.bne.get_max_pages", return_value=0),
            patch("api.providers.bne.download_file") as mock_download,
        ):
            result = download_bne_work({"id": COOKBOOK_UUID}, temp_output_dir)

        assert result is False
        mock_download.assert_not_called()

    def test_resolves_a_catalogue_id_through_see_also(
        self, _download_env: Any, temp_output_dir: str
    ) -> None:
        """A datos.bne.es record is translated into the digital namespace."""
        _download_env.side_effect = ["/tmp/a.pdf", None]

        with patch(
            "api.providers.bne.make_request", return_value=SPARQL_SEE_ALSO_RESPONSE
        ) as mock_request:
            assert download_bne_work({"id": "bimo0001244675"}, temp_output_dir) is True

        query = mock_request.call_args.kwargs["params"]["query"]
        assert "https://datos.bne.es/resource/bimo0001244675" in query
        first_url = _download_env.call_args_list[0].args[0]
        assert "id=a984ca89-2da2-4d68-b979-a996cf9b5eac" in first_url

    def test_authority_record_without_a_digital_copy_fails_cleanly(
        self, _download_env: Any, temp_output_dir: str
    ) -> None:
        with patch(
            "api.providers.bne.make_request", return_value=EMPTY_SPARQL_RESPONSE
        ):
            result = download_bne_work({"id": "XX3469244"}, temp_output_dir)

        assert result is False
        _download_env.assert_not_called()

    def test_missing_identifier_fails_cleanly(
        self, _download_env: Any, temp_output_dir: str
    ) -> None:
        assert download_bne_work({}, temp_output_dir) is False
        _download_env.assert_not_called()
