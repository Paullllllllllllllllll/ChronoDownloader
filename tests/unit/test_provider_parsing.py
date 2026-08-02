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


def test_all_results_are_search_results() -> None:
    """Sanity: the guards above must not change the returned type."""
    payload = {"response": {"docs": [{"identifier": "x", "title": "y"}]}}
    with patch("api.providers.internet_archive.make_request", return_value=payload):
        from api.providers.internet_archive import search_internet_archive

        results = search_internet_archive("y")

    assert all(isinstance(r, SearchResult) for r in results)
