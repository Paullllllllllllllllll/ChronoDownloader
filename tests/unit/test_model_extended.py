"""Extended tests for api.model module — SearchResult and helper functions."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.model import (
    QuotaDeferredException,
    SearchResult,
    _as_list,
    convert_to_searchresult,
    resolve_item_field,
    resolve_item_id,
)


# ============================================================================
# resolve_item_field
# ============================================================================

class TestResolveItemField:
    """Tests for unified field extraction."""

    def test_searchresult_attr(self):
        sr = SearchResult(provider="IA", title="Test", raw={"identifier": "raw_id"})
        assert resolve_item_field(sr, "identifier", attr="title") == "Test"

    def test_searchresult_raw_fallback(self):
        sr = SearchResult(provider="IA", title="Test", raw={"custom_key": "raw_val"})
        assert resolve_item_field(sr, "custom_key") == "raw_val"

    def test_searchresult_default(self):
        sr = SearchResult(provider="IA", title="Test", raw={})
        assert resolve_item_field(sr, "missing", default="fallback") == "fallback"

    def test_dict_lookup(self):
        data = {"identifier": "abc123"}
        assert resolve_item_field(data, "identifier") == "abc123"

    def test_dict_default(self):
        data = {"other": "value"}
        assert resolve_item_field(data, "missing", default="x") == "x"

    def test_non_dict_non_sr(self):
        assert resolve_item_field(42, "key", default="fallback") == "fallback"

    def test_searchresult_attr_defaults_to_raw_key(self):
        sr = SearchResult(provider="IA", title="Test", source_id="src1", raw={"source_id": "raw"})
        # attr defaults to raw_key ("source_id"), which matches a real attribute
        assert resolve_item_field(sr, "source_id") == "src1"


# ============================================================================
# resolve_item_id
# ============================================================================

class TestResolveItemId:
    """Tests for ID extraction from SearchResult or dict."""

    def test_searchresult_source_id(self):
        sr = SearchResult(provider="IA", title="Test", source_id="abc", raw={})
        assert resolve_item_id(sr) == "abc"

    def test_searchresult_raw_fallback(self):
        sr = SearchResult(provider="IA", title="Test", source_id=None, raw={"id": "123"})
        assert resolve_item_id(sr) == "123"

    def test_searchresult_multiple_keys(self):
        sr = SearchResult(provider="IA", title="Test", source_id=None, raw={"identifier": "x"})
        assert resolve_item_id(sr, "id", "identifier") == "x"

    def test_searchresult_none_when_missing(self):
        sr = SearchResult(provider="IA", title="Test", source_id=None, raw={})
        assert resolve_item_id(sr) is None

    def test_dict_lookup(self):
        data = {"identifier": "abc123"}
        assert resolve_item_id(data, "identifier") == "abc123"

    def test_dict_first_match(self):
        data = {"ark_id": "ark:/123", "id": "id1"}
        assert resolve_item_id(data, "ark_id", "id") == "ark:/123"

    def test_dict_none_when_missing(self):
        assert resolve_item_id({}) is None

    def test_non_dict_returns_none(self):
        assert resolve_item_id("string") is None

    def test_default_key_is_id(self):
        data = {"id": "val"}
        assert resolve_item_id(data) == "val"


# ============================================================================
# _as_list
# ============================================================================

class TestAsList:
    """Tests for value-to-list conversion."""

    def test_none(self):
        assert _as_list(None) == []

    def test_string(self):
        assert _as_list("author") == ["author"]

    def test_comma_separated_string(self):
        assert _as_list("a, b, c") == ["a", "b", "c"]

    def test_list(self):
        assert _as_list(["a", "b"]) == ["a", "b"]

    def test_list_with_none(self):
        assert _as_list(["a", None, "b"]) == ["a", "b"]

    def test_integer(self):
        assert _as_list(42) == ["42"]

    def test_empty_string(self):
        assert _as_list("") == [""]

    def test_comma_only(self):
        assert _as_list(",") == []


# ============================================================================
# convert_to_searchresult
# ============================================================================

class TestConvertToSearchResult:
    """Tests for dict-to-SearchResult conversion."""

    def test_full_dict(self):
        data = {
            "title": "Test Book",
            "creator": ["Author A"],
            "date": "1850",
            "identifier": "abc123",
            "iiif_manifest": "https://example.org/manifest",
            "item_url": "https://example.org/item",
            "thumbnail": "https://example.org/thumb.jpg",
        }
        sr = convert_to_searchresult("Provider", data)
        assert sr.title == "Test Book"
        assert sr.creators == ["Author A"]
        assert sr.date == "1850"
        assert sr.source_id == "abc123"
        assert sr.iiif_manifest == "https://example.org/manifest"
        assert sr.item_url == "https://example.org/item"
        assert sr.raw is data

    def test_empty_dict(self):
        sr = convert_to_searchresult("Provider", {})
        assert sr.title == "N/A"
        assert sr.creators == []
        assert sr.date is None
        assert sr.source_id is None

    def test_uses_name_as_title(self):
        sr = convert_to_searchresult("P", {"name": "Named Item"})
        assert sr.title == "Named Item"

    def test_uses_label_as_title(self):
        sr = convert_to_searchresult("P", {"label": "Labeled Item"})
        assert sr.title == "Labeled Item"

    def test_creator_key(self):
        sr = convert_to_searchresult("P", {"creator": "Single Author"})
        assert sr.creators == ["Single Author"]

    def test_creators_key(self):
        sr = convert_to_searchresult("P", {"creators": ["A", "B"]})
        assert sr.creators == ["A", "B"]

    def test_contributor_names_key(self):
        sr = convert_to_searchresult("P", {"contributor_names": ["Contributor"]})
        assert sr.creators == ["Contributor"]

    def test_date_from_year(self):
        sr = convert_to_searchresult("P", {"year": "1900"})
        assert sr.date == "1900"

    def test_source_id_from_id(self):
        sr = convert_to_searchresult("P", {"id": "id_val"})
        assert sr.source_id == "id_val"

    def test_source_id_from_uid(self):
        sr = convert_to_searchresult("P", {"uid": "uid_val"})
        assert sr.source_id == "uid_val"

    def test_item_url_from_url(self):
        sr = convert_to_searchresult("P", {"url": "https://example.org"})
        assert sr.item_url == "https://example.org"

    def test_item_url_from_guid(self):
        sr = convert_to_searchresult("P", {"guid": "https://example.org"})
        assert sr.item_url == "https://example.org"


# ============================================================================
# QuotaDeferredException
# ============================================================================

class TestQuotaDeferredExceptionExtended:
    """Extended tests for QuotaDeferredException."""

    def test_repr_with_reset_time(self):
        dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
        exc = QuotaDeferredException("ia", reset_time=dt)
        r = repr(exc)
        assert "ia" in r
        assert "2025" in r

    def test_repr_without_reset_time(self):
        exc = QuotaDeferredException("ia")
        r = repr(exc)
        assert "unknown" in r

    def test_custom_message(self):
        exc = QuotaDeferredException("ia", message="Custom msg")
        assert str(exc) == "Custom msg"
        assert exc.message == "Custom msg"

    def test_default_message(self):
        exc = QuotaDeferredException("ia")
        assert "ia" in exc.message
        assert "Quota exhausted" in exc.message


# ============================================================================
# SearchResult
# ============================================================================

class TestSearchResultExtended:
    """Extended tests for SearchResult dataclass."""

    def test_to_dict_with_raw(self):
        sr = SearchResult(provider="IA", title="Test", raw={"key": "val"})
        d = sr.to_dict(include_raw=True)
        assert "raw" in d
        assert d["raw"]["key"] == "val"

    def test_to_dict_without_raw(self):
        sr = SearchResult(provider="IA", title="Test", raw={"key": "val"})
        d = sr.to_dict(include_raw=False)
        assert "raw" not in d

    def test_default_values(self):
        sr = SearchResult(provider="IA", title="Test")
        assert sr.creators == []
        assert sr.date is None
        assert sr.source_id is None
        assert sr.iiif_manifest is None
        assert sr.item_url is None
        assert sr.thumbnail_url is None
        assert sr.provider_key is None
        assert sr.raw == {}
