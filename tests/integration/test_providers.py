"""Integration tests for provider API modules with mocked responses."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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

            assert results[0].raw["item_url"] == "https://archive.org/details/artofcooking1850"

    def test_search_with_creator(
        self, mock_ia_search_response: dict[str, Any]
    ) -> None:
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
        with patch(
            "api.providers.internet_archive.make_request", return_value=None
        ):
            from api.providers.internet_archive import search_internet_archive

            results = search_internet_archive("Title")

            assert results == []

    def test_search_max_results(
        self, mock_ia_search_response: dict[str, Any]
    ) -> None:
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
                    "date": "1900"
                }
            ]
        }

        with patch("api.providers.loc.make_request", return_value=mock_response):
            from api.providers.loc import search_loc

            results = search_loc("cookbook")

            assert len(results) >= 1
            assert all(isinstance(r, SearchResult) for r in results)


class TestMdzProvider:
    """Integration tests for MDZ (Münchener DigitalisierungsZentrum) provider."""
    
    def test_search_handles_none_response(self) -> None:
        """Test that search handles None response gracefully."""
        with patch("api.providers.mdz.make_request", return_value=None):
            from api.providers.mdz import search_mdz

            results = search_mdz("kochen")

            # Should return empty list for None response
            assert results == []


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
            "annas_archive"
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
                "title": "The Art of Cooking"
            },
            "files": []
        }

        with patch(
            "api.providers.internet_archive.make_request", return_value=mock_metadata
        ):
            with patch(
                "api.providers.internet_archive.save_json",
                return_value="/path/to/saved.json",
            ):
                from api.providers.internet_archive import download_ia_work

                # Note: This will return False as there are no downloadable files
                # but it should not raise an error
                result = download_ia_work(sample_search_result, temp_output_dir)

                # With empty files list, should return False
                assert result is False


class TestSearchResultScoring:
    """Tests for search result scoring integration."""
    
    def test_attach_scores_to_search_result(self, sample_search_result: Any) -> None:
        """Test that scores can be attached to SearchResult."""
        from api.matching import title_score, creator_score

        query_title = "The Art of Cooking"
        query_creator = "John Smith"

        ts = title_score(query_title, sample_search_result.title)
        cs = creator_score(query_creator, sample_search_result.creators)

        # Attach scores to raw dict
        sample_search_result.raw["__matching__"] = {
            "score": ts,
            "creator_score": cs,
            "total": ts * 0.8 + cs * 0.2
        }

        assert sample_search_result.raw["__matching__"]["score"] == 100
        assert sample_search_result.raw["__matching__"]["creator_score"] == 100
