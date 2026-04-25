"""Tests for api.query_helpers module — SRU/SPARQL escaping utilities."""
from __future__ import annotations

import pytest

from api.query_helpers import escape_sparql_string, escape_sru_literal


# ============================================================================
# escape_sru_literal
# ============================================================================

class TestEscapeSruLiteral:
    """Tests for SRU/CQL literal escaping."""

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
        result = escape_sru_literal("a\n\n\tb")
        assert result == "a b"


# ============================================================================
# escape_sparql_string
# ============================================================================

class TestEscapeSparqlString:
    """Tests for SPARQL string escaping."""

    def test_plain_string(self) -> None:
        assert escape_sparql_string("hello world") == "hello world"

    def test_escapes_backslashes(self) -> None:
        assert escape_sparql_string("path\\file") == "path\\\\file"

    def test_escapes_single_quotes(self) -> None:
        assert escape_sparql_string("it's done") == "it\\'s done"

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
