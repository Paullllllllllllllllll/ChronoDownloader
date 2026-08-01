"""Integration tests for provider API modules with mocked responses."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from api.model import SearchResult


class TestInternetArchiveProvider:
    """Integration tests for Internet Archive provider."""

    def test_search_returns_results(
        self, mock_ia_search_response: dict[str, Any]
    ) -> None:
        """Test that search returns SearchResult objects."""
        with patch(
            "api.providers.internet_archive.make_request",
            return_value=mock_ia_search_response,
        ):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("The Art of Cooking")

            assert len(results) == 2
            assert all(isinstance(r, SearchResult) for r in results)

    def test_search_extracts_metadata(
        self, mock_ia_search_response: dict[str, Any]
    ) -> None:
        """Test that search correctly extracts metadata."""
        with patch(
            "api.providers.internet_archive.make_request",
            return_value=mock_ia_search_response,
        ):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("The Art of Cooking")

            first = results[0]
            assert first.provider == "Internet Archive"
            assert first.title == "The Art of Cooking"
            assert first.source_id == "artofcooking1850"
            assert "John Smith" in first.creators

    def test_search_builds_item_url(
        self, mock_ia_search_response: dict[str, Any]
    ) -> None:
        """Test that item URL is constructed correctly."""
        with patch(
            "api.providers.internet_archive.make_request",
            return_value=mock_ia_search_response,
        ):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("The Art of Cooking")

            assert (
                results[0].raw["item_url"]
                == "https://archive.org/details/artofcooking1850"
            )

    def test_search_with_creator(self, mock_ia_search_response: dict[str, Any]) -> None:
        """Test search with creator parameter."""
        with patch(
            "api.providers.internet_archive.make_request",
            return_value=mock_ia_search_response,
        ) as mock:
            from api.providers.internet_archive import search_internet_archive

            search_internet_archive("The Art of Cooking", creator="John Smith")

            # Verify the query included creator
            call_args = mock.call_args
            assert "creator" in str(call_args)

    def test_search_empty_response(self) -> None:
        """Test search with empty response."""
        empty_response: dict[str, Any] = {"response": {"docs": []}}
        with patch(
            "api.providers.internet_archive.make_request", return_value=empty_response
        ):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("Nonexistent Title")

            assert results == []

    def test_search_none_response(self) -> None:
        """Test search with None response."""
        with patch("api.providers.internet_archive.make_request", return_value=None):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("Title")

            assert results == []

    def test_search_max_results(self, mock_ia_search_response: dict[str, Any]) -> None:
        """Test that max_results parameter is passed."""
        with patch(
            "api.providers.internet_archive.make_request",
            return_value=mock_ia_search_response,
        ) as mock:
            from api.providers.internet_archive import search_internet_archive

            search_internet_archive("Title", max_results=5)

            call_args = mock.call_args
            params = call_args[1].get("params", {})
            assert params.get("rows") == "5"

    def test_search_handles_null_creator(self) -> None:
        """A present-but-null creator must not raise (join(None) -> TypeError)
        and non-string list entries are coerced rather than crashing. An absent
        creator yields None (no creators), not a literal "N/A" sentinel that
        creator_score would match against."""
        response = {
            "response": {
                "docs": [
                    {"identifier": "a", "title": "T1", "creator": None},
                    {"identifier": "b", "title": "T2", "creator": [123, "X"]},
                ]
            }
        }
        with patch(
            "api.providers.internet_archive.make_request", return_value=response
        ):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("Title")

            assert len(results) == 2
            assert results[0].raw["creators"] == []
            assert results[0].creators == []
            assert results[1].creators == ["123", "X"]

    def test_search_no_year_yields_none_not_na(self) -> None:
        """An absent "year" must yield raw["year"] is None, not the retired
        "N/A" sentinel that would leak into SearchResult.date."""
        response = {
            "response": {
                "docs": [{"identifier": "x1", "title": "T1"}],
            }
        }
        with patch(
            "api.providers.internet_archive.make_request", return_value=response
        ):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("Title")

            assert results[0].raw["year"] is None


class TestGallicaProvider:
    """Integration tests for BnF Gallica provider."""

    def test_search_with_results(self) -> None:
        """Test that search can process results."""
        # Note: The actual Gallica API response format may differ
        # This test verifies the function handles None gracefully
        with patch("api.providers.bnf_gallica.make_request", return_value=None):
            from api.providers.bnf_gallica import search_gallica

            results = search_gallica("cuisine")

            # Should return empty list for None response
            assert results == []

    def test_search_empty_response(self) -> None:
        """Test search with empty response."""
        with patch("api.providers.bnf_gallica.make_request", return_value=None):
            from api.providers.bnf_gallica import search_gallica

            results = search_gallica("nonexistent")

            assert results == []


class TestLocProvider:
    """Integration tests for Library of Congress provider."""

    def test_search_returns_results(self) -> None:
        """Test that search returns SearchResult objects."""
        mock_response = {
            "results": [
                {
                    "id": "http://www.loc.gov/item/12345/",
                    "title": "American Cookbook",
                    "contributor": ["Chef Smith"],
                    "date": "1900",
                }
            ]
        }

        with patch("api.providers.loc.make_request", return_value=mock_response):
            from api.providers.loc import search_loc

            results = search_loc("cookbook")

            assert len(results) >= 1
            assert all(isinstance(r, SearchResult) for r in results)

    def test_search_contributor_names_as_string(self) -> None:
        """A string (not list) contributor_names must not be indexed as
        ``[0]`` (which would take just the first character)."""
        mock_response = {
            "results": [
                {
                    "id": "http://www.loc.gov/item/12345/",
                    "title": "American Cookbook",
                    "contributor_names": "Chef Smith",
                }
            ]
        }
        with patch("api.providers.loc.make_request", return_value=mock_response):
            from api.providers.loc import search_loc

            results = search_loc("cookbook")

            assert results[0].raw["creator"] == "Chef Smith"

    def test_search_malformed_record_does_not_discard_others(self) -> None:
        """One malformed record must not discard already-parsed results."""
        from api.model import convert_to_searchresult as real_convert
        from api.providers import loc

        mock_response = {
            "results": [
                {"id": "http://www.loc.gov/item/1/", "title": "Bad"},
                {"id": "http://www.loc.gov/item/2/", "title": "Good"},
            ]
        }
        calls = {"n": 0}

        def flaky(provider: str, raw: dict[str, Any]) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            return real_convert(provider, raw)

        with (
            patch("api.providers.loc.make_request", return_value=mock_response),
            patch("api.providers.loc.convert_to_searchresult", side_effect=flaky),
        ):
            results = loc.search_loc("cookbook")

            assert len(results) == 1
            assert results[0].raw["id"] == "2"

    def test_rendering_survives_failed_image_fallback(
        self, temp_output_dir: str
    ) -> None:
        """A successful manifest-level PDF/EPUB rendering must not be
        discarded when the manifest has no IIIF image services and the
        subsequent sample-image fallback download also fails."""
        item_json = {
            "item": {
                "resources": [{"iiif_manifest": "https://example.org/manifest.json"}],
                "image_url": "https://example.org/sample.jpg",
            }
        }
        manifest = {"@id": "m"}
        with (
            patch("api.providers.loc.make_request", side_effect=[item_json, manifest]),
            patch("api.providers.loc.save_json", return_value=None),
            patch("api.providers.loc.download_iiif_renderings", return_value=1),
            patch("api.providers.loc.prefer_pdf_over_images", return_value=False),
            patch("api.providers.loc.extract_image_service_bases", return_value=[]),
            patch("api.providers.loc.download_file", return_value=None),
        ):
            from api.providers.loc import download_loc_work

            result = download_loc_work(
                {"item_url": "https://www.loc.gov/item/1/", "id": "1"},
                temp_output_dir,
            )

            assert result is True


class TestMdzProvider:
    """Integration tests for MDZ (Münchener DigitalisierungsZentrum) provider."""

    def test_search_handles_none_response(self) -> None:
        """Test that search handles None response gracefully."""
        with patch("api.providers.mdz.make_request", return_value=None):
            from api.providers.mdz import search_mdz

            results = search_mdz("kochen")

            # Should return empty list for None response
            assert results == []

    def test_search_handles_list_title(self) -> None:
        """A list-valued (highlighted) title must be coerced, not dropped by
        the broad except (which silently yielded zero results)."""
        response = {
            "docs": [{"id": "bsb123", "title": ["Kochbuch"], "iiifAvailable": True}]
        }
        with patch("api.providers.mdz.make_request", return_value=response):
            from api.providers.mdz import search_mdz

            results = search_mdz("kochen")

            assert len(results) == 1
            assert results[0].raw["title"] == "Kochbuch"


class TestEuropeanaProvider:
    """Integration tests for the Europeana provider."""

    def test_search_handles_empty_title_list(self) -> None:
        """A present-but-empty ``title: []`` must not raise IndexError (which
        aborted the whole search); a string dcCreator is used as-is."""
        response = {
            "success": True,
            "items": [
                {"id": "/1/x", "title": [], "dcCreator": "Solo Author"},
                {"id": "/1/y", "title": ["Good Title"]},
            ],
        }
        with (
            patch("api.providers.europeana._api_key", return_value="KEY"),
            patch("api.providers.europeana.make_request", return_value=response),
        ):
            from api.providers.europeana import search_europeana

            results = search_europeana("cookbook")

            assert len(results) == 2
            # A titleless record must carry an empty title, not a
            # sentinel that scores 35+ against short queries.
            assert results[0].raw["title"] == ""
            assert results[0].raw["creator"] == "Solo Author"
            assert results[1].raw["title"] == "Good Title"
            assert results[1].raw["creator"] is None

    def test_search_result_manifest_url_carries_no_api_key(self) -> None:
        """The wskey must not be persisted in search results (it would leak
        into work.json, index.csv and --search output)."""
        response = {"success": True, "items": [{"id": "/1/x", "title": ["T"]}]}
        with (
            patch("api.providers.europeana._api_key", return_value="SECRET"),
            patch("api.providers.europeana.make_request", return_value=response),
        ):
            from api.providers.europeana import search_europeana

            results = search_europeana("cookbook")

            manifest = results[0].raw["iiif_manifest"]
            assert manifest == (
                "https://iiif.europeana.eu/presentation/1/x/manifest?format=3"
            )
            assert "SECRET" not in manifest

    def test_download_appends_api_key_at_fetch_time(self, temp_output_dir: str) -> None:
        """The key-less stored manifest URL must still be authenticated when
        actually fetched."""
        with (
            patch("api.providers.europeana._api_key", return_value="SECRET"),
            patch("api.providers.europeana.save_json", return_value=None),
            patch(
                "api.providers.europeana.make_request", return_value=None
            ) as mock_req,
        ):
            from api.providers.europeana import download_europeana_work

            download_europeana_work(
                {
                    "id": "/1/x",
                    "iiif_manifest": (
                        "https://iiif.europeana.eu/presentation/1/x/manifest?format=3"
                    ),
                },
                temp_output_dir,
            )

            fetched = mock_req.call_args[0][0]
            assert fetched == (
                "https://iiif.europeana.eu/presentation/1/x/manifest"
                "?format=3&wskey=SECRET"
            )

    def test_download_leaves_foreign_manifest_urls_untouched(
        self, temp_output_dir: str
    ) -> None:
        """A provider-hosted manifest neither needs nor understands wskey."""
        with (
            patch("api.providers.europeana._api_key", return_value="SECRET"),
            patch("api.providers.europeana.save_json", return_value=None),
            patch(
                "api.providers.europeana.make_request", return_value=None
            ) as mock_req,
        ):
            from api.providers.europeana import download_europeana_work

            download_europeana_work(
                {
                    "id": "/1/x",
                    "iiif_manifest": "https://example.org/iiif/1/manifest",
                },
                temp_output_dir,
            )

            assert mock_req.call_args[0][0] == "https://example.org/iiif/1/manifest"


class TestSbbDigitalProvider:
    """Integration tests for the SBB (Staatsbibliothek zu Berlin) provider."""

    def test_pdf_loop_honours_max_pages(self, temp_output_dir: str) -> None:
        """Per-page PDF filegroups can hold one PDF per page, so the PDF loop
        must apply the same max_pages cap as the image loop."""
        with (
            patch("api.providers.sbb_digital.make_request", return_value="<mets/>"),
            patch("api.providers.sbb_digital.save_json", return_value=None),
            patch(
                "api.providers.sbb_digital._collect_mets_urls",
                return_value=(["p1", "p2", "p3", "p4"], []),
            ),
            patch("api.providers.sbb_digital.get_max_pages", return_value=2),
            patch("api.providers.sbb_digital.budget_exhausted", return_value=False),
            patch(
                "api.providers.sbb_digital.prefer_pdf_over_images", return_value=False
            ),
            patch(
                "api.providers.sbb_digital.download_file", return_value="/x/f.pdf"
            ) as mock_dl,
        ):
            from api.providers.sbb_digital import download_sbb_digital_work

            result = download_sbb_digital_work({"id": "PPN123"}, temp_output_dir)

            assert result is True
            assert mock_dl.call_count == 2

    def test_pdf_loop_stops_on_exhausted_budget(self, temp_output_dir: str) -> None:
        """The PDF loop must respect the global download budget."""
        with (
            patch("api.providers.sbb_digital.make_request", return_value="<mets/>"),
            patch("api.providers.sbb_digital.save_json", return_value=None),
            patch(
                "api.providers.sbb_digital._collect_mets_urls",
                return_value=(["p1", "p2"], []),
            ),
            patch("api.providers.sbb_digital.get_max_pages", return_value=0),
            patch("api.providers.sbb_digital.budget_exhausted", return_value=True),
            patch(
                "api.providers.sbb_digital.prefer_pdf_over_images", return_value=False
            ),
            patch(
                "api.providers.sbb_digital.download_file", return_value="/x/f.pdf"
            ) as mock_dl,
        ):
            from api.providers.sbb_digital import download_sbb_digital_work

            result = download_sbb_digital_work({"id": "PPN123"}, temp_output_dir)

            assert result is False
            mock_dl.assert_not_called()

    METS_MULTI_FILEGRP = (
        '<?xml version="1.0"?>'
        '<mets:mets xmlns:mets="http://www.loc.gov/METS/" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<mets:fileSec><mets:fileGrp USE="THUMBS">'
        '<mets:file MIMETYPE="image/jpeg">'
        '<mets:FLocat xlink:href="https://x/t1.jpg"/></mets:file>'
        '<mets:file MIMETYPE="image/jpeg">'
        '<mets:FLocat xlink:href="https://x/t2.jpg"/></mets:file>'
        "</mets:fileGrp>"
        '<mets:fileGrp USE="MAX">'
        '<mets:file MIMETYPE="image/jpeg">'
        '<mets:FLocat xlink:href="https://x/m1.jpg"/></mets:file>'
        '<mets:file MIMETYPE="image/jpeg">'
        '<mets:FLocat xlink:href="https://x/m2.jpg"/></mets:file>'
        "</mets:fileGrp></mets:fileSec></mets:mets>"
    )

    def test_mets_collects_one_filegrp_not_every_resolution(self) -> None:
        """A METS file section holds one fileGrp per derivative. Collecting
        across all of them fetched every page three to five times over and
        spent max_pages on thumbnails, which come first in document order."""
        from api.providers.sbb_digital import _collect_mets_urls

        _pdfs, images = _collect_mets_urls(self.METS_MULTI_FILEGRP)

        assert images == ["https://x/m1.jpg", "https://x/m2.jpg"]

    def test_mets_without_use_labels_keeps_every_image(self) -> None:
        """An unlabeled fileGrp cannot be ranked, so nothing is dropped."""
        from api.providers.sbb_digital import _collect_mets_urls

        mets = (
            '<?xml version="1.0"?>'
            '<mets:mets xmlns:mets="http://www.loc.gov/METS/" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            "<mets:fileSec><mets:fileGrp>"
            '<mets:file MIMETYPE="image/jpeg">'
            '<mets:FLocat xlink:href="https://x/a.jpg"/></mets:file>'
            '<mets:file MIMETYPE="application/pdf">'
            '<mets:FLocat xlink:href="https://x/a.pdf"/></mets:file>'
            "</mets:fileGrp></mets:fileSec></mets:mets>"
        )

        pdfs, images = _collect_mets_urls(mets)

        assert pdfs == ["https://x/a.pdf"]
        assert images == ["https://x/a.jpg"]


class TestHathiTrustProvider:
    """Integration tests for the HathiTrust Bibliographic API provider."""

    def test_search_extracts_htid_from_top_level_items(self) -> None:
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
        with patch("api.providers.hathitrust.make_request", return_value=response):
            from api.providers.hathitrust import search_hathitrust

            results = search_hathitrust("Cookery oclc:12345")

            assert len(results) == 1
            first = results[0]
            assert first.raw["htid"] == "uc1.b1234567"
            assert (
                first.raw["item_url"]
                == "https://babel.hathitrust.org/cgi/pt?id=uc1.b1234567"
            )
            # Identifier must be the htid, not the 9-digit record number.
            assert first.source_id == "uc1.b1234567"


class TestDdbProvider:
    """Integration tests for the DDB provider."""

    def test_search_handles_non_list_view(self) -> None:
        """A dict-valued (or too-short) "view" must not raise IndexError and
        abort the whole search loop; creator falls back to None."""
        response = {
            "results": [
                {
                    "docs": [
                        {"id": "abc", "label": "Kochbuch", "view": {"unexpected": 1}},
                        {"id": "def", "label": "Backbuch", "view": ["a", "b"]},
                    ]
                }
            ]
        }
        with (
            patch("api.providers.ddb._api_key", return_value="KEY"),
            patch("api.providers.ddb.make_request", return_value=response),
        ):
            from api.providers.ddb import search_ddb

            results = search_ddb("cookbook")

            assert len(results) == 2
            assert results[0].raw["title"] == "Kochbuch"
            assert results[0].raw["creator"] is None
            assert results[1].raw["creator"] is None

    def test_search_handles_list_label(self) -> None:
        """A list-valued label/title must not make .replace raise AttributeError."""
        response = {
            "results": [
                {
                    "docs": [
                        {"id": "abc", "label": ["<match>Koch</match>buch"]},
                        {"id": "def", "label": [], "title": "Backbuch"},
                    ]
                }
            ]
        }
        with (
            patch("api.providers.ddb._api_key", return_value="KEY"),
            patch("api.providers.ddb.make_request", return_value=response),
        ):
            from api.providers.ddb import search_ddb

            results = search_ddb("cookbook")

            assert len(results) == 2
            assert results[0].raw["title"] == "Kochbuch"
            assert results[1].raw["title"] == "Backbuch"


class TestDplaProvider:
    """Integration tests for the DPLA provider."""

    def test_search_malformed_record_does_not_discard_others(self) -> None:
        """One malformed record must not discard already-parsed results."""
        from api.model import convert_to_searchresult as real_convert
        from api.providers import dpla

        response = {
            "docs": [
                {"id": "bad", "sourceResource": {"title": "Bad"}},
                {"id": "good", "sourceResource": {"title": "Good"}},
            ]
        }
        calls = {"n": 0}

        def flaky(provider: str, raw: dict[str, Any]) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            return real_convert(provider, raw)

        with (
            patch("api.providers.dpla._api_key", return_value="KEY"),
            patch("api.providers.dpla.make_request", return_value=response),
            patch("api.providers.dpla.convert_to_searchresult", side_effect=flaky),
        ):
            results = dpla.search_dpla("cookbook")

            assert len(results) == 1
            assert results[0].raw["id"] == "good"

    def test_search_empty_creators_yields_none_not_empty_string(self) -> None:
        """An item with no creators must yield no creators at all, not the
        empty-string artifact of joining an empty list."""
        response = {
            "docs": [{"id": "x1", "sourceResource": {"title": "T1", "creator": []}}]
        }
        with (
            patch("api.providers.dpla._api_key", return_value="KEY"),
            patch("api.providers.dpla.make_request", return_value=response),
        ):
            from api.providers.dpla import search_dpla

            results = search_dpla("cookbook")

            assert results[0].raw["creators"] == []
            assert results[0].creators == []


class TestBudgetGuards:
    """Regression tests for missing download-budget guards on IIIF page loops."""

    def test_bne_pdf_loop_stops_on_exhausted_budget(self, temp_output_dir: str) -> None:
        """The BNE PDF page-range loop must respect the global download
        budget."""
        with (
            patch("api.providers.bne.save_json", return_value=None),
            patch("api.providers.bne.get_max_pages", return_value=0),
            patch("api.providers.bne.budget_exhausted", return_value=True),
            patch("api.providers.bne.download_file") as mock_dl,
        ):
            from api.providers.bne import download_bne_work

            result = download_bne_work(
                {"id": "a984ca89-2da2-4d68-b979-a996cf9b5eac"}, temp_output_dir
            )

            assert result is False
            mock_dl.assert_not_called()


class TestGoogleBooksProvider:
    """Integration tests for the Google Books provider."""

    def test_author_query_uses_space_not_plus(self) -> None:
        """Field clauses must be space-separated: a literal '+' is URL-encoded
        to %2B by requests, nullifying the combined title+author query."""
        with patch(
            "api.providers.google_books.make_request", return_value=None
        ) as mock:
            from api.providers.google_books import search_google_books

            search_google_books("Cookery", creator="Glasse")

            first_q = mock.call_args_list[0].kwargs["params"]["q"]
            assert "+inauthor" not in first_q
            assert 'inauthor:"Glasse"' in first_q

    def test_search_handles_null_and_non_string_authors(self) -> None:
        """A present-but-null "authors" (or one holding non-strings) must not
        make the join raise TypeError and abort the whole search."""
        response = {
            "items": [
                {
                    "id": "g1",
                    "volumeInfo": {"title": "T1", "authors": None},
                    "accessInfo": {"publicDomain": True},
                },
                {
                    "id": "g2",
                    "volumeInfo": {"title": "T2", "authors": [123, "X"]},
                    "accessInfo": {"publicDomain": True},
                },
            ]
        }
        with patch("api.providers.google_books.make_request", return_value=response):
            from api.providers.google_books import search_google_books

            results = search_google_books("Cookery")

            assert len(results) == 2
            # An absent author list yields no creators. Joining into "" left
            # creators == [""], a phantom author persisted to the ledgers.
            assert results[0].raw["creators"] == []
            assert results[0].creators == []
            assert results[1].creators == ["123", "X"]


class TestBritishLibraryProvider:
    """Integration tests for the British Library provider."""

    SRU_NO_ARK = (
        '<?xml version="1.0"?>'
        '<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">'
        "<records><record><recordData>"
        '<dc xmlns="http://purl.org/dc/elements/1.1/">'
        "<title>Cookery</title><creator>Anon</creator>"
        "<identifier>http://example.org/not-an-ark</identifier>"
        "</dc></recordData></record></records>"
        "</searchRetrieveResponse>"
    )

    def test_sru_records_without_ark_fall_through_to_sparql(self) -> None:
        """An identifier-less SRU record is undownloadable (source_id=None) and
        must not suppress the BNB SPARQL fallback."""
        sparql_response = {
            "results": {
                "bindings": [
                    {
                        "title": {"value": "Cookery"},
                        "same": {"value": "http://bnb.example/ark:/81055/vdc_0001"},
                    }
                ]
            }
        }
        with patch(
            "api.providers.british_library.make_request",
            side_effect=[self.SRU_NO_ARK, sparql_response],
        ):
            from api.providers.british_library import search_british_library

            results = search_british_library("Cookery")

            assert len(results) == 1
            assert results[0].raw["source"] == "bnb_sparql"
            assert results[0].source_id == "vdc_0001"

    def test_malformed_record_does_not_discard_earlier_results(self) -> None:
        """One bad record must not throw away records already parsed."""
        from api.providers import british_library

        good = (
            '<?xml version="1.0"?>'
            '<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">'
            "<records>"
            "<record><recordData>"
            '<dc xmlns="http://purl.org/dc/elements/1.1/">'
            "<title>A</title><identifier>ark:/81055/vdc_A</identifier>"
            "</dc></recordData></record>"
            "<record><recordData>"
            '<dc xmlns="http://purl.org/dc/elements/1.1/">'
            "<title>B</title><identifier>ark:/81055/vdc_B</identifier>"
            "</dc></recordData></record>"
            "</records></searchRetrieveResponse>"
        )

        from api.model import convert_to_searchresult as real_convert

        calls = {"n": 0}

        def flaky(provider: str, raw: dict[str, Any]) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            return real_convert(provider, raw)

        with (
            patch("api.providers.british_library.make_request", return_value=good),
            patch(
                "api.providers.british_library.convert_to_searchresult",
                side_effect=flaky,
            ),
        ):
            results = british_library.search_british_library("Cookery")

            assert len(results) == 1
            assert results[0].source_id == "vdc_B"

    def test_sru_record_without_creator_has_none_not_na(self) -> None:
        """An SRU record with no <dc:creator> element must yield
        raw["creator"] is None, not the retired "N/A" sentinel."""
        xml = (
            '<?xml version="1.0"?>'
            '<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">'
            "<records><record><recordData>"
            '<dc xmlns="http://purl.org/dc/elements/1.1/">'
            "<title>Cookery</title>"
            "<identifier>ark:/81055/vdc_X</identifier>"
            "</dc></recordData></record></records>"
            "</searchRetrieveResponse>"
        )
        with patch("api.providers.british_library.make_request", return_value=xml):
            from api.providers.british_library import search_british_library

            results = search_british_library("Cookery")

            assert len(results) == 1
            assert results[0].raw["creator"] is None

    def test_viewer_fallback_runs_when_manifest_endpoint_returns_html(
        self, temp_output_dir: str
    ) -> None:
        """make_request returns a str for an HTML body, and api.bl.uk answers
        with an error page rather than a status code. A truthiness test on the
        result skipped the viewer fallback in exactly that case."""
        manifest: dict[str, Any] = {"sequences": []}
        responses: list[Any] = [
            "<html>Service unavailable</html>",
            '<html>var m = "https://api.bl.uk/x/manifest.json";</html>',
            manifest,
        ]
        with (
            patch(
                "api.providers.british_library.make_request", side_effect=responses
            ) as mock_req,
            patch("api.providers.british_library.save_json", return_value=None),
            patch(
                "api.providers.british_library.download_iiif_renderings", return_value=0
            ),
            patch(
                "api.providers.british_library.extract_image_service_bases",
                return_value=[],
            ),
        ):
            from api.providers.british_library import download_british_library_work

            download_british_library_work({"identifier": "vdc_X"}, temp_output_dir)

            # Three calls: the failing manifest endpoint, the viewer page, and
            # the manifest discovered inside it.
            assert mock_req.call_count == 3


class TestERaraProvider:
    """Integration tests for the e-rara provider."""

    def test_build_query_joins_terms_with_and(self) -> None:
        """Two quoted CQL terms separated by a bare space are invalid CQL."""
        from api.providers.e_rara import _build_query

        query = _build_query("Kochbuch", "Rumpolt")

        assert query == '"Kochbuch" and "Rumpolt"'

    def test_build_query_single_term_unchanged(self) -> None:
        from api.providers.e_rara import _build_query

        assert _build_query("Kochbuch", None) == '"Kochbuch"'


class TestProviderRegistry:
    """Tests for the providers registry."""

    def test_all_providers_registered(self) -> None:
        """Test that all expected providers are registered."""
        from api.providers import PROVIDERS

        expected_providers = [
            "bnf_gallica",
            "internet_archive",
            "loc",
            "europeana",
            "dpla",
            "ddb",
            "british_library",
            "mdz",
            "polona",
            "bne",
            "google_books",
            "hathitrust",
            "wellcome",
            "annas_archive",
        ]

        for provider in expected_providers:
            assert provider in PROVIDERS, f"Provider {provider} not in registry"

    def test_provider_tuple_structure(self) -> None:
        """Test that each provider has correct tuple structure."""
        from api.providers import PROVIDERS

        for key, value in PROVIDERS.items():
            assert isinstance(value, tuple), f"{key} value is not a tuple"
            assert len(value) == 3, f"{key} tuple has wrong length"

            search_fn, download_fn, name = value
            assert callable(search_fn), f"{key} search function not callable"
            assert callable(download_fn), f"{key} download function not callable"
            assert isinstance(name, str), f"{key} name is not a string"

    def test_provider_names_are_display_friendly(self) -> None:
        """Test that provider names are human-readable."""
        from api.providers import PROVIDERS

        for key, (_, _, name) in PROVIDERS.items():
            # Name should be title-cased or properly formatted
            assert name, f"{key} has empty name"
            # Name should not be snake_case
            assert "_" not in name or name == "Anna's Archive", (
                f"{key} name appears to be snake_case"
            )


class TestDownloadFunctions:
    """Tests for provider download functions with mocked responses."""

    def test_ia_download_with_no_identifier(self, temp_output_dir: str) -> None:
        """Test IA download returns False when no identifier."""
        from api.providers.internet_archive import download_ia_work

        result = download_ia_work({}, temp_output_dir)

        assert result is False

    def test_ia_download_with_search_result(
        self, temp_output_dir: str, sample_search_result: Any
    ) -> None:
        """Test IA download with SearchResult object."""
        mock_metadata = {
            "metadata": {
                "identifier": "artofcooking1850",
                "title": "The Art of Cooking",
            },
            "files": [],
        }

        with (
            patch(
                "api.providers.internet_archive.make_request",
                return_value=mock_metadata,
            ),
            patch(
                "api.providers.internet_archive.save_json",
                return_value="/path/to/saved.json",
            ),
        ):
            from api.providers.internet_archive import download_ia_work

            # Note: This will return False as there are no downloadable files
            # but it should not raise an error
            result = download_ia_work(sample_search_result, temp_output_dir)

            # With empty files list, should return False
            assert result is False

    def test_ia_search_handles_string_creator(self) -> None:
        """BUG-2: a single-string creator must not be split into characters."""
        resp = {
            "response": {
                "docs": [
                    {
                        "identifier": "x1",
                        "title": "T",
                        "creator": "Jane Doe",
                        "year": "1900",
                    }
                ]
            }
        }
        with patch("api.providers.internet_archive.make_request", return_value=resp):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("T")

            assert results[0].creators == ["Jane Doe"]

    def test_ia_search_handles_list_creator(self) -> None:
        """A list creator is carried through as a list of creators."""
        resp = {
            "response": {
                "docs": [
                    {
                        "identifier": "x2",
                        "title": "T",
                        "creator": ["A", "B"],
                        "year": "1900",
                    }
                ]
            }
        }
        with patch("api.providers.internet_archive.make_request", return_value=resp):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("T")

            assert results[0].creators == ["A", "B"]

    def test_ia_download_thumbnail_only_returns_false(
        self, temp_output_dir: str
    ) -> None:
        """BUG-8: a thumbnail alone must not count as a completed download."""
        metadata = {"files": [{"name": "cover_thumb.jpg", "format": "Thumbnail"}]}
        with (
            patch("api.providers.internet_archive.make_request", return_value=metadata),
            patch("api.providers.internet_archive.save_json", return_value=None),
            patch(
                "api.providers.internet_archive.download_file",
                return_value="/x/thumb.jpg",
            ),
            patch(
                "api.providers.internet_archive.download_iiif_renderings",
                return_value=0,
            ),
            patch(
                "api.providers.internet_archive.extract_image_service_bases",
                return_value=[],
            ),
        ):
            from api.providers.internet_archive import download_ia_work

            result = download_ia_work({"identifier": "id1"}, temp_output_dir)

            assert result is False

    def test_ia_download_skips_djvu_text_derivatives(
        self, temp_output_dir: str
    ) -> None:
        """A substring test against "format" matched DjVuTXT and Djvu XML.
        Those extensions are disallowed objects, so each was streamed in
        full, filed under metadata/ and reported as a failure -- the whole
        derivative family downloaded and discarded."""
        metadata = {
            "files": [
                {"name": "id1_djvu.txt", "format": "DjVuTXT"},
                {"name": "id1_djvu.xml", "format": "Djvu XML"},
                {"name": "id1.pdf", "format": "Text PDF"},
            ]
        }
        requested: list[str] = []

        def _fake_download(url: str, folder: str, stem: str) -> str:
            requested.append(url)
            return "/x/f.pdf"

        with (
            patch("api.providers.internet_archive.make_request", return_value=metadata),
            patch("api.providers.internet_archive.save_json", return_value=None),
            patch(
                "api.providers.internet_archive.prefer_pdf_over_images",
                return_value=False,
            ),
            patch(
                "api.providers.internet_archive.download_file",
                side_effect=_fake_download,
            ),
        ):
            from api.providers.internet_archive import download_ia_work

            result = download_ia_work({"identifier": "id1"}, temp_output_dir)

            assert result is True
            # Exactly one primary object: preferred_exts is a priority order,
            # not a shopping list, so the EPUB/DjVu passes must not run once a
            # PDF has landed -- even with prefer_pdf_over_images disabled.
            assert len(requested) == 1
            assert requested[0].endswith("/id1.pdf")

    def test_gallica_download_no_content_returns_false(
        self, temp_output_dir: str
    ) -> None:
        """BUG-1: a manifest with no renderings and no image services is not success."""
        manifest = {"@id": "m"}
        with (
            patch("api.providers.bnf_gallica.make_request", return_value=manifest),
            patch("api.providers.bnf_gallica.save_json", return_value=None),
            patch("api.providers.bnf_gallica.download_iiif_renderings", return_value=0),
            patch(
                "api.providers.bnf_gallica.extract_image_service_bases",
                return_value=[],
            ),
        ):
            from api.providers.bnf_gallica import download_gallica_work

            result = download_gallica_work({"ark_id": "bpt6k123"}, temp_output_dir)

            assert result is False

    def test_gallica_download_renderings_only_returns_true(
        self, temp_output_dir: str
    ) -> None:
        """A downloaded rendering with no image services still counts as success."""
        manifest = {"@id": "m"}
        with (
            patch("api.providers.bnf_gallica.make_request", return_value=manifest),
            patch("api.providers.bnf_gallica.save_json", return_value=None),
            patch("api.providers.bnf_gallica.download_iiif_renderings", return_value=1),
            patch(
                "api.providers.bnf_gallica.extract_image_service_bases",
                return_value=[],
            ),
            patch(
                "api.providers.bnf_gallica.prefer_pdf_over_images", return_value=False
            ),
        ):
            from api.providers.bnf_gallica import download_gallica_work

            result = download_gallica_work({"ark_id": "bpt6k123"}, temp_output_dir)

            assert result is True

    def test_gallica_rendering_survives_failed_image_downloads(
        self, temp_output_dir: str
    ) -> None:
        """A successfully downloaded PDF/EPUB rendering must not be discarded
        when the subsequent image downloads all fail.

        The per-page loop lives in ``api.iiif._strategies.download_page_images``,
        so the failing per-service download is pinned there.
        """
        manifest = {"@id": "m"}
        with (
            patch("api.providers.bnf_gallica.make_request", return_value=manifest),
            patch("api.providers.bnf_gallica.save_json", return_value=None),
            patch("api.providers.bnf_gallica.download_iiif_renderings", return_value=1),
            patch(
                "api.providers.bnf_gallica.extract_image_service_bases",
                return_value=["https://svc/1", "https://svc/2"],
            ),
            patch(
                "api.providers.bnf_gallica.prefer_pdf_over_images", return_value=False
            ),
            patch("api.iiif._strategies.budget_exhausted", return_value=False),
            patch(
                "api.iiif._strategies.download_one_from_service",
                return_value=False,
            ),
        ):
            from api.providers.bnf_gallica import download_gallica_work

            result = download_gallica_work({"ark_id": "bpt6k123"}, temp_output_dir)

            assert result is True

    def test_loc_fallback_handles_list_image_url(self, temp_output_dir: str) -> None:
        """LoC commonly returns image_url as a list ordered by increasing
        resolution; the last entry must be used rather than the list ignored."""
        item_json = {
            "item": {
                "image_url": [
                    "//tile.loc.gov/image/small.jpg",
                    "//tile.loc.gov/image/large.jpg",
                ]
            }
        }
        with (
            patch("api.providers.loc.make_request", return_value=item_json),
            patch("api.providers.loc.save_json", return_value=None),
            patch(
                "api.providers.loc.download_file", return_value="/x/img.jpg"
            ) as mock_dl,
        ):
            from api.providers.loc import download_loc_work

            result = download_loc_work(
                {"item_url": "https://www.loc.gov/item/1/", "id": "1"},
                temp_output_dir,
            )

            assert result is True
            assert mock_dl.call_args[0][0] == "https://tile.loc.gov/image/large.jpg"

    def test_hathitrust_download_without_api_key_returns_false(
        self, temp_output_dir: str
    ) -> None:
        """BUG-1: no page image downloaded (no API key) is not a completed work."""
        with (
            patch("api.providers.hathitrust._api_key", return_value=None),
            patch("api.providers.hathitrust.save_json", return_value=None),
        ):
            from api.providers.hathitrust import download_hathitrust_work

            result = download_hathitrust_work(
                {"htid": "abc", "bib": {"x": 1}}, temp_output_dir
            )

            assert result is False


class TestQuotedQueryEscaping:
    """Embedded double quotes must not break quoted query phrases."""

    def test_ia_strips_quotes_from_title_and_creator(self) -> None:
        with patch(
            "api.providers.internet_archive.make_request", return_value=None
        ) as mock:
            from api.providers.internet_archive import search_internet_archive

            search_internet_archive('Der "wahre" Koch', creator='Jean "le" Bon')

            q = mock.call_args[1].get("params", {}).get("q", "")
            assert 'title:("Der  wahre  Koch")' in q
            assert 'creator:("Jean  le  Bon")' in q

    def test_europeana_strips_quotes_from_title_and_creator(self) -> None:
        with (
            patch("api.providers.europeana._api_key", return_value="key"),
            patch("api.providers.europeana.make_request", return_value=None) as mock,
        ):
            from api.providers.europeana import search_europeana

            search_europeana('Der "wahre" Koch', creator='Jean "le" Bon')

            query = mock.call_args[1].get("params", {}).get("query", "")
            assert 'title:"Der  wahre  Koch"' in query
            assert 'who:"Jean  le  Bon"' in query

    def test_ddb_strips_quotes_from_title_and_creator(self) -> None:
        with (
            patch("api.providers.ddb._api_key", return_value="key"),
            patch("api.providers.ddb.make_request", return_value=None) as mock,
        ):
            from api.providers.ddb import search_ddb

            search_ddb('Der "wahre" Koch', creator='Jean "le" Bon')

            query = mock.call_args[1].get("params", {}).get("query", "")
            assert '"Der  wahre  Koch"' in query
            assert 'creator:"Jean  le  Bon"' in query

    def test_google_books_strips_quotes_in_strict_variant(self) -> None:
        with patch(
            "api.providers.google_books.make_request", return_value=None
        ) as mock:
            from api.providers.google_books import search_google_books

            search_google_books('Der "wahre" Koch', creator='Jean "le" Bon')

            first_q = mock.call_args_list[0][1].get("params", {}).get("q", "")
            assert 'intitle:"Der  wahre  Koch"' in first_q
            assert 'inauthor:"Jean  le  Bon"' in first_q


class TestSearchResultScoring:
    """Tests for search result scoring integration."""

    def test_attach_scores_to_search_result(self, sample_search_result: Any) -> None:
        """Test that scores can be attached to SearchResult."""
        from api.matching import creator_score, title_score

        query_title = "The Art of Cooking"
        query_creator = "John Smith"

        ts = title_score(query_title, sample_search_result.title)
        cs = creator_score(query_creator, sample_search_result.creators)

        # Attach scores to raw dict
        sample_search_result.raw["__matching__"] = {
            "score": ts,
            "creator_score": cs,
            "total": ts * 0.8 + cs * 0.2,
        }

        assert sample_search_result.raw["__matching__"]["score"] == 100
        assert sample_search_result.raw["__matching__"]["creator_score"] == 100


class TestPageImageLoopConsolidation:
    """The per-page image loop is shared, not copied.

    Each connector below hands ``api.iiif.download_page_images`` its own
    provider key, which drives both the ``{key}_{id}_p{index:05d}.jpg``
    filename shape and the ``get_max_pages(key)`` config lookup. The loop
    invariants themselves (page cap, budget stop, per-page recovery, filename
    shape) are pinned in tests/unit/test_download_helpers.py.
    """

    MANIFEST = {"@id": "m"}
    SERVICES = ["https://svc/1", "https://svc/2"]

    def test_gallica_uses_shared_loop_with_gallica_key(
        self, temp_output_dir: str
    ) -> None:
        with (
            patch("api.providers.bnf_gallica.make_request", return_value=self.MANIFEST),
            patch("api.providers.bnf_gallica.save_json", return_value=None),
            patch("api.providers.bnf_gallica.download_iiif_renderings", return_value=0),
            patch(
                "api.providers.bnf_gallica.extract_image_service_bases",
                return_value=self.SERVICES,
            ),
            patch(
                "api.providers.bnf_gallica.download_page_images", return_value=True
            ) as mock_pages,
        ):
            from api.providers.bnf_gallica import download_gallica_work

            assert download_gallica_work({"ark_id": "bpt6k123"}, temp_output_dir)
            mock_pages.assert_called_once_with(
                self.SERVICES, temp_output_dir, "gallica", "bpt6k123"
            )

    def test_polona_uses_shared_loop_with_polona_key(
        self, temp_output_dir: str
    ) -> None:
        with (
            patch("api.providers.polona.make_request", return_value=self.MANIFEST),
            patch("api.providers.polona.save_json", return_value=None),
            patch("api.providers.polona.download_iiif_renderings", return_value=0),
            patch(
                "api.providers.polona.extract_image_service_bases",
                return_value=self.SERVICES,
            ),
            patch(
                "api.providers.polona.download_page_images", return_value=True
            ) as mock_pages,
        ):
            from api.providers.polona import download_polona_work

            assert download_polona_work({"id": "9876"}, temp_output_dir)
            mock_pages.assert_called_once_with(
                self.SERVICES, temp_output_dir, "polona", "9876"
            )

    def test_ddb_uses_shared_loop_with_ddb_key(self, temp_output_dir: str) -> None:
        item_meta = {"iiifManifest": "https://example.org/manifest.json"}
        with (
            patch("api.providers.ddb._api_key", return_value=None),
            patch(
                "api.providers.ddb.make_request",
                side_effect=[item_meta, self.MANIFEST],
            ),
            patch("api.providers.ddb.save_json", return_value=None),
            patch("api.providers.ddb.download_iiif_renderings", return_value=0),
            patch(
                "api.providers.ddb.extract_image_service_bases",
                return_value=self.SERVICES,
            ),
            patch(
                "api.providers.ddb.download_page_images", return_value=True
            ) as mock_pages,
        ):
            from api.providers.ddb import download_ddb_work

            assert download_ddb_work({"id": "DDB1"}, temp_output_dir)
            mock_pages.assert_called_once_with(
                self.SERVICES, temp_output_dir, "ddb", "DDB1"
            )

    def test_dpla_uses_shared_loop_with_dpla_key(self, temp_output_dir: str) -> None:
        item_details = {"object": "https://iiif.example.org/manifest.json"}
        with (
            patch("api.providers.dpla._api_key", return_value=None),
            patch(
                "api.providers.dpla.make_request",
                side_effect=[item_details, self.MANIFEST],
            ),
            patch("api.providers.dpla.save_json", return_value=None),
            patch("api.providers.dpla.download_iiif_renderings", return_value=0),
            patch(
                "api.providers.dpla.extract_image_service_bases",
                return_value=self.SERVICES,
            ),
            patch(
                "api.providers.dpla.download_page_images", return_value=True
            ) as mock_pages,
        ):
            from api.providers.dpla import download_dpla_work

            assert download_dpla_work({"id": "DPLA1"}, temp_output_dir)
            mock_pages.assert_called_once_with(
                self.SERVICES, temp_output_dir, "dpla", "DPLA1"
            )

    def test_europeana_uses_shared_loop_with_europeana_key(
        self, temp_output_dir: str
    ) -> None:
        with (
            patch("api.providers.europeana._api_key", return_value="KEY"),
            patch("api.providers.europeana.make_request", return_value=self.MANIFEST),
            patch("api.providers.europeana.save_json", return_value=None),
            patch("api.providers.europeana.download_iiif_renderings", return_value=0),
            patch(
                "api.providers.europeana.extract_image_service_bases",
                return_value=self.SERVICES,
            ),
            patch(
                "api.providers.europeana.download_page_images", return_value=True
            ) as mock_pages,
        ):
            from api.providers.europeana import download_europeana_work

            item = {
                "id": "EU1",
                "iiif_manifest": "https://iiif.europeana.eu/1/manifest",
            }
            assert download_europeana_work(item, temp_output_dir)
            mock_pages.assert_called_once_with(
                self.SERVICES, temp_output_dir, "europeana", "EU1"
            )

    def test_europeana_falls_back_to_shared_direct_url_loop(
        self, temp_output_dir: str
    ) -> None:
        """No IIIF Image service: the direct whole-image URLs go through the
        shared direct-URL loop under the same provider key."""
        urls = ["https://img/1", "https://img/2"]
        with (
            patch("api.providers.europeana._api_key", return_value="KEY"),
            patch("api.providers.europeana.make_request", return_value=self.MANIFEST),
            patch("api.providers.europeana.save_json", return_value=None),
            patch("api.providers.europeana.download_iiif_renderings", return_value=0),
            patch(
                "api.providers.europeana.extract_image_service_bases", return_value=[]
            ),
            patch(
                "api.providers.europeana.extract_direct_image_urls", return_value=urls
            ),
            patch(
                "api.providers.europeana.download_direct_image_urls", return_value=True
            ) as mock_direct,
        ):
            from api.providers.europeana import download_europeana_work

            item = {
                "id": "EU1",
                "iiif_manifest": "https://iiif.europeana.eu/1/manifest",
            }
            assert download_europeana_work(item, temp_output_dir)
            mock_direct.assert_called_once_with(
                urls, temp_output_dir, "europeana", "EU1"
            )

    def test_loc_uses_shared_loop_with_loc_key(self, temp_output_dir: str) -> None:
        item_json = {
            "item": {
                "resources": [{"iiif_manifest": "https://example.org/manifest.json"}]
            }
        }
        with (
            patch(
                "api.providers.loc.make_request",
                side_effect=[item_json, self.MANIFEST],
            ),
            patch("api.providers.loc.save_json", return_value=None),
            patch("api.providers.loc.download_iiif_renderings", return_value=0),
            patch(
                "api.providers.loc.extract_image_service_bases",
                return_value=self.SERVICES,
            ),
            patch(
                "api.providers.loc.download_page_images", return_value=True
            ) as mock_pages,
        ):
            from api.providers.loc import download_loc_work

            item = {"item_url": "https://www.loc.gov/item/1/", "id": "LOC1"}
            assert download_loc_work(item, temp_output_dir)
            mock_pages.assert_called_once_with(
                self.SERVICES, temp_output_dir, "loc", "LOC1"
            )
