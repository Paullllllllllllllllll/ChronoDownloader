"""Tests of the decision-model client against scripted sessions.

The requests go through a real :class:`~chronodownloader.net.Network` over
the in-memory coordinator of the network tests; sessions answer from a
script and record what was sent. Recorded answers and catalog records come
from the frozen jev_eval fixture. The question texts are compared with the
evaluation scripts in ``docs/jev_eval`` when that gitignored folder is
present.
"""

from __future__ import annotations

import ast
import base64
import functools
import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from chronodownloader.coord import BreakerState, BreakerStatus
from chronodownloader.matching.answers import PAGE_TYPES, RELATIONS
from chronodownloader.matching.judge import (
    LIBRARY_NAMES,
    PAGE_QUESTIONS,
    WORK_QUESTIONS,
    JudgeClient,
    JudgeRefused,
    JudgeUnavailable,
    MalformedAnswer,
    MissingJudgeKey,
    catalog_record,
    page_request,
)
from chronodownloader.net import JUDGE, Cause, Network
from chronodownloader.providers.base import SearchQuery, SearchRecord
from chronodownloader.settings import DEFAULT_API_KEYS, Judge, JudgeNetwork, Settings
from tests.net.support import (
    NOW,
    FakeCoordinator,
    Reply,
    ScriptedServer,
    ScriptedSession,
    response,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "matching" / "jev_eval"
EVAL_DIR = ROOT / "docs" / "jev_eval"
ENDPOINT = "https://openrouter.example/api/alpha/decisions"
KEY = "sk-or-v1-0123456789abcdef-secret"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00page"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRpage"

SETTINGS = Settings(
    judge=Judge(
        endpoint=ENDPOINT,
        network=JudgeNetwork(max_attempts=3, base_backoff_s=0.5, timeout_s=5.0),
    )
)


@functools.cache
def _pairs() -> tuple[dict[str, Any], ...]:
    lines = (FIXTURE / "pairs.jsonl").read_text(encoding="utf-8").splitlines()
    return tuple(json.loads(line) for line in lines if line)


@functools.cache
def _pages() -> tuple[dict[str, Any], ...]:
    lines = (FIXTURE / "pages.jsonl").read_text(encoding="utf-8").splitlines()
    return tuple(json.loads(line) for line in lines if line)


def _record(candidate: dict[str, Any]) -> SearchRecord:
    return SearchRecord(
        provider=candidate["provider"],
        source_id=candidate["source_id"],
        title=candidate["title"],
        creators=tuple(candidate["creators"]),
        date=candidate["date"],
        raw=candidate["raw_fields"] or {},
    )


def _query(work: dict[str, Any]) -> SearchQuery:
    return SearchQuery(
        title=work["short_title"],
        creator=work["author"],
        year=work["year"],
        place=work["place"],
        publisher=work["publisher"],
        language=work["language"],
        edition=work["edition_statement"],
    )


def _eval_literal(script: str, name: str) -> Any:
    """Return the literal assigned to *name* in a ``docs/jev_eval`` script."""
    path = EVAL_DIR / script
    if not path.is_file():
        pytest.skip(f"docs/jev_eval/{script} is not in this checkout")
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.AnnAssign | ast.Assign)
            and node.value is not None
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in (
                    [node.target] if isinstance(node, ast.AnnAssign) else node.targets
                )
            )
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {script}")


def _json_reply(payload: Any, status: int = 200) -> Callable[[], requests.Response]:
    return response(
        status,
        [json.dumps(payload).encode("utf-8")],
        {"Content-Type": "application/json"},
        url=ENDPOINT,
    )


def _work_payload(model: str = "typesafe/jev-1.14-20261014") -> dict[str, Any]:
    return {
        "id": "dec-1",
        "model": model,
        "answers": {
            "match": {
                "type": "choice",
                "choice": "other_edition",
                "probabilities": {
                    "same_edition": 0.0,
                    "other_edition": 0.85,
                    "translation_or_adaptation": 0.02,
                    "different_work": 0.13,
                    "insufficient": 0.0,
                },
                "confidence": 0.81,
            },
            "same_work": {"type": "noul", "noul": 0.68},
        },
    }


class Harness:
    """A judge client over a scripted session and the fake coordinator."""

    def __init__(self, coordinator: FakeCoordinator, sleeps: list[float]) -> None:
        self.coordinator = coordinator
        self.sleeps = sleeps
        self.networks: list[Network] = []

    def client(
        self,
        session: requests.Session,
        *,
        settings: Settings = SETTINGS,
        environ: dict[str, str] | None = None,
    ) -> JudgeClient:
        network = Network(
            settings,
            self.coordinator,
            session_factory=lambda: session,
            sleep=self.sleeps.append,
            clock=lambda: NOW,
        )
        self.networks.append(network)
        return JudgeClient(
            network,
            settings,
            environ={"OPENROUTER_API_KEY": KEY} if environ is None else environ,
            clock=lambda: NOW,
        )


@pytest.fixture
def coordinator() -> FakeCoordinator:
    return FakeCoordinator()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def harness(coordinator: FakeCoordinator, sleeps: list[float]) -> Iterator[Harness]:
    built = Harness(coordinator, sleeps)
    yield built
    for network in built.networks:
        network.close()


class TestCatalogRecord:
    def test_equals_the_stored_record_for_every_fixture_pair(self) -> None:
        pairs = _pairs()
        mismatches = [
            pair["pair_id"]
            for pair in pairs
            if catalog_record(_record(pair["candidate"]))
            != pair["judge_catalog_record"]
        ]
        assert len(pairs) == 294
        assert mismatches == []

    def test_strips_markup_and_keeps_bibliographic_raw_fields(self) -> None:
        record = SearchRecord(
            provider="mdz",
            source_id="bsb1",
            title="Le <em>cuisinier</em> fran&ccedil;ois",
            creators=("<em>La</em> Varenne",),
            date="1651",
            raw={
                "publisher": "Pierre <b>David</b>",
                "creator": "dropped: the record names the creators",
                "thumbnail": "dropped: not bibliographic",
                "__matching__": {"score": 1},
                "language": "",
                "subjects": ("Cooking", "France"),
            },
        )
        assert catalog_record(record) == {
            "library": "MDZ (Bayerische Staatsbibliothek)",
            "title": "Le cuisinier françois",
            "creators": ["La Varenne"],
            "date": "1651",
            "publisher": "Pierre David",
            "subjects": ["Cooking", "France"],
        }

    def test_cuts_long_raw_fields_as_the_evaluation_did(self) -> None:
        raw = {"description": "x" * 2000, "year": 1651}
        built = catalog_record(SearchRecord("loc", "1", "T", raw=raw))
        assert built["library"] == "Library of Congress"
        assert built["description"] == "x" * 297 + "..."
        assert built["year"] == "1651"

    def test_library_names_of_the_evaluation_are_kept(self) -> None:
        names = _eval_literal("decisions_run.py", "PROVIDER_NAMES")
        assert {key: LIBRARY_NAMES[key] for key in names} == names
        assert set(LIBRARY_NAMES) == set(SETTINGS.providers)


class TestQuestions:
    def test_work_questions_are_the_evaluation_texts(self) -> None:
        assert _eval_literal("decisions_run.py", "QUESTIONS") == dict(WORK_QUESTIONS)

    def test_page_questions_reuse_the_page_run_texts(self) -> None:
        page_run = _eval_literal("page_run.py", "PAGE_QUESTIONS")
        criteria = PAGE_QUESTIONS["title_page_type"]["criteria"]
        assert list(criteria) == list(PAGE_TYPES)
        assert "half_title" in criteria
        assert {
            kind: text for kind, text in criteria.items() if kind != "half_title"
        } == page_run["page_type"]["criteria"]
        for key in ("title_page_of_sought", "imprint_year_matches"):
            single = page_run[key]["instructions"]
            assert single.startswith("This page ")
            assert PAGE_QUESTIONS[key]["instructions"] == single.replace(
                "This page", "One of the images", 1
            )


class TestWorkRequest:
    def test_body_holds_model_state_and_questions(
        self, harness: Harness, coordinator: FakeCoordinator
    ) -> None:
        pair = _pairs()[0]
        work = pair["work"]
        session = ScriptedSession(_json_reply(_work_payload()))
        harness.client(session).judge_work(_query(work), _record(pair["candidate"]))
        (call,) = session.calls
        assert (call["method"], call["url"]) == ("POST", ENDPOINT)
        assert call["headers"]["Authorization"] == f"Bearer {KEY}"
        assert call["json"] == {
            "model": "typesafe/jev-1.14",
            "state": {
                "sought_work": {
                    "title": work["short_title"],
                    "author": work["author"],
                    "year": work["year"],
                    "place": work["place"],
                    "publisher": work["publisher"],
                    "language": work["language"],
                    "edition_statement": work["edition_statement"],
                },
                "catalog_record": pair["judge_catalog_record"],
            },
            "questions": dict(WORK_QUESTIONS),
        }
        json.dumps(call["json"])
        assert coordinator.slots_taken == [JUDGE]
        assert coordinator.slots_released == [JUDGE]
        (reservation,) = coordinator.reservations
        assert reservation.provider == JUDGE
        assert reservation.slot is not None

    def test_missing_input_fields_are_sent_as_null(self, harness: Harness) -> None:
        session = ScriptedSession(_json_reply(_work_payload()))
        harness.client(session).judge_work(
            SearchQuery("Le cuisinier"), SearchRecord("mdz", "b", "Le cuisinier")
        )
        sought = session.calls[0]["json"]["state"]["sought_work"]
        assert sought == {
            "title": "Le cuisinier",
            "author": None,
            "year": None,
            "place": None,
            "publisher": None,
            "language": None,
            "edition_statement": None,
        }

    def test_recorded_answers_parse_into_work_answers(self, harness: Harness) -> None:
        checked = 0
        for pair in _pairs():
            for condition in ("minimal", "rich"):
                for recorded in pair["answers"][condition].values():
                    answers = recorded["answers"]
                    payload = {"model": recorded["model"], "answers": answers}
                    client = harness.client(ScriptedSession(_json_reply(payload)))
                    got = client.judge_work(
                        _query(pair["work"]), _record(pair["candidate"])
                    )
                    match = answers["match"]
                    assert got.same_work == answers["same_work"]["noul"]
                    assert got.relation == match["choice"]
                    assert dict(got.relation_probabilities) == match["probabilities"]
                    assert got.confidence == match["confidence"]
                    assert got.model == recorded["model"]
                    assert got.requested_model == "typesafe/jev-1.14"
                    checked += 1
        assert checked == 294 * 4

    def test_a_model_setting_is_requested(self, harness: Harness) -> None:
        settings = Settings(judge=Judge(endpoint=ENDPOINT, model="openai/other"))
        session = ScriptedSession(_json_reply(_work_payload("openai/other-1")))
        answer = harness.client(session, settings=settings).judge_work(
            SearchQuery("T"), SearchRecord("mdz", "b", "T")
        )
        assert session.calls[0]["json"]["model"] == "openai/other"
        assert (answer.model, answer.requested_model) == (
            "openai/other-1",
            "openai/other",
        )
        assert answer.relation in RELATIONS


def _page_payload(page: dict[str, Any], model_key: str) -> dict[str, Any]:
    recorded = page["answers"][model_key]
    answers = recorded["answers"]
    return {
        "model": recorded["model"],
        "answers": {
            "title_page_of_sought": answers["title_page_of_sought"],
            "imprint_year_matches": answers["imprint_year_matches"],
            "title_page_type": answers["page_type"],
        },
    }


class TestPageCheck:
    def test_one_request_over_all_pages(
        self, harness: Harness, coordinator: FakeCoordinator
    ) -> None:
        page = _pages()[0]
        session = ScriptedSession(_json_reply(_page_payload(page, "luna")))
        sought = SearchQuery("Les Secrets", creator="Alexis Pimontois", year=1555)
        harness.client(session).judge_pages(sought, [JPEG, PNG, JPEG])
        (call,) = session.calls
        body = call["json"]
        assert body["model"] == "openai/gpt-6-luna-decisions"
        assert body["questions"] == json.loads(json.dumps(dict(PAGE_QUESTIONS)))
        text, *images = body["state"]
        assert text["type"] == "text"
        assert json.loads(text["text"])["sought_work"]["year"] == 1555
        assert [image["type"] for image in images] == ["image_url"] * 3
        assert images[0]["image_url"]["url"] == (
            "data:image/jpeg;base64," + base64.b64encode(JPEG).decode("ascii")
        )
        assert images[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert coordinator.slots_taken == [JUDGE]

    def test_recorded_page_answers_parse_into_page_answers(
        self, harness: Harness
    ) -> None:
        checked = 0
        for page in _pages():
            sought = page["sought_work"]
            query = SearchQuery(
                sought["title"],
                creator=sought["author"],
                year=sought["year"],
                place=sought["place"],
                edition=sought["edition"],
            )
            for model_key in ("jev", "luna"):
                payload = _page_payload(page, model_key)
                client = harness.client(ScriptedSession(_json_reply(payload)))
                got = client.judge_pages(query, [JPEG, JPEG])
                answers = payload["answers"]
                assert got.title_page == answers["title_page_of_sought"]["noul"]
                assert (
                    got.imprint_year_matches
                    == (answers["imprint_year_matches"]["noul"])
                )
                kind = answers["title_page_type"]
                assert got.title_page_type == kind["choice"]
                assert dict(got.page_type_probabilities) == kind["probabilities"]
                assert (got.pages, got.model) == (2, payload["model"])
                assert got.requested_model == "openai/gpt-6-luna-decisions"
                checked += 1
        assert checked == 300

    def test_half_title_is_a_page_type(self, harness: Harness) -> None:
        payload = {
            "model": "openai/gpt-6-luna-decisions-20261006",
            "answers": {
                "title_page_of_sought": {"type": "noul", "noul": 0.2},
                "imprint_year_matches": {"type": "noul", "noul": 0.1},
                "title_page_type": {
                    "type": "choice",
                    "choice": "half_title",
                    "probabilities": {"half_title": 0.7, "title_page": 0.3},
                    "confidence": 0.6,
                },
            },
        }
        client = harness.client(ScriptedSession(_json_reply(payload)))
        got = client.judge_pages(SearchQuery("T", year=1700), [JPEG])
        assert got.title_page_type == "half_title"

    def test_without_a_year_the_imprint_question_is_not_asked(
        self, harness: Harness
    ) -> None:
        payload = _page_payload(_pages()[0], "jev")
        del payload["answers"]["imprint_year_matches"]
        session = ScriptedSession(_json_reply(payload))
        got = harness.client(session).judge_pages(SearchQuery("T"), [JPEG])
        assert "imprint_year_matches" not in session.calls[0]["json"]["questions"]
        assert got.imprint_year_matches is None

    @pytest.mark.parametrize(
        ("images", "message"),
        [([], "at least one"), ([b"%PDF-1.4"], "page image 1")],
    )
    def test_unusable_images_are_refused_before_sending(
        self, images: list[bytes], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            page_request("m", SearchQuery("T"), images)


class TestKey:
    def test_a_missing_key_names_the_variable_and_fuzzy(self, harness: Harness) -> None:
        with pytest.raises(MissingJudgeKey) as excinfo:
            harness.client(ScriptedSession(_json_reply({})), environ={})
        assert excinfo.value.env_var == "OPENROUTER_API_KEY"
        assert "OPENROUTER_API_KEY" in str(excinfo.value)
        assert "--matching fuzzy" in str(excinfo.value)

    def test_the_variable_comes_from_the_settings(self, harness: Harness) -> None:
        settings = Settings(
            api_keys={**DEFAULT_API_KEYS, "openrouter": "MY_ROUTER_KEY"},
            judge=SETTINGS.judge,
        )
        session = ScriptedSession(_json_reply(_work_payload()))
        with pytest.raises(MissingJudgeKey, match="MY_ROUTER_KEY"):
            harness.client(session, settings=settings)
        client = harness.client(
            session, settings=settings, environ={"MY_ROUTER_KEY": f" {KEY}\n"}
        )
        client.judge_work(SearchQuery("T"), SearchRecord("mdz", "b", "T"))
        assert session.calls[0]["headers"]["Authorization"] == f"Bearer {KEY}"

    def test_an_unusable_key_is_refused_without_echoing_it(
        self, harness: Harness
    ) -> None:
        with pytest.raises(MissingJudgeKey) as excinfo:
            harness.client(
                ScriptedSession(_json_reply({})),
                environ={"OPENROUTER_API_KEY": "sk-secret\nInjected: x"},
            )
        assert "sk-secret" not in str(excinfo.value)
        assert "--matching fuzzy" in str(excinfo.value)


class TestOutage:
    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(_json_reply({}, 503), id="503"),
            pytest.param(_json_reply({}, 429), id="429"),
            pytest.param(
                requests.exceptions.ReadTimeout("read timed out"), id="timeout"
            ),
        ],
    )
    def test_a_persistent_failure_defers_with_retry_after(
        self,
        failure: Any,
        harness: Harness,
        coordinator: FakeCoordinator,
        sleeps: list[float],
    ) -> None:
        session = ScriptedSession(failure)
        with pytest.raises(JudgeUnavailable) as excinfo:
            harness.client(session).judge_work(
                SearchQuery("T"), SearchRecord("mdz", "b", "T")
            )
        assert excinfo.value.retry_after == NOW + 60 * 60
        assert len(session.calls) == 3
        assert len(sleeps) == 2
        assert coordinator.slots_released == [JUDGE]

    def test_a_transient_failure_is_retried(self, harness: Harness) -> None:
        session = ScriptedSession(_json_reply({}, 503), _json_reply(_work_payload()))
        answer = harness.client(session).judge_work(
            SearchQuery("T"), SearchRecord("mdz", "b", "T")
        )
        assert answer.same_work == 0.68
        assert len(session.calls) == 2

    def test_an_open_breaker_defers_until_it_admits_a_probe(
        self, harness: Harness, coordinator: FakeCoordinator
    ) -> None:
        until = NOW + 3 * 60 * 60
        coordinator.breakers[JUDGE] = BreakerState(
            BreakerStatus.OPEN, 3, until, "transient"
        )
        session = ScriptedSession(_json_reply(_work_payload()))
        with pytest.raises(JudgeUnavailable) as excinfo:
            harness.client(session).judge_pages(SearchQuery("T"), [JPEG])
        assert excinfo.value.retry_after == until
        assert session.calls == []

    def test_retry_after_follows_the_setting(self, harness: Harness) -> None:
        settings = Settings(
            judge=Judge(
                endpoint=ENDPOINT,
                retry_after_minutes=5.0,
                network=JudgeNetwork(max_attempts=1),
            )
        )
        session = ScriptedSession(_json_reply({}, 502))
        with pytest.raises(JudgeUnavailable) as excinfo:
            harness.client(session, settings=settings).judge_work(
                SearchQuery("T"), SearchRecord("mdz", "b", "T")
            )
        assert excinfo.value.retry_after == NOW + 300

    def test_a_breaker_opened_by_refusals_stays_a_refusal(
        self, harness: Harness, coordinator: FakeCoordinator
    ) -> None:
        coordinator.breakers[JUDGE] = BreakerState(
            BreakerStatus.OPEN, 3, NOW + 600, "blocked"
        )
        session = ScriptedSession(_json_reply(_work_payload()))
        with pytest.raises(JudgeRefused) as excinfo:
            harness.client(session).judge_work(
                SearchQuery("T"), SearchRecord("mdz", "b", "T")
            )
        assert (excinfo.value.cause, excinfo.value.status) == ("blocked", None)
        assert session.calls == []

    @pytest.mark.parametrize(
        ("status", "cause"),
        [(401, Cause.BLOCKED), (402, Cause.REJECTED), (400, Cause.REJECTED)],
    )
    def test_a_refusal_is_not_deferred(
        self, status: int, cause: Cause, harness: Harness
    ) -> None:
        session = ScriptedSession(_json_reply({"error": {"message": "no"}}, status))
        with pytest.raises(JudgeRefused) as excinfo:
            harness.client(session).judge_work(
                SearchQuery("T"), SearchRecord("mdz", "b", "T")
            )
        assert (excinfo.value.cause, excinfo.value.status) == (cause, status)
        assert len(session.calls) == 1


def _malformed(answers: dict[str, Any]) -> dict[str, Any]:
    payload = _work_payload()
    payload["answers"] = {**payload["answers"], **answers}
    return payload


MALFORMED = [
    pytest.param(
        response(200, [b"<html>busy</html>"], {"Content-Type": "text/html"}),
        "not JSON",
        id="html",
    ),
    pytest.param(_json_reply([1, 2]), "not a JSON object", id="list"),
    pytest.param(
        _json_reply({"error": {"message": "model overloaded"}}),
        "no answers: model overloaded",
        id="error-object",
    ),
    pytest.param(
        _json_reply(_malformed({"same_work": {"type": "noul", "noul": 1.5}})),
        "same_work: expected a probability",
        id="noul-range",
    ),
    pytest.param(
        _json_reply(_malformed({"same_work": {"type": "choice", "choice": "yes"}})),
        "same_work: expected a predicate answer",
        id="noul-type",
    ),
    pytest.param(
        _json_reply(_malformed({"match": {"type": "choice", "choice": "maybe"}})),
        "match: unknown choice 'maybe'",
        id="unknown-choice",
    ),
    pytest.param(
        _json_reply(
            _malformed(
                {
                    "match": {
                        "type": "choice",
                        "choice": "same_edition",
                        "probabilities": {"elsewhere": 0.5},
                    }
                }
            )
        ),
        "probabilities name unknown values",
        id="unknown-probability",
    ),
    pytest.param(
        _json_reply(
            {"model": "m", "answers": {"match": _work_payload()["answers"]["match"]}}
        ),
        "same_work: no answer",
        id="missing-question",
    ),
]


class TestMalformedAnswer:
    @pytest.mark.parametrize(("reply", "message"), MALFORMED)
    def test_is_its_own_error(
        self,
        reply: Callable[[], requests.Response],
        message: str,
        harness: Harness,
    ) -> None:
        session = ScriptedSession(reply)
        with pytest.raises(MalformedAnswer, match=message):
            harness.client(session).judge_work(
                SearchQuery("T"), SearchRecord("mdz", "b", "T")
            )
        assert len(session.calls) == 1


class TestSecrecy:
    @pytest.mark.parametrize(
        "reply",
        [
            pytest.param(_json_reply({}, 503), id="outage"),
            pytest.param(requests.exceptions.ConnectionError("reset"), id="reset"),
            pytest.param(_json_reply({}, 401), id="refused"),
            pytest.param(_json_reply({"answers": "none"}), id="malformed"),
            pytest.param(
                _json_reply({"error": {"message": f"bad key {KEY}"}}), id="echoed"
            ),
            pytest.param(
                _json_reply({"error": {"message": "x" * 190 + KEY}}),
                id="echoed-across-the-cut",
            ),
            pytest.param(
                _json_reply(
                    _malformed({"match": {"type": "choice", "choice": f"x{KEY}"}})
                ),
                id="echoed-choice",
            ),
        ],
    )
    def test_the_key_appears_in_no_error_or_log(
        self, reply: Any, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        client = harness.client(ScriptedSession(reply))
        with pytest.raises(Exception) as excinfo:
            client.judge_work(SearchQuery("T"), SearchRecord("mdz", "b", "T"))
        assert isinstance(
            excinfo.value, JudgeUnavailable | JudgeRefused | MalformedAnswer
        )
        chain: list[BaseException] = []
        error: BaseException | None = excinfo.value
        while error is not None:
            chain.append(error)
            error = error.__cause__ or error.__context__
        for raised in chain:
            assert KEY[:10] not in str(raised)
            assert KEY[:10] not in repr(raised)
        assert KEY not in caplog.text
        assert KEY not in repr(client)

    def test_the_key_reaches_the_endpoint_only_in_the_header(self) -> None:
        server = ScriptedServer()
        server.start()
        payload = json.dumps(_work_payload()).encode("utf-8")
        url = server.script(
            "/decide", Reply(200, payload, {"Content-Type": "application/json"})
        )
        settings = Settings(
            judge=Judge(endpoint=url, network=JudgeNetwork(timeout_s=5.0))
        )
        try:
            with Network(settings, FakeCoordinator()) as network:
                client = JudgeClient(
                    network, settings, environ={"OPENROUTER_API_KEY": KEY}
                )
                answer = client.judge_work(
                    SearchQuery("T"), SearchRecord("mdz", "b", "T")
                )
        finally:
            server.stop()
        (arrival,) = server.arrivals
        assert arrival.method == "POST"
        assert arrival.headers["Authorization"] == f"Bearer {KEY}"
        assert KEY not in arrival.path
        assert answer.relation == "other_edition"
