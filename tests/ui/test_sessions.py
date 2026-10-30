"""Scripted guided sessions run end to end through :func:`run_app`.

Each session answers the wizard with a scripted prompter, then runs the
chosen command through the same plan and run as the command line, against
the fake providers and the stub judge of ``tests/run/support.py``.
"""

from __future__ import annotations

import logging
from io import StringIO
from pathlib import Path
from typing import Any, Final

import pytest
from rich.console import Console

from chronodownloader import run as core
from chronodownloader._core import PageCheck, VerifiedWork, VerifyReport
from chronodownloader.matching.answers import PageAnswer
from chronodownloader.output.store import read_files, read_work_status
from chronodownloader.settings import load
from chronodownloader.spec import DOWNLOAD_OPTIONS, resolve_download
from chronodownloader.ui import progress as progress_module
from chronodownloader.ui.app import run_app
from tests.run.support import (
    PROVIDERS,
    FakeProvider,
    StubJudge,
    record,
    run_command,
    settings_file,
    wiring,
    write_csv,
)
from tests.support.testing import KEEP, ScriptedPrompter
from tests.ui.support import WORKS, console

COOK = record("mdz", "bsb1", "Le cuisinier françois", year=1651)
KOCH = record("mdz", "bsb2", "Ein new Kochbuch")
MATCHING_KEEPS: Final = [KEEP] * 5
INTERRUPT: Final = object()
"""A scripted answer that presses Ctrl+C at its prompt."""


class InterruptingPrompter(ScriptedPrompter):
    """A scripted prompter that raises KeyboardInterrupt at :data:`INTERRUPT`."""

    def _take(self, message: str) -> Any:
        answer = super()._take(message)
        if answer is INTERRUPT:
            raise KeyboardInterrupt
        return answer


def fakes() -> list[FakeProvider]:
    return [
        FakeProvider("mdz", records={"cuisinier": [COOK], "kochbuch": [KOCH]}),
        FakeProvider("internet_archive"),
    ]


def judge() -> StubJudge:
    return StubJudge({"mdz:bsb1": 0.97, "mdz:bsb2": 0.97})


class Run:
    """A guided session over the test's settings file, fakes and console."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = settings_file(tmp_path)
        self.csv = write_csv(tmp_path / "works.csv", WORKS)
        self.out = tmp_path / "out"
        self.stdout = StringIO()
        self.screen = ""
        self.prompter: ScriptedPrompter | None = None

    def __call__(
        self,
        answers: list[Any],
        *,
        providers: list[FakeProvider] | None = None,
        settings: Path | None = None,
        terminal: bool = False,
    ) -> int:
        self.prompter = InterruptingPrompter(answers)
        screen, buffer = console(terminal=terminal)
        code = run_app(
            settings or self.settings,
            prompter=self.prompter,
            console=screen,
            stdout=self.stdout,
            clipboard=lambda _argv, _data: None,
            wiring=wiring(providers if providers is not None else fakes(), judge()),
            require_tty=False,
        )
        self.screen = buffer.getvalue()
        return code

    def download_answers(
        self, *, dry_run: Any = KEEP, decisions: Any = KEEP
    ) -> list[Any]:
        """Download the CSV's works as page images into the output root;
        ``decisions="shadow"`` keeps the settings' decision model."""
        model = [KEEP] * 3 if decisions == "shadow" else []
        return [
            KEEP,
            str(self.csv),
            *[KEEP] * 4,
            *MATCHING_KEEPS,
            "images",
            KEEP,
            False,
            KEEP,
            KEEP,
            str(self.out),
            decisions,
            KEEP,
            *model,
            KEEP,
            dry_run,
            "run",
        ]


@pytest.fixture
def session(tmp_path: Path) -> Run:
    return Run(tmp_path)


def statuses(out: Path) -> dict[str, str]:
    return {
        str(row["entry_id"]): str(row["status"])
        for row in read_work_status(out, ["E1", "E2", "E9"])
    }


def test_a_download_session_runs_the_works_and_prints_the_summary(
    session: Run,
) -> None:
    assert session(session.download_answers()) == 0

    assert session.prompter is not None
    session.prompter.finish()
    assert statuses(session.out) == {"E1": "complete", "E2": "complete"}
    assert "E1: complete" in session.screen
    assert "Run summary: 2 work(s): 2 complete" in session.screen


def test_a_shadow_download_session_shows_the_gate_checks(session: Run) -> None:
    # The fake page files are no decodable images: each check fails unsent.
    assert session(session.download_answers(decisions="shadow")) == 0

    assert session.prompter is not None
    session.prompter.finish()
    assert "Gate checks" in session.screen
    assert "unreadable page" in session.screen
    assert "Gate (shadow, model typesafe/jev-1.14): 0 request(s)" in session.screen
    assert "Run summary: 2 work(s): 2 complete" in session.screen


def test_a_download_session_under_a_terminal_shows_the_live_display(
    session: Run,
) -> None:
    assert session(session.download_answers(), terminal=True) == 0

    assert statuses(session.out) == {"E1": "complete", "E2": "complete"}
    assert "Statuses: 2 complete" in session.screen
    assert "Run summary: 2 work(s): 2 complete" in session.screen


def test_a_dry_run_session_shows_the_plan_and_writes_nothing(session: Run) -> None:
    assert session(session.download_answers(dry_run=True)) == 0

    assert not session.out.exists()
    assert "Dry run: 2 work(s)" in session.screen
    assert "Nothing was requested or written." in session.screen


def test_a_run_logs_to_the_file_and_a_dry_run_writes_no_log(
    session: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = core.run

    def logging_run(*args: Any, **kwargs: Any) -> Any:
        logging.getLogger("chronodownloader.run").info("ui info record")
        return real(*args, **kwargs)

    monkeypatch.setattr(core, "run", logging_run)
    assert session(session.download_answers(dry_run=True)) == 0
    assert not (tmp_path / "logs").exists()

    assert session(session.download_answers()) == 0
    text = (tmp_path / "logs" / "application.log").read_text(encoding="utf-8")
    assert "INFO - chronodownloader.run - ui info record" in text


def test_a_failed_work_exits_1_as_the_command_does(session: Run) -> None:
    broken = [FakeProvider("mdz", records={"cuisinier": [COOK]}, failing={"p00001"})]
    providers = [*broken, FakeProvider("internet_archive")]

    code = session(session.download_answers(), providers=providers)

    assert statuses(session.out)["E1"] == "partial"
    assert code == 1


def test_a_fetch_session_downloads_the_named_item(session: Run) -> None:
    answers = [
        "fetch",
        "E9",
        "Le cuisinier francois",
        "provider",
        "mdz",
        "bsb1",
        KEEP,
        "images",
        KEEP,
        False,
        KEEP,
        KEEP,
        str(session.out),
        KEEP,
        "run",
    ]
    assert session(answers) == 0

    assert statuses(session.out) == {"E9": "complete"}
    assert len(read_files(session.out, "E9")) == 3


def test_an_ad_hoc_search_session_prints_the_candidates(session: Run) -> None:
    answers = [
        "search",
        "title",
        "Le cuisinier francois",
        KEEP,
        "1651",
        KEEP,
        KEEP,
        KEEP,
        *MATCHING_KEEPS,
        KEEP,
        KEEP,
        "run",
    ]
    assert session(answers) == 0

    assert "*mdz:bsb1" in session.screen
    assert "Run summary: 1 work(s): 1 accepted" in session.screen
    assert not session.out.exists()


def _downloaded(session: Run) -> None:
    flags = {
        "input": session.csv,
        "output": session.out,
        "providers": PROVIDERS,
        "format": "images",
        "include_metadata": False,
    }
    settings = load(session.settings, DOWNLOAD_OPTIONS)
    result = run_command(resolve_download, settings, flags, fakes(), judge())
    assert result.counts.complete == 2


@pytest.mark.parametrize(("missing", "code"), [(False, 0), (True, 1)])
def test_a_verify_session_checks_the_files_and_exits_like_the_command(
    session: Run, missing: bool, code: int
) -> None:
    _downloaded(session)
    if missing:
        (row,) = read_work_status(session.out, ["E1"])
        first = read_files(session.out, "E1")[0]
        (session.out / str(row["folder"]) / first.path).unlink()
    answers = ["verify", str(session.out), KEEP, KEEP, KEEP, "run"]

    assert session(answers) == code

    assert "Verified works" in session.screen
    assert ("E1: missing" in session.screen) is missing


def test_the_verify_view_shows_the_page_answers() -> None:
    answer = PageAnswer(0.12, 0.34, "half_title", pages=3)
    check = PageCheck("mdz", "bsb1", "E1", None, "checked", pages=3, answer=answer)
    report = VerifyReport((VerifiedWork("E1", "e1_work", "complete", 3, pages=check),))
    screen = Console(record=True, width=200)

    screen.print(progress_module._verify_view(report))

    text = screen.export_text()
    for shown in ("0.12", "0.34", "half_title", "checked"):
        assert shown in text


def test_ctrl_c_at_a_prompt_exits_130(session: Run) -> None:
    assert session([KEEP, str(session.csv), INTERRUPT]) == 130

    assert "Interrupted by the user." in session.screen
    assert not session.out.exists()


def test_ctrl_c_during_the_run_exits_130_and_releases_the_works(
    session: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = progress_module.RichProgress.work_started
    seen: list[str] = []

    def interrupt(self: progress_module.RichProgress, event: Any) -> None:
        started(self, event)
        seen.append(event.entry_id)
        if len(seen) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(progress_module.RichProgress, "work_started", interrupt)

    assert session(session.download_answers()) == 130

    assert "Interrupted by the user." in session.screen
    monkeypatch.undo()
    assert session(session.download_answers()) == 0
    assert statuses(session.out) == {"E1": "complete", "E2": "complete"}


def test_switching_commands_and_copying_writes_the_command(session: Run) -> None:
    answers = ["verify", str(session.out), "E1", KEEP, KEEP, "copy"]

    assert session(answers) == 0

    line = session.stdout.getvalue()
    assert line.startswith("chronodownloader verify --entry-ids E1 --output ")
    assert "--settings" in line
    assert "Copied the command to the clipboard." in session.screen


def test_back_from_a_chosen_command_returns_to_the_command_step(
    session: Run,
) -> None:
    answers = ["verify", "<", "verify", KEEP, KEEP, KEEP, KEEP, "quit"]

    assert session(answers) == 0

    assert session.prompter is not None
    session.prompter.finish()
    assert session.prompter.transcript.count("? Command:") == 2


def test_the_review_withholds_run_while_the_plan_fails(
    session: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    answers = session.download_answers()[:-1] + ["quit"]

    assert session(answers) == 0

    assert session.prompter is not None
    transcript = session.prompter.transcript
    assert "Plan error:" in transcript
    assert "Run unavailable: the plan failed; edit a step" in transcript
    assert "   Run\n" not in transcript


def test_a_settings_error_exits_2(session: Run, tmp_path: Path) -> None:
    assert session([], settings=tmp_path / "missing.yaml") == 2

    assert "Error:" in session.screen
