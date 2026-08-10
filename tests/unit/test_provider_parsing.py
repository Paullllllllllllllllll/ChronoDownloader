"""Regression tests for provider response parsing.

Each test pins a defect where the connector parsed the payload it actually
receives at the wrong level, dropped a field the response already carried, or
let one malformed record discard the whole result set.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from api.model import SearchResult


class TestLocSearchParsing:
    """LoC drops the date it is handed and trips over a non-dict 'content'."""

    @staticmethod
    def _payload(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"results": items}

    def test_date_is_carried_into_the_result(self) -> None:
        payload = self._payload(
            [
                {
                    "id": "https://www.loc.gov/item/12345/",
                    "title": "The Cook's Oracle",
                    "contributor_names": ["Kitchiner, William"],
                    "url": "https://www.loc.gov/item/12345/",
                    "date": "1822",
                }
            ]
        )
        with patch("api.providers.loc.make_request", return_value=payload):
            from api.providers.loc import search_loc

            results = search_loc("The Cook's Oracle")

        assert results[0].date == "1822"

    def test_dates_list_is_used_when_date_is_absent(self) -> None:
        payload = self._payload(
            [
                {
                    "id": "https://www.loc.gov/item/6789/",
                    "title": "Le cuisinier royal",
                    "url": "https://www.loc.gov/item/6789/",
                    "dates": ["1817-01-01T00:00:00Z"],
                }
            ]
        )
        with patch("api.providers.loc.make_request", return_value=payload):
            from api.providers.loc import search_loc

            results = search_loc("Le cuisinier royal")

        assert results[0].date == "1817-01-01T00:00:00Z"

    def test_non_dict_content_does_not_raise(self) -> None:
        # LoC sometimes answers with "content" as a string; .get() on it raised.
        with patch(
            "api.providers.loc.make_request",
            return_value={"content": "no results found"},
        ):
            from api.providers.loc import search_loc

            assert search_loc("nothing here") == []


class TestMdzHtmlFallback:
    """The HTML fallback skipped every unprefixed /view/ link."""

    HTML = (
        "<html><body>"
        '<a href="/view/bsb10123456">Ein Koch-Buch</a>'
        '<a href="/en/view/bsb10999999">A Cookbook</a>'
        "</body></html>"
    )

    def test_unprefixed_view_href_is_matched(self) -> None:
        with patch(
            "api.providers.mdz.make_request", side_effect=[{"docs": []}, self.HTML]
        ):
            from api.providers.mdz import search_mdz

            results = search_mdz("Koch-Buch", max_results=5)

        ids = [r.source_id for r in results]
        assert "bsb10123456" in ids
        assert "bsb10999999" in ids


class TestAnnasArchiveTableParsing:
    """The year column named in the table layout was parsed and discarded."""

    HTML = """
    <table>
      <tr><th>i</th><th>Title</th><th>Author</th><th>Publisher</th>
          <th>Year</th><th>File</th></tr>
      <tr>
        <td></td>
        <td><a href="/md5/0123456789abcdef0123456789abcdef">Le Cuisinier</a></td>
        <td>La Varenne</td>
        <td>Paris</td>
        <td>1651</td>
        <td>book.pdf</td>
      </tr>
    </table>
    """

    def test_year_cell_becomes_the_result_date(self) -> None:
        with patch("api.providers.annas_archive.make_request", return_value=self.HTML):
            from api.providers.annas_archive import search_annas_archive

            results = search_annas_archive("Le Cuisinier")

        assert results and results[0].date == "1651"

    def test_short_row_leaves_the_date_empty(self) -> None:
        html = """
        <table>
          <tr><th>i</th><th>Title</th></tr>
          <tr>
            <td></td>
            <td><a href="/md5/0123456789abcdef0123456789abcdef">Le Cuisinier</a></td>
          </tr>
        </table>
        """
        with patch("api.providers.annas_archive.make_request", return_value=html):
            from api.providers.annas_archive import search_annas_archive

            results = search_annas_archive("Le Cuisinier")

        assert results and results[0].date is None


class TestAnnasArchiveLinkFilter:
    """A '#' anywhere in the href rejected every fragment-carrying link."""

    HTML = """
    <html><body>
      <a href="/slow_download/abc/0/0#download">Slow download</a>
      <a href="#top">Back to top</a>
      <a href="/login">Log in</a>
    </body></html>
    """

    def test_fragment_in_href_no_longer_skips_the_download_link(
        self, temp_output_dir: str
    ) -> None:
        with (
            patch("api.providers.annas_archive.make_request", return_value=self.HTML),
            patch("api.providers.annas_archive.save_json", return_value=None),
            patch(
                "api.providers.annas_archive.download_file", return_value="/out/f.pdf"
            ) as mock_dl,
        ):
            from api.providers.annas_archive import _download_via_scraping

            assert _download_via_scraping("abc", temp_output_dir) is True

        urls = [call.args[0] for call in mock_dl.call_args_list]
        assert any(u.endswith("/slow_download/abc/0/0#download") for u in urls)
        assert not any("#top" in u for u in urls)
        assert not any("login" in u for u in urls)


class TestDdbManifestPatterns:
    """The isShownAt link DDB hands out varies in shape per provider."""

    def test_every_bsb_url_shape_yields_the_manifest(self) -> None:
        """The pattern demanded exactly one path segment before the id.

        DDB's isShownAt for MDZ items arrives language-prefixed, as the
        urn:nbn resolver form, and in the resolver's "details:" form; only
        the bare /view/ shape matched, so the manifest was never built and
        the connector fell back to the preview thumbnail.
        """
        from api.providers.ddb import _extract_iiif_manifest_url

        expected = (
            "https://api.digitale-sammlungen.de/iiif/presentation/v2/"
            "bsb10301321/manifest"
        )
        for url in (
            "https://www.digitale-sammlungen.de/view/bsb10301321",
            "https://www.digitale-sammlungen.de/en/view/bsb10301321",
            "https://www.digitale-sammlungen.de/de/view/bsb10301321?page=7",
            "https://mdz-nbn-resolving.de/urn:nbn:de:bvb:12-bsb10301321-4",
            "https://mdz-nbn-resolving.de/details:bsb10301321",
        ):
            assert _extract_iiif_manifest_url(url) == expected, url

    def test_heidelberg_query_string_stays_out_of_the_manifest_url(self) -> None:
        from api.providers.ddb import _extract_iiif_manifest_url

        assert _extract_iiif_manifest_url(
            "https://digi.ub.uni-heidelberg.de/diglit/rumohr1822?sid=abc123"
        ) == ("https://digi.ub.uni-heidelberg.de/diglit/iiif/rumohr1822/manifest.json")

    def test_unknown_host_yields_nothing(self) -> None:
        from api.providers.ddb import _extract_iiif_manifest_url

        assert _extract_iiif_manifest_url("https://example.org/item/1") is None
        assert _extract_iiif_manifest_url("") is None


class TestSlubPpnCheckDigit:
    """K10plus PPNs end in a modulo-11 check digit that may be "X"."""

    def test_trailing_check_digit_survives(self) -> None:
        """``\\d+`` truncated the X for about one record in eleven, and the
        manifest built from the short PPN 404s."""
        from api.providers.slub import _extract_ppn_from_url

        assert (
            _extract_ppn_from_url("https://digital.slub-dresden.de/id33299526X")
            == "33299526X"
        )
        assert (
            _extract_ppn_from_url("http://digital.slub-dresden.de/ppn33299526X")
            == "33299526X"
        )

    def test_numeric_ppns_are_unchanged(self) -> None:
        from api.providers.slub import _extract_ppn_from_url

        assert (
            _extract_ppn_from_url("https://digital.slub-dresden.de/id403708982")
            == "403708982"
        )
        assert _extract_ppn_from_url("https://example.org/no-ppn-here") is None
        assert _extract_ppn_from_url(None) is None

    def test_check_digit_reaches_the_manifest_url(self, temp_output_dir: str) -> None:
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
        with (
            patch("api.providers.slub.make_request", return_value=source_record),
            patch("api.providers.slub.save_json", return_value=None),
            patch(
                "api.providers.slub.download_iiif_manifest_and_images",
                return_value=True,
            ) as mock_dl,
        ):
            from api.providers.slub import download_slub_work

            assert download_slub_work({"id": "kxp-de14-1"}, temp_output_dir) is True

        assert mock_dl.call_args.kwargs["manifest_url"] == (
            "https://iiif.slub-dresden.de/iiif/2/33299526X/manifest.json"
        )


class TestPerRecordGuards:
    """One malformed record must not discard a provider's whole result set."""

    def test_ddb_skips_only_the_bad_doc(self) -> None:
        payload = {
            "results": [
                {
                    "docs": [
                        # A non-dict doc raised AttributeError on .get and
                        # discarded the good record behind it.
                        "not-a-dict",
                        {
                            "id": "ABCDEF1234567890",
                            "label": "Neues <match>Kochbuch</match>",
                            "view": ["Braun, Emmy (Verfasser*in)"],
                        },
                    ]
                }
            ]
        }
        with (
            patch("api.providers.ddb._api_key", return_value="KEY"),
            patch("api.providers.ddb.make_request", return_value=payload),
        ):
            from api.providers.ddb import search_ddb

            results = search_ddb("Kochbuch")

        assert [r.source_id for r in results] == ["ABCDEF1234567890"]
        assert results[0].title == "Neues Kochbuch"

    def test_ddb_tolerates_a_null_docs_list(self) -> None:
        """ "docs": null raised TypeError over the whole result set."""
        payload = {
            "results": [
                {"docs": None},
                {"docs": [{"id": "ABC", "label": "Kochbuch"}]},
                "not-a-dict",
            ]
        }
        with (
            patch("api.providers.ddb._api_key", return_value="KEY"),
            patch("api.providers.ddb.make_request", return_value=payload),
        ):
            from api.providers.ddb import search_ddb

            results = search_ddb("Kochbuch")

        assert [r.source_id for r in results] == ["ABC"]

    def test_internet_archive_skips_only_the_bad_doc(self) -> None:
        payload = {
            "response": {
                "docs": [
                    # A non-dict doc raised AttributeError and lost both records.
                    "not-a-dict",
                    {
                        "identifier": "artofcooking1850",
                        "title": "The Art of Cooking",
                        "creator": "John Smith",
                    },
                ]
            }
        }
        with patch("api.providers.internet_archive.make_request", return_value=payload):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("The Art of Cooking")

        assert [r.source_id for r in results] == ["artofcooking1850"]

    def test_google_books_skips_only_the_bad_volume(self) -> None:
        payload = {
            "items": [
                "not-a-dict",
                {
                    "id": "vol1",
                    "volumeInfo": {"title": "Cookery", "publishedDate": "1828"},
                    "accessInfo": {"publicDomain": True},
                },
            ]
        }
        with (
            patch("api.providers.google_books._api_key", return_value=None),
            patch("api.providers.google_books.make_request", return_value=payload),
        ):
            from api.providers.google_books import search_google_books

            results = search_google_books("Cookery")

        assert [r.source_id for r in results] == ["vol1"]
        assert results[0].date == "1828"

    def test_wellcome_skips_only_the_bad_work(self) -> None:
        payload = {
            "results": [
                "not-a-dict",
                {
                    "id": "w1",
                    "title": "A Booke of Cookerie",
                    "items": [
                        {
                            "locations": [
                                {
                                    "locationType": {"id": "iiif-image"},
                                    "url": "https://iiif.wellcomecollection.org/"
                                    "image/w1/info.json",
                                }
                            ]
                        }
                    ],
                },
            ]
        }
        with patch("api.providers.wellcome.make_request", return_value=payload):
            from api.providers.wellcome import search_wellcome

            results = search_wellcome("A Booke of Cookerie")

        assert [r.source_id for r in results] == ["w1"]


class TestGallicaSearchParsing:
    """Gallica must confine the ark capture and carry every field it parses."""

    @staticmethod
    def _sru_response(identifier: str, creators: list[str]) -> str:
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
        )

    def _search(self, identifier: str, creators: list[str]) -> list[SearchResult]:
        xml = self._sru_response(identifier, creators)
        with patch("api.providers.bnf_gallica.make_request", return_value=xml):
            from api.providers.bnf_gallica import search_gallica

            return search_gallica("cuisinier royal")

    def test_ark_capture_stops_at_qualifier_and_query(self) -> None:
        """Dot-qualifiers, query strings, and prose stay out of the ark."""
        identifier = (
            "Disponible sur https://gallica.bnf.fr/ark:/12148/bpt6k12345q"
            ".texteBrut?rk=21459;2 (consulté le 01.08.2026)"
        )
        results = self._search(identifier, ["Viard, Alexandre"])

        assert [r.raw["ark_id"] for r in results] == ["bpt6k12345q"]

    def test_every_creator_is_carried(self) -> None:
        """Multi-author records keep all dc:creator entries for scoring."""
        results = self._search(
            "https://gallica.bnf.fr/ark:/12148/bpt6k12345q",
            ["Viard, Alexandre", "Fouret, Léon"],
        )

        assert results[0].creators == ["Viard, Alexandre", "Fouret, Léon"]

    def test_item_url_is_the_landing_page(self) -> None:
        """The result carries the landing URL for the CSV link column."""
        results = self._search(
            "https://gallica.bnf.fr/ark:/12148/bpt6k12345q", ["Viard"]
        )

        assert results[0].item_url == "https://gallica.bnf.fr/ark:/12148/bpt6k12345q"


def test_all_results_are_search_results() -> None:
    """Sanity: the guards above must not change the returned type."""
    payload = {"response": {"docs": [{"identifier": "x", "title": "y"}]}}
    with patch("api.providers.internet_archive.make_request", return_value=payload):
        from api.providers.internet_archive import search_internet_archive

        results = search_internet_archive("y")

    assert all(isinstance(r, SearchResult) for r in results)


# ============================================================================
# Query echo suppression and null-envelope guards
# ============================================================================


class TestQueryEchoSuppression:
    """A record must never inherit the query's own title or creator.

    Echoing the searched-for string back as record metadata scores a perfect
    100 against itself, so an arbitrary work outranks candidates whose
    provider reported real (possibly non-matching) metadata.
    """

    MDZ_HTML = '<html><body><a href="/view/bsb10123456">Ein Koch-Buch</a></body></html>'

    def test_mdz_html_fallback_carries_no_creator(self) -> None:
        with patch(
            "api.providers.mdz.make_request", side_effect=[{"docs": []}, self.MDZ_HTML]
        ):
            from api.providers.mdz import search_mdz

            results = search_mdz("Koch-Buch", creator="Rumpolt", max_results=5)

        assert results
        assert all(not r.creators for r in results)

    def test_hathitrust_untitled_bib_record_gets_empty_title(self) -> None:
        data = {
            "records": {"123": {"publishDates": ["1651"]}},
            "items": [{"fromRecord": "123", "htid": "x1"}],
        }
        with patch("api.providers.hathitrust.make_request", return_value=data):
            from api.providers.hathitrust import search_hathitrust

            results = search_hathitrust("Le Cuisinier oclc:12345")

        assert results
        assert not results[0].title


class TestSbbMetsPdfDeduplication:
    """A PDF listed in several fileGrps must be downloaded once, not per group."""

    METS = """<mets:mets xmlns:mets="http://www.loc.gov/METS/"
                         xmlns:xlink="http://www.w3.org/1999/xlink">
      <mets:fileSec>
        <mets:fileGrp USE="DEFAULT">
          <mets:file MIMETYPE="application/pdf">
            <mets:FLocat xlink:href="https://sbb.example/doc.pdf"/>
          </mets:file>
        </mets:fileGrp>
        <mets:fileGrp USE="DOWNLOAD">
          <mets:file MIMETYPE="application/pdf">
            <mets:FLocat xlink:href="https://sbb.example/doc.pdf"/>
          </mets:file>
        </mets:fileGrp>
      </mets:fileSec>
    </mets:mets>"""

    def test_duplicate_pdf_href_is_collected_once(self) -> None:
        from api.providers.sbb_digital import _collect_mets_urls

        pdf_urls, _ = _collect_mets_urls(self.METS)

        assert pdf_urls == ["https://sbb.example/doc.pdf"]


class TestDownloadEnvelopeGuards:
    """Non-dict and null envelope members must degrade, not raise."""

    def test_loc_string_item_and_resources_do_not_raise(self, temp_dir: str) -> None:
        from api.providers.loc import download_loc_work

        sr = SearchResult(
            provider="Library of Congress",
            title="Test",
            source_id="abc123",
            provider_key="loc",
            item_url="https://www.loc.gov/item/abc123/",
            raw={"id": "abc123", "item_url": "https://www.loc.gov/item/abc123/"},
        )
        envelope = {"item": "https://www.loc.gov/item/abc123/", "resources": "none"}
        with patch("api.providers.loc.make_request", return_value=envelope):
            assert download_loc_work(sr, temp_dir) is False

    def test_europeana_title_is_never_built_into_a_manifest_url(
        self, temp_dir: str
    ) -> None:
        from api.providers.europeana import download_europeana_work

        with patch(
            "api.providers.europeana.make_request", return_value=None
        ) as mock_req:
            ok = download_europeana_work({"title": "Le Cuisinier / Royal"}, temp_dir)

        assert ok is False
        assert mock_req.call_count == 0

    def test_google_books_null_volume_and_access_info_do_not_raise(
        self, temp_dir: str
    ) -> None:
        from api.providers.google_books import download_google_books_work

        volume = {"id": "vol1", "volumeInfo": None, "accessInfo": None}
        with (
            patch("api.providers.google_books.make_request", return_value=volume),
            patch("api.providers.google_books.download_file", return_value=None),
        ):
            assert download_google_books_work({"id": "vol1"}, temp_dir) is False
