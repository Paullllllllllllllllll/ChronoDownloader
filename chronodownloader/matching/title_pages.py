"""The title-page reader: a chat model transcribes a work's picked title page.

:class:`TitleReader` sends the one page that ``title_page_picks`` picks,
downscaled to ``title_pages.image_max_px`` on its longer side, to the chat
model ``title_pages.model`` through an OpenRouter chat completions endpoint
(``title_pages.endpoint``) with :data:`SYSTEM` as the system prompt, low
reasoning effort and a strict JSON schema (:data:`SCHEMA`): whether the page
is the book's title page, and its title, place, year and printed date. The
design is the measured title pipeline of ``docs/jev_eval/openai_decisions/
run_title_pipeline.py``, which sent the same prompt through the Anthropic API;
author and printer are not asked, since the evaluation has not shown they can
be trusted. Every field of an answer is a model reading, not catalog data.

Transport. Requests go through the run's :class:`~chronodownloader.net.
Network` under the provider key ``classify``
(:func:`~chronodownloader.matching.judge.post_decision`), with the key of
``api_keys.openrouter`` and ``title_pages.timeout_s``. The key is masked in
every string of a response before it is read. An answer is validated against
the schema: any other shape, a refusal or a cut answer raises
:class:`~chronodownloader.matching.judge.MalformedAnswer`.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from chronodownloader.common.decisions import DecisionUsage
from chronodownloader.net import CLASSIFY, Network
from chronodownloader.settings import Settings

from .classifier import KEY_SERVICES, MissingDecisionKey, downscale
from .judge import (
    JudgeError,
    JudgeUnavailable,
    MalformedAnswer,
    mask_key,
    post_decision,
    usable_key,
)

__all__ = [
    "MAX_TOKENS",
    "PROMPT_VERSION",
    "SCHEMA",
    "SYSTEM",
    "MissingTitleKey",
    "TitleAnswer",
    "TitleReader",
    "TitleReading",
    "parse_reading",
    "title_key",
    "title_request",
]

PROMPT_VERSION: Final = "2026-10-30"
"""The version of :data:`SYSTEM` and :data:`SCHEMA` stored with every reading."""

MAX_TOKENS: Final = 4000
"""The answer's token limit, reasoning included, as in the measured run."""

SCHEMA: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "is_title_page": {"type": "boolean"},
            "title": {"type": ["string", "null"]},
            "place": {"type": ["string", "null"]},
            "year": {"type": ["integer", "null"]},
            "year_as_printed": {"type": ["string", "null"]},
        },
        "required": ["is_title_page", "title", "place", "year", "year_as_printed"],
        "additionalProperties": False,
    }
)
"""The answer schema: the measured one without ``author`` and ``printer``."""

SYSTEM: Final = (
    "Transcribe the imprint data of this page of an early printed book. "
    "is_title_page: whether it is the book's title page. title: the main title "
    "as printed (diplomatic, up to the author statement or first full stop); "
    "place as printed; year as an integer (convert Roman numerals) and "
    "year_as_printed as the date string printed; null where absent."
)
"""The measured system prompt without the author and printer fields."""

_TEXT_FIELDS: Final = ("title", "place", "year_as_printed")


class MissingTitleKey(MissingDecisionKey):
    """The OpenRouter key of the title-page reader is unset or unusable."""

    def __init__(self, env_var: str, *, unusable: bool = False) -> None:
        self.env_var = env_var
        state = "holds an unusable value" if unusable else "is not set"
        JudgeError.__init__(
            self,
            f"classify transcribes title pages through OpenRouter, but the "
            f"environment variable {env_var} {state}; set it, or set "
            f"title_pages.model to null to classify without transcription",
        )


def title_key(settings: Settings, environ: Mapping[str, str] | None = None) -> str:
    """Return the OpenRouter key of the reader from *environ* (the process's).

    Raises:
        MissingTitleKey: The key's variable is unset, empty or holds anything
            but visible ASCII characters.
    """
    env_var = settings.api_key_env(KEY_SERVICES["openrouter"])
    key = (os.environ if environ is None else environ).get(env_var, "").strip()
    if not key:
        raise MissingTitleKey(env_var)
    if not usable_key(key):
        raise MissingTitleKey(env_var, unusable=True)
    return key


@dataclass(frozen=True)
class TitleReading:
    """A model's reading of one page: every field as the schema names it."""

    is_title_page: bool
    title: str | None
    place: str | None
    year: int | None
    year_as_printed: str | None


@dataclass(frozen=True)
class TitleAnswer:
    """A reading with the usage of its request and the model that answered."""

    reading: TitleReading
    usage: DecisionUsage
    model: str


def title_request(model: str, effort: str, image: bytes) -> dict[str, Any]:
    """Return the chat request body that asks *model* to read the JPEG *image*."""
    url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "reasoning": {"effort": effort},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "title_page",
                "strict": True,
                "schema": copy.deepcopy(dict(SCHEMA)),
            },
        },
        "usage": {"include": True},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": url}}],
            },
        ],
    }


def _integer(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage(raw: Any) -> DecisionUsage:
    if not isinstance(raw, Mapping):
        return DecisionUsage()
    details = raw.get("prompt_tokens_details")
    details = details if isinstance(details, Mapping) else {}
    cost = raw.get("cost")
    return DecisionUsage(
        input_tokens=_integer(raw, "prompt_tokens"),
        output_tokens=_integer(raw, "completion_tokens"),
        cached_tokens=_integer(details, "cached_tokens"),
        cost=float(cost)
        if isinstance(cost, int | float) and not isinstance(cost, bool)
        else None,
    )


def _content(payload: Mapping[str, Any]) -> str:
    """Return the answer text of a chat completion, or raise MalformedAnswer."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        error = payload.get("error")
        message = error.get("message") if isinstance(error, Mapping) else None
        detail = f": {message[:200]}" if isinstance(message, str) else ""
        raise MalformedAnswer(f"the response holds no answer{detail}")
    choice = choices[0] if isinstance(choices[0], Mapping) else {}
    message = choice.get("message")
    message = message if isinstance(message, Mapping) else {}
    if message.get("refusal"):
        raise MalformedAnswer("the model declined to read the page")
    if choice.get("finish_reason") == "length":
        raise MalformedAnswer("the answer was cut at the token limit")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, Mapping)
        )
    if not isinstance(content, str):
        raise MalformedAnswer("the answer holds no text")
    return content


def parse_reading(payload: Any) -> tuple[TitleReading, DecisionUsage, str]:
    """Return the reading, usage and model of a decoded chat completion.

    Raises:
        MalformedAnswer: No answer, a refusal, a cut answer, or an answer that
            does not fit :data:`SCHEMA`.
    """
    if not isinstance(payload, Mapping):
        raise MalformedAnswer("the response is not a JSON object")
    try:
        answer = json.loads(_content(payload))
    except ValueError as exc:
        raise MalformedAnswer("the answer is not JSON") from exc
    if not isinstance(answer, Mapping) or set(answer) != set(SCHEMA["required"]):
        raise MalformedAnswer("the answer does not hold exactly the schema's fields")
    if not isinstance(answer["is_title_page"], bool):
        raise MalformedAnswer("is_title_page: expected true or false")
    year = answer["year"]
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise MalformedAnswer("year: expected an integer or null")
    for name in _TEXT_FIELDS:
        if answer[name] is not None and not isinstance(answer[name], str):
            raise MalformedAnswer(f"{name}: expected a string or null")
    reading = TitleReading(
        is_title_page=answer["is_title_page"],
        title=answer["title"],
        place=answer["place"],
        year=year,
        year_as_printed=answer["year_as_printed"],
    )
    model = payload.get("model")
    return (
        reading,
        _usage(payload.get("usage")),
        model if isinstance(model, str) else "",
    )


class TitleReader:
    """Asks the chat model ``title_pages.model`` to read title pages; thread-safe.

    *network* is the run's client; *environ* is where the key is looked up
    (the process environment by default); *clock* gives the epoch seconds a
    deferral counts from.

    Raises:
        MissingTitleKey: The OpenRouter key is unset or unusable.
        ValueError: ``title_pages.model`` is null (the reader is off).
    """

    def __init__(
        self,
        network: Network,
        settings: Settings,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        block = settings.title_pages
        if block.model is None:
            raise ValueError("title_pages.model is null: the reader is off")
        key = title_key(settings, environ)
        self.model = block.model
        self._block = block
        self._retry_after_minutes = settings.judge.retry_after_minutes
        self._network = network
        self._authorization = MappingProxyType({"Authorization": f"Bearer {key}"})
        self._key = key
        self._clock = clock

    def __repr__(self) -> str:
        return f"TitleReader(model={self.model!r})"

    def read(self, image: bytes) -> TitleAnswer:
        """Return the model's reading of the page *image* (any Pillow image).

        Raises:
            OSError: Pillow cannot read *image*.
            ValueError: The image exceeds Pillow's decompression-bomb limit.
            JudgeUnavailable: The endpoint stays unreachable.
            JudgeRefused: The endpoint refused the request.
            MalformedAnswer: The answer does not fit the schema.
        """
        page = downscale(image, self._block.image_max_px)
        body = title_request(self.model, self._block.effort, page)
        data = post_decision(
            self._network,
            self._block.endpoint,
            body,
            self._authorization,
            unavailable=self._unavailable,
            timeout_s=self._block.timeout_s,
            key=CLASSIFY,
        )
        reading, usage, model = parse_reading(mask_key(data, self._key))
        return TitleAnswer(reading, usage, model)

    def _unavailable(self, reason: str, until: float | None) -> JudgeUnavailable:
        retry_after = self._clock() + self._retry_after_minutes * 60.0
        if until is not None:
            retry_after = max(retry_after, until)
        return JudgeUnavailable(reason, retry_after)
