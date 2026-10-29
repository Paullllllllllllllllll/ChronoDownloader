"""The decision-model client: judges candidates and first pages.

:class:`JudgeClient` asks the OpenRouter Decisions endpoint
(``judge.endpoint``) through the run's :class:`~chronodownloader.net.Network`
under the provider key ``judge``: every call holds one of the
``judge.network.slots`` slots, is paced and breaker-checked by the
machine-wide coordinator, and has its transient failures retried per
``judge.network``.

Work questions. :meth:`JudgeClient.judge_work` sends the model
``judge.model`` a state of two objects. ``sought_work`` holds every field of
the :class:`~chronodownloader.providers.base.SearchQuery` (title, author,
year, place, publisher, language, edition statement; null where the input
has none), the shape of the ``rich`` condition of the jev_eval run
(``docs/jev_eval/decisions_run.py``) without its ``full_title``, which the
input does not carry. ``catalog_record`` is built from the candidate's
:class:`~chronodownloader.providers.base.SearchRecord` as that run built it:
the library's display name, title, creators and date, then the provider's
raw fields whose names suggest bibliographic content (the
``useful_raw_fields`` filter of ``docs/jev_eval/harvest.py``), with markup
and entities stripped from every string. The questions ``match`` (five-way
choice) and ``same_work`` (noul) are the run's texts verbatim, since the
measured accuracy rests on them.

Page check. :meth:`JudgeClient.judge_pages` sends the model
``judge.page_model`` one request over the first pages together, so that a
title showing through a blank leaf is weighed against the page that carries
it: the state is the sought work as JSON text followed by the page images as
data URLs. It asks whether one of the images is the sought work's title page,
whether one shows an imprint year equal to the sought year (only when the
query has a year), and which kind of page best shows the title
(:data:`~chronodownloader.matching.answers.PAGE_TYPES`). The caller passes
the image bytes; the client reads no file and fetches no image.

Dialect. The questions are built and the answers read by the OpenRouter
dialect of :mod:`chronodownloader.common.decisions`. Only the work request's
state stays this module's own: it is sent as one JSON object, the shape the
jev_eval run measured, where the dialect sends a list of parts.

Errors. A missing or unusable key raises :class:`MissingJudgeKey` when the
client is built; the CLI maps it to exit 2 and names ``--matching fuzzy``.
A persistent outage (transient failures after the retries, an open breaker,
a lost slot) raises :class:`JudgeUnavailable` with ``retry_after``; a
request the endpoint refuses (an invalid key, a bad request, spent credit),
and an open breaker that refusals opened, raise :class:`JudgeRefused`; an
answer that does not fit the questions, or that declines one, raises
:class:`MalformedAnswer`. No
error falls back to text scores. The key travels only in the Authorization
header and appears in no message (an echo in an answer is masked), log line
or repr.

Request shape. The input carries no full title, so the sought work lies
between the jev_eval ``minimal`` and ``rich`` conditions; both reached zero
errors among automatic decisions at .9/.1. The live round measures the
production request itself.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

from chronodownloader.common import decisions
from chronodownloader.common.decisions import (
    Choice,
    ChoiceAnswer,
    DecisionResult,
    Image,
    Json,
    Predicate,
    PredicateAnswer,
    Question,
    Text,
)
from chronodownloader.coord import SlotLost
from chronodownloader.net import JUDGE, BreakerOpen, Cause, Network, RequestFailed
from chronodownloader.providers.base import SearchQuery, SearchRecord
from chronodownloader.settings import Settings

from .answers import PAGE_TYPES, RELATIONS, PageAnswer, PageType, WorkAnswer

__all__ = [
    "LIBRARY_NAMES",
    "PAGE_QUESTIONS",
    "WORK_QUESTIONS",
    "JudgeClient",
    "JudgeError",
    "JudgeRefused",
    "JudgeUnavailable",
    "MalformedAnswer",
    "MissingJudgeKey",
    "catalog_record",
    "judge_key",
    "page_request",
    "sought_work",
    "work_request",
]

KEY_SERVICE: Final = "openrouter"
"""The ``api_keys`` service whose environment variable holds the key."""

_DIALECT: Final = "openrouter"

WORK_QUESTIONS: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "match": {
            "type": "choice",
            "instructions": "Does the catalog record describe the sought work?",
            "criteria": {
                "same_edition": (
                    "The same work in the same edition: same text, language and "
                    "author, and year, place and printer agree or are compatible "
                    "with the sought edition (a digitized copy of that printing)."
                ),
                "other_edition": (
                    "The same work in the same language, but a different edition, "
                    "printing or year (reprint, revised or enlarged edition, later "
                    "printing)."
                ),
                "translation_or_adaptation": (
                    "A translation of the sought work into another language, or a "
                    "work derived from it under another author or title."
                ),
                "different_work": (
                    "A different work, even if the title or author is similar."
                ),
                "insufficient": "The record is too sparse to decide.",
            },
        },
        "same_work": {
            "type": "noul",
            "instructions": (
                "The catalog record describes the sought work in some edition (any "
                "printing of the same text in the same language)."
            ),
        },
    }
)
"""The work questions of the jev_eval run, verbatim."""

_PAGE_TYPE_CRITERIA: Final[Mapping[PageType, str]] = MappingProxyType(
    {
        "cover_or_binding": (
            "Binding, cover, spine, endpaper, or a library label, stamp or color "
            "chart with no printed text of the book."
        ),
        "blank": "An empty or nearly empty page.",
        "half_title": (
            "A half-title: a page carrying only the short title (no author or "
            "imprint), before the title page."
        ),
        "title_page": "The book's title page.",
        "frontispiece_or_plate": (
            "A full-page illustration, frontispiece or engraved plate."
        ),
        "front_matter": (
            "Dedication, preface, foreword, address to the reader, privilege, "
            "approbation or license."
        ),
        "table_of_contents": "A table of contents or list of chapters at the front.",
        "recipe_text": "Body text containing recipes or cooking instructions.",
        "other_body_text": (
            "Body text that is not recipes (e.g., treatise text, menus, bills of "
            "fare, carving or household advice)."
        ),
        "index": "An index or alphabetical table, usually at the back.",
        "other": "Anything else.",
    }
)

PAGE_QUESTIONS: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "title_page_of_sought": {
            "type": "noul",
            "instructions": (
                "One of the images is the title page of the sought work (its title "
                "and author as given in the text, allowing for historical spelling, "
                "abbreviation and Latin or vernacular forms)."
            ),
        },
        "imprint_year_matches": {
            "type": "noul",
            "instructions": (
                "One of the images shows a printed date (imprint year, in Arabic or "
                "Roman numerals) equal to the sought work's year."
            ),
        },
        "title_page_type": {
            "type": "choice",
            "instructions": (
                "What kind of page is the image that best shows the sought work's "
                "title?"
            ),
            "criteria": {kind: _PAGE_TYPE_CRITERIA[kind] for kind in PAGE_TYPES},
        },
    }
)
"""The page questions: the per-page texts of the jev_eval page run, asked of
the images together; ``imprint_year_matches`` only when the year is known."""


def _questions(texts: Mapping[str, Any]) -> tuple[Question, ...]:
    """Return *texts* (``noul`` and ``choice`` entries) as dialect questions."""
    built: list[Question] = []
    for name, entry in texts.items():
        if entry["type"] == "noul":
            built.append(Predicate(name, entry["instructions"]))
        else:
            built.append(Choice(name, entry["instructions"], entry["criteria"]))
    return tuple(built)


_WORK: Final = _questions(WORK_QUESTIONS)
_PAGE: Final = _questions(PAGE_QUESTIONS)

LIBRARY_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "mdz": "MDZ (Bayerische Staatsbibliothek)",
        "slub": "SLUB Dresden",
        "bnf_gallica": "BnF Gallica",
        "e_rara": "e-rara",
        "internet_archive": "Internet Archive",
        "wellcome": "Wellcome Collection",
        "europeana": "Europeana",
        "ddb": "Deutsche Digitale Bibliothek",
        "polona": "Polona",
        "sbb_digital": "SBB Digital Collections",
        "loc": "Library of Congress",
        "dpla": "DPLA",
        "british_library": "British Library",
        "bne": "BNE (Biblioteca Nacional de España)",
        "google_books": "Google Books",
        "hathitrust": "HathiTrust",
        "annas_archive": "Anna's Archive",
    }
)
"""The library named in the catalog record; the first ten as in the jev_eval
run, the others (which it did not search) added."""

_USEFUL_KEY = re.compile(
    r"publisher|place|language|lang|extent|description|edition|subject|format|"
    r"type|pages|imprint|printer|contributor|note|issued|year|date",
    re.IGNORECASE,
)
"""Raw field names kept in the catalog record (jev_eval ``harvest.py``)."""

_RAW_LIMIT: Final = 1500
"""Above this JSON length every raw value is cut to :data:`_VALUE_LIMIT`."""

_VALUE_LIMIT: Final = 300
_RECORD_KEYS: Final = frozenset({"title", "creators", "creator", "date"})
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_KEY_SHAPE = re.compile(r"[\x21-\x7e]+")
"""A usable key: visible ASCII only, so no header error can echo it."""


class JudgeError(Exception):
    """Base of the errors the judge raises."""


class MissingJudgeKey(JudgeError):
    """The decision model's key is not set or unusable (exit 2 at the CLI).

    ``env_var`` names the environment variable that should hold it.
    """

    def __init__(self, env_var: str, *, unusable: bool = False) -> None:
        self.env_var = env_var
        state = "holds an unusable value" if unusable else "is not set"
        super().__init__(
            f"the decision model needs an OpenRouter key, but the environment "
            f"variable {env_var} {state}; set it, or run with --matching fuzzy "
            f"to match on text scores without the decision model"
        )


class JudgeUnavailable(JudgeError):
    """The endpoint stays unreachable: defer the work until ``retry_after``.

    ``retry_after`` is epoch seconds: now plus ``judge.retry_after_minutes``,
    or the time an open breaker admits a probe when that is later.
    """

    def __init__(self, reason: str, retry_after: float) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(f"decision model unavailable: {reason}")


class JudgeRefused(JudgeError):
    """The endpoint answered with a refusal that a later retry does not change.

    Refusals are an invalid key, a bad request or spent credit.
    ``cause`` and ``status`` are those of the failed request, or of the
    refusals that opened the judge's breaker (``status`` None).
    """

    def __init__(self, error: RequestFailed | BreakerOpen) -> None:
        self.cause: str | None = error.cause
        self.status = error.status if isinstance(error, RequestFailed) else None
        super().__init__(f"decision model refused the request: {error}")


class MalformedAnswer(JudgeError):
    """The endpoint's answer does not fit the questions asked."""


def _plain(value: Any) -> Any:
    """Return *value* as JSON data.

    Mappings become dicts and sequences lists; anything else, a non-finite
    number included, becomes its text.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return str(value)


def _clean(value: Any) -> Any:
    """Strip markup (MDZ ``<em>``) and entities from every string."""
    if isinstance(value, str):
        return html.unescape(_TAG.sub("", value)).strip()
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    return value


def _trim(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _useful_raw_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return the raw fields with bibliographic names, cut when long."""
    kept: dict[str, Any] = {}
    for key, value in _plain(raw).items():
        if key.startswith("__") or value in (None, "", [], {}):
            continue
        if _USEFUL_KEY.search(key):
            kept[key] = value
    if len(json.dumps(kept, ensure_ascii=False)) > _RAW_LIMIT:
        kept = {key: _trim(value, _VALUE_LIMIT) for key, value in kept.items()}
    return kept


def catalog_record(record: SearchRecord) -> dict[str, Any]:
    """Return the catalog record the judge sees for *record*."""
    built: dict[str, Any] = {
        "library": LIBRARY_NAMES.get(record.provider, record.provider),
        "title": record.title,
        "creators": list(record.creators),
        "date": record.date,
    }
    for key, value in _useful_raw_fields(record.raw).items():
        if key not in built and key not in _RECORD_KEYS:
            built[key] = value
    cleaned: dict[str, Any] = _clean(_plain(built))
    return cleaned


def sought_work(query: SearchQuery) -> dict[str, Any]:
    """Return the sought work the judge sees: every field of *query*."""
    return {
        "title": query.title,
        "author": query.creator,
        "year": query.year,
        "place": query.place,
        "publisher": query.publisher,
        "language": query.language,
        "edition_statement": query.edition,
    }


def work_request(
    model: str, query: SearchQuery, record: SearchRecord
) -> dict[str, Any]:
    """Return the request body that judges *record* against *query*."""
    state = {
        "sought_work": sought_work(query),
        "catalog_record": catalog_record(record),
    }
    body = decisions.request_body(_DIALECT, model, [Json(state)], _WORK)
    body["state"] = state
    return body


def page_request(
    model: str, query: SearchQuery, page_images: Sequence[bytes]
) -> dict[str, Any]:
    """Return the request body of the page check over *page_images*.

    Raises:
        ValueError: No image, or an image of an unknown type.
    """
    if not page_images:
        raise ValueError("the page check needs at least one page image")
    images = [Image(data) for data in page_images]
    for index, image in enumerate(images, start=1):
        try:
            _ = image.media_type
        except ValueError:
            raise ValueError(
                f"page image {index} is not a JPEG, PNG, GIF or WebP image"
            ) from None
    text = json.dumps({"sought_work": sought_work(query)}, ensure_ascii=False)
    questions = [
        question
        for question in _PAGE
        if question.name != "imprint_year_matches" or query.year is not None
    ]
    return decisions.request_body(_DIALECT, model, [Text(text), *images], questions)


def _predicate(result: DecisionResult, name: str) -> float:
    answer = result.answers[name]
    if not isinstance(answer, PredicateAnswer):
        raise MalformedAnswer(f"{name}: the model declined the question")
    return answer.probability


def _choice[T: str](
    result: DecisionResult, name: str, options: Sequence[T]
) -> tuple[T, dict[str, float], float | None]:
    """Return the choice, its probabilities and the confidence of *name*."""
    answer = result.answers[name]
    if not isinstance(answer, ChoiceAnswer):
        raise MalformedAnswer(f"{name}: the model declined the question")
    choice = next(option for option in options if option == answer.choice)
    return choice, dict(answer.probabilities), answer.confidence


def judge_key(settings: Settings, environ: Mapping[str, str] | None = None) -> str:
    """Return the decision model's key from *environ*.

    *environ* defaults to the process environment; a planner checks the key
    without building a client.

    Raises:
        MissingJudgeKey: The key's environment variable is unset, empty or
            holds anything but visible ASCII characters.
    """
    env_var = settings.api_key_env(KEY_SERVICE)
    key = (os.environ if environ is None else environ).get(env_var, "").strip()
    if not key:
        raise MissingJudgeKey(env_var)
    if not _KEY_SHAPE.fullmatch(key):
        raise MissingJudgeKey(env_var, unusable=True)
    return key


class JudgeClient:
    """Asks the decision model about candidates and pages; thread-safe.

    *network* is the run's client, built from the same *settings*; *environ*
    is where the key is looked up (the process environment by default);
    *clock* gives the epoch seconds a deferral counts from.

    Raises:
        MissingJudgeKey: The key's environment variable is unset, empty or
            holds anything but visible ASCII characters.
    """

    def __init__(
        self,
        network: Network,
        settings: Settings,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        key = judge_key(settings, environ)
        self._network = network
        self._judge = settings.judge
        self._authorization = MappingProxyType({"Authorization": f"Bearer {key}"})
        self._key = key
        self._clock = clock

    def __repr__(self) -> str:
        return (
            f"JudgeClient(model={self._judge.model!r}, "
            f"page_model={self._judge.page_model!r})"
        )

    def judge_work(self, sought: SearchQuery, candidate: SearchRecord) -> WorkAnswer:
        """Return the model's answer on whether *candidate* is *sought*.

        Raises:
            JudgeUnavailable: The endpoint stays unreachable; defer the work.
            JudgeRefused: The endpoint refused the request.
            MalformedAnswer: The answer does not fit the questions.
        """
        model = self._judge.model
        result = self._parse(self._post(work_request(model, sought, candidate)), _WORK)
        relation, probabilities, confidence = _choice(result, "match", RELATIONS)
        return WorkAnswer(
            same_work=_predicate(result, "same_work"),
            relation=relation,
            relation_probabilities=probabilities,
            confidence=confidence,
            model=result.model,
            requested_model=model,
        )

    def judge_pages(
        self, sought: SearchQuery, page_images: Sequence[bytes]
    ) -> PageAnswer:
        """Return the page check over *page_images*, the first pages in order.

        ``imprint_year_matches`` is None when *sought* has no year.

        Raises:
            ValueError: No image, or an image of an unknown type.
            JudgeUnavailable: The endpoint stays unreachable; defer the check.
            JudgeRefused: The endpoint refused the request.
            MalformedAnswer: The answer does not fit the questions.
        """
        model = self._judge.page_model
        body = page_request(model, sought, page_images)
        asked = [q for q in _PAGE if q.name in body["questions"]]
        result = self._parse(self._post(body), asked)
        kind, probabilities, _ = _choice(result, "title_page_type", PAGE_TYPES)
        year = None
        if sought.year is not None:
            year = _predicate(result, "imprint_year_matches")
        return PageAnswer(
            title_page=_predicate(result, "title_page_of_sought"),
            imprint_year_matches=year,
            title_page_type=kind,
            page_type_probabilities=probabilities,
            pages=len(page_images),
            model=result.model,
            requested_model=model,
        )

    def _parse(self, data: Any, questions: Sequence[Question]) -> DecisionResult:
        """Return the answers of *data*, masking this client's key in an error.

        An answer's text is untrusted: an error envelope may echo the key. The
        error is raised outside the handler, so no unmasked message stays in
        its chain.
        """
        try:
            return decisions.parse_response(_DIALECT, data, questions)
        except decisions.MalformedAnswer as exc:
            message = str(exc).replace(self._key, "***")
        raise MalformedAnswer(message)

    def _post(self, body: Mapping[str, Any]) -> Any:
        """Send *body* under a judge slot and return the decoded response."""
        try:
            with self._network.hold_slot(JUDGE) as slot:
                response = self._network.request(
                    JUDGE,
                    "POST",
                    self._judge.endpoint,
                    json=body,
                    headers=self._authorization,
                    slot=slot,
                )
        except BreakerOpen as exc:
            # Refusals (401, 403) count as breaker failures too: an open
            # breaker they opened stays a refusal, not an outage.
            if exc.cause == Cause.BLOCKED:
                raise JudgeRefused(exc) from exc
            raise self._unavailable(str(exc), exc.until) from exc
        except SlotLost as exc:
            raise self._unavailable("the judge slot was lost", None) from exc
        except RequestFailed as exc:
            if exc.cause in (Cause.TRANSIENT, Cause.DNS, Cause.SSL):
                raise self._unavailable(str(exc), None) from exc
            raise JudgeRefused(exc) from exc
        try:
            return response.json()
        except ValueError as exc:
            raise MalformedAnswer("the response is not JSON") from exc

    def _unavailable(self, reason: str, until: float | None) -> JudgeUnavailable:
        retry_after = self._clock() + self._judge.retry_after_minutes * 60.0
        if until is not None:
            retry_after = max(retry_after, until)
        return JudgeUnavailable(reason, retry_after)
