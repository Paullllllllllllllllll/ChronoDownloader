"""Tests for the edition-year ranking penalty (``selection.year_tolerance``).

The research unit is the EDITION, so a modern reprint must not outrank a
period edition of the same title. The penalty implementing that preference is
a ranking term only -- it never gates -- and it fails open in every direction
so that early modern imprints with messy or absent dates are never
disadvantaged. These tests pin the year extraction, the penalty shape, the
fail-open matrix, the config semantics, the CSV plumbing, and dormancy (a CSV
without a year column ranks exactly as it did before the feature existed).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import api.core.config as core_config
from api.core.config import DEFAULT_YEAR_TOLERANCE, get_year_tolerance
from api.matching import extract_year
from api.model import SearchResult
from main.cli.overrides import (
    _apply_runtime_config_overrides,
    _looks_like_cli_invocation,
)
from main.cli.parser import create_cli_parser
from main.data.works_csv import get_row_year
from main.orchestration import pipeline, selection
from main.orchestration.execution import _parse_work_row, _run_sequential
from main.orchestration.selection import (
    MAX_YEAR_PENALTY,
    YEAR_PENALTY_PER_YEAR,
    attach_scores,
    collect_candidates_all,
    score_candidate,
    select_best_candidate,
    year_penalty,
)


@pytest.fixture(autouse=True)
def _restore_config_cache() -> Iterator[None]:
    """Snapshot and restore the module-level config cache around each test."""
    saved = core_config._CONFIG_CACHE
    try:
        yield
    finally:
        core_config._CONFIG_CACHE = saved


def _set_tolerance(value: Any) -> None:
    core_config._CONFIG_CACHE = {"selection": {"year_tolerance": value}}


def _result(
    title: str = "Le Cuisinier francois",
    date: str | None = None,
    provider_key: str = "p1",
    source_id: str = "s1",
) -> SearchResult:
    return SearchResult(
        provider="Provider",
        title=title,
        date=date,
        source_id=source_id,
        provider_key=provider_key,
        raw={},
    )


# ============================================================================
# extract_year
# ============================================================================


class TestExtractYear:
    """A four-digit year is read out of free-form imprint data, or nothing is."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1651", 1651),
            (1651, 1651),
            (1651.0, 1651),
            ("Paris: chez Pierre David, 1651", 1651),
            ("[1651]", 1651),
            ("circa 1651", 1651),
            ("1651?", 1651),
            ("A Paris, 1651.", 1651),
            ("  1651  ", 1651),
        ],
    )
    def test_plain_years(self, value: Any, expected: int) -> None:
        assert extract_year(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1651-1660", 1651),
            ("1651 - 1660", 1651),
            ("1651/1652", 1651),
            ("between 1651 and 1660", 1651),
            ("2019-03-01", 2019),
            ("1889-01-01T00:00:00Z", 1889),
        ],
    )
    def test_ranges_and_dates_take_the_opening_year(
        self, value: str, expected: int
    ) -> None:
        assert extract_year(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "S. XVIII",
            "s. XVII",
            "18. Jh.",
            "eighteenth century",
            "MDCLI",
            "M.DC.LI.",
            "s.d.",
            "n.d.",
            "unknown",
            "nan",
            float("nan"),
            True,
            [],
        ],
    )
    def test_fails_open_on_unparseable_values(self, value: Any) -> None:
        """Centuries, Roman numerals, verbal dates, and garbage yield None."""
        assert extract_year(value) is None

    @pytest.mark.parametrize("value", ["0999", "2101", "3000", "12345", "0042"])
    def test_rejects_implausible_years(self, value: str) -> None:
        assert extract_year(value) is None

    def test_first_plausible_run_wins(self) -> None:
        """An implausible leading run is skipped, not taken as the year."""
        assert extract_year("Vol. 3000, printed 1651") == 1651
        assert extract_year("shelfmark 12345, 1651") == 1651


# ============================================================================
# year_penalty: shape
# ============================================================================


class TestYearPenaltyShape:
    """Capped linear penalty beyond the tolerance window."""

    @pytest.mark.parametrize("candidate", [1640, 1645, 1650, 1655, 1660])
    def test_inside_window_is_free(self, candidate: int) -> None:
        assert year_penalty(1650, str(candidate), tolerance=10) == 0.0

    def test_boundary_is_inclusive(self) -> None:
        assert year_penalty(1650, "1660", tolerance=10) == 0.0
        assert year_penalty(1650, "1661", tolerance=10) == pytest.approx(
            YEAR_PENALTY_PER_YEAR
        )

    def test_grows_with_distance_beyond_the_window(self) -> None:
        penalties = [
            year_penalty(1650, str(1650 + d), tolerance=10) for d in (11, 20, 30, 40)
        ]
        assert penalties == sorted(penalties)
        assert penalties[0] < penalties[-1]
        assert year_penalty(1650, "1670", tolerance=10) == pytest.approx(
            10 * YEAR_PENALTY_PER_YEAR
        )

    def test_is_capped(self) -> None:
        far = year_penalty(1651, "2019", tolerance=10)
        farther = year_penalty(1651, "2100", tolerance=10)
        assert far == MAX_YEAR_PENALTY
        assert farther == MAX_YEAR_PENALTY

    def test_cap_is_reached_exactly_at_its_distance(self) -> None:
        span = int(MAX_YEAR_PENALTY / YEAR_PENALTY_PER_YEAR)
        assert year_penalty(1650, str(1650 + 10 + span - 1), tolerance=10) < (
            MAX_YEAR_PENALTY
        )
        assert year_penalty(1650, str(1650 + 10 + span), tolerance=10) == (
            MAX_YEAR_PENALTY
        )

    def test_is_symmetric(self) -> None:
        assert year_penalty(1650, "1600", tolerance=5) == year_penalty(
            1650, "1700", tolerance=5
        )

    def test_zero_tolerance_prefers_the_exact_year(self) -> None:
        assert year_penalty(1651, "1651", tolerance=0) == 0.0
        assert year_penalty(1651, "1652", tolerance=0) == pytest.approx(
            YEAR_PENALTY_PER_YEAR
        )

    def test_negative_tolerance_behaves_like_zero(self) -> None:
        assert year_penalty(1651, "1651", tolerance=-5) == 0.0
        assert year_penalty(1651, "1652", tolerance=-5) == pytest.approx(
            YEAR_PENALTY_PER_YEAR
        )


# ============================================================================
# year_penalty: fail-open matrix
# ============================================================================


class TestYearPenaltyFailsOpen:
    """Missing or unreadable year information is never held against a candidate."""

    @pytest.mark.parametrize(
        ("query_year", "candidate_date"),
        [
            (None, "2019"),
            (None, None),
            (1651, None),
            (1651, ""),
            (1651, "S. XVIII"),
            (1651, "18. Jh."),
            (1651, "s.d."),
            (1651, "MDCLI"),
        ],
    )
    def test_zero_penalty(self, query_year: int | None, candidate_date: str) -> None:
        assert year_penalty(query_year, candidate_date, tolerance=10) == 0.0

    def test_undated_candidate_never_outranks_a_matching_one(self) -> None:
        """The term is a penalty only, so an absent date is never a bonus."""
        _set_tolerance(10)
        dated = score_candidate(
            _result(date="1651"), "Le Cuisinier francois", None, 0.2, 1651
        )
        undated = score_candidate(
            _result(date=None), "Le Cuisinier francois", None, 0.2, 1651
        )
        assert undated["total"] <= dated["total"]
        assert undated["year_penalty"] == 0.0
        assert dated["year_penalty"] == 0.0


# ============================================================================
# score_candidate
# ============================================================================


class TestScoreCandidateYearTerm:
    """The penalty enters the ranking total and nothing else."""

    def test_dormant_without_a_query_year(self) -> None:
        _set_tolerance(0)
        scores = score_candidate(
            _result(date="2019"), "Le Cuisinier francois", None, 0.2
        )
        assert scores["year_penalty"] == 0.0
        assert scores["candidate_year"] is None
        assert scores["total"] == pytest.approx(
            scores["title_score"] + scores["creator_bonus"] + scores["boost"]
        )

    def test_no_config_read_without_a_query_year(self) -> None:
        """Dormancy is total: an absent query year reads no configuration."""
        with patch(
            "main.orchestration.selection.get_year_tolerance",
            side_effect=AssertionError("config must not be read"),
        ):
            score_candidate(_result(date="2019"), "Le Cuisinier francois", None, 0.2)

    def test_penalty_is_subtracted_from_the_total(self) -> None:
        _set_tolerance(10)
        scores = score_candidate(
            _result(date="2019"), "Le Cuisinier francois", None, 0.2, 1651
        )
        assert scores["year_penalty"] == MAX_YEAR_PENALTY
        assert scores["candidate_year"] == 2019
        assert scores["total"] == pytest.approx(
            scores["title_score"]
            + scores["creator_bonus"]
            + scores["boost"]
            - MAX_YEAR_PENALTY
        )

    def test_gate_score_is_the_pure_title_score(self) -> None:
        """``min_title_score`` reads ``score``, which the penalty must not touch."""
        _set_tolerance(0)
        penalized = score_candidate(
            _result(date="2019"), "Le Cuisinier francois", None, 0.2, 1651
        )
        clean = score_candidate(
            _result(date="1651"), "Le Cuisinier francois", None, 0.2, 1651
        )
        assert penalized["score"] == clean["score"] == 100
        assert penalized["title_score"] == clean["title_score"]

    def test_tolerance_comes_from_config(self) -> None:
        _set_tolerance(500)
        scores = score_candidate(
            _result(date="2019"), "Le Cuisinier francois", None, 0.2, 1651
        )
        assert scores["year_penalty"] == 0.0


# ============================================================================
# Ranking integration
# ============================================================================


def _search_for(*results: SearchResult) -> Any:
    def _search(
        title: str, creator: str | None = None, max_results: int = 5
    ) -> list[SearchResult]:
        return list(results)

    return _search


def _download_stub(*_args: Any, **_kwargs: Any) -> bool:
    return True


class TestRankingIntegration:
    """A period edition outranks a modern reprint of the same title."""

    def _candidates(self) -> list[SearchResult]:
        # The reprint is listed FIRST and carries the richer metadata boost, so
        # without the year term it wins on ranking order.
        reprint = SearchResult(
            provider="Provider",
            title="Le Cuisinier francois",
            date="2019",
            source_id="reprint",
            provider_key="p1",
            item_url="https://example.org/reprint",
            iiif_manifest="https://example.org/reprint/manifest.json",
            raw={},
        )
        period = SearchResult(
            provider="Provider",
            title="Le Cuisinier francois",
            date="1651",
            source_id="period",
            provider_key="p1",
            raw={},
        )
        return [reprint, period]

    def _select(self, candidates: list[SearchResult]) -> SearchResult | None:
        providers = [("p1", _download_stub, _download_stub, "Provider")]
        with patch("main.orchestration.selection.get_min_title_score", return_value=35):
            selected, _tuple = select_best_candidate(candidates, providers, 35)
        return selected

    def test_period_edition_beats_reprint_when_a_year_is_known(self) -> None:
        _set_tolerance(10)
        candidates = self._candidates()
        for sr in candidates:
            attach_scores(sr, "Le Cuisinier francois", None, 0.2, 1651)

        selected = self._select(candidates)
        assert selected is not None
        assert selected.source_id == "period"

    def test_reprint_wins_again_when_no_year_is_supplied(self) -> None:
        """Dormancy: without a query year the pre-feature ranking is restored."""
        _set_tolerance(10)
        candidates = self._candidates()
        for sr in candidates:
            attach_scores(sr, "Le Cuisinier francois", None, 0.2)

        selected = self._select(candidates)
        assert selected is not None
        assert selected.source_id == "reprint"

    def test_dormant_totals_are_identical_to_the_pre_feature_formula(self) -> None:
        _set_tolerance(0)
        candidates = self._candidates()
        for sr in candidates:
            attach_scores(sr, "Le Cuisinier francois", None, 0.2)
        for sr in candidates:
            scores = sr.raw["__matching__"]
            assert scores["total"] == pytest.approx(
                scores["title_score"] + scores["creator_bonus"] + scores["boost"]
            )

    def test_gate_is_unchanged_by_the_penalty(self) -> None:
        """A wrongly-dated candidate still clears (and a bad title still fails)."""
        _set_tolerance(0)
        wrong_year = SearchResult(
            provider="Provider",
            title="Le Cuisinier francois",
            date="2019",
            source_id="reprint",
            provider_key="p1",
            raw={},
        )
        wrong_title = SearchResult(
            provider="Provider",
            title="A Treatise on Naval Gunnery",
            date="1651",
            source_id="unrelated",
            provider_key="p1",
            raw={},
        )
        for sr in (wrong_year, wrong_title):
            attach_scores(sr, "Le Cuisinier francois", None, 0.2, 1651)

        # The unrelated title is rejected by the gate on its title score alone,
        # and the heavily penalized reprint still clears it.
        assert wrong_title.raw["__matching__"]["score"] < 35
        assert wrong_year.raw["__matching__"]["score"] >= 35
        assert wrong_year.raw["__matching__"]["year_penalty"] == MAX_YEAR_PENALTY

        selected = self._select([wrong_year, wrong_title])
        assert selected is not None
        assert selected.source_id == "reprint"

    def test_penalty_applies_through_collect_candidates_all(self) -> None:
        """The query year reaches scoring through the real collection path."""
        core_config._CONFIG_CACHE = {
            "selection": {"year_tolerance": 10, "max_parallel_searches": 1}
        }
        reprint, period = self._candidates()
        providers = [
            (
                "p1",
                _search_for(reprint, period),
                _download_stub,
                "Provider",
            )
        ]
        candidates = collect_candidates_all(
            providers, "Le Cuisinier francois", None, 0.2, 5, 1651
        )
        by_id = {c.source_id: c.raw["__matching__"] for c in candidates}
        assert by_id["period"]["year_penalty"] == 0.0
        assert by_id["reprint"]["year_penalty"] == MAX_YEAR_PENALTY
        assert by_id["period"]["total"] > by_id["reprint"]["total"]

    def test_parallel_fan_out_carries_the_query_year(self) -> None:
        core_config._CONFIG_CACHE = {"selection": {"year_tolerance": 10}}
        reprint, period = self._candidates()
        providers = [
            ("p1", _search_for(reprint), _download_stub, "One"),
            ("p2", _search_for(period), _download_stub, "Two"),
        ]
        candidates = selection._collect_candidates_parallel(
            providers, "Le Cuisinier francois", None, 0.2, 5, 2, 1651
        )
        by_id = {c.source_id: c.raw["__matching__"] for c in candidates}
        assert by_id["reprint"]["year_penalty"] == MAX_YEAR_PENALTY
        assert by_id["period"]["year_penalty"] == 0.0


class TestPipelinePlumbing:
    """The query year survives the pipeline's own call chain."""

    def test_search_work_forwards_the_year(self) -> None:
        core_config._CONFIG_CACHE = {"selection": {}}
        with patch(
            "main.orchestration.pipeline.collect_candidates_all", return_value=[]
        ) as mock_collect:
            pipeline.search_work("Le Cuisinier francois", None, "E1", query_year=1651)
        assert mock_collect.call_args.args[-1] == 1651

    def test_search_work_defaults_to_no_year(self) -> None:
        core_config._CONFIG_CACHE = {"selection": {}}
        with patch(
            "main.orchestration.pipeline.collect_candidates_all", return_value=[]
        ) as mock_collect:
            pipeline.search_work("Le Cuisinier francois")
        assert mock_collect.call_args.args[-1] is None

    def test_prepare_work_forwards_the_year(self, temp_dir: str) -> None:
        core_config._CONFIG_CACHE = {"selection": {}, "download": {}}
        with patch(
            "main.orchestration.pipeline._collect_and_select",
            return_value=([], None, None),
        ) as mock_select:
            pipeline._prepare_work("Le Cuisinier francois", None, "E1", temp_dir, 1651)
        assert mock_select.call_args.args[-1] == 1651


# ============================================================================
# Configuration semantics
# ============================================================================


class TestGetYearTolerance:
    """Absent key, explicit zero, negative, and non-numeric values."""

    def test_absent_key_uses_the_documented_default(self) -> None:
        core_config._CONFIG_CACHE = {"selection": {}}
        assert get_year_tolerance() == DEFAULT_YEAR_TOLERANCE

    def test_absent_section_uses_the_documented_default(self) -> None:
        core_config._CONFIG_CACHE = {}
        assert get_year_tolerance() == DEFAULT_YEAR_TOLERANCE

    def test_explicit_zero_is_honored(self) -> None:
        _set_tolerance(0)
        assert get_year_tolerance() == 0

    def test_explicit_value_is_honored(self) -> None:
        _set_tolerance(25)
        assert get_year_tolerance() == 25

    def test_numeric_string_is_coerced(self) -> None:
        _set_tolerance("5")
        assert get_year_tolerance() == 5

    def test_negative_is_clamped(self) -> None:
        _set_tolerance(-3)
        assert get_year_tolerance() == 0

    def test_non_numeric_falls_back(self) -> None:
        _set_tolerance("a decade or so")
        assert get_year_tolerance() == DEFAULT_YEAR_TOLERANCE


# ============================================================================
# Works-CSV plumbing
# ============================================================================


class TestGetRowYear:
    """The query year is sourced from an optional CSV column."""

    def test_year_column(self) -> None:
        row = pd.Series({"entry_id": "E1", "short_title": "T", "year": 1651})
        assert get_row_year(row) == 1651

    def test_earliest_year_column_is_ignored(self) -> None:
        """The sampling frame's first-attestation year must not activate
        the penalty: it is not the edition sought."""
        row = pd.Series({"entry_id": "E1", "earliest_year": "1651"})
        assert get_row_year(row) is None

    def test_year_wins_regardless_of_earliest_year(self) -> None:
        row = pd.Series({"year": 1660, "earliest_year": 1651})
        assert get_row_year(row) == 1660

    def test_blank_year_stays_dormant_despite_earliest_year(self) -> None:
        row = pd.Series({"year": "", "earliest_year": 1651})
        assert get_row_year(row) is None

    @pytest.mark.parametrize("column", ["Year", "YEAR", "yEaR"])
    def test_column_lookup_is_case_insensitive(self, column: str) -> None:
        row = pd.Series({column: 1651})
        assert get_row_year(row) == 1651

    def test_absent_column_leaves_the_feature_dormant(self) -> None:
        row = pd.Series({"entry_id": "E1", "short_title": "T", "main_author": "A"})
        assert get_row_year(row) is None

    @pytest.mark.parametrize("value", [pd.NA, float("nan"), "", "n.d.", "S. XVIII"])
    def test_unusable_cells_yield_none(self, value: Any) -> None:
        row = pd.Series({"year": value})
        assert get_row_year(row) is None

    def test_float_cell_from_pandas_inference(self) -> None:
        row = pd.Series({"year": 1651.0})
        assert get_row_year(row) == 1651


class TestParseWorkRow:
    """``_parse_work_row`` carries the year alongside the search fields."""

    def test_year_is_returned_when_present(self) -> None:
        row = pd.Series(
            {"short_title": "T", "main_author": "A", "entry_id": "E1", "year": 1651}
        )
        with patch(
            "main.orchestration.execution.is_direct_download_enabled",
            return_value=False,
        ):
            parsed = _parse_work_row(row, 0, logging.getLogger("test"))
        assert parsed is not None
        assert parsed[5] == 1651

    def test_year_is_none_without_a_column(self) -> None:
        row = pd.Series({"short_title": "T", "main_author": "A", "entry_id": "E1"})
        with patch(
            "main.orchestration.execution.is_direct_download_enabled",
            return_value=False,
        ):
            parsed = _parse_work_row(row, 0, logging.getLogger("test"))
        assert parsed is not None
        assert parsed[5] is None


class TestBatchPlumbing:
    """The batch runner forwards the CSV year to the pipeline."""

    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.pipeline")
    def test_sequential_forwards_the_year(
        self, mock_pipeline: MagicMock, _mock_direct: MagicMock
    ) -> None:
        mock_pipeline.process_work.return_value = {
            "status": "completed",
            "item_url": "https://example.org",
            "provider": "IA",
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Le Cuisinier francois"],
                "main_author": ["La Varenne"],
                "entry_id": ["E001"],
                "year": [1651],
            }
        )
        _run_sequential(works_df, "/output", False, logging.getLogger("test"))
        assert mock_pipeline.process_work.call_args.kwargs["query_year"] == 1651

    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.pipeline")
    def test_sequential_passes_none_without_a_year_column(
        self, mock_pipeline: MagicMock, _mock_direct: MagicMock
    ) -> None:
        mock_pipeline.process_work.return_value = {
            "status": "completed",
            "item_url": "https://example.org",
            "provider": "IA",
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Le Cuisinier francois"],
                "main_author": ["La Varenne"],
                "entry_id": ["E001"],
            }
        )
        _run_sequential(works_df, "/output", False, logging.getLogger("test"))
        assert mock_pipeline.process_work.call_args.kwargs["query_year"] is None


# ============================================================================
# CLI wiring
# ============================================================================


class TestCliOverride:
    """``--year-tolerance`` overrides the config value for one run."""

    def test_parser_accepts_the_flag(self) -> None:
        args = create_cli_parser().parse_args(["--year-tolerance", "3"])
        assert args.year_tolerance == 3

    def test_default_is_none(self) -> None:
        args = create_cli_parser().parse_args([])
        assert args.year_tolerance is None

    def test_override_reaches_the_config_cache(self) -> None:
        args = create_cli_parser().parse_args(["--year-tolerance", "3"])
        merged = _apply_runtime_config_overrides(
            args, {"selection": {"year_tolerance": 30}}, logging.getLogger("test")
        )
        assert merged["selection"]["year_tolerance"] == 3
        assert get_year_tolerance() == 3

    def test_negative_override_is_clamped(self) -> None:
        args = create_cli_parser().parse_args(["--year-tolerance=-4"])
        merged = _apply_runtime_config_overrides(
            args, {"selection": {}}, logging.getLogger("test")
        )
        assert merged["selection"]["year_tolerance"] == 0

    def test_absent_flag_leaves_the_config_value_alone(self) -> None:
        args = create_cli_parser().parse_args([])
        merged = _apply_runtime_config_overrides(
            args, {"selection": {"year_tolerance": 30}}, logging.getLogger("test")
        )
        assert merged["selection"]["year_tolerance"] == 30

    def test_flag_is_recognized_as_a_cli_invocation(self) -> None:
        assert _looks_like_cli_invocation(["--year-tolerance", "3"])
        assert _looks_like_cli_invocation(["--year-tolerance=3"])
