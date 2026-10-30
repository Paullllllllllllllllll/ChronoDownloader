"""The gate's page check: do a candidate's first pages show the sought work?

:class:`GateAsker` sends one Decisions request per candidate: the sought work
(:func:`~chronodownloader.matching.judge.sought_work`) as JSON, then each page
as ``Page n:`` and its image, and asks the predicates ``title_page_of_sought``
and ``same_work`` (:data:`GATE_QUESTIONS`), the choice ``relation`` over the
five relations (:data:`RELATIONS`, ``insufficient`` included) and the page
type of every page (:func:`~chronodownloader.common.page_types.
page_type_question` under ``decisions.profile``). The texts are those of the
measured run (``docs/jev_eval/openai_decisions/run_gate_v2.py``, condition
``jev_or``, and ``run_gate.py`` for the relations), which asked 25 fine page
types; production asks the shared types of ``page_types``. A change of any
question text bumps :data:`GATE_QUESTION_VERSION`.

Model, dialect, endpoint and key are those of ``classify``
(:func:`~chronodownloader.matching.classifier.decision_target`); requests go
under the network key ``classify``, never ``judge``, so a gate outage leaves
the judge's breaker alone. The caller downscales the pages and hashes them;
the asker reads no file.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, cast

from chronodownloader.common import decisions
from chronodownloader.common.decisions import (
    Choice,
    ChoiceAnswer,
    DecisionUsage,
    Image,
    Json,
    Part,
    Predicate,
    PredicateAnswer,
    Question,
    Text,
    request_fingerprint,
)
from chronodownloader.common.page_types import VERSION, Profile, page_type_question
from chronodownloader.net import CLASSIFY, Network
from chronodownloader.providers.base import SearchQuery
from chronodownloader.settings import Settings

from .classifier import decision_key
from .judge import (
    DecisionTarget,
    JudgeUnavailable,
    MalformedAnswer,
    parse_masked,
    post_decision,
    sought_work,
)

__all__ = [
    "GATE_PAGES",
    "GATE_PAGE_PX",
    "GATE_QUESTIONS",
    "GATE_QUESTION_VERSION",
    "RELATIONS",
    "GateAnswer",
    "GateAsker",
    "gate_fingerprint",
    "gate_request",
    "page_hash",
    "sought_hash",
]

GATE_QUESTION_VERSION: Final = "2026-10-30"
"""The version of the gate's questions, stored with every check."""

GATE_PAGES: Final = 10
"""The leading pages a check reads."""

GATE_PAGE_PX: Final = 1500
"""The longer side of a page sent, as measured."""

GATE_QUESTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "title_page_of_sought": (
            "One of the pages is the title page of the sought work (its title and "
            "author as given in the text, allowing for historical spelling, "
            "abbreviation and Latin or vernacular forms)."
        ),
        "same_work": (
            "The pages come from a copy of the sought work in some edition (any "
            "printing of the same text in the same language)."
        ),
    }
)
"""The gate's predicates, verbatim from the measured run."""

RELATION_QUESTION: Final = "What do the pages show relative to the sought work?"

RELATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "same_edition": (
            "A copy of the sought work in the same edition (year, place and printer "
            "agree or are compatible)."
        ),
        "other_edition": (
            "The sought work in the same language, but another edition, printing or "
            "year."
        ),
        "translation_or_adaptation": (
            "A translation of the sought work, or a work derived from it under "
            "another title or author."
        ),
        "different_work": "A different work, even if the title or subject is similar.",
        "insufficient": (
            "The pages are too uninformative to decide (no title page, no legible "
            "title)."
        ),
    }
)
"""The relation choices, verbatim from the measured run."""


@dataclass(frozen=True)
class GateAnswer:
    """The answers of one check, the usage of its request and the model."""

    answers: Mapping[str, Any]
    usage: DecisionUsage
    model: str


def page_hash(data: bytes) -> str:
    """Return the SHA-256 hex digest of the page bytes sent."""
    return hashlib.sha256(data).hexdigest()


def sought_hash(query: SearchQuery) -> str:
    """Return the SHA-256 hex digest of the sought work the check reads."""
    text = json.dumps(sought_work(query), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gate_fingerprint(target: DecisionTarget, settings: Settings) -> str:
    """Return the fingerprint of the gate requests to *target*."""
    return request_fingerprint(
        tool="chronodownloader-gate",
        dialect=target.dialect,
        model=target.model,
        endpoint=target.endpoint,
        questions=GATE_QUESTION_VERSION,
        taxonomy=VERSION,
        profile=settings.decisions.profile,
        image_max_px=GATE_PAGE_PX,
        pages=GATE_PAGES,
        context="sought_work",
    )


def _page_name(number: int) -> str:
    return f"page_type__{number:02d}"


def _questions(pages: int, profile: Profile) -> list[Question]:
    built: list[Question] = [
        Predicate(name, text) for name, text in GATE_QUESTIONS.items()
    ]
    built.append(Choice("relation", RELATION_QUESTION, RELATIONS))
    built += [
        page_type_question(_page_name(n), f"page {n}", profile)
        for n in range(1, pages + 1)
    ]
    return built


def gate_request(
    target: DecisionTarget,
    profile: Profile,
    query: SearchQuery,
    images: Sequence[bytes],
) -> tuple[dict[str, Any], list[Question]]:
    """Return the gate request body over *images* and its questions.

    Raises:
        ValueError: No image, too many, or an image of an unknown type.
    """
    if not images:
        raise ValueError("a gate check needs at least one page image")
    parts: list[Part] = [Json({"sought_work": sought_work(query)})]
    for number, data in enumerate(images, start=1):
        parts += [Text(f"Page {number}:"), Image(data)]
    questions = _questions(len(images), profile)
    body = decisions.request_body(target.dialect, target.model, parts, questions)
    return body, questions


def _answers(result: decisions.DecisionResult, pages: int) -> dict[str, Any]:
    """Return the answers as stored: probabilities and choices, None declined."""
    out: dict[str, Any] = {}
    for name in GATE_QUESTIONS:
        answer = result.answers[name]
        out[name] = answer.probability if isinstance(answer, PredicateAnswer) else None
    relation = result.answers["relation"]
    out["relation"] = (
        {"choice": relation.choice, "probabilities": dict(relation.probabilities)}
        if isinstance(relation, ChoiceAnswer)
        else None
    )
    types: list[Any] = []
    for number in range(1, pages + 1):
        answer = result.answers[_page_name(number)]
        types.append(
            {"choice": answer.choice, "probabilities": dict(answer.probabilities)}
            if isinstance(answer, ChoiceAnswer)
            else None
        )
    out["page_types"] = types
    if out["same_work"] is None or out["relation"] is None:
        raise MalformedAnswer("the model declined a gate question")
    return out


class GateAsker:
    """Asks the gate's questions of a candidate's pages; thread-safe.

    Raises:
        MissingDecisionKey: The dialect's key is unset or unusable.
    """

    def __init__(
        self,
        network: Network,
        settings: Settings,
        target: DecisionTarget,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        key = decision_key(settings, target.dialect, environ)
        self.target = target
        self.fingerprint = gate_fingerprint(target, settings)
        self.profile = cast(Profile, settings.decisions.profile)
        self._timeout_s = settings.decisions.timeout_s
        self._retry_after_minutes = settings.judge.retry_after_minutes
        self._network = network
        self._authorization = MappingProxyType({"Authorization": f"Bearer {key}"})
        self._key = key
        self._clock = clock

    def __repr__(self) -> str:
        return f"GateAsker(model={self.target.model!r})"

    def ask(self, query: SearchQuery, images: Sequence[bytes]) -> GateAnswer:
        """Return the answers on *images*, the candidate's leading pages in order.

        Raises:
            ValueError: No image, too many, or an image of an unknown type.
            JudgeUnavailable: The endpoint stays unreachable.
            JudgeRefused: The endpoint refused the request.
            MalformedAnswer: The answer does not fit the questions.
        """
        body, questions = gate_request(self.target, self.profile, query, images)
        data = post_decision(
            self._network,
            self.target.endpoint,
            body,
            self._authorization,
            unavailable=self._unavailable,
            timeout_s=self._timeout_s,
            key=CLASSIFY,
        )
        result = parse_masked(self.target.dialect, data, questions, self._key)
        return GateAnswer(_answers(result, len(images)), result.usage, result.model)

    def _unavailable(self, reason: str, until: float | None) -> JudgeUnavailable:
        retry_after = self._clock() + self._retry_after_minutes * 60.0
        if until is not None:
            retry_after = max(retry_after, until)
        return JudgeUnavailable(reason, retry_after)
