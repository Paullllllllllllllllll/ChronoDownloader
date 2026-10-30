"""End to end: ``classify`` labels the stored page images and picks title pages.

The works are downloaded by the fake provider, their page files replaced by
real JPEGs (red for a title page, white otherwise), and the decision endpoint
is a fake session that answers each image by its colour, in the dialect the
request uses.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
import requests
from PIL import Image

from chronodownloader import run as core
from chronodownloader._classify import source_pages
from chronodownloader.cli.summary import report_fields
from chronodownloader.common.decisions import ENDPOINTS
from chronodownloader.coord import BreakerStatus, read_snapshot
from chronodownloader.net import CLASSIFY, JUDGE
from chronodownloader.output.store import (
    DATABASE_NAME,
    FileRow,
    Store,
    WorkInput,
    read_files,
    read_page_labels,
    read_title_page_picks,
    read_title_page_readings,
    read_work_status,
)
from chronodownloader.settings import Decisions, Settings, TitlePages
from chronodownloader.spec import resolve_classify, resolve_download
from tests.net.support import response
from tests.run.support import (
    PROVIDERS,
    FakeProvider,
    Recorder,
    StubJudge,
    record,
    run_command,
    wiring,
    write_csv,
)

RED = (220, 20, 20)
WHITE = (250, 250, 250)


def jpeg(colour: tuple[int, int, int], size: tuple[int, int] = (1200, 1600)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG")
    return buffer.getvalue()


def _image(url: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


CHAT = "https://openrouter.ai/api/v1/chat/completions"
READ = {
    "is_title_page": True,
    "title": "Le cuisinier françois",
    "place": "Paris",
    "year": 1651,
    "year_as_printed": "M. DC. LI.",
}


def _json(payload: Any, url: str, status: int = 200) -> requests.Response:
    headers = {"Content-Type": "application/json"}
    return response(status, [json.dumps(payload).encode()], headers, url=url)()


@dataclass
class DecisionSession(requests.Session):
    """Answers decision and title-page requests by the colour of each image.

    A red image is a title page. ``calls`` records url, headers and body of
    the decision requests, ``chats`` those of the chat endpoint; *status*
    other than 200 refuses every decision request and *chat_status* every
    chat request; *raw* and *chat_raw* replace the answers; *before* runs on
    each decision request before it is answered.
    """

    status: int = 200
    before: Callable[[], None] | None = None
    raw: Any = None
    chat_status: int = 200
    chat_raw: Any = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    chats: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__()

    def _chat(self, url: str, body: Any) -> requests.Response:
        if self.chat_status != 200:
            return response(self.chat_status, [b"down"], url=url)()
        if self.chat_raw is not None:
            return _json(self.chat_raw, url)
        (part,) = body["messages"][1]["content"]
        red = _image(part["image_url"]["url"]).getpixel((0, 0))[1] < 100
        answer = READ if red else dict.fromkeys(READ) | {"is_title_page": False}
        return _json(
            {
                "model": body["model"],
                "choices": [
                    {
                        "message": {"role": "assistant", "content": json.dumps(answer)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1500,
                    "completion_tokens": 60,
                    "cost": 0.002,
                },
            },
            url,
        )

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        body = kwargs["json"]
        call = {"url": url, "headers": kwargs["headers"], "body": body}
        if url == CHAT:
            self.chats.append(call)
            return self._chat(url, body)
        self.calls.append(call)
        if self.before is not None:
            self.before()
        if self.status != 200:
            return response(self.status, [b"down"], url=url)()
        if self.raw is not None:
            reply = json.dumps(self.raw).encode()
            return response(
                200, [reply], {"Content-Type": "application/json"}, url=url
            )()
        openai = "input" in body
        if openai:
            parts = body["input"][0]["content"]
            urls = [p["image_url"] for p in parts if p["type"] == "input_image"]
            names = [q["name"] for q in body["questions"]]
        else:
            parts = body["state"]
            urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
            names = list(body["questions"])
        answers = {}
        for name, data_url in zip(names, urls, strict=True):
            red = _image(data_url).getpixel((0, 0))[1] < 100
            title = 0.9 if red else 0.05
            answers[name] = {
                "name": name,
                "type": "choice",
                "choice": "title_page" if red else "body_text",
                "probabilities": {"title_page": title, "body_text": 0.95 - title},
            }
        usage: dict[str, Any] = {
            "input_tokens": 100,
            "output_tokens": 4,
            "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 2},
        }
        if not openai:
            usage["cost"] = 0.25
        payload = {
            "model": body["model"],
            "answers": list(answers.values()) if openai else answers,
            "usage": usage,
        }
        headers = {"Content-Type": "application/json"}
        return response(200, [json.dumps(payload).encode()], headers, url=url)()

    def images(self, index: int = 0) -> list[Image.Image]:
        body = self.calls[index]["body"]
        if "input" in body:
            parts = body["input"][0]["content"]
            return [_image(p["image_url"]) for p in parts if p["type"] == "input_image"]
        return [
            _image(p["image_url"]["url"]) for p in body["state"] if "image_url" in p
        ]


COOK = record("mdz", "bsb1", "Le cuisinier françois")
KOCH = record("mdz", "bsb2", "Ein new Kochbuch")


def downloaded(tmp_path: Path, settings: Settings, out: Path) -> None:
    """Download E1 (4 page images, the second a title page) and E2 (a PDF)."""
    csv = write_csv(
        tmp_path / "works.csv",
        [
            {"entry_id": "E1", "title": "Le cuisinier francois", "year": "1651"},
            {"entry_id": "E2", "title": "Ein new Kochbuch"},
        ],
    )
    mdz = FakeProvider(
        "mdz",
        records={"cuisinier": [COOK], "kochbuch": [KOCH]},
        page_counts={"bsb1": 4},
        pdf={"bsb2"},
    )
    judge = StubJudge({"mdz:bsb1": 0.97, "mdz:bsb2": 0.97})
    flags = {"input": csv, "output": out, "providers": PROVIDERS}
    flags |= {"include_metadata": False}
    assert run_command(resolve_download, settings, flags, [mdz], judge).counts.complete
    for row in read_files(out, "E1"):
        colour = RED if row.item == "p00002" else WHITE
        (_folder(out, "E1") / row.path).write_bytes(jpeg(colour))


def _folder(out: Path, entry_id: str) -> Path:
    (row,) = read_work_status(out, [entry_id])
    return out / str(row["folder"])


def path_of(out: Path, item: str, entry_id: str = "E1") -> str:
    """Return the recorded path of the work's page *item*."""
    (row,) = [row for row in read_files(out, entry_id) if row.item == item]
    return row.path


def classify(
    settings: Settings, session: DecisionSession, **flags: Any
) -> core.RunResult:
    resolution = resolve_classify(flags)
    run_plan = core.plan(resolution.spec, settings, sources=resolution.sources)
    wired = wiring((), session_factory=lambda: session)
    return core.run(run_plan, Recorder(), wiring=wired)


def test_classify_labels_every_page_and_picks_the_title_page(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    session = DecisionSession()

    result = classify(settings, session, output=out)

    assert result.counts == core.WorkCounts(total=2, complete=1, skipped=1)
    report = result.report
    assert isinstance(report, core.ClassifyReport)
    e1, e2 = report.works
    assert (e1.pages, e1.asked, e1.labelled, e1.title_page) == (
        4,
        4,
        4,
        path_of(out, "p00002"),
    )
    assert e1.probability == pytest.approx(0.9)
    assert (e2.outcome, e2.note) == ("skipped", "no page images recorded")
    (call,) = session.calls
    assert call["url"] == ENDPOINTS["openrouter"]
    assert call["headers"]["Authorization"] == "Bearer sk-or-test-key"
    work = json.loads(call["body"]["state"][0]["text"])["work"]
    assert (work["title"], work["year"]) == ("Le cuisinier francois", "1651")
    assert len(call["body"]["questions"]) == 4
    assert max(session.images()[0].size) == 768
    files = {row.path: row.attempt_id for row in read_files(out, "E1")}
    labels = read_page_labels(out, "E1")
    assert [(label.path, label.page_index) for label in labels] == [
        (path_of(out, f"p0000{n}"), n - 1) for n in range(1, 5)
    ]
    assert all(label.attempt_id == files[label.path] for label in labels)
    assert {label.page_type for label in labels} == {"title_page", "body_text"}
    assert labels[0].fingerprint == report.fingerprint
    assert report_fields(report) == {
        "pages_total": 4,
        "pages_asked": 4,
        "pages_current": 0,
        "pages_labelled": 4,
        "pages_stale": 0,
        "pages_failed": 0,
        "title_pages": 1,
        "decision_requests": 1,
        "decision_input_tokens": 100,
        "decision_output_tokens": 4,
        "decision_cached_tokens": 10,
        "decision_cache_write_tokens": 2,
        "decision_cost": 0.25,
        "title_pages_transcribed": 1,
        "title_requests": 1,
        "title_input_tokens": 1500,
        "title_output_tokens": 60,
        "title_cost": 0.002,
    }
    assert e1.title == "Le cuisinier françois"
    (chat,) = session.chats
    assert chat["headers"]["Authorization"] == "Bearer sk-or-test-key"
    (part,) = chat["body"]["messages"][1]["content"]
    assert max(_image(part["image_url"]["url"]).size) == 1500
    (reading,) = read_title_page_readings(out, ["E1"])
    assert {key: reading[key] for key in READ} == READ | {"is_title_page": 1}
    assert (reading["path"], reading["page_index"]) == (path_of(out, "p00002"), 1)
    assert reading["model"] == "anthropic/claude-haiku-5.5"
    (row,) = read_work_status(out, ["E1"])
    assert row["status"] == "complete"


def test_a_rerun_skips_current_labels_and_force_asks_again(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    classify(settings, DecisionSession(), output=out)

    again = DecisionSession()
    result = classify(settings, again, output=out)
    assert again.calls == []
    assert isinstance(result.report, core.ClassifyReport)
    assert (result.report.works[0].current, result.report.works[0].asked) == (4, 0)
    assert result.counts.complete == 1

    forced = DecisionSession()
    classify(settings, forced, output=out, force=True)
    assert len(forced.calls) == 1

    other = replace(settings, decisions=Decisions(image_max_px=512))
    resized = DecisionSession()
    classify(other, resized, output=out)
    assert max(resized.images()[0].size) == 512


def test_pages_go_in_groups_of_pages_per_request(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    grouped = replace(settings, decisions=Decisions(pages_per_request=3))
    session = DecisionSession()

    result = classify(grouped, session, output=out, entry_ids=("E1",))

    sizes = sorted(len(call["body"]["questions"]) for call in session.calls)
    assert sizes == [1, 3]
    assert result.counts == core.WorkCounts(total=1, complete=1)
    assert isinstance(result.report, core.ClassifyReport)
    assert result.report.usage.requests == 2
    assert result.report.usage.cost == pytest.approx(0.5)
    assert len(read_page_labels(out, "E1")) == 4


def test_a_model_without_vendor_goes_to_openai_with_its_key(
    tmp_path: Path, settings: Settings, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloaded(tmp_path, settings, out)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(core.PlanError, match="OPENAI_API_KEY") as raised:
        classify(settings, DecisionSession(), output=out, decision_model="gpt-6-luna")
    assert raised.value.exit_code == 2
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    session = DecisionSession()

    result = classify(settings, session, output=out, decision_model="gpt-6-luna")

    (call,) = session.calls
    assert call["url"] == ENDPOINTS["openai"]
    assert call["headers"]["Authorization"] == "Bearer sk-openai-test"
    assert call["body"]["input"][0]["content"][0]["type"] == "input_json"
    assert isinstance(result.report, core.ClassifyReport)
    assert (result.report.dialect, result.report.usage.cost) == ("openai", None)
    assert result.report.works[0].title_page == path_of(out, "p00002")
    assert {label.dialect for label in read_page_labels(out, "E1")} == {"openai"}

    proxy = DecisionSession()
    classify(
        settings,
        proxy,
        output=out,
        decision_model="gpt-6-luna",
        decision_provider="openrouter",
        decision_endpoint="https://proxy.example/decisions",
    )
    assert proxy.calls[0]["url"] == "https://proxy.example/decisions"
    assert "state" in proxy.calls[0]["body"]


def test_a_dry_run_lists_the_pages_and_changes_nothing(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    before = (out / DATABASE_NAME).read_bytes()
    resolution = resolve_classify({"output": out, "dry_run": True})

    run_plan = core.plan(resolution.spec, settings)

    assert run_plan.counts == core.WorkCounts(total=2, skipped=1)
    assert isinstance(run_plan.report, core.ClassifyReport)
    assert [(w.outcome, w.asked) for w in run_plan.report.works] == [
        ("planned", 4),
        ("skipped", 0),
    ]
    assert (out / DATABASE_NAME).read_bytes() == before


def test_a_work_another_run_holds_is_blocked(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    other = Store(out)
    other.begin_run("download", run_id="other")
    (row,) = read_work_status(out, ["E1"])
    assert other.acquire(WorkInput("E1", "x"), str(row["folder"])) is not None
    session = DecisionSession()
    try:
        result = classify(settings, session, output=out, entry_ids=("E1",))
    finally:
        other.close()

    assert result.counts == core.WorkCounts(total=1, blocked=1)
    assert session.calls == []
    assert read_page_labels(out, "E1") == []


def test_a_file_replaced_while_asked_lands_no_label(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)

    def replace_files() -> None:
        # An older process replaces the files, ignoring the lease.
        with contextlib.closing(sqlite3.connect(out / DATABASE_NAME)) as connection:
            connection.execute("UPDATE files SET attempt_id = attempt_id + 100")
            connection.commit()

    session = DecisionSession(before=replace_files)
    result = classify(settings, session, output=out, entry_ids=("E1",))

    assert result.counts == core.WorkCounts(total=1, failed=1)
    assert isinstance(result.report, core.ClassifyReport)
    (work,) = result.report.works
    assert (work.labelled, work.stale, work.title_page) == (0, 4, None)
    assert read_page_labels(out, "E1") == []
    (pick,) = read_title_page_picks(out, ["E1"])
    assert pick["pages_labelled"] == 0


def test_an_unreadable_page_fails_and_an_outage_defers(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    (broken,) = [row for row in read_files(out, "E1") if row.item == "p00004"]
    (_folder(out, "E1") / broken.path).write_bytes(b"not an image")

    result = classify(settings, DecisionSession(), output=out, entry_ids=("E1",))

    assert result.counts == core.WorkCounts(total=1, failed=1)
    assert isinstance(result.report, core.ClassifyReport)
    (work,) = result.report.works
    assert (work.labelled, work.failed) == (3, 1)
    assert work.note is not None and broken.path in work.note
    assert work.title_page == path_of(out, "p00002")

    (_folder(out, "E1") / broken.path).write_bytes(jpeg(WHITE))
    down = classify(
        settings, DecisionSession(status=503), output=out, entry_ids=("E1",)
    )
    assert down.counts == core.WorkCounts(total=1, deferred=1)


def test_a_metadata_update_after_planning_survives_and_is_sent(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    resolution = resolve_classify({"output": out, "entry_ids": ("E1",)})
    run_plan = core.plan(resolution.spec, settings)
    with contextlib.closing(sqlite3.connect(out / DATABASE_NAME)) as connection:
        connection.execute(
            "UPDATE works SET title = 'Le cuisinier', place = 'Paris' "
            "WHERE entry_id = 'E1'"
        )
        connection.commit()
    session = DecisionSession()

    core.run(run_plan, Recorder(), wiring=wiring((), session_factory=lambda: session))

    (row,) = read_work_status(out, ["E1"])
    assert (row["title"], row["place"]) == ("Le cuisinier", "Paris")
    work = json.loads(session.calls[0]["body"]["state"][0]["text"])["work"]
    assert (work["title"], work["place"]) == ("Le cuisinier", "Paris")


def _file(item: str) -> FileRow:
    return FileRow(
        "E1", f"objects/{item}.jpg", "image", item, 1, None, None, None, None, 1, "t"
    )


def test_pages_keep_their_source_position_across_a_gap() -> None:
    pages = source_pages([_file("p00004"), _file("p00001"), _file("p00002")])
    assert [(page.index, page.file.item) for page in pages] == [
        (0, "p00001"),
        (1, "p00002"),
        (3, "p00004"),
    ]
    mixed = source_pages([_file("p00004"), _file("cover")])
    assert [(page.index, page.file.item) for page in mixed] == [
        (0, "cover"),
        (1, "p00004"),
    ]


def test_a_reclassification_drops_the_labels_of_other_requests_first(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    classify(settings, DecisionSession(), output=out, entry_ids=("E1",))
    assert len(read_page_labels(out, "E1")) == 4

    other = replace(settings, decisions=Decisions(model="vendor/other-model"))
    result = classify(other, DecisionSession(status=503), output=out, entry_ids=("E1",))

    assert result.counts == core.WorkCounts(total=1, deferred=1)
    assert read_page_labels(out, "E1") == []
    (pick,) = read_title_page_picks(out, ["E1"])
    assert (pick["pages_labelled"], pick["pages_leading"], pick["path"]) == (
        0,
        4,
        None,
    )


def test_a_refused_classify_model_opens_its_own_breaker_not_the_judges(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    single = replace(settings, decisions=Decisions(pages_per_request=1))

    result = classify(single, DecisionSession(status=401), output=out)

    assert result.counts.failed == 1
    snapshot = read_snapshot(settings.state_directory())
    assert snapshot.breakers[CLASSIFY].status is not BreakerStatus.CLOSED
    judge = snapshot.breakers.get(JUDGE)
    assert judge is None or judge.status is BreakerStatus.CLOSED


def test_a_malformed_answer_counts_as_a_request(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)

    result = classify(settings, DecisionSession(raw={"answers": "none"}), output=out)

    assert result.counts.failed == 1
    assert isinstance(result.report, core.ClassifyReport)
    assert result.report.usage.requests == 1
    assert result.report.usage.input_tokens == 0


def _readings(out: Path) -> int:
    with contextlib.closing(sqlite3.connect(out / DATABASE_NAME)) as connection:
        return int(connection.execute("SELECT count(*) FROM title_pages").fetchone()[0])


def test_a_rerun_keeps_the_reading_and_force_reads_again(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    classify(settings, DecisionSession(), output=out)

    again = DecisionSession()
    rerun = classify(settings, again, output=out)
    assert (again.calls, again.chats) == ([], [])
    assert isinstance(rerun.report, core.ClassifyReport)
    assert rerun.report.works[0].title == READ["title"]
    assert not rerun.report.works[0].transcribed

    forced = DecisionSession()
    classify(settings, forced, output=out, force=True)
    assert len(forced.chats) == 1


def test_a_work_whose_pick_is_gone_loses_its_reading(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    classify(settings, DecisionSession(), output=out, entry_ids=("E1",))
    assert _readings(out) == 1
    title = _folder(out, "E1") / path_of(out, "p00002")
    title.write_bytes(jpeg(WHITE))

    session = DecisionSession()
    result = classify(settings, session, output=out, entry_ids=("E1",), force=True)

    assert result.counts == core.WorkCounts(total=1, complete=1)
    assert session.chats == []
    assert (_readings(out), read_title_page_readings(out)) == (0, [])


def test_an_outage_of_the_title_model_defers_the_work(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)

    down = classify(
        settings, DecisionSession(chat_status=503), output=out, entry_ids=("E1",)
    )

    assert down.counts == core.WorkCounts(total=1, deferred=1)
    assert len(read_page_labels(out, "E1")) == 4
    assert _readings(out) == 0


def test_a_reading_off_the_schema_fails_the_work(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    downloaded(tmp_path, settings, out)
    bad = {"choices": [{"message": {"content": '{"title": "x"}'}}]}
    result = classify(
        settings, DecisionSession(chat_raw=bad), output=out, entry_ids=("E1",)
    )
    assert result.counts == core.WorkCounts(total=1, failed=1)
    assert isinstance(result.report, core.ClassifyReport)
    assert result.report.title_usage.requests == 1
    assert result.report.works[0].note is not None
    assert "schema" in result.report.works[0].note


def test_a_null_title_model_turns_transcription_off(
    tmp_path: Path, settings: Settings, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloaded(tmp_path, settings, out)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(core.PlanError, match="title_pages.model"):
        classify(settings, DecisionSession(), output=out, decision_model="gpt-6-luna")

    off = replace(settings, title_pages=TitlePages(model=None))
    session = DecisionSession()
    result = classify(off, session, output=out, decision_model="gpt-6-luna")

    assert result.counts.complete == 1
    assert session.chats == []
    assert isinstance(result.report, core.ClassifyReport)
    assert result.report.title_model is None
