"""End to end: the download's post-phase (the gate in shadow mode, ``--classify``)
and the page model of ``inspect`` and ``verify --pages``.

The fake provider stages real JPEGs and logs its downloads and samples; the
fake decision session serves sampled pages, logs every decision request and
answers each question generically (a predicate with ``same_work``'s
probability, a choice with its first value), in the request's dialect.
"""

from __future__ import annotations

import base64
import contextlib
import csv
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

from chronodownloader import _gate as gate_module
from chronodownloader import run as core
from chronodownloader._runtime import Wiring
from chronodownloader.cli.summary import exit_code, report_fields
from chronodownloader.common.decisions import ENDPOINTS
from chronodownloader.matching.gate import GATE_PAGE_PX
from chronodownloader.output.store import (
    DATABASE_NAME,
    GateCheck,
    Store,
    read_gate_checks,
    read_page_labels,
)
from chronodownloader.providers.base import Capabilities, StagedFile
from chronodownloader.settings import Settings, TitlePages
from chronodownloader.spec import (
    SpecError,
    resolve_download,
    resolve_inspect,
    resolve_verify,
)
from tests.net.support import response
from tests.run.support import (
    PAGE_HOST,
    PROVIDERS,
    FakeProvider,
    Recorder,
    StubJudge,
    record,
    write_csv,
)

COOK = record("mdz", "bsb1", "Le cuisinier françois")
KOCH = record("mdz", "bsb2", "Ein new Kochbuch")
DRUCK = record("mdz", "bsb3", "Ein Druckbuch")
META = record("internet_archive", "ia1", "Das Metabuch")
ONLY_E1 = {"entry_id": "E1", "title": "Le cuisinier francois", "year": "1651"}


def jpeg(shade: int, size: tuple[int, int] = (1200, 1600)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (shade, shade, shade)).save(buffer, "JPEG")
    return buffer.getvalue()


@dataclass
class Provider(FakeProvider):
    """A fake provider whose page images are real JPEGs of grey *shade*, but
    undecodable bytes for the items of *broken*."""

    log: list[str] = field(default_factory=list)
    shade: int = 200
    broken: set[str] = field(default_factory=set)

    def _stage(
        self, staging_dir: Path, name: str, data: bytes, kind: Any, item: str
    ) -> StagedFile:
        if kind == "image" and item not in self.broken:
            data = jpeg(self.shade)
        return super()._stage(staging_dir, name, data, kind, item)

    def download(self, record_or_id: Any, **kwargs: Any) -> Any:
        self.log.append(f"download:{getattr(record_or_id, 'source_id', record_or_id)}")
        return super().download(record_or_id, **kwargs)

    def pages(self, record_or_id: Any, **kwargs: Any) -> Any:
        self.log.append(f"sample:{getattr(record_or_id, 'source_id', record_or_id)}")
        return super().pages(record_or_id, **kwargs)


def _json(payload: Any, url: str) -> requests.Response:
    headers = {"Content-Type": "application/json"}
    return response(200, [json.dumps(payload).encode()], headers, url=url)()


@dataclass
class DecisionSession(requests.Session):
    """Serves sampled pages and answers every decision request generically.

    ``calls`` records url, headers and body of the decision requests; *log*
    gets ``gate`` or ``decision`` for each; *status* other than 200 refuses
    them; *before* runs on each before it is answered.
    """

    log: list[str] = field(default_factory=list)
    same_work: float = 0.8
    status: int = 200
    before: Callable[[], None] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__()

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        if url.startswith(PAGE_HOST):
            jpeg_type = {"Content-Type": "image/jpeg"}
            return response(200, [jpeg(120)], jpeg_type, url=url)()
        body = kwargs["json"]
        openai = "input" in body
        if openai:
            questions = {q["name"]: q for q in body["questions"]}
        else:
            questions = dict(body["questions"])
        self.log.append("gate" if "relation" in questions else "decision")
        self.calls.append({"url": url, "headers": kwargs["headers"], "body": body})
        if self.before is not None:
            self.before()
        if self.status != 200:
            return response(self.status, [b"down"], url=url)()
        answers = {name: self._answer(name, q, openai) for name, q in questions.items()}
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
        return _json(payload, url)

    def _answer(self, name: str, question: Any, openai: bool) -> dict[str, Any]:
        if question["type"] in ("predicate", "noul"):
            return {"name": name, "type": "predicate", "probability": self.same_work}
        if openai:
            first = question["choices"][0]["value"]
        else:
            first = next(iter(question["criteria"]))
        return {
            "name": name,
            "type": "choice",
            "choice": first,
            "probabilities": {first: 0.9},
        }

    def gate_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if "relation" in _names(call["body"])]


def _names(body: dict[str, Any]) -> list[str]:
    questions = body["questions"]
    if isinstance(questions, list):
        return [question["name"] for question in questions]
    return list(questions)


def _images(body: dict[str, Any]) -> list[Image.Image]:
    urls = [p["image_url"]["url"] for p in body["state"] if p["type"] == "image_url"]
    return [
        Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) for url in urls
    ]


@dataclass
class Scene:
    """The works, providers and session of one download scenario."""

    tmp_path: Path
    settings: Settings
    rows: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"entry_id": "E1", "title": "Le cuisinier francois", "year": "1651"},
            {"entry_id": "E2", "title": "Ein new Kochbuch"},
            {"entry_id": "E4", "title": "Das Metabuch"},
        ]
    )
    log: list[str] = field(default_factory=list)
    judge: StubJudge = field(
        default_factory=lambda: StubJudge(
            {
                "mdz:bsb1": 0.97,
                "mdz:bsb2": 0.5,
                "mdz:bsb3": 0.97,
                "internet_archive:ia1": 0.97,
            }
        )
    )

    def __post_init__(self) -> None:
        self.mdz = Provider(
            "mdz",
            records={"cuisinier": [COOK], "kochbuch": [KOCH], "druckbuch": [DRUCK]},
            page_counts={"bsb1": 4, "bsb2": 12},
            pdf={"bsb3"},
            log=self.log,
        )
        self.ia = Provider(
            "internet_archive",
            records={"metabuch": [META]},
            capabilities=Capabilities(downloads=False),
            log=self.log,
        )
        self.session = DecisionSession(log=self.log)

    def csv(self) -> Path:
        return write_csv(self.tmp_path / "works.csv", self.rows)

    def wiring(self) -> Wiring:
        judge = self.judge
        return Wiring(
            providers={
                "mdz": lambda _s: self.mdz,
                "internet_archive": lambda _s: self.ia,
            },
            judge=lambda _network, _settings, **_kw: judge,
            session_factory=lambda: self.session,
            sleep=lambda _seconds: None,
        )

    def download(
        self, out: Path, events: Recorder | None = None, **flags: Any
    ) -> core.RunResult:
        given = {"input": self.csv(), "output": out, "providers": PROVIDERS}
        given |= {"include_metadata": False, "format": "images"}
        resolution = resolve_download(given | flags, self.settings.defaults)
        run_plan = core.plan(resolution.spec, self.settings, sources=resolution.sources)
        return core.run(run_plan, events or Recorder(), wiring=self.wiring())


def _gate(result: core.RunResult) -> core.GateReport:
    report = result.report
    assert isinstance(report, core.DownloadReport)
    return report.gate


def test_shadow_checks_the_downloaded_and_review_band_works_after_the_downloads(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    scene = Scene(tmp_path, settings)

    result = scene.download(out, decisions="shadow")

    assert result.counts == core.WorkCounts(total=3, complete=1, needs_review=2)
    first = min(i for i, entry in enumerate(scene.log) if not entry.startswith("down"))
    assert all(not entry.startswith("download") for entry in scene.log[first:])
    assert sorted(scene.log[first:]) == ["gate", "gate", "sample:bsb2"]
    assert scene.mdz.sampled == [("bsb2", 10)]
    assert scene.ia.sampled == []
    gate = _gate(result)
    assert {(c.entry_id, c.band, c.outcome) for c in gate.checks} == {
        ("E1", "accepted", "ok"),
        ("E2", "review", "ok"),
    }
    rows = {row["entry_id"]: row for row in read_gate_checks(out)}
    assert set(rows) == {"E1", "E2"}
    assert (rows["E1"]["page_source"], rows["E2"]["page_source"]) == (
        "downloaded",
        "sampled",
    )
    assert rows["E1"]["same_work"] == pytest.approx(0.8)
    assert rows["E1"]["relation"] == "same_edition"
    by_pages = {len(_images(c["body"])): c for c in scene.session.gate_calls()}
    assert set(by_pages) == {4, 10}
    downloaded = by_pages[4]
    assert downloaded["url"] == ENDPOINTS["openrouter"]
    assert downloaded["headers"]["Authorization"] == "Bearer sk-or-test-key"
    state = downloaded["body"]["state"]
    sought = json.loads(state[0]["text"])["sought_work"]
    assert (sought["title"], sought["year"]) == ("Le cuisinier francois", 1651)
    assert [part["text"] for part in state[1:4:2]] == ["Page 1:", "Page 2:"]
    assert {max(image.size) for image in _images(downloaded["body"])} == {GATE_PAGE_PX}
    names = list(downloaded["body"]["questions"])
    assert names[:3] == ["title_page_of_sought", "same_work", "relation"]
    assert names[3:] == [f"page_type__{n:02d}" for n in range(1, 5)]
    fields = report_fields(result.report)
    assert (fields["gate_checks"], fields["gate_requests"]) == (2, 2)
    assert fields["gate_cost"] == pytest.approx(0.5)
    assert "pages_labelled" not in fields


def test_off_and_shadow_end_alike_even_through_a_gate_outage(
    tmp_path: Path, settings: Settings
) -> None:
    ended: dict[str, tuple[Any, ...]] = {}
    for mode, status in (("off", 200), ("shadow", 200), ("shadow", 503)):
        scene = Scene(tmp_path, settings)
        scene.session.status = status
        events = Recorder()
        out = tmp_path / f"out_{mode}_{status}"
        result = scene.download(out, events, decisions=mode)
        ended[f"{mode}_{status}"] = (
            result.counts,
            exit_code(result.counts),
            events.statuses(),
        )
        if mode == "shadow":
            gate = _gate(result)
            assert gate.count("failed" if status == 503 else "ok") == 2
        if status == 503:
            assert {row["status"] for row in read_gate_checks(out)} == {"failed"}
        if mode == "off":
            assert scene.session.calls == []
            assert read_gate_checks(out) == []
    assert ended["off_200"] == ended["shadow_200"] == ended["shadow_503"]


@pytest.mark.parametrize(
    "change", ["none", "model", "endpoint", "sought", "pages", "item"]
)
def test_a_stored_check_is_reused_until_a_part_of_its_key_changes(
    tmp_path: Path, settings: Settings, out: Path, change: str
) -> None:
    scene = Scene(tmp_path, settings, rows=[ONLY_E1])
    scene.download(out, decisions="shadow")
    assert len(scene.session.gate_calls()) == 1

    again: dict[str, Any] = {"decisions": "shadow", "force": True}
    if change == "model":
        again["decision_model"] = "typesafe/jev-1.15"
    elif change == "endpoint":
        again["decision_endpoint"] = "https://decisions.example.org/v1/decisions"
    elif change == "sought":
        scene.rows = [ONLY_E1 | {"year": "1652"}]
    elif change == "pages":
        scene.mdz.shade = 90
    elif change == "item":
        scene.mdz.records = {"cuisinier": [replace(COOK, source_id="bsb9")]}
        scene.judge.answers["mdz:bsb9"] = 0.97
        again["research"] = True
    result = scene.download(out, **again)

    gate = _gate(result)
    asked = len(scene.session.gate_calls()) - 1
    if change == "none":
        assert (asked, gate.count("reused"), gate.count("ok")) == (0, 1, 0)
        assert len(read_gate_checks(out)) == 1
        first, reuse = _checks(out)
        assert (reuse.reused_from, reuse.usage, reuse.status) == (
            first.check_id,
            None,
            "ok",
        )
        assert reuse.answers == first.answers
    else:
        assert (asked, gate.count("reused"), gate.count("ok")) == (1, 0, 1)


def _checks(out: Path, entry_id: str = "E1") -> list[GateCheck]:
    """Return the work's stored gate checks, oldest first."""
    with Store(out, reconcile=False) as store:
        return store.page_checks(entry_id)


def test_the_latest_row_shows_the_answer_a_reuse_took(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    scene = Scene(tmp_path, settings, rows=[ONLY_E1])
    scene.download(out, decisions="shadow")
    other = {"decision_model": "typesafe/jev-1.15", "force": True}
    scene.download(out, decisions="shadow", **other)
    result = scene.download(out, decisions="shadow", force=True)

    assert _gate(result).count("reused") == 1
    assert len(scene.session.gate_calls()) == 2
    (row,) = read_gate_checks(out)
    assert row["model"] == "typesafe/jev-1.14"
    first, _second, third = _checks(out)
    assert third.reused_from == first.check_id


@pytest.mark.parametrize("change", ["candidate", "sought", "files", "status"])
def test_a_work_that_changed_while_asked_drops_the_check_as_stale(
    tmp_path: Path, settings: Settings, out: Path, change: str
) -> None:
    scene = Scene(tmp_path, settings, rows=[ONLY_E1])
    sql = {
        "candidate": "UPDATE works SET selected_candidate = NULL",
        "sought": "UPDATE works SET year = '1700'",
        # A concurrent fetch --force: same candidate, new files.
        "files": "UPDATE files SET attempt_id = attempt_id + 100",
        "status": "UPDATE works SET status = 'failed'",
    }[change]

    def meanwhile() -> None:
        with contextlib.closing(sqlite3.connect(out / DATABASE_NAME)) as connection:
            connection.execute(sql)
            connection.commit()

    scene.session.before = meanwhile
    result = scene.download(out, decisions="shadow")

    assert result.counts.complete == 1
    assert _gate(result).count("stale") == 1
    assert read_gate_checks(out) == []
    assert report_fields(result.report)["gate_stale"] == 1


def test_a_pdf_only_download_is_recorded_unavailable_without_a_request(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    scene = Scene(
        tmp_path, settings, rows=[{"entry_id": "E3", "title": "Ein Druckbuch"}]
    )

    result = scene.download(out, format="pdf", decisions="shadow")

    assert result.counts.complete == 1
    assert scene.session.calls == []
    assert _gate(result).count("unavailable") == 1
    (row,) = read_gate_checks(out)
    assert (row["status"], row["same_work"]) == ("unavailable", None)


def test_an_unreadable_page_fails_the_check_without_a_request(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    scene = Scene(tmp_path, settings, rows=[ONLY_E1])
    scene.mdz.broken = {"p00002"}

    result = scene.download(out, decisions="shadow")

    assert scene.session.calls == []
    (check,) = _gate(result).checks
    assert check.outcome == "failed"
    assert check.note is not None and "image_002.jpg" in check.note
    (row,) = _checks(out)
    assert (row.status, row.page_hashes) == ("failed", ())


def _index(out: Path) -> dict[str, dict[str, str]]:
    with (out / "index.csv").open(encoding="utf-8", newline="") as handle:
        return {row["entry_id"]: row for row in csv.DictReader(handle)}


def _works(out: Path) -> list[tuple[Any, ...]]:
    with contextlib.closing(sqlite3.connect(out / DATABASE_NAME)) as connection:
        return connection.execute(
            "SELECT entry_id, updated_at, last_run_id FROM works ORDER BY entry_id"
        ).fetchall()


def test_the_post_phase_leaves_the_works_rows_and_the_index_alone(
    tmp_path: Path, settings: Settings
) -> None:
    quiet = replace(settings, title_pages=TitlePages(model=None))
    scene = Scene(tmp_path, quiet, rows=[ONLY_E1])
    scene.download(tmp_path / "off")
    shadow = tmp_path / "shadow"
    before: list[list[tuple[Any, ...]]] = []
    scene.session.before = lambda: before.append(_works(shadow))

    scene.download(shadow, decisions="shadow", classify=True)

    assert len(scene.session.calls) == 2
    assert before[0] == _works(shadow)
    # The two runs differ only in when they wrote.
    timed = ("updated_at", "last_attempt_at")
    off, on = _index(tmp_path / "off"), _index(shadow)
    for row in (*off.values(), *on.values()):
        for name in timed:
            row.pop(name)
    assert off == on


def test_a_store_error_fails_one_check_and_classify_still_runs(
    tmp_path: Path, settings: Settings, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet = replace(settings, title_pages=TitlePages(model=None))
    scene = Scene(tmp_path, quiet)
    real = Store.record_page_check

    def locked(self: Store, lease: Any, check: GateCheck) -> int:
        if check.band == "review":
            raise sqlite3.OperationalError("database is locked")
        return real(self, lease, check)

    monkeypatch.setattr(Store, "record_page_check", locked)
    result = scene.download(out, decisions="shadow", classify=True)

    gate = _gate(result)
    outcomes = {check.entry_id: check.outcome for check in gate.checks}
    assert outcomes == {"E1": "ok", "E2": "failed"}
    (failed,) = [check for check in gate.checks if check.outcome == "failed"]
    assert failed.note is not None and "database is locked" in failed.note
    assert gate.usage.requests == 2
    report = result.report
    assert isinstance(report, core.DownloadReport) and report.classify is not None
    assert len(read_page_labels(out, "E1")) == 4


def test_a_gate_that_stops_keeps_its_report_and_classify_runs(
    tmp_path: Path, settings: Settings, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet = replace(settings, title_pages=TitlePages(model=None))
    scene = Scene(tmp_path, quiet, rows=[ONLY_E1])

    def broken(*_args: Any) -> Any:
        raise RuntimeError("no jobs today")

    monkeypatch.setattr(gate_module._Gate, "jobs", broken)
    events = Recorder()
    result = scene.download(out, events, decisions="shadow", classify=True)

    assert _gate(result).mode == "shadow"
    assert any("no jobs today" in warning for warning in events.warnings)
    assert len(read_page_labels(out, "E1")) == 4


def test_a_size_limit_that_stops_the_run_skips_the_post_phase(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    scene = Scene(tmp_path, settings)

    result = scene.download(
        out, decisions="shadow", classify=True, total_images_gb=1e-9, on_exceed="stop"
    )

    assert result.counts.partial == 1
    assert scene.session.calls == []
    assert _gate(result).checks == ()
    assert isinstance(result.report, core.DownloadReport)
    assert result.report.classify is None


def test_classify_labels_the_downloaded_works_after_the_downloads(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    scene = Scene(tmp_path, replace(settings, title_pages=TitlePages(model=None)))

    result = scene.download(out, classify=True)

    assert scene.session.gate_calls() == []
    assert scene.log[-1] == "decision"
    assert scene.mdz.sampled == []
    assert len(read_page_labels(out, "E1")) == 4
    report = result.report
    assert isinstance(report, core.DownloadReport)
    assert report.classify is not None
    assert [work.entry_id for work in report.classify.works] == ["E1"]
    fields = report_fields(report)
    assert (fields["pages_labelled"], fields["gate_checks"]) == (4, 0)


def test_a_post_phase_without_its_key_is_refused_before_the_run(
    tmp_path: Path, settings: Settings, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = Scene(tmp_path, settings)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(core.PlanError, match="OPENAI_API_KEY"):
        scene.download(out, decisions="shadow", decision_model="gpt-5.5")
    assert not out.exists()


# -- the page model of inspect and verify --pages -------------------------------


def _real_judge(scene: Scene) -> Wiring:
    return Wiring(
        providers={
            "mdz": lambda _s: scene.mdz,
            "internet_archive": lambda _s: scene.ia,
        },
        session_factory=lambda: scene.session,
        sleep=lambda _seconds: None,
    )


def test_verify_pages_asks_the_named_model_in_its_dialect(
    tmp_path: Path, settings: Settings, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    scene = Scene(tmp_path, settings, rows=[ONLY_E1])
    scene.download(out)
    resolution = resolve_verify(
        {"output": out, "pages": True, "first": 2, "decision_model": "gpt-5.5"}
    )
    run_plan = core.plan(resolution.spec, settings, sources=resolution.sources)

    result = core.run(run_plan, Recorder(), wiring=_real_judge(scene))

    assert result.counts.complete == 1
    (call,) = scene.session.calls
    assert call["url"] == ENDPOINTS["openai"]
    assert call["headers"]["Authorization"] == "Bearer sk-openai-test"
    assert call["body"]["model"] == "gpt-5.5"
    images = [p for p in call["body"]["input"][0]["content"] if "image_url" in p]
    assert len(images) == 2


def test_inspect_asks_the_named_model_at_the_named_endpoint(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    scene = Scene(
        tmp_path, settings, rows=[{"entry_id": "E2", "title": "Ein new Kochbuch"}]
    )
    scene.download(out)
    endpoint = "https://decisions.example.org/v1/decisions"
    resolution = resolve_inspect(
        {
            "output": out,
            "entry_ids": ("E2",),
            "decision_model": "vendor/page-model",
            "decision_endpoint": endpoint,
        }
    )
    run_plan = core.plan(resolution.spec, settings, sources=resolution.sources)

    result = core.run(run_plan, Recorder(), wiring=_real_judge(scene))

    assert result.counts.complete == 1
    (call,) = scene.session.calls
    assert call["url"] == endpoint
    assert call["headers"]["Authorization"] == "Bearer sk-or-test-key"
    assert call["body"]["model"] == "vendor/page-model"
    assert "state" in call["body"]


def test_verify_refuses_a_page_model_without_pages(out: Path) -> None:
    with pytest.raises(SpecError, match="--decision-model needs --pages"):
        resolve_verify({"output": out, "decision_model": "gpt-5.5"})
