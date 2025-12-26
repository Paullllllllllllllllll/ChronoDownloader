"""Integration tests for selection module."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from api.model import SearchResult


class TestCollectCandidatesSequential:
    """Tests for sequential candidate collection."""
    
    def test_collects_from_all_providers(self):
        """Test that candidates are collected from all providers."""
        from main.selection import collect_candidates_sequential
        
        # Mock provider tuples
        def mock_search_1(title, creator, max_results):
            return [{"title": "Result 1", "id": "id1"}]
        
        def mock_search_2(title, creator, max_results):
            return [{"title": "Result 2", "id": "id2"}]
        
        providers = [
            ("provider1", mock_search_1, lambda x, y: True, "Provider 1"),
            ("provider2", mock_search_2, lambda x, y: True, "Provider 2"),
        ]
        
        with patch("main.selection.get_max_results_for_provider", return_value=5):
            with patch("main.selection.get_min_title_score", return_value=0):
                all_candidates, selected, selected_tuple = collect_candidates_sequential(
                    providers,
                    "Test Title",
                    None,
                    min_title_score=0,
                    creator_weight=0.2,
                    max_candidates_per_provider=5
                )
        
        # Should have collected candidates from first provider that matches
        assert len(all_candidates) >= 1
    
    def test_stops_on_first_match(self):
        """Test that collection stops on first acceptable match."""
        from main.selection import collect_candidates_sequential
        
        search_call_count = {"count": 0}
        
        def mock_search_1(title, creator, max_results):
            search_call_count["count"] += 1
            return [{"title": title, "id": "id1"}]  # Exact match
        
        def mock_search_2(title, creator, max_results):
            search_call_count["count"] += 1
            return [{"title": "Different", "id": "id2"}]
        
        providers = [
            ("provider1", mock_search_1, lambda x, y: True, "Provider 1"),
            ("provider2", mock_search_2, lambda x, y: True, "Provider 2"),
        ]
        
        with patch("main.selection.get_max_results_for_provider", return_value=5):
            with patch("main.selection.get_min_title_score", return_value=80):
                collect_candidates_sequential(
                    providers,
                    "Test Title",
                    None,
                    min_title_score=80,
                    creator_weight=0.2,
                    max_candidates_per_provider=5
                )
        
        # Should stop after first provider since it has an exact match
        assert search_call_count["count"] == 1


class TestSelectBestCandidate:
    """Tests for best candidate selection."""
    
    def test_selects_by_provider_priority_and_score(self):
        """Test that selection respects provider priority then score."""
        from main.selection import select_best_candidate
        
        # Both candidates meet threshold, p1 has higher priority
        candidates = [
            SearchResult(
                provider="Provider 1",
                title="First Provider",
                provider_key="p1",
                raw={"__matching__": {"score": 90, "total": 90}}
            ),
            SearchResult(
                provider="Provider 2",
                title="Second Provider",
                provider_key="p2",
                raw={"__matching__": {"score": 95, "total": 95}}
            ),
        ]
        
        # p1 is first in hierarchy
        providers = [
            ("p1", None, None, "Provider 1"),
            ("p2", None, None, "Provider 2"),
        ]
        
        with patch("main.selection.get_min_title_score", return_value=60):
            selected, selected_tuple = select_best_candidate(
                candidates, providers, min_title_score=60
            )
        
        assert selected is not None
        # Provider priority trumps score
        assert selected.provider_key == "p1"
    
    def test_respects_provider_hierarchy(self):
        """Test that provider hierarchy is respected."""
        from main.selection import select_best_candidate
        
        candidates = [
            SearchResult(
                provider="Low Priority",
                title="Same Score",
                provider_key="p2",
                raw={"__matching__": {"score": 90, "total": 90}}
            ),
            SearchResult(
                provider="High Priority",
                title="Same Score",
                provider_key="p1",
                raw={"__matching__": {"score": 90, "total": 90}}
            ),
        ]
        
        # p1 has higher priority (lower index)
        providers = [
            ("p1", None, None, "High Priority"),
            ("p2", None, None, "Low Priority"),
        ]
        
        with patch("main.selection.get_min_title_score", return_value=60):
            selected, _ = select_best_candidate(
                candidates, providers, min_title_score=60
            )
        
        assert selected.provider_key == "p1"
    
    def test_filters_below_threshold(self):
        """Test that candidates below threshold are filtered."""
        from main.selection import select_best_candidate
        
        candidates = [
            SearchResult(
                provider="Provider",
                title="Low Score",
                provider_key="p1",
                raw={"__matching__": {"score": 50, "total": 50}}
            ),
        ]
        
        providers = [("p1", None, None, "Provider")]
        
        with patch("main.selection.get_min_title_score", return_value=80):
            selected, _ = select_best_candidate(
                candidates, providers, min_title_score=80
            )
        
        assert selected is None
    
    def test_returns_none_for_empty_candidates(self):
        """Test that None is returned for empty candidates list."""
        from main.selection import select_best_candidate
        
        selected, selected_tuple = select_best_candidate([], [], min_title_score=80)
        
        assert selected is None
        assert selected_tuple is None


class TestAttachScores:
    """Tests for score attachment to search results."""
    
    def test_attach_scores(self):
        """Test attaching scores to search result."""
        from main.selection import attach_scores
        
        sr = SearchResult(
            provider="Test",
            title="The Art of Cooking",
            creators=["John Smith"],
            provider_key="test",
            raw={}
        )
        
        attach_scores(sr, "The Art of Cooking", "John Smith", creator_weight=0.2)
        
        assert "__matching__" in sr.raw
        assert "score" in sr.raw["__matching__"]
        assert "boost" in sr.raw["__matching__"]
        assert "total" in sr.raw["__matching__"]
    
    def test_attach_scores_perfect_match(self):
        """Test scores for perfect match."""
        from main.selection import attach_scores
        
        sr = SearchResult(
            provider="Test",
            title="Exact Title",
            creators=["Same Creator"],
            provider_key="test",
            raw={}
        )
        
        attach_scores(sr, "Exact Title", "Same Creator", creator_weight=0.2)
        
        # combined_match_score returns 100 for perfect title + creator match
        assert sr.raw["__matching__"]["score"] == 100


class TestPrepareSearchResult:
    """Tests for search result preparation."""
    
    def test_prepare_search_result(self):
        """Test preparing a search result from raw data."""
        from main.selection import prepare_search_result
        
        raw_data = {
            "title": "Test Title",
            "id": "test123",
            "creator": "Test Author"
        }
        
        sr = prepare_search_result("test_provider", "Test Provider", raw_data)
        
        assert isinstance(sr, SearchResult)
        assert sr.provider == "Test Provider"
        assert sr.provider_key == "test_provider"
        assert sr.title == "Test Title"


class TestCallSearchFunction:
    """Tests for search function calling."""
    
    def test_calls_with_correct_args(self):
        """Test that search function is called with correct arguments."""
        from main.selection import call_search_function
        
        mock_search = MagicMock(return_value=[])
        
        call_search_function(mock_search, "Title", "Creator", 5)
        
        mock_search.assert_called_once()
        call_args = mock_search.call_args
        assert call_args[0][0] == "Title"  # title
    
    def test_handles_typeerror_fallbacks(self):
        """Test that TypeError triggers fallback signatures."""
        from main.selection import call_search_function
        
        # Search function that only accepts title
        def simple_search(title):
            return [{"title": title}]
        
        result = call_search_function(simple_search, "Title", "Creator", 5)
        
        assert len(result) == 1
