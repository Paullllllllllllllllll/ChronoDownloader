"""Extended tests for main.selection module — candidate scoring and ranking."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from api.model import SearchResult
from main.selection import (
    _collect_candidates_exhaustive,
    _collect_candidates_parallel,
    _get_max_parallel_searches,
    _search_single_provider,
    attach_scores,
    collect_candidates_all,
    get_max_results_for_provider,
    score_candidate,
    select_best_candidate,
)


def _make_sr(title="Test", provider_key="ia", source_id="id1",
             iiif_manifest=None, item_url=None, creators=None):
    return SearchResult(
        provider="Test Provider",
        title=title,
        creators=creators or [],
        source_id=source_id,
        iiif_manifest=iiif_manifest,
        item_url=item_url,
        provider_key=provider_key,
        raw={},
    )


def _make_provider_tuple(key="ia", name="Internet Archive"):
    return (key, MagicMock(name=f"search_{key}"), MagicMock(name=f"download_{key}"), name)


# ============================================================================
# score_candidate
# ============================================================================

class TestScoreCandidate:
    """Tests for individual candidate scoring."""

    def test_basic_scoring(self):
        sr = _make_sr(title="The Art of Cooking")
        result = score_candidate(sr, "The Art of Cooking", None, 0.2)
        assert "score" in result
        assert "boost" in result
        assert "total" in result
        assert result["score"] >= 80

    def test_iiif_manifest_boost(self):
        sr_with = _make_sr(title="Test", iiif_manifest="https://example.org/manifest")
        sr_without = _make_sr(title="Test")
        s_with = score_candidate(sr_with, "Test", None, 0.2)
        s_without = score_candidate(sr_without, "Test", None, 0.2)
        assert s_with["boost"] > s_without["boost"]
        assert s_with["boost"] >= 3.0

    def test_item_url_boost(self):
        sr_with = _make_sr(title="Test", item_url="https://example.org/item")
        sr_without = _make_sr(title="Test")
        s_with = score_candidate(sr_with, "Test", None, 0.2)
        s_without = score_candidate(sr_without, "Test", None, 0.2)
        assert s_with["boost"] > s_without["boost"]

    def test_total_equals_score_plus_boost(self):
        sr = _make_sr(title="Test", iiif_manifest="url", item_url="url")
        result = score_candidate(sr, "Test", None, 0.2)
        assert result["total"] == pytest.approx(result["score"] + result["boost"])


# ============================================================================
# attach_scores
# ============================================================================

class TestAttachScores:
    """Tests for attaching scores to SearchResult raw dict."""

    def test_attaches_matching_dict(self):
        sr = _make_sr(title="Test Book")
        attach_scores(sr, "Test Book", None, 0.2)
        assert "__matching__" in sr.raw
        assert "score" in sr.raw["__matching__"]
        assert "boost" in sr.raw["__matching__"]
        assert "total" in sr.raw["__matching__"]

    def test_preserves_existing_raw_data(self):
        sr = _make_sr(title="Test")
        sr.raw["existing_key"] = "value"
        attach_scores(sr, "Test", None, 0.2)
        assert sr.raw["existing_key"] == "value"


# ============================================================================
# get_max_results_for_provider
# ============================================================================

class TestGetMaxResultsForProvider:
    """Tests for provider-specific max_results."""

    @patch("main.selection.get_provider_setting", return_value="10")
    def test_returns_configured_value(self, mock_setting):
        assert get_max_results_for_provider("ia") == 10

    @patch("main.selection.get_provider_setting", return_value=None)
    def test_returns_default_when_not_configured(self, mock_setting):
        assert get_max_results_for_provider("ia") == 5

    @patch("main.selection.get_provider_setting", return_value="invalid")
    def test_returns_default_on_invalid_value(self, mock_setting):
        assert get_max_results_for_provider("ia") == 5

    @patch("main.selection.get_provider_setting", return_value=None)
    def test_custom_default(self, mock_setting):
        assert get_max_results_for_provider("ia", default=10) == 10


# ============================================================================
# _get_max_parallel_searches
# ============================================================================

class TestGetMaxParallelSearches:
    """Tests for parallel search config."""

    @patch("main.selection.get_config", return_value={"selection": {"max_parallel_searches": 4}})
    def test_returns_configured_value(self, mock_cfg):
        assert _get_max_parallel_searches() == 4

    @patch("main.selection.get_config", return_value={})
    def test_returns_1_when_not_configured(self, mock_cfg):
        assert _get_max_parallel_searches() == 1

    @patch("main.selection.get_config", return_value={"selection": {"max_parallel_searches": 0}})
    def test_minimum_is_1(self, mock_cfg):
        assert _get_max_parallel_searches() == 1

    @patch("main.selection.get_config", return_value={"selection": {"max_parallel_searches": "invalid"}})
    def test_returns_1_on_invalid(self, mock_cfg):
        assert _get_max_parallel_searches() == 1


# ============================================================================
# _search_single_provider
# ============================================================================

class TestSearchSingleProvider:
    """Tests for single-provider search in parallel mode."""

    @patch("main.selection.get_max_results_for_provider", return_value=5)
    def test_returns_scored_candidates(self, mock_max):
        search_fn = MagicMock(return_value=[
            SearchResult(provider="IA", title="Test Book", raw={}, creators=[]),
        ])
        provider_tuple = ("ia", search_fn, MagicMock(), "Internet Archive")
        pkey, pname, candidates = _search_single_provider(
            provider_tuple, "Test Book", None, 5, 0.2
        )
        assert pkey == "ia"
        assert pname == "Internet Archive"
        assert len(candidates) == 1
        assert "__matching__" in candidates[0].raw

    @patch("main.selection.get_max_results_for_provider", return_value=5)
    def test_handles_search_exception(self, mock_max):
        search_fn = MagicMock(side_effect=Exception("API error"))
        provider_tuple = ("ia", search_fn, MagicMock(), "Internet Archive")
        pkey, pname, candidates = _search_single_provider(
            provider_tuple, "Test Book", None, 5, 0.2
        )
        assert candidates == []

    @patch("main.selection.get_max_results_for_provider", return_value=5)
    def test_handles_empty_results(self, mock_max):
        search_fn = MagicMock(return_value=[])
        provider_tuple = ("ia", search_fn, MagicMock(), "Internet Archive")
        pkey, pname, candidates = _search_single_provider(
            provider_tuple, "Test", None, 5, 0.2
        )
        assert candidates == []


# ============================================================================
# _collect_candidates_exhaustive
# ============================================================================

class TestCollectCandidatesExhaustive:
    """Tests for exhaustive sequential candidate collection."""

    @patch("main.selection.get_max_results_for_provider", return_value=5)
    def test_searches_all_providers(self, mock_max):
        sr1 = SearchResult(provider="IA", title="Test", raw={}, creators=[])
        sr2 = SearchResult(provider="MDZ", title="Test", raw={}, creators=[])
        p1 = ("ia", MagicMock(return_value=[sr1]), MagicMock(), "Internet Archive")
        p2 = ("mdz", MagicMock(return_value=[sr2]), MagicMock(), "MDZ")
        candidates = _collect_candidates_exhaustive([p1, p2], "Test", None, 0.2, 5)
        assert len(candidates) == 2

    @patch("main.selection.get_max_results_for_provider", return_value=5)
    def test_handles_empty_results_from_provider(self, mock_max):
        p1 = ("ia", MagicMock(return_value=[]), MagicMock(), "Internet Archive")
        candidates = _collect_candidates_exhaustive([p1], "Test", None, 0.2, 5)
        assert candidates == []

    @patch("main.selection.get_max_results_for_provider", return_value=5)
    def test_handles_provider_exception(self, mock_max):
        p1 = ("ia", MagicMock(side_effect=Exception("error")), MagicMock(), "IA")
        candidates = _collect_candidates_exhaustive([p1], "Test", None, 0.2, 5)
        assert candidates == []


# ============================================================================
# collect_candidates_all
# ============================================================================

class TestCollectCandidatesAll:
    """Tests for the collect_candidates_all dispatch."""

    @patch("main.selection._get_max_parallel_searches", return_value=1)
    @patch("main.selection._collect_candidates_exhaustive")
    def test_uses_sequential_when_parallel_is_1(self, mock_seq, mock_parallel):
        mock_seq.return_value = []
        collect_candidates_all([], "Test", None, 0.2, 5)
        mock_seq.assert_called_once()

    @patch("main.selection._get_max_parallel_searches", return_value=1)
    @patch("main.selection._collect_candidates_exhaustive")
    def test_uses_sequential_for_single_provider(self, mock_seq, mock_parallel):
        mock_seq.return_value = []
        provider = _make_provider_tuple()
        collect_candidates_all([provider], "Test", None, 0.2, 5)
        mock_seq.assert_called_once()


# ============================================================================
# select_best_candidate
# ============================================================================

class TestSelectBestCandidateExtended:
    """Extended tests for best candidate selection logic."""

    def test_returns_none_for_empty_candidates(self):
        selected, provider_tuple = select_best_candidate([], [], 85)
        assert selected is None
        assert provider_tuple is None

    def test_filters_below_threshold(self):
        sr = _make_sr(title="Completely Different Title")
        sr.raw["__matching__"] = {"score": 10, "total": 10}
        provider = _make_provider_tuple()
        selected, _ = select_best_candidate([sr], [provider], 85)
        assert selected is None

    @patch("main.selection.get_min_title_score", return_value=85)
    def test_selects_highest_scoring_candidate(self, mock_min):
        sr1 = _make_sr(title="Test", provider_key="ia", source_id="1")
        sr1.raw["__matching__"] = {"score": 90, "total": 93}
        sr2 = _make_sr(title="Test", provider_key="ia", source_id="2")
        sr2.raw["__matching__"] = {"score": 95, "total": 98}
        provider = _make_provider_tuple()
        selected, _ = select_best_candidate([sr1, sr2], [provider], 85)
        assert selected.source_id == "2"

    @patch("main.selection.get_min_title_score", return_value=85)
    def test_respects_provider_priority(self, mock_min):
        sr1 = _make_sr(title="Test", provider_key="ia", source_id="1")
        sr1.raw["__matching__"] = {"score": 90, "total": 95}
        sr2 = _make_sr(title="Test", provider_key="mdz", source_id="2")
        sr2.raw["__matching__"] = {"score": 90, "total": 95}
        p_mdz = _make_provider_tuple("mdz", "MDZ")
        p_ia = _make_provider_tuple("ia", "Internet Archive")
        # MDZ first in priority
        selected, pt = select_best_candidate([sr1, sr2], [p_mdz, p_ia], 85)
        assert selected.provider_key == "mdz"

    @patch("main.selection.get_min_title_score", return_value=50)
    def test_per_provider_threshold(self, mock_min):
        """Per-provider threshold should be used."""
        sr = _make_sr(title="Test", provider_key="ia")
        sr.raw["__matching__"] = {"score": 60, "total": 60}
        provider = _make_provider_tuple()
        selected, _ = select_best_candidate([sr], [provider], 85)
        assert selected is not None
