"""Regression tests for the pure-title matching gate (audit B6, decision 10).

Pre-fix, ``score_candidate`` stored the creator-weighted combined score under
``"score"`` and both selection gates compared it against ``min_title_score``.
With ``creator_weight=0.2`` a 100%% title match lacking creator metadata scored
only 80 and was rejected under the default threshold of 85. The gate must key
on the PURE title score; creator similarity is a ranking bonus only.
"""

from __future__ import annotations

import pytest

from api.matching import title_score
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


def test_titleless_record_cannot_clear_the_shipped_gate() -> None:
    """A record with no title must score 0, not clear min_title_score.

    Connectors used to substitute the literal ``"N/A"`` (and MDZ/Wellcome the
    caller's own query) for a missing title. ``normalize_text("N/A")`` is
    ``"n a"``, which scores 35-36 against a short query -- at or above the 35
    that ``config.example.json`` ships -- so a record whose title could not be
    read outranked genuinely matching candidates. The token-set scorer leaves
    those numbers exactly where they were: none of these queries shares a
    token with ``"n a"``, so the empty intersection sends the comparison back
    to the plain sorted-token ratio.
    """
    for query in ("Almanach", "Analecta", "Manual de arte"):
        assert 35 <= title_score(query, "N/A") <= 36  # the sentinel is not inert
        assert score_candidate(_sr(""), query, None, 0.2)["score"] == pytest.approx(0.0)


def test_query_echo_would_score_a_perfect_match() -> None:
    """Echoing the query back as the title fakes a flawless hit."""
    query = "Le Cuisinier royal et bourgeois"
    assert score_candidate(_sr(query), query, None, 0.2)["score"] == pytest.approx(
        100.0
    )
    assert score_candidate(_sr(""), query, None, 0.2)["score"] == pytest.approx(0.0)
