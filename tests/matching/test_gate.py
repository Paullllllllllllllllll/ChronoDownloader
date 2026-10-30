"""The gate's request, answers, fingerprint and hashes."""

from __future__ import annotations

import io
from dataclasses import replace
from typing import Any

import pytest
from PIL import Image

from chronodownloader.common.decisions import parse_response
from chronodownloader.matching import gate
from chronodownloader.matching.judge import DecisionTarget, MalformedAnswer
from chronodownloader.providers.base import SearchQuery
from chronodownloader.settings import Decisions, Settings

TARGET = DecisionTarget("gpt-5.5", "openai", "https://api.openai.com/v1/decisions")
QUERY = SearchQuery(title="Le cuisinier françois", creator="La Varenne", year="1651")


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), (200, 200, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


def test_the_request_sends_the_sought_work_then_each_page_with_its_number() -> None:
    body, questions = gate.gate_request(TARGET, "general", QUERY, [_jpeg(), _jpeg()])

    content = body["input"][0]["content"]
    assert [part["type"] for part in content] == [
        "input_json",
        "input_text",
        "input_image",
        "input_text",
        "input_image",
    ]
    assert content[0]["data"]["sought_work"]["title"] == "Le cuisinier françois"
    assert (content[1]["text"], content[3]["text"]) == ("Page 1:", "Page 2:")
    names = [question["name"] for question in body["questions"]]
    assert names == [
        "title_page_of_sought",
        "same_work",
        "relation",
        "page_type__01",
        "page_type__02",
    ]
    relation = body["questions"][2]
    assert [choice["value"] for choice in relation["choices"]] == list(gate.RELATIONS)
    assert [q.name for q in questions] == names


def test_a_request_without_pages_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one page"):
        gate.gate_request(TARGET, "general", QUERY, [])


def _payload(same_work: dict[str, Any]) -> dict[str, Any]:
    first = {"type": "choice", "choice": "same_edition"}
    return {
        "model": "gpt-5.5",
        "answers": [
            {"name": "title_page_of_sought", "type": "predicate", "probability": 0.7},
            {"name": "same_work", **same_work},
            {"name": "relation", **first, "probabilities": {"same_edition": 0.8}},
            {"name": "page_type__01", "type": "refusal", "reason": "blank"},
        ],
    }


def test_the_answers_keep_probabilities_and_choices_and_a_declined_page() -> None:
    _body, questions = gate.gate_request(TARGET, "general", QUERY, [_jpeg()])
    result = parse_response(
        "openai", _payload({"type": "predicate", "probability": 0.9}), questions
    )

    answers = gate._answers(result, 1)

    assert answers == {
        "title_page_of_sought": 0.7,
        "same_work": 0.9,
        "relation": {"choice": "same_edition", "probabilities": {"same_edition": 0.8}},
        "page_types": [None],
    }


def test_a_declined_same_work_is_a_malformed_answer() -> None:
    _body, questions = gate.gate_request(TARGET, "general", QUERY, [_jpeg()])
    result = parse_response("openai", _payload({"type": "refusal"}), questions)

    with pytest.raises(MalformedAnswer, match="declined"):
        gate._answers(result, 1)


def test_the_fingerprint_follows_the_endpoint_and_the_profile() -> None:
    settings = Settings()
    base = gate.gate_fingerprint(TARGET, settings)

    moved = replace(TARGET, endpoint="https://example.org/v1/decisions")
    assert gate.gate_fingerprint(moved, settings) != base
    other = replace(settings, decisions=Decisions(profile="culinary"))
    assert gate.gate_fingerprint(TARGET, other) != base
    assert gate.gate_fingerprint(TARGET, Settings()) == base


def test_the_sought_hash_follows_the_sought_fields() -> None:
    assert gate.sought_hash(QUERY) == gate.sought_hash(replace(QUERY))
    assert gate.sought_hash(QUERY) != gate.sought_hash(replace(QUERY, year="1652"))
