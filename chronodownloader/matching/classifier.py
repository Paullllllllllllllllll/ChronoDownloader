"""The page classifier: the page type of each page image, from a decision model.

:class:`PageClassifier` asks one request per group of pages: the work's
input fields as a JSON part (:data:`CONTEXT_FIELDS`, null where the input has
none), then each page as ``Image n:`` and its image, with one page-type
choice per page (:func:`~chronodownloader.common.page_types.
page_type_question` under ``decisions.profile``). The caller downscales the
pages (:func:`downscale`) and groups them; the classifier reads no file.

Model and dialect. :func:`decision_target` resolves the model, dialect and
endpoint: a classify flag wins over the ``decisions`` block, a ``vendor/model``
id means OpenRouter (:func:`~chronodownloader.common.decisions.dialect_for`),
and without an endpoint the dialect's Decisions endpoint applies. The key
comes from ``api_keys.openai`` or ``api_keys.openrouter`` by dialect
(:func:`decision_key`).

Transport. Requests go through the run's :class:`~chronodownloader.net.
Network` under the provider key ``classify``
(:func:`~chronodownloader.matching.judge.post_decision`): the policy and the
slot limit of ``judge.network``, with pacing, slots and a breaker apart from
the judge's, so a refused or failing classify model never defers a download,
and ``decisions.timeout_s`` as the request timeout. The
errors are the judge's: :class:`~chronodownloader.matching.judge.
JudgeUnavailable`, :class:`~chronodownloader.matching.judge.JudgeRefused` and
:class:`~chronodownloader.matching.judge.MalformedAnswer`, with the key
masked.

Fingerprint. :attr:`PageClassifier.fingerprint` hashes the request settings
that change an answer (tool, dialect, model, endpoint, taxonomy version,
profile, image size, pages per request and the context fields); a stored label
with the same fingerprint and attempt is not asked again.
"""

from __future__ import annotations

import io
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, cast

from PIL import Image as PILImage

from chronodownloader.common import decisions
from chronodownloader.common.decisions import (
    ENDPOINTS,
    ChoiceAnswer,
    DecisionUsage,
    Dialect,
    Image,
    Json,
    Part,
    Question,
    Text,
    dialect_for,
    request_fingerprint,
)
from chronodownloader.common.page_types import VERSION, Profile, page_type_question
from chronodownloader.net import CLASSIFY, Network
from chronodownloader.settings import Decisions, Settings

from .judge import (
    JudgeError,
    JudgeUnavailable,
    parse_masked,
    post_decision,
    usable_key,
)

__all__ = [
    "CONTEXT_FIELDS",
    "KEY_SERVICES",
    "DecisionTarget",
    "MissingDecisionKey",
    "PageClassifier",
    "PageTypes",
    "decision_key",
    "decision_target",
    "downscale",
    "fingerprint",
    "page_request",
]

KEY_SERVICES: Final[Mapping[Dialect, str]] = MappingProxyType(
    {"openai": "openai", "openrouter": "openrouter"}
)
"""The ``api_keys`` service whose environment variable holds each dialect's key."""

_PROVIDER_NAMES: Final[Mapping[Dialect, str]] = MappingProxyType(
    {"openai": "OpenAI", "openrouter": "OpenRouter"}
)

CONTEXT_FIELDS: Final = (
    "title",
    "creator",
    "year",
    "place",
    "publisher",
    "language",
    "edition",
)
"""The work's input fields sent with its pages."""

JPEG_QUALITY: Final = 85
"""The quality of a downscaled page, as in ChronoTranscriber's pre-pass."""


class MissingDecisionKey(JudgeError):
    """The classify model's key is not set or unusable (exit 2 at the CLI).

    ``env_var`` names the environment variable that should hold it.
    """

    def __init__(self, env_var: str, dialect: Dialect, *, unusable: bool = False):
        self.env_var = env_var
        state = "holds an unusable value" if unusable else "is not set"
        other = "openrouter" if dialect == "openai" else "openai"
        super().__init__(
            f"classify needs an {_PROVIDER_NAMES[dialect]} key for the decision "
            f"model, but the environment variable {env_var} {state}; set it, or "
            f"choose another model or --decision-provider {other}"
        )


@dataclass(frozen=True)
class DecisionTarget:
    """The model, dialect and endpoint of the classify requests."""

    model: str
    dialect: Dialect
    endpoint: str


def decision_target(
    settings: Settings,
    model: str | None = None,
    provider: str | None = None,
    endpoint: str | None = None,
) -> DecisionTarget:
    """Return the classify target: each flag given wins over ``decisions``.

    Raises:
        ValueError: *provider* is no dialect.
    """
    block = settings.decisions
    chosen = model or block.model
    dialect = dialect_for(chosen, provider or block.provider)
    return DecisionTarget(
        chosen, dialect, endpoint or block.endpoint or ENDPOINTS[dialect]
    )


def decision_key(
    settings: Settings, dialect: Dialect, environ: Mapping[str, str] | None = None
) -> str:
    """Return the key of *dialect* from *environ* (the process environment).

    Raises:
        MissingDecisionKey: The key's environment variable is unset, empty or
            holds anything but visible ASCII characters.
    """
    env_var = settings.api_key_env(KEY_SERVICES[dialect])
    key = (os.environ if environ is None else environ).get(env_var, "").strip()
    if not key:
        raise MissingDecisionKey(env_var, dialect)
    if not usable_key(key):
        raise MissingDecisionKey(env_var, dialect, unusable=True)
    return key


def fingerprint(target: DecisionTarget, block: Decisions) -> str:
    """Return the fingerprint of the requests to *target* under *block*."""
    return request_fingerprint(
        tool="chronodownloader",
        dialect=target.dialect,
        model=target.model,
        endpoint=target.endpoint,
        taxonomy=VERSION,
        profile=block.profile,
        image_max_px=block.image_max_px,
        pages_per_request=block.pages_per_request,
        context="work",
        context_fields=list(CONTEXT_FIELDS),
    )


def downscale(data: bytes, max_px: int) -> bytes:
    """Return *data* as a JPEG whose longer side is at most *max_px*.

    Raises:
        OSError: Pillow cannot read *data* as an image.
        ValueError: The image exceeds Pillow's decompression-bomb limit.
    """
    try:
        with PILImage.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGB")
    except PILImage.DecompressionBombError as exc:
        raise ValueError(str(exc)) from exc
    image.thumbnail((max_px, max_px))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def _question_name(number: int) -> str:
    return f"page_type__{number:02d}"


def page_request(
    target: DecisionTarget,
    profile: Profile,
    work: Mapping[str, Any],
    images: Sequence[bytes],
) -> tuple[dict[str, Any], list[Question]]:
    """Return the request body over *images* and its questions, in page order.

    Raises:
        ValueError: No image, too many, or an image of an unknown type.
    """
    if not images:
        raise ValueError("a page request needs at least one image")
    context = {name: work.get(name) for name in CONTEXT_FIELDS}
    parts: list[Part] = [Json({"work": context})]
    questions: list[Question] = []
    for number, data in enumerate(images, start=1):
        parts += [Text(f"Image {number}:"), Image(data)]
        questions.append(
            page_type_question(_question_name(number), f"image {number}", profile)
        )
    body = decisions.request_body(target.dialect, target.model, parts, questions)
    return body, questions


@dataclass(frozen=True)
class PageTypes:
    """The answers of one request, a choice per page or None where declined."""

    answers: tuple[ChoiceAnswer | None, ...]
    usage: DecisionUsage
    model: str


class PageClassifier:
    """Asks the decision model for the page type of page images; thread-safe.

    *network* is the run's client; *environ* is where the key is looked up
    (the process environment by default); *clock* gives the epoch seconds a
    deferral counts from.

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
        block = settings.decisions
        self._block = block
        self.target = target
        self.profile = cast(Profile, block.profile)
        self.image_max_px = block.image_max_px
        self.pages_per_request = block.pages_per_request
        self._timeout_s = block.timeout_s
        self._retry_after_minutes = settings.judge.retry_after_minutes
        self._network = network
        self._authorization = MappingProxyType({"Authorization": f"Bearer {key}"})
        self._key = key
        self._clock = clock

    def __repr__(self) -> str:
        return f"PageClassifier(model={self.target.model!r})"

    @property
    def fingerprint(self) -> str:
        """Return the fingerprint of this classifier's requests."""
        return fingerprint(self.target, self._block)

    def classify(self, work: Mapping[str, Any], images: Sequence[bytes]) -> PageTypes:
        """Return the page type of each of *images*, downscaled, in order.

        *work* holds the work's input fields (:data:`CONTEXT_FIELDS`).

        Raises:
            ValueError: No image, too many, or an image of an unknown type.
            JudgeUnavailable: The endpoint stays unreachable.
            JudgeRefused: The endpoint refused the request.
            MalformedAnswer: The answer does not fit the questions.
        """
        body, questions = page_request(self.target, self.profile, work, images)
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
        answers = tuple(
            answer if isinstance(answer, ChoiceAnswer) else None
            for answer in (result.answers[q.name] for q in questions)
        )
        return PageTypes(answers, result.usage, result.model)

    def _unavailable(self, reason: str, until: float | None) -> JudgeUnavailable:
        retry_after = self._clock() + self._retry_after_minutes * 60.0
        if until is not None:
            retry_after = max(retry_after, until)
        return JudgeUnavailable(reason, retry_after)
