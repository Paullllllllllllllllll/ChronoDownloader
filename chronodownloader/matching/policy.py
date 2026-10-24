"""Decisions per candidate and outcomes per work, under the matching mode.

Pure functions over typed inputs; the run loop searches, asks the judge and
stores the results.

Candidate decision (:func:`decide_candidate`), one of ``accept``,
``reject`` and ``review``:

- ``judge``: the judge's ``same_work`` probability against
  ``same_work_accept`` (at or above: accept) and ``same_work_reject`` (at or
  below: reject); review between.
- ``fuzzy``: the text score total against ``text_accept`` and
  ``text_reject`` in the same way; a None threshold never fires, so with
  ``text_accept`` None nothing is accepted on the text score.
- ``hybrid``: a text score total at or below ``text_prefilter`` rejects the
  candidate without a judgment; every other candidate is decided as under
  ``judge``.

Under ``judge`` and ``hybrid`` the text score never accepts.
:func:`to_judge` names the candidates that need a judgment.

Work outcome (:func:`decide_work`). An accepted candidate is acceptable
when the edition policy allows its relation to the sought work:

- ``any_edition`` (default): the same or another edition; a translation or
  adaptation goes to review;
- ``same_edition``: the same edition only; another edition goes to review;
- ``any_language``: any edition, translations and adaptations included.

The ``same_work`` question excludes translations, so a candidate the judge
holds a translation (relation ``translation_or_adaptation`` with a
probability at or above ``same_work_accept``) counts as accepted whatever its
``same_work`` probability, and the edition policy decides on it.

An accepted candidate whose judged relation is ``different_work`` or
``insufficient`` contradicts its ``same_work`` probability and goes to
review. Under ``fuzzy`` the relation is unknown: ``same_edition`` sends
every accepted candidate to review, the other policies accept it.

The work is ``accepted`` with the first acceptable candidate in preference
order, else ``needs_review`` when any candidate is held by the policy or
lies in the review band (the reason is that of the first such candidate,
held candidates first), else ``no_match``.

Preference order. Under ``judge`` and ``hybrid``: the relation in
:data:`~chronodownloader.matching.answers.RELATIONS` order (same edition
first), then the ``same_work`` probability in bands of
:data:`PROBABILITY_BAND` (higher first), then IIIF availability, then the
provider order. Under ``fuzzy``: the year penalty (the year distance beyond
the tolerance window; lower first), then the text score total (higher
first), then IIIF availability, then the provider order. Remaining ties go
to the higher text score total, then to the input order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from chronodownloader.matching.answers import RELATIONS, WorkAnswer
from chronodownloader.matching.fuzzy import TextScore
from chronodownloader.providers.base import SearchRecord
from chronodownloader.settings import JudgeThresholds

__all__ = [
    "EDITION_POLICIES",
    "MODES",
    "PROBABILITY_BAND",
    "REVIEW_REASONS",
    "Basis",
    "Candidate",
    "CandidateDecision",
    "Decision",
    "EditionPolicy",
    "Mode",
    "Outcome",
    "ReviewReason",
    "WorkOutcome",
    "decide_candidate",
    "decide_work",
    "to_judge",
]

type Mode = Literal["judge", "fuzzy", "hybrid"]
type EditionPolicy = Literal["any_edition", "same_edition", "any_language"]
type Decision = Literal["accept", "reject", "review"]
type Basis = Literal["judge", "text", "prefilter"]
type Outcome = Literal["accepted", "needs_review", "no_match"]
type ReviewReason = Literal[
    "review_band",
    "other_edition",
    "translation",
    "relation_conflict",
    "edition_unknown",
]

MODES: Final[tuple[Mode, ...]] = ("judge", "fuzzy", "hybrid")
EDITION_POLICIES: Final[tuple[EditionPolicy, ...]] = (
    "any_edition",
    "same_edition",
    "any_language",
)
REVIEW_REASONS: Final[tuple[ReviewReason, ...]] = (
    "review_band",
    "other_edition",
    "translation",
    "relation_conflict",
    "edition_unknown",
)

PROBABILITY_BAND: Final = 0.05
"""Width of the ``same_work`` bands of the preference order."""

_BAND_PERCENT = round(PROBABILITY_BAND * 100)

# Preference groups of a decided candidate in the outcome's order.
_ACCEPTABLE, _HELD, _REVIEW, _REJECTED = range(4)


@dataclass(frozen=True)
class Candidate:
    """One search candidate with its text score and, if asked, its judgment.

    ``iiif`` is True when the candidate can be fetched over IIIF (its record
    names a manifest or its provider declares ``Capabilities.iiif``); the
    caller decides which.
    """

    record: SearchRecord
    text: TextScore
    iiif: bool = False
    answer: WorkAnswer | None = None


@dataclass(frozen=True)
class CandidateDecision:
    """A candidate's decision under the mode and its basis.

    ``held`` names the edition-policy reason that sends an accepted
    candidate to review; it is None for every other candidate and before
    :func:`decide_work` ran.
    """

    candidate: Candidate
    decision: Decision
    basis: Basis
    held: ReviewReason | None = None


@dataclass(frozen=True)
class WorkOutcome:
    """A work's outcome and every candidate's decision in preference order.

    ``chosen`` is set when ``outcome`` is ``accepted``, ``reason`` when it
    is ``needs_review``. ``decisions`` lists acceptable candidates first,
    then held ones, then the review band, then rejected candidates.
    """

    outcome: Outcome
    chosen: Candidate | None
    reason: ReviewReason | None
    decisions: tuple[CandidateDecision, ...]


def _by_threshold(value: float, accept: float | None, reject: float | None) -> Decision:
    if accept is not None and value >= accept:
        return "accept"
    if reject is not None and value <= reject:
        return "reject"
    return "review"


def _prefiltered(candidate: Candidate, thresholds: JudgeThresholds) -> bool:
    cut = thresholds.text_prefilter
    return cut is not None and candidate.text.total <= cut


def to_judge(
    candidates: Sequence[Candidate], mode: Mode, thresholds: JudgeThresholds
) -> tuple[Candidate, ...]:
    """Return the candidates the judge must answer before a decision.

    Every candidate under ``judge``, none under ``fuzzy``, and under
    ``hybrid`` those whose text score total lies above ``text_prefilter``.
    """
    if mode == "fuzzy":
        return ()
    if mode == "hybrid":
        return tuple(c for c in candidates if not _prefiltered(c, thresholds))
    return tuple(candidates)


def decide_candidate(
    candidate: Candidate, mode: Mode, thresholds: JudgeThresholds
) -> CandidateDecision:
    """Return the candidate's decision under *mode* and *thresholds*.

    Raises ValueError when *mode* needs a judgment the candidate lacks.
    """
    if mode == "fuzzy":
        decision = _by_threshold(
            candidate.text.total, thresholds.text_accept, thresholds.text_reject
        )
        return CandidateDecision(candidate, decision, "text")
    if mode == "hybrid" and _prefiltered(candidate, thresholds):
        return CandidateDecision(candidate, "reject", "prefilter")
    if candidate.answer is None:
        record = candidate.record
        raise ValueError(
            f"candidate {record.provider}:{record.source_id} has no judgment "
            f"under --matching {mode}"
        )
    decision = _by_threshold(
        candidate.answer.same_work,
        thresholds.same_work_accept,
        thresholds.same_work_reject,
    )
    return CandidateDecision(candidate, decision, "judge")


def _held(
    candidate: Candidate, mode: Mode, policy: EditionPolicy
) -> ReviewReason | None:
    """Return why the policy holds an accepted candidate, or None."""
    if mode == "fuzzy" or candidate.answer is None:
        return "edition_unknown" if policy == "same_edition" else None
    relation = candidate.answer.relation
    if relation == "same_edition":
        return None
    if relation == "other_edition":
        return "other_edition" if policy == "same_edition" else None
    if relation == "translation_or_adaptation":
        return None if policy == "any_language" else "translation"
    return "relation_conflict"


def _translation(decided: CandidateDecision, thresholds: JudgeThresholds) -> bool:
    """Return True for a judged candidate the judge holds a translation.

    The ``same_work`` question excludes translations, so a translation
    scores low there; its own probability decides instead.
    """
    answer = decided.candidate.answer
    if decided.basis != "judge" or answer is None:
        return False
    if answer.relation != "translation_or_adaptation":
        return False
    probability = answer.relation_probabilities.get(answer.relation, 0.0)
    return probability >= thresholds.same_work_accept


def _band(probability: float) -> int:
    return round(probability * 100) // _BAND_PERCENT


def _group(decided: CandidateDecision) -> int:
    if decided.decision == "accept":
        return _HELD if decided.held is not None else _ACCEPTABLE
    return _REVIEW if decided.decision == "review" else _REJECTED


def _sort_key(
    item: tuple[int, CandidateDecision], mode: Mode, provider_order: Sequence[str]
) -> tuple[float, ...]:
    index, decided = item
    candidate = decided.candidate
    answer = candidate.answer
    if mode == "fuzzy":
        edition = candidate.text.year_penalty
        strength = -candidate.text.total
    elif answer is None:
        edition, strength = float(len(RELATIONS)), 0.0
    else:
        edition = float(RELATIONS.index(answer.relation))
        strength = float(-_band(answer.same_work))
    provider = candidate.record.provider
    rank = (
        provider_order.index(provider)
        if provider in provider_order
        else len(provider_order)
    )
    return (
        _group(decided),
        edition,
        strength,
        0 if candidate.iiif else 1,
        rank,
        -candidate.text.total,
        index,
    )


def decide_work(
    candidates: Sequence[Candidate],
    *,
    mode: Mode,
    thresholds: JudgeThresholds,
    edition_policy: EditionPolicy = "any_edition",
    provider_order: Sequence[str] = (),
) -> WorkOutcome:
    """Return the work's outcome over its *candidates*.

    *provider_order* is the configured preference order of providers; a
    provider missing from it ranks after every listed one. Raises
    ValueError as :func:`decide_candidate` does.
    """
    decided: list[CandidateDecision] = []
    for candidate in candidates:
        decision = decide_candidate(candidate, mode, thresholds)
        if _translation(decision, thresholds):
            held: ReviewReason | None = (
                None if edition_policy == "any_language" else "translation"
            )
            decision = CandidateDecision(candidate, "accept", "judge", held)
        elif decision.decision == "accept":
            held = _held(candidate, mode, edition_policy)
            decision = CandidateDecision(candidate, "accept", decision.basis, held)
        decided.append(decision)
    ranked = tuple(
        d
        for _, d in sorted(
            enumerate(decided),
            key=lambda item: _sort_key(item, mode, provider_order),
        )
    )
    if not ranked or _group(ranked[0]) == _REJECTED:
        return WorkOutcome("no_match", None, None, ranked)
    first = ranked[0]
    if _group(first) == _ACCEPTABLE:
        return WorkOutcome("accepted", first.candidate, None, ranked)
    reason: ReviewReason = first.held if first.held is not None else "review_band"
    return WorkOutcome("needs_review", None, reason, ranked)
