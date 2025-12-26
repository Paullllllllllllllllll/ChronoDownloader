"""Unit tests for api.model module."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.model import (
    QuotaDeferredException,
    SearchResult,
    _as_list,
    convert_to_searchresult,
)


class TestSearchResult:
    """Tests for SearchResult dataclass."""
    
    def test_basic_creation(self):
        """Test basic SearchResult creation."""
        sr = SearchResult(
            provider="Internet Archive",
            title="Test Title"
        )
        assert sr.provider == "Internet Archive"
        assert sr.title == "Test Title"
        assert sr.creators == []
        assert sr.date is None
        assert sr.source_id is None
    
    def test_full_creation(self, sample_search_result):
        """Test SearchResult with all fields."""
        sr = sample_search_result
        assert sr.provider == "Internet Archive"
        assert sr.title == "The Art of Cooking"
        assert sr.creators == ["John Smith"]
        assert sr.date == "1850"
        assert sr.source_id == "artofcooking1850"
        assert sr.iiif_manifest is not None
        assert sr.item_url is not None
        assert sr.thumbnail_url is not None
        assert sr.provider_key == "internet_archive"
        assert isinstance(sr.raw, dict)
    
    def test_to_dict_without_raw(self, sample_search_result):
        """Test conversion to dict without raw data."""
        d = sample_search_result.to_dict(include_raw=False)
        assert "provider" in d
        assert "title" in d
        assert "raw" not in d
    
    def test_to_dict_with_raw(self, sample_search_result):
        """Test conversion to dict with raw data."""
        d = sample_search_result.to_dict(include_raw=True)
        assert "raw" in d
        assert d["raw"]["identifier"] == "artofcooking1850"
    
    def test_default_values(self):
        """Test default values for optional fields."""
        sr = SearchResult(provider="Test", title="Title")
        assert sr.creators == []
        assert sr.date is None
        assert sr.source_id is None
        assert sr.iiif_manifest is None
        assert sr.item_url is None
        assert sr.thumbnail_url is None
        assert sr.provider_key is None
        assert sr.raw == {}


class TestAsList:
    """Tests for _as_list helper function."""
    
    def test_none_value(self):
        """Test with None value."""
        assert _as_list(None) == []
    
    def test_single_string(self):
        """Test with single string."""
        assert _as_list("hello") == ["hello"]
    
    def test_comma_separated_string(self):
        """Test with comma-separated string."""
        result = _as_list("one, two, three")
        assert result == ["one", "two", "three"]
    
    def test_list_input(self):
        """Test with list input."""
        assert _as_list(["one", "two"]) == ["one", "two"]
    
    def test_list_with_none_elements(self):
        """Test list with None elements."""
        result = _as_list(["one", None, "two"])
        assert result == ["one", "two"]
    
    def test_non_string_list(self):
        """Test list with non-string elements."""
        result = _as_list([1, 2, 3])
        assert result == ["1", "2", "3"]
    
    def test_other_type(self):
        """Test with other types."""
        assert _as_list(42) == ["42"]


class TestConvertToSearchResult:
    """Tests for convert_to_searchresult function."""
    
    def test_basic_conversion(self):
        """Test basic dict to SearchResult conversion."""
        data = {
            "title": "Test Title",
            "creator": "John Smith",
            "identifier": "test123"
        }
        sr = convert_to_searchresult("Test Provider", data)
        
        assert sr.provider == "Test Provider"
        assert sr.title == "Test Title"
        assert sr.creators == ["John Smith"]
        assert sr.source_id == "test123"
    
    def test_raw_preserved(self):
        """Test that raw data is preserved."""
        data = {"title": "Test", "extra_field": "extra_value"}
        sr = convert_to_searchresult("Provider", data)
        
        assert sr.raw == data
        assert sr.raw["extra_field"] == "extra_value"
    
    def test_title_fallbacks(self):
        """Test title extraction with fallback fields."""
        # No title field
        data1 = {"name": "By Name"}
        assert convert_to_searchresult("P", data1).title == "By Name"
        
        # No title or name
        data2 = {"label": "By Label"}
        assert convert_to_searchresult("P", data2).title == "By Label"
        
        # No title fields
        data3 = {}
        assert convert_to_searchresult("P", data3).title == "N/A"
    
    def test_creator_extraction(self):
        """Test creator extraction from various fields."""
        # creators field
        data1 = {"title": "T", "creators": ["A", "B"]}
        assert convert_to_searchresult("P", data1).creators == ["A", "B"]
        
        # creator field
        data2 = {"title": "T", "creator": "Single Author"}
        assert convert_to_searchresult("P", data2).creators == ["Single Author"]
        
        # contributor_names field
        data3 = {"title": "T", "contributor_names": ["Contributor"]}
        assert convert_to_searchresult("P", data3).creators == ["Contributor"]
    
    def test_date_extraction(self):
        """Test date extraction from various fields."""
        # date field
        data1 = {"title": "T", "date": "1850"}
        assert convert_to_searchresult("P", data1).date == "1850"
        
        # year field
        data2 = {"title": "T", "year": 1850}
        assert convert_to_searchresult("P", data2).date == "1850"
        
        # issued field
        data3 = {"title": "T", "issued": "1850-01-01"}
        assert convert_to_searchresult("P", data3).date == "1850-01-01"
    
    def test_source_id_extraction(self):
        """Test source ID extraction from various fields."""
        # id field
        data1 = {"title": "T", "id": "id123"}
        assert convert_to_searchresult("P", data1).source_id == "id123"
        
        # identifier field
        data2 = {"title": "T", "identifier": "ident123"}
        assert convert_to_searchresult("P", data2).source_id == "ident123"
        
        # ark_id field
        data3 = {"title": "T", "ark_id": "ark:/12148/bpt6k123"}
        assert convert_to_searchresult("P", data3).source_id == "ark:/12148/bpt6k123"
    
    def test_url_extraction(self):
        """Test URL extraction."""
        data = {
            "title": "T",
            "iiif_manifest": "https://example.com/manifest.json",
            "item_url": "https://example.com/item",
            "thumbnail": "https://example.com/thumb.jpg"
        }
        sr = convert_to_searchresult("P", data)
        
        assert sr.iiif_manifest == "https://example.com/manifest.json"
        assert sr.item_url == "https://example.com/item"
        assert sr.thumbnail_url == "https://example.com/thumb.jpg"
    
    def test_url_fallbacks(self):
        """Test URL extraction with fallback fields."""
        # manifest fallback
        data1 = {"title": "T", "manifest": "https://manifest.json"}
        assert convert_to_searchresult("P", data1).iiif_manifest == "https://manifest.json"
        
        # url fallback for item_url
        data2 = {"title": "T", "url": "https://item.url"}
        assert convert_to_searchresult("P", data2).item_url == "https://item.url"
        
        # thumbnail_url and image fallbacks
        data3 = {"title": "T", "thumbnail_url": "https://thumb1.jpg"}
        assert convert_to_searchresult("P", data3).thumbnail_url == "https://thumb1.jpg"


class TestQuotaDeferredException:
    """Tests for QuotaDeferredException class."""
    
    def test_basic_creation(self):
        """Test basic exception creation."""
        exc = QuotaDeferredException("internet_archive")
        assert exc.provider == "internet_archive"
        assert exc.reset_time is None
        assert "internet_archive" in exc.message
        assert "Quota exhausted" in exc.message
    
    def test_with_reset_time(self):
        """Test exception with reset time."""
        reset = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        exc = QuotaDeferredException("annas_archive", reset_time=reset)
        assert exc.reset_time == reset
    
    def test_with_custom_message(self):
        """Test exception with custom message."""
        exc = QuotaDeferredException(
            "provider",
            message="Custom error message"
        )
        assert exc.message == "Custom error message"
    
    def test_str_representation(self):
        """Test string representation."""
        exc = QuotaDeferredException("test_provider")
        assert "test_provider" in str(exc)
    
    def test_repr(self):
        """Test repr representation."""
        reset = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        exc = QuotaDeferredException("provider", reset_time=reset)
        repr_str = repr(exc)
        assert "QuotaDeferredException" in repr_str
        assert "provider" in repr_str
    
    def test_repr_without_reset_time(self):
        """Test repr without reset time."""
        exc = QuotaDeferredException("provider")
        repr_str = repr(exc)
        assert "unknown" in repr_str
    
    def test_is_exception(self):
        """Test that it's a proper exception."""
        exc = QuotaDeferredException("provider")
        assert isinstance(exc, Exception)
        
        with pytest.raises(QuotaDeferredException):
            raise exc
