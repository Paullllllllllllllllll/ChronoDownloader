"""The title-page reader: request body, answer validation, key masking."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from PIL import Image

from chronodownloader.matching.judge import MalformedAnswer
from chronodownloader.matching.title_pages import (
    MissingTitleKey,
    TitleReader,
    TitleReading,
    parse_reading,
    title_request,
)
from chronodownloader.settings import Settings, TitlePages
from tests.providers.routed import RoutedSession, json_route, routed_network

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
KEY = "sk-or-v1-0123456789abcdef-secret"
ANSWER = {
    "is_title_page": True,
    "title": "Ein new Kochbuch",
    "place": "Franckfort am Mayn",
    "year": 1581,
    "year_as_printed": "M.D.LXXXI.",
}


def _completion(content: Any, **extra: Any) -> dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content)
    choice = {
        "message": {"role": "assistant", "content": text},
        "finish_reason": "stop",
    }
    return {
        "model": "anthropic/claude-haiku-5.5",
        "choices": [choice | extra],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 50, "cost": 0.0014},
    }


def test_the_request_body_is_the_measured_one_through_openrouter() -> None:
    body = title_request("anthropic/claude-haiku-5.5", "low", b"\xff\xd8\xffjpeg")

    assert body == {
        "model": "anthropic/claude-haiku-5.5",
        "max_tokens": 4000,
        "reasoning": {"effort": "low"},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "title_page",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "is_title_page": {"type": "boolean"},
                        "title": {"type": ["string", "null"]},
                        "place": {"type": ["string", "null"]},
                        "year": {"type": ["integer", "null"]},
                        "year_as_printed": {"type": ["string", "null"]},
                    },
                    "required": [
                        "is_title_page",
                        "title",
                        "place",
                        "year",
                        "year_as_printed",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "usage": {"include": True},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Transcribe the imprint data of this page of an early printed "
                    "book. is_title_page: whether it is the book's title page. "
                    "title: the main title as printed (diplomatic, up to the author "
                    "statement or first full stop); place as printed; year as an "
                    "integer (convert Roman numerals) and year_as_printed as the "
                    "date string printed; null where absent."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,/9j/anBlZw=="},
                    }
                ],
            },
        ],
    }
    assert json.dumps(body)  # the body serializes as the request sends it


def test_a_valid_answer_is_read_with_its_usage() -> None:
    reading, usage, model = parse_reading(_completion(ANSWER))

    assert reading == TitleReading(**ANSWER)
    assert (usage.input_tokens, usage.output_tokens, usage.cost) == (1200, 50, 0.0014)
    assert model == "anthropic/claude-haiku-5.5"
    nulls = dict.fromkeys(ANSWER) | {"is_title_page": False}
    assert parse_reading(_completion(nulls))[0] == TitleReading(**nulls)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_completion(ANSWER | {"author": "Rumpolt"}), id="extra-field"),
        pytest.param(
            _completion({k: v for k, v in ANSWER.items() if k != "place"}),
            id="missing-field",
        ),
        pytest.param(_completion(ANSWER | {"year": "1581"}), id="year-text"),
        pytest.param(_completion(ANSWER | {"year": True}), id="year-bool"),
        pytest.param(_completion(ANSWER | {"is_title_page": "yes"}), id="flag-text"),
        pytest.param(_completion(ANSWER | {"title": 7}), id="title-number"),
        pytest.param(_completion("not json"), id="not-json"),
        pytest.param(_completion(["a list"]), id="not-an-object"),
        pytest.param(_completion(ANSWER, finish_reason="length"), id="cut"),
        pytest.param(
            {"choices": [{"message": {"content": None, "refusal": "no"}}]},
            id="refusal",
        ),
        pytest.param({"error": {"message": "no credit"}}, id="error"),
        pytest.param([], id="not-an-object-response"),
    ],
)
def test_an_answer_off_the_schema_is_malformed(payload: Any) -> None:
    with pytest.raises(MalformedAnswer):
        parse_reading(payload)


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (3000, 2000), (240, 240, 240)).save(buffer, "JPEG")
    return buffer.getvalue()


def _reader(session: RoutedSession, **block: Any) -> TitleReader:
    network = routed_network("mdz", session)
    settings = Settings(title_pages=TitlePages(**block))
    return TitleReader(network, settings, environ={"OPENROUTER_API_KEY": KEY})


def test_the_reader_sends_the_page_downscaled_with_the_key() -> None:
    session = RoutedSession({ENDPOINT: json_route(_completion(ANSWER))})

    answer = _reader(session, image_max_px=1000, effort="medium").read(_jpeg())

    assert answer.reading.year == 1581
    (call,) = session.calls
    assert (call["method"], call["url"]) == ("POST", ENDPOINT)
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["json"]["reasoning"] == {"effort": "medium"}
    url = call["json"]["messages"][1]["content"][0]["image_url"]["url"]
    image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert image.size == (1000, 667)


def test_an_echoed_key_is_masked_before_the_answer_is_read() -> None:
    payload = {"error": {"message": "x" * 190 + KEY}}
    session = RoutedSession({ENDPOINT: json_route(payload)})

    with pytest.raises(MalformedAnswer) as raised:
        _reader(session).read(_jpeg())

    assert KEY[:10] not in str(raised.value)


def test_a_missing_key_names_the_switch_that_turns_reading_off() -> None:
    with pytest.raises(MissingTitleKey, match="title_pages.model"):
        TitleReader(routed_network("mdz", RoutedSession()), Settings(), environ={})
    with pytest.raises(ValueError, match="off"):
        TitleReader(
            routed_network("mdz", RoutedSession()),
            Settings(title_pages=TitlePages(model=None)),
            environ={"OPENROUTER_API_KEY": KEY},
        )
