"""Regression tests for the pure-title matching gate (audit B6, decision 10).

Pre-fix, ``score_candidate`` stored the creator-weighted combined score under
``"score"`` and both selection gates compared it against ``min_title_score``.
With ``creator_weight=0.2`` a 100%% title match lacking creator metadata scored
only 80 and was rejected under the default threshold of 85. The gate must key
on the PURE title score; creator similarity is a ranking bonus only.
"""

from __future__ import annotations

import pytest

from api.model import SearchResult
from main.orchestration.selection import score_candidate


def _sr(title: str, creators: list[str] | None = None) -> SearchResult:
    return SearchResult(
        provider="Test",
        title=title,
        creators=creators or [],
        provider_key="test",
        raw={},
    )


def test_pure_title_gate_ignores_missing_creator() -> None:
    """A 100%% title match with no creator metadata gates on the full title."""
    sr = _sr("The Art of Cooking")
    result = score_candidate(
        sr, "The Art of Cooking", query_creator="Some Author", creator_weight=0.2
    )
    # Pre-fix this was 80 (100 * (1 - 0.2)); post-fix it is the full title score.
    assert result["score"] == pytest.approx(100.0)
    assert result["score"] >= 85  # accepted under the default threshold


def test_creator_is_ranking_bonus_not_penalty() -> None:
    """Creator match boosts the ranking; missing creator never penalizes."""
    with_creator = _sr("Ancient Recipes", creators=["Jane Doe"])
    without_creator = _sr("Ancient Recipes", creators=[])

    s_with = score_candidate(with_creator, "Ancient Recipes", "Jane Doe", 0.2)
    s_without = score_candidate(without_creator, "Ancient Recipes", "Jane Doe", 0.2)

    # The gate value (pure title) is identical regardless of creator presence.
    assert s_with["score"] == pytest.approx(s_without["score"])
    # A matching creator boosts the ranking total...
    assert s_with["total"] > s_without["total"]
    # ...but a missing creator never drops the ranking below the title score.
    assert s_without["total"] >= s_without["score"]
