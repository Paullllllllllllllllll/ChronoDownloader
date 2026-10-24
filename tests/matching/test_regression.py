"""Regression gate: the matching policy on the jev_eval fixture in all modes.

A stub judge answers from the recorded answers of Jev (``rich`` and, for
comparison, ``minimal``); the run loop's order is replayed per work: text
score, :func:`to_judge`, the judge's answers, :func:`decide_work`. The
policy runs with the shipped threshold defaults.

Pair figures are over the 280 decided pairs (label not ``insufficient``),
as in the fixture README: accuracy is the share of pairs whose same-work
call is right (judge: ``same_work >= .5``; a prefiltered pair counts as a
no; fuzzy: accepted counts as a yes); automatic decisions are accepts and
rejects, an error is an accept of a different work or a reject of the same
work. The README's corrected figures: ``rich`` .98 (275 right), 197
automatic decisions, 0 errors; ``minimal`` .97 (272 right), 219, 0. The
fuzzy figures are those measured when the thresholds were set (phase 5).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from chronodownloader.matching.answers import WorkAnswer
from chronodownloader.matching.policy import (
    Candidate,
    Mode,
    decide_candidate,
    decide_work,
    to_judge,
)
from chronodownloader.settings import JudgeThresholds

from .jev_fixture import PROVIDER_ORDER, answer, candidate, decided, pairs

THRESHOLDS = JudgeThresholds()

type Key = tuple[int, str, str]


def _key(entry_id: int, cand: Candidate) -> Key:
    return entry_id, cand.record.provider, cand.record.source_id


class StubJudge:
    """Answers from the fixture's recorded answers; counts the questions."""

    def __init__(self, condition: str, model: str = "jev") -> None:
        self._answers: dict[Key, WorkAnswer] = {
            _key(p["entry_id"], candidate(p)): answer(p, condition, model)
            for p in pairs()
        }
        self.asked: list[Key] = []

    def judge(self, entry_id: int, cands: Sequence[Candidate]) -> dict[Key, WorkAnswer]:
        keys = [_key(entry_id, c) for c in cands]
        self.asked.extend(keys)
        return {k: self._answers[k] for k in keys}


@dataclass
class Figures:
    right: int = 0
    automatic: int = 0
    errors: int = 0
    lost: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    wrong_choices: int = 0


def _run(mode: Mode, condition: str | None) -> tuple[Figures, StubJudge | None]:
    judge = StubJudge(condition) if condition else None
    works: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs():
        works[pair["entry_id"]].append(pair)
    figures = Figures()
    labels = {_key(p["entry_id"], candidate(p)): p["label"] for p in pairs()}
    for entry_id, rows in works.items():
        cands = [candidate(p) for p in rows]
        asked = to_judge(cands, mode, THRESHOLDS)
        answers = judge.judge(entry_id, asked) if judge and asked else {}
        cands = [replace(c, answer=answers.get(_key(entry_id, c))) for c in cands]
        for cand in cands:
            label = labels[_key(entry_id, cand)]
            if label["relation"] == "insufficient":
                continue
            truth = bool(label["same_work"])
            got = decide_candidate(cand, mode, THRESHOLDS)
            if got.decision != "review":
                figures.automatic += 1
                figures.errors += (got.decision == "accept") != truth
            if got.basis == "prefilter":
                figures.lost += truth
                call = False
            elif got.basis == "text":
                call = got.decision == "accept"
            else:
                assert cand.answer is not None
                call = cand.answer.same_work >= 0.5
            figures.right += call == truth
        outcome = decide_work(
            cands,
            mode=mode,
            thresholds=THRESHOLDS,
            provider_order=PROVIDER_ORDER,
        )
        figures.outcomes[outcome.outcome] += 1
        if outcome.chosen is not None:
            chosen = labels[_key(entry_id, outcome.chosen)]
            if chosen["relation"] != "insufficient":
                figures.wrong_choices += not chosen["same_work"]
    return figures, judge


def test_fixture_shape() -> None:
    rows = pairs()
    assert len(rows) == 294
    assert len(decided(rows)) == 280
    keys = {_key(p["entry_id"], candidate(p)) for p in rows}
    assert len(keys) == 294
    assert len({p["entry_id"] for p in rows}) == 30


@pytest.mark.parametrize(
    ("condition", "right", "automatic", "accepted"),
    [("rich", 275, 197, 10), ("minimal", 272, 219, 13)],
)
def test_judge_mode_keeps_the_measured_figures(
    condition: str, right: int, automatic: int, accepted: int
) -> None:
    figures, judge = _run("judge", condition)
    assert judge is not None
    assert len(judge.asked) == 294
    assert figures.right >= right
    assert figures.automatic >= automatic
    assert figures.errors == 0
    assert figures.outcomes["accepted"] >= accepted
    assert figures.wrong_choices == 0


def test_fuzzy_mode_keeps_the_measured_figures() -> None:
    figures, judge = _run("fuzzy", None)
    assert judge is None
    # Nothing is accepted on the text score: 191 right is the share of
    # different-work pairs; 65 of them are rejected, none wrongly.
    assert figures.right >= 191
    assert figures.automatic >= 65
    assert figures.errors == 0
    assert figures.outcomes == Counter({"needs_review": 29, "no_match": 1})
    assert figures.wrong_choices == 0


def test_fuzzy_thresholds_hold_on_every_pair() -> None:
    """The conservative rule behind the defaults, over all 294 pairs."""
    totals = [(candidate(p).text.total, bool(p["label"]["same_work"])) for p in pairs()]
    same = [t for t, y in totals if y]
    different = [t for t, y in totals if not y]
    assert THRESHOLDS.text_accept is None
    assert max(different) >= max(same)  # no accept threshold without false accepts
    assert THRESHOLDS.text_reject is not None
    assert THRESHOLDS.text_prefilter is not None
    assert min(same) > THRESHOLDS.text_reject
    assert min(same) > THRESHOLDS.text_prefilter


def test_recorded_translations_follow_the_edition_policy() -> None:
    """The judge's translations reach the edition policy despite a low
    ``same_work`` (that question excludes translations)."""
    found = 0
    for pair in pairs():
        if pair["label"]["relation"] != "translation_or_adaptation":
            continue
        cand = replace(candidate(pair), answer=answer(pair, "rich", "jev"))
        assert cand.answer is not None
        if cand.answer.relation != "translation_or_adaptation":
            continue
        found += 1
        default = decide_work([cand], mode="judge", thresholds=THRESHOLDS)
        assert (default.outcome, default.reason) == ("needs_review", "translation")
        anything = decide_work(
            [cand], mode="judge", thresholds=THRESHOLDS, edition_policy="any_language"
        )
        assert (anything.outcome, anything.chosen) == ("accepted", cand)
    assert found == 2


@pytest.mark.parametrize(
    ("condition", "right", "automatic"),
    [("rich", 275, 197), ("minimal", 272, 219)],
)
def test_hybrid_mode_loses_no_same_work_pair(
    condition: str, right: int, automatic: int
) -> None:
    figures, judge = _run("hybrid", condition)
    assert judge is not None
    assert figures.lost == 0
    assert len(judge.asked) < 294
    assert figures.right >= right
    assert figures.automatic >= automatic
    assert figures.errors == 0
    assert figures.wrong_choices == 0
