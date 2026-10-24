"""The matching policy: candidate decisions, edition policy, preference order."""

from __future__ import annotations

from collections.abc import Sequence
from typing import get_args

import pytest

from chronodownloader import spec
from chronodownloader.matching.answers import Relation, WorkAnswer
from chronodownloader.matching.fuzzy import TextScore
from chronodownloader.matching.policy import (
    EDITION_POLICIES,
    MODES,
    REVIEW_REASONS,
    Candidate,
    CandidateDecision,
    EditionPolicy,
    Mode,
    ReviewReason,
    decide_candidate,
    decide_work,
    to_judge,
)
from chronodownloader.providers.base import SearchRecord
from chronodownloader.settings import JudgeThresholds

THRESHOLDS = JudgeThresholds(
    same_work_accept=0.9,
    same_work_reject=0.1,
    text_accept=110.0,
    text_reject=30.0,
    text_prefilter=30.0,
)


def _text(total: float, year_penalty: float = 0.0) -> TextScore:
    return TextScore(
        title=total,
        creator=0.0,
        creator_bonus=0.0,
        boost=0.0,
        year_penalty=year_penalty,
        candidate_year=None,
        total=total,
    )


def _cand(
    source_id: str,
    *,
    provider: str = "mdz",
    total: float = 80.0,
    same_work: float | None = None,
    relation: Relation = "other_edition",
    iiif: bool = False,
    year_penalty: float = 0.0,
) -> Candidate:
    answer = None
    if same_work is not None:
        answer = WorkAnswer(same_work=same_work, relation=relation)
    return Candidate(
        record=SearchRecord(provider=provider, source_id=source_id, title="T"),
        text=_text(total, year_penalty),
        iiif=iiif,
        answer=answer,
    )


def _ids(decisions: Sequence[CandidateDecision]) -> list[str]:
    return [d.candidate.record.source_id for d in decisions]


def test_option_values_match_the_cli_tables() -> None:
    assert spec.MATCHING_MODES == MODES == get_args(Mode.__value__)
    assert (
        spec.EDITION_POLICIES == EDITION_POLICIES == get_args(EditionPolicy.__value__)
    )
    assert get_args(ReviewReason.__value__) == REVIEW_REASONS


class TestCandidateDecision:
    @pytest.mark.parametrize(
        ("same_work", "expected"),
        [
            (1.0, "accept"),
            (0.9, "accept"),
            (0.89, "review"),
            (0.5, "review"),
            (0.11, "review"),
            (0.1, "reject"),
            (0.0, "reject"),
        ],
    )
    def test_judge_bands_are_inclusive(self, same_work: float, expected: str) -> None:
        got = decide_candidate(_cand("a", same_work=same_work), "judge", THRESHOLDS)
        assert (got.decision, got.basis) == (expected, "judge")

    @pytest.mark.parametrize(
        ("total", "expected"),
        [
            (123.5, "accept"),
            (110.0, "accept"),
            (109.9, "review"),
            (30.1, "review"),
            (30.0, "reject"),
            (-25.0, "reject"),
        ],
    )
    def test_fuzzy_bands_are_inclusive(self, total: float, expected: str) -> None:
        got = decide_candidate(_cand("a", total=total), "fuzzy", THRESHOLDS)
        assert (got.decision, got.basis) == (expected, "text")

    def test_fuzzy_ignores_a_judgment(self) -> None:
        got = decide_candidate(_cand("a", total=50, same_work=1.0), "fuzzy", THRESHOLDS)
        assert got.decision == "review"

    def test_null_text_thresholds_never_fire(self) -> None:
        open_ = JudgeThresholds(text_accept=None, text_reject=None, text_prefilter=None)
        for total in (-25.0, 0.0, 123.5):
            got = decide_candidate(_cand("a", total=total), "fuzzy", open_)
            assert got.decision == "review"

    def test_default_thresholds_never_accept_on_text(self) -> None:
        got = decide_candidate(_cand("a", total=123.5), "fuzzy", JudgeThresholds())
        assert got.decision == "review"
        got = decide_candidate(_cand("a", total=30.0), "fuzzy", JudgeThresholds())
        assert got.decision == "reject"

    def test_judge_never_accepts_on_text(self) -> None:
        got = decide_candidate(
            _cand("a", total=123.5, same_work=0.5), "judge", THRESHOLDS
        )
        assert got.decision == "review"
        got = decide_candidate(
            _cand("a", total=0.0, same_work=0.95), "judge", THRESHOLDS
        )
        assert got.decision == "accept"

    def test_hybrid_prefilter_rejects_without_a_judgment(self) -> None:
        got = decide_candidate(_cand("a", total=30.0), "hybrid", THRESHOLDS)
        assert (got.decision, got.basis) == ("reject", "prefilter")
        got = decide_candidate(
            _cand("a", total=30.0, same_work=1.0), "hybrid", THRESHOLDS
        )
        assert (got.decision, got.basis) == ("reject", "prefilter")

    def test_hybrid_never_accepts_on_text(self) -> None:
        got = decide_candidate(
            _cand("a", total=123.5, same_work=0.5), "hybrid", THRESHOLDS
        )
        assert (got.decision, got.basis) == ("review", "judge")

    @pytest.mark.parametrize("mode", ["judge", "hybrid"])
    def test_a_missing_judgment_is_an_error(self, mode: Mode) -> None:
        with pytest.raises(ValueError, match="mdz:a has no judgment"):
            decide_candidate(_cand("a", total=80.0), mode, THRESHOLDS)


class TestToJudge:
    def test_per_mode(self) -> None:
        cands = [_cand("low", total=30.0), _cand("high", total=30.5)]
        assert to_judge(cands, "judge", THRESHOLDS) == tuple(cands)
        assert to_judge(cands, "fuzzy", THRESHOLDS) == ()
        assert to_judge(cands, "hybrid", THRESHOLDS) == (cands[1],)

    def test_no_prefilter_judges_everything(self) -> None:
        cands = [_cand("low", total=-25.0)]
        open_ = JudgeThresholds(text_prefilter=None)
        assert to_judge(cands, "hybrid", open_) == tuple(cands)


def _work(
    cands: list[Candidate],
    mode: Mode = "judge",
    policy: EditionPolicy = "any_edition",
    order: tuple[str, ...] = (),
) -> tuple[str, str | None, str | None, list[str]]:
    out = decide_work(
        cands,
        mode=mode,
        thresholds=THRESHOLDS,
        edition_policy=policy,
        provider_order=order,
    )
    chosen = out.chosen.record.source_id if out.chosen else None
    return out.outcome, chosen, out.reason, _ids(out.decisions)


class TestEditionPolicy:
    @pytest.mark.parametrize(
        ("relation", "policy", "outcome", "reason"),
        [
            ("same_edition", "any_edition", "accepted", None),
            ("same_edition", "same_edition", "accepted", None),
            ("same_edition", "any_language", "accepted", None),
            ("other_edition", "any_edition", "accepted", None),
            ("other_edition", "same_edition", "needs_review", "other_edition"),
            ("other_edition", "any_language", "accepted", None),
            ("translation_or_adaptation", "any_edition", "needs_review", "translation"),
            (
                "translation_or_adaptation",
                "same_edition",
                "needs_review",
                "translation",
            ),
            ("translation_or_adaptation", "any_language", "accepted", None),
            ("different_work", "any_language", "needs_review", "relation_conflict"),
            ("insufficient", "any_edition", "needs_review", "relation_conflict"),
        ],
    )
    def test_relation_against_policy(
        self,
        relation: Relation,
        policy: EditionPolicy,
        outcome: str,
        reason: str | None,
    ) -> None:
        got = _work([_cand("a", same_work=0.95, relation=relation)], policy=policy)
        assert got[0] == outcome
        assert got[2] == reason
        assert got[1] == ("a" if outcome == "accepted" else None)

    def test_translation_goes_to_review_when_nothing_else_is_acceptable(self) -> None:
        cands = [
            _cand("translation", same_work=0.99, relation="translation_or_adaptation"),
            _cand("band", same_work=0.5),
            _cand("other", same_work=0.02, relation="different_work"),
        ]
        outcome, chosen, reason, order = _work(cands)
        assert (outcome, chosen, reason) == ("needs_review", None, "translation")
        assert order == ["translation", "band", "other"]

    def test_an_acceptable_edition_wins_over_a_held_translation(self) -> None:
        cands = [
            _cand("translation", same_work=0.99, relation="translation_or_adaptation"),
            _cand("edition", same_work=0.91, relation="other_edition"),
        ]
        assert _work(cands)[:2] == ("accepted", "edition")

    def test_fuzzy_relation_is_unknown(self) -> None:
        cands = [_cand("a", total=115.0)]
        assert _work(cands, "fuzzy")[:2] == ("accepted", "a")
        assert _work(cands, "fuzzy", "any_language")[:2] == ("accepted", "a")
        assert _work(cands, "fuzzy", "same_edition")[::2] == (
            "needs_review",
            "edition_unknown",
        )


class TestOutcome:
    def test_no_candidates_is_no_match(self) -> None:
        assert _work([]) == ("no_match", None, None, [])

    def test_all_rejected_is_no_match(self) -> None:
        cands = [_cand("a", same_work=0.05), _cand("b", same_work=0.1)]
        assert _work(cands)[:3] == ("no_match", None, None)

    def test_review_band_is_needs_review(self) -> None:
        cands = [_cand("a", same_work=0.05), _cand("b", same_work=0.6)]
        assert _work(cands)[:3] == ("needs_review", None, "review_band")

    def test_hybrid_prefiltered_candidates_are_rejected(self) -> None:
        cands = [_cand("noise", total=10.0), _cand("work", total=90.0, same_work=0.97)]
        outcome, chosen, _, order = _work(cands, "hybrid")
        assert (outcome, chosen, order) == ("accepted", "work", ["work", "noise"])


class TestPreference:
    def test_same_edition_before_other_edition(self) -> None:
        cands = [
            _cand("other", same_work=1.0, relation="other_edition", iiif=True),
            _cand("same", same_work=0.9, relation="same_edition"),
        ]
        assert _work(cands)[1] == "same"

    def test_probability_band_before_iiif(self) -> None:
        cands = [
            _cand("iiif", same_work=0.91, iiif=True),
            _cand("strong", same_work=0.99),
        ]
        assert _work(cands)[1] == "strong"

    def test_iiif_breaks_a_tie_within_a_band(self) -> None:
        cands = [
            _cand("plain", same_work=0.99, total=120.0),
            _cand("iiif", same_work=0.96, iiif=True, total=50.0),
        ]
        assert _work(cands)[1] == "iiif"

    @pytest.mark.parametrize(
        ("low", "high", "same_band"),
        [
            (0.90, 0.94, True),
            (0.94, 0.95, False),
            (0.95, 0.99, True),
            (0.99, 1.0, False),
        ],
    )
    def test_band_edges(self, low: float, high: float, same_band: bool) -> None:
        cands = [
            _cand("low", same_work=low, iiif=True),
            _cand("high", same_work=high),
        ]
        assert _work(cands)[1] == ("low" if same_band else "high")

    def test_provider_order_after_iiif(self) -> None:
        cands = [
            _cand("ia", provider="internet_archive", same_work=0.97),
            _cand("mdz", provider="mdz", same_work=0.98),
            _cand("unlisted", provider="loc", same_work=0.99),
        ]
        order = ("internet_archive", "mdz")
        assert _work(cands, order=order)[3] == ["ia", "mdz", "unlisted"]

    def test_ties_go_to_the_text_score_then_the_input_order(self) -> None:
        cands = [
            _cand("first", same_work=0.95, total=60.0),
            _cand("second", same_work=0.95, total=60.0),
            _cand("best", same_work=0.95, total=61.0),
        ]
        assert _work(cands)[3] == ["best", "first", "second"]

    def test_fuzzy_orders_by_year_distance_then_text(self) -> None:
        cands = [
            _cand("far", total=120.0, year_penalty=3.0, iiif=True),
            _cand("near_low", total=111.0),
            _cand("near_high", total=112.0),
        ]
        assert _work(cands, "fuzzy")[3] == ["near_high", "near_low", "far"]

    def test_fuzzy_iiif_and_provider_break_text_ties(self) -> None:
        cands = [
            _cand("ia", provider="internet_archive", total=115.0),
            _cand("mdz", provider="mdz", total=115.0),
            _cand("iiif", provider="loc", total=115.0, iiif=True),
        ]
        order = ("mdz", "internet_archive")
        assert _work(cands, "fuzzy", order=order)[3] == ["iiif", "mdz", "ia"]

    def test_groups_order_the_decisions(self) -> None:
        cands = [
            _cand("rejected", same_work=0.0),
            _cand("band", same_work=0.5),
            _cand("held", same_work=0.95, relation="translation_or_adaptation"),
            _cand("accepted", same_work=0.9),
        ]
        assert _work(cands)[3] == ["accepted", "held", "band", "rejected"]
        decisions = decide_work(cands, mode="judge", thresholds=THRESHOLDS).decisions
        assert [d.held for d in decisions] == [None, "translation", None, None]
