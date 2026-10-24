"""The shared query helpers: SRU and SPARQL escapes, MODS readers, years.

Ported from v1.30.3 ``tests/unit/test_query_helpers.py`` and the MODS
classes of ``tests/unit/test_provider_metadata.py``; assertions unchanged.
The year parser is new.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from chronodownloader.providers._query import (
    escape_sparql_string,
    escape_sru_literal,
    mods_creator,
    mods_date,
    parse_year,
)

MODS_NS = {"mods": "http://www.loc.gov/mods/v3"}


@pytest.mark.parametrize(
    ("text", "year"),
    [
        ("1845", 1845),
        ("[1770]", 1770),
        ("1886-1945", 1886),
        ("ca.1850", 1850),
        ("1850s", 1850),
        ("A. MDCCLXII", None),
        ("S.XVIII", None),
        ("18501", None),
        ("0123 1066 1688", 1688),
        ("1400", 1400),
        ("2100", 2100),
        ("2101", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_year_takes_the_first_plausible_year(
    text: str | None, year: int | None
) -> None:
    assert parse_year(text) == year


def _mods(xml: str) -> ET.Element:
    return ET.fromstring(xml)


class TestEscapeSruLiteral:
    """SRU/CQL literal escaping."""

    def test_plain_string(self) -> None:
        assert escape_sru_literal("hello world") == "hello world"

    def test_escapes_backslashes(self) -> None:
        assert escape_sru_literal("path\\file") == "path\\\\file"

    def test_escapes_double_quotes(self) -> None:
        assert escape_sru_literal('say "hello"') == 'say \\"hello\\"'

    def test_collapses_newlines_and_tabs(self) -> None:
        assert escape_sru_literal("line1\nline2\ttab") == "line1 line2 tab"

    def test_collapses_carriage_returns(self) -> None:
        assert escape_sru_literal("line1\r\nline2") == "line1 line2"

    def test_none_returns_empty(self) -> None:
        assert escape_sru_literal(None) == ""

    def test_empty_string(self) -> None:
        assert escape_sru_literal("") == ""

    def test_combined_escaping(self) -> None:
        result = escape_sru_literal('say "hello\\world"\nend')
        assert result == 'say \\"hello\\\\world\\" end'

    def test_non_string_input(self) -> None:
        assert escape_sru_literal(42) == "42"  # type: ignore[arg-type]

    def test_multiple_consecutive_whitespace(self) -> None:
        assert escape_sru_literal("a\n\n\tb") == "a b"


class TestEscapeSparqlString:
    """SPARQL string escaping."""

    def test_plain_string(self) -> None:
        assert escape_sparql_string("hello world") == "hello world"

    def test_escapes_backslashes(self) -> None:
        assert escape_sparql_string("path\\file") == "path\\\\file"

    def test_escapes_single_quotes(self) -> None:
        assert escape_sparql_string("it's done") == "it\\'s done"

    def test_escapes_double_quotes(self) -> None:
        # The BNB SPARQL fallback embeds titles in double-quoted literals; an
        # unescaped '"' broke the query silently.
        assert escape_sparql_string('say "cheese"') == 'say \\"cheese\\"'

    def test_replaces_newlines(self) -> None:
        assert escape_sparql_string("line1\nline2") == "line1 line2"

    def test_replaces_carriage_returns(self) -> None:
        assert escape_sparql_string("line1\rline2") == "line1 line2"

    def test_replaces_tabs(self) -> None:
        assert escape_sparql_string("col1\tcol2") == "col1 col2"

    def test_none_returns_empty(self) -> None:
        assert escape_sparql_string(None) == ""

    def test_empty_string(self) -> None:
        assert escape_sparql_string("") == ""

    def test_combined_escaping(self) -> None:
        result = escape_sparql_string("it's a 'test'\\path\n")
        assert result == "it\\'s a \\'test\\'\\\\path "

    def test_non_string_input(self) -> None:
        assert escape_sparql_string(42) == "42"  # type: ignore[arg-type]


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
    """Only a top-level ``<name>`` describes the work: the first record is
    anonymous, and its ``<subject><name>`` is the writer it is about."""

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
