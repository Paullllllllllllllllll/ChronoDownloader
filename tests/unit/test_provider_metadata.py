"""Tests for the metadata connectors read off a record.

Two classes of defect are pinned here. The first is authorship read from the
wrong place: DDB's creator came from a fixed index into an unlabeled,
variable-length display array, and the MODS connectors matched
``.//mods:name``, which finds a ``<subject><name>`` heading as readily as the
record's own name. The second is publication dates present in the response
and dropped on the floor -- ``SearchResult.date`` is persisted to work.json
and to the run index, and only five of the enabled connectors filled it.

The fixtures are trimmed but verbatim snippets of live responses recorded on
01.08.2026; the tests never touch the network.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from unittest.mock import patch

from api.providers.ddb import _creator_from_view
from api.providers.dpla import _display_date
from api.query_helpers import mods_creator, mods_date

MODS_NS = {"mods": "http://www.loc.gov/mods/v3"}


def _mods(xml: str) -> Any:
    return ET.fromstring(xml)


# ============================================================================
# DDB "view" array
# ============================================================================


class TestDdbCreatorFromView:
    """A fixed index into DDB's view array cannot mean anything.

    Live view arrays for one query ran 3 to 9 entries long and held titles,
    places, publishers, languages, extents and document types in no fixed
    order. Index 6 returned "Monografie" (a document type), "Fleischhauer &
    Spohn" (a publisher) and "XXIV, 400 Seiten" (an extent statement) as the
    author, each of which was persisted to work.json and scored against the
    query's creator.
    """

    def test_role_marked_author_is_recovered(self) -> None:
        view = [
            "Braun, Emmy (Verfasser*in)",
            "SLUB Dresden (Besitzer*in)",
            "Monografie",
            "Achte verbesserte und vermehrte Auflage",
            "Grünstadt : J. Schäffer's Buchhandlung , [circa 1900]",
            "Deutsch",
            "XXIV, 400 Seiten",
        ]
        assert _creator_from_view(view) == "Braun, Emmy"

    def test_non_author_roles_are_not_creators(self) -> None:
        for entry in (
            "Kaven, Johann Heinrich (Drucker*in)",
            "SLUB Dresden (Besitzer*in)",
            "Strahowsky, Bartholomäus (Gravierer*in)",
        ):
            assert _creator_from_view([entry]) is None, entry

    def test_unlabeled_entries_yield_nothing(self) -> None:
        """Better no author than the publisher or the page count."""
        view = [
            "",
            "Neues Kochbuch",
            "Bauer, Josefine",
            "Veröffentlichung",
            "Stuttgart",
            "Fleischhauer & Spohn",
            "1951",
        ]
        assert _creator_from_view(view) is None

    def test_odd_shapes_are_tolerated(self) -> None:
        assert _creator_from_view(None) is None
        assert _creator_from_view({"not": "a list"}) is None
        assert _creator_from_view([]) is None
        assert _creator_from_view([None, 42, "Monografie"]) is None
        assert _creator_from_view(["   (Verfasser*in)"]) is None


# ============================================================================
# MODS records (e-rara, SBB)
# ============================================================================


SUBJECT_ONLY_RECORD = """
<mods xmlns="http://www.loc.gov/mods/v3">
  <titleInfo><title>Neues, deutsches Kochbuch</title></titleInfo>
  <originInfo>
    <publisher>Fleischhauer und Spohn</publisher>
    <dateIssued>1839</dateIssued>
  </originInfo>
  <subject>
    <name type="personal">
      <displayForm>Löffler, Friederike Luise</displayForm>
    </name>
  </subject>
</mods>
"""

NAMED_RECORD = """
<mods xmlns="http://www.loc.gov/mods/v3">
  <titleInfo><title>Kochbuch, den Hausfrauen gewidmet</title></titleInfo>
  <name><namePart>Städtische Licht- und Wasserwerke</namePart></name>
  <originInfo><dateIssued>1907</dateIssued></originInfo>
  <subject><name><displayForm>Someone Else</displayForm></name></subject>
</mods>
"""


class TestModsCreator:
    """Only a top-level ``<name>`` describes the work.

    The first record below is anonymous: its only ``<name>`` sits inside a
    ``<subject>``, so the connector credited the cookery writer the book is
    *about* as its author.
    """

    def test_a_subject_heading_is_not_the_creator(self) -> None:
        assert mods_creator(_mods(SUBJECT_ONLY_RECORD), MODS_NS) is None

    def test_the_records_own_name_still_wins(self) -> None:
        assert (
            mods_creator(_mods(NAMED_RECORD), MODS_NS)
            == "Städtische Licht- und Wasserwerke"
        )

    def test_display_form_is_preferred_over_name_part(self) -> None:
        xml = """
        <mods xmlns="http://www.loc.gov/mods/v3">
          <name>
            <namePart>Haller</namePart>
            <displayForm>Haller, Ludwig Albrecht</displayForm>
          </name>
        </mods>
        """
        assert mods_creator(_mods(xml), MODS_NS) == "Haller, Ludwig Albrecht"

    def test_an_empty_name_is_skipped(self) -> None:
        xml = """
        <mods xmlns="http://www.loc.gov/mods/v3">
          <name><displayForm>   </displayForm></name>
          <name><namePart>Real, Author</namePart></name>
        </mods>
        """
        assert mods_creator(_mods(xml), MODS_NS) == "Real, Author"


class TestModsDate:
    """dateIssued sits in the record and was never read."""

    def test_reads_date_issued(self) -> None:
        assert mods_date(_mods(SUBJECT_ONLY_RECORD), MODS_NS) == "1839"
        assert mods_date(_mods(NAMED_RECORD), MODS_NS) == "1907"

    def test_absent_date_is_none(self) -> None:
        xml = """
        <mods xmlns="http://www.loc.gov/mods/v3">
          <titleInfo><title>Undated</title></titleInfo>
        </mods>
        """
        assert mods_date(_mods(xml), MODS_NS) is None


# ============================================================================
# MODS record identifiers (SBB)
# ============================================================================


def _sbb_volume_sru(own_ppn: str | None = "PPN723456789") -> str:
    """A multi-volume item as the GBV SRU endpoint returns it.

    The volume's own recordInfo sits at the top level of the MODS record;
    the series it belongs to carries a recordInfo of its own inside
    ``<relatedItem type="host">``, which comes first in document order.
    """
    own = (
        f"""<mods:recordInfo>
            <mods:recordIdentifier source="gbv-ppn">{own_ppn}</mods:recordIdentifier>
          </mods:recordInfo>"""
        if own_ppn
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:version>1.2</srw:version>
  <srw:numberOfRecords>1</srw:numberOfRecords>
  <srw:records>
    <srw:record>
      <srw:recordSchema>mods</srw:recordSchema>
      <srw:recordPacking>xml</srw:recordPacking>
      <srw:recordData>
        <mods:mods xmlns:mods="http://www.loc.gov/mods/v3" version="3.7">
          <mods:titleInfo>
            <mods:title>Allgemeines deutsches Kochbuch</mods:title>
            <mods:subTitle>Zweiter Band</mods:subTitle>
          </mods:titleInfo>
          <mods:name type="personal">
            <mods:namePart>Loeffler, Friederike Luise</mods:namePart>
          </mods:name>
          <mods:originInfo>
            <mods:place><mods:placeTerm>Stuttgart</mods:placeTerm></mods:place>
            <mods:dateIssued>1846</mods:dateIssued>
          </mods:originInfo>
          <mods:relatedItem type="host">
            <mods:titleInfo>
              <mods:title>Allgemeines deutsches Kochbuch</mods:title>
            </mods:titleInfo>
            <mods:recordInfo>
              <mods:recordIdentifier source="gbv-ppn"
                >PPN100000000</mods:recordIdentifier>
            </mods:recordInfo>
          </mods:relatedItem>
          {own}
        </mods:mods>
      </srw:recordData>
      <srw:recordPosition>1</srw:recordPosition>
    </srw:record>
  </srw:records>
</srw:searchRetrieveResponse>
"""


class TestSbbRecordIdentifier:
    """A volume must be downloaded under its own PPN, not the series'.

    ``.//mods:recordInfo`` descends into ``<relatedItem type="host">``, whose
    recordInfo names the series. The METS resolver, the viewer URL and every
    downloaded file then pointed at the wrong object -- the same defect class
    that ``.//mods:name`` had for authorship.
    """

    def test_the_volumes_own_ppn_wins_over_the_host_record(self) -> None:
        from api.providers.sbb_digital import search_sbb_digital

        with patch(
            "api.providers.sbb_digital.make_request", return_value=_sbb_volume_sru()
        ):
            results = search_sbb_digital("Allgemeines deutsches Kochbuch")

        assert len(results) == 1
        first = results[0]
        assert first.source_id == "PPN723456789"
        assert first.raw["mets_url"].endswith("PPN723456789")
        assert first.raw["item_url"].endswith("PPN723456789")
        assert first.creators == ["Loeffler, Friederike Luise"]
        assert first.date == "1846"

    def test_a_record_with_only_a_host_ppn_is_skipped(self) -> None:
        """Without its own identifier the record cannot be resolved."""
        from api.providers.sbb_digital import search_sbb_digital

        host_only = _sbb_volume_sru(own_ppn=None)
        with patch("api.providers.sbb_digital.make_request", return_value=host_only):
            assert search_sbb_digital("Allgemeines deutsches Kochbuch") == []


# ============================================================================
# DPLA date unwrapping
# ============================================================================


class TestDplaDisplayDate:
    """DPLA wraps its dates in a displayDate object, often inside a list."""

    def test_unwraps_the_list_and_the_object(self) -> None:
        assert _display_date([{"displayDate": "between 1958-1961"}]) == (
            "between 1958-1961"
        )
        assert _display_date({"displayDate": "1897"}) == "1897"

    def test_falls_back_to_begin(self) -> None:
        assert _display_date({"begin": "1897-01-01"}) == "1897-01-01"

    def test_bare_string_and_empties(self) -> None:
        assert _display_date("1789") == "1789"
        assert _display_date([]) is None
        assert _display_date(None) is None
        assert _display_date({"displayDate": "   "}) is None


# ============================================================================
# Dates reaching SearchResult
# ============================================================================


class TestDatesReachTheSearchResult:
    """Six connectors dropped a date the response already carried.

    SearchResult.date is persisted to work.json and to index.csv, so for a
    corpus keyed on print date the year column was empty for most of the
    enabled providers.
    """

    def test_mdz_reads_publication_date(self) -> None:
        from api.providers.mdz import search_mdz

        response = {
            "docs": [
                {
                    "id": "bsb10000001",
                    "title": "Bayersches Kochbuch",
                    "authors": ["Anon"],
                    "publicationDate": "(1837)",
                    "iiifAvailable": True,
                }
            ]
        }
        with patch("api.providers.mdz.make_request", return_value=response):
            assert search_mdz("Kochbuch")[0].date == "(1837)"

    def test_google_books_reads_published_date(self) -> None:
        from api.providers.google_books import search_google_books

        response = {
            "items": [
                {
                    "id": "vol1",
                    "volumeInfo": {
                        "title": "The Cook and Housewife's Manual",
                        "authors": ["Margaret Dods"],
                        "publishedDate": "1828",
                    },
                    "accessInfo": {
                        "viewability": "ALL_PAGES",
                        "publicDomain": True,
                        "pdf": {"isAvailable": True, "downloadLink": "https://x/p.pdf"},
                    },
                }
            ]
        }
        with patch("api.providers.google_books.make_request", return_value=response):
            assert search_google_books("manual")[0].date == "1828"

    def test_europeana_reads_year_and_drops_the_provider_sentinel(self) -> None:
        from api.providers.europeana import search_europeana

        response = {
            "success": True,
            "items": [
                {
                    "id": "/123/abc",
                    "title": ["Kookboek"],
                    "guid": "https://www.europeana.eu/item/123/abc",
                    "year": ["1783"],
                }
            ],
        }
        with (
            patch("api.providers.europeana.make_request", return_value=response),
            patch("api.providers.europeana._api_key", return_value="k"),
        ):
            result = search_europeana("kookboek")[0]

        assert result.date == "1783"
        # Last survivor of the "N/A" sentinel purge.
        assert result.raw["provider"] is None
