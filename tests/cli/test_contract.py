"""The agent contract of every command: exit codes and the JSON line."""

from __future__ import annotations

import importlib
import socket
from pathlib import Path
from typing import Any

import pytest

from chronodownloader import __main__ as entry
from chronodownloader import run as core
from chronodownloader.cli.summary import COMMAND_FIELDS, exit_code
from chronodownloader.events import RunEvents
from chronodownloader.run import RunPlan, RunResult, WorkCounts
from chronodownloader.settings import Settings
from chronodownloader.spec import COMMANDS, CommandSpec
from tests.cli.conftest import json_lines
from tests.support.testing import WriteGuard

SUMMARY_KEYS = {
    "dry_run",
    "command",
    "items_total",
    "items_complete",
    "items_partial",
    "items_failed",
    "items_no_match",
    "items_skipped",
    "items_needs_review",
    "items_deferred",
    "items_blocked",
}

VALID: dict[str, list[str]] = {
    "download": ["--input", "works.csv"],
    "fetch": ["--entry-id", "E1", "--url", "https://example.org/manifest"],
    "search": ["--title", "Le cuisinier francois"],
    "inspect": ["--provider", "mdz", "--id", "bsb00000001"],
    "verify": [],
    "classify": [],
    "status": [],
    "drop": ["--output", "out", "--all"],
    "providers": [],
    "ui": [],
}
"""A valid command line of each command, without --json."""

DRY_RUN = ("download", "fetch", "search", "inspect", "verify", "classify", "drop")
"""The commands that take --dry-run."""

JSON_COMMANDS = [command for command in COMMANDS if command != "ui"]
"""The commands with a summary line; ``tests/run/`` runs each end to end."""


def _one_line(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], str]:
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1, captured.out
    (payload,) = json_lines(captured.out)
    own = set(COMMAND_FIELDS.get(payload["command"] or "", {}))
    assert set(payload) == SUMMARY_KEYS | own
    return payload, captured.err


def test_the_valid_lines_cover_every_command() -> None:
    assert set(VALID) == set(COMMANDS)


def test_ui_without_a_terminal_exits_2_without_a_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert entry.main(["ui"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "needs an interactive terminal" in captured.err


def test_without_json_stdout_stays_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["download", *VALID["download"]]) == 2
    assert capsys.readouterr().out == ""


USAGE_ERRORS = [
    pytest.param(
        ["download", "--input", "w.csv", "--concurrency", "0"], id="bad-value"
    ),
    pytest.param(["download", "--input", "w.csv", "--bogus"], id="unknown-flag"),
    pytest.param(["bogus"], id="unknown-command"),
    pytest.param(["download"], id="no-input"),
    pytest.param(["download", "--input", "w.csv", "--providers", "gallica"], id="key"),
    pytest.param(["search", "--input", "w.csv", "--title", "x"], id="exclusive"),
    pytest.param(["fetch", "--entry-id", "E1"], id="no-item"),
    pytest.param(["drop", "--output", "out"], id="no-selection"),
    pytest.param(["verify", "--first", "3"], id="first-without-pages"),
    pytest.param(["classify", "--decision-provider", "anthropic"], id="dialect"),
]


@pytest.mark.parametrize("argv", USAGE_ERRORS)
def test_a_usage_error_exits_2_with_one_json_line(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert entry.main([*argv, "--json"]) == 2

    payload, err = _one_line(capsys)
    expected = argv[0] if argv[0] in COMMANDS else None
    assert payload["command"] == expected
    assert "[ERROR] Invalid arguments" in err


def test_a_missing_settings_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.yaml"
    assert entry.main(["providers", "--settings", str(missing), "--json"]) == 2

    payload, err = _one_line(capsys)
    assert payload["command"] == "providers"
    assert "[ERROR] Invalid arguments" in err


@pytest.mark.parametrize(
    ("failure", "code"), [(KeyboardInterrupt(), 130), (RuntimeError("boom"), 1)]
)
@pytest.mark.parametrize("command", JSON_COMMANDS)
def test_interrupt_and_crash_write_one_json_line(
    command: str,
    failure: BaseException,
    code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(core, "plan", _raise)

    assert entry.main([command, *VALID[command], "--json"]) == code
    payload, err = _one_line(capsys)
    assert payload["command"] == command
    assert payload["items_failed"] == 0
    assert "[ERROR]" in err


def test_a_settings_file_that_is_not_utf8_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    latin1 = tmp_path / "latin1.yaml"
    latin1.write_bytes("state_dir: 'C:/Daten/Kochbücher'\n".encode("latin-1"))
    assert entry.main(["providers", "--settings", str(latin1), "--json"]) == 2

    payload, err = _one_line(capsys)
    assert payload["command"] == "providers"
    assert "UTF-8" in err


def test_the_settings_reach_the_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("providers:\n  mdz:\n    network:\n      delay_ms: 1234\n", "utf-8")
    seen: list[Settings] = []

    def plan(spec: CommandSpec, settings: Settings, **_kwargs: Any) -> RunPlan:
        seen.append(settings)
        return RunPlan(spec, settings)

    monkeypatch.setattr(core, "plan", plan)
    argv = ["download", *VALID["download"], "--settings", str(path), "--dry-run"]

    assert entry.main([*argv, "--json"]) == 0
    (settings,) = seen
    assert settings.source is not None
    assert settings.source.resolve() == path.resolve()
    assert settings.providers["mdz"].network.delay_ms == 1234


@pytest.mark.parametrize(
    ("failure", "code"), [(KeyboardInterrupt(), 130), (ImportError("broken"), 1)]
)
def test_a_failure_while_importing_the_handler_writes_one_json_line(
    failure: BaseException,
    code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _raise(name: str) -> Any:
        raise failure

    monkeypatch.setattr(importlib, "import_module", _raise)

    assert entry.main(["download", *VALID["download"], "--json"]) == code
    payload, err = _one_line(capsys)
    assert payload["command"] == "download"
    assert "[ERROR]" in err


def test_a_missing_handler_module_exits_1_with_one_json_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handlers = {**entry._HANDLERS, "status": ("chronodownloader.cli.gone", "execute")}
    monkeypatch.setattr(entry, "_HANDLERS", handlers)

    assert entry.main(["status", "--json"]) == 1
    payload, err = _one_line(capsys)
    assert payload["command"] == "status"
    assert "Unexpected error" in err


def test_an_interrupt_while_parsing_writes_one_json_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise() -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "build_parser", _raise)

    assert entry.main(["verify", "--json"]) == 130
    payload, _err = _one_line(capsys)
    assert payload["command"] == "verify"


def test_help_with_json_prints_help_and_no_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        entry.main(["download", "--help", "--json"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: chronodownloader download")
    assert '"dry_run"' not in out


def test_abbreviated_flags_are_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["verify", "--js"]) == 2
    assert capsys.readouterr().out == ""


PLANNED = WorkCounts(total=3, skipped=2)


class _NoNetwork(AssertionError):
    """A dry run opened a connection."""


@pytest.mark.parametrize("command", DRY_RUN)
def test_a_dry_run_plans_without_writes_network_or_run(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    strict_guard: WriteGuard,
) -> None:
    def plan(spec: CommandSpec, settings: Settings, **_kwargs: Any) -> RunPlan:
        return RunPlan(spec, settings, counts=PLANNED)

    def run(run_plan: RunPlan, events: RunEvents) -> RunResult:
        raise AssertionError("a dry run must not run")

    def connect(*_args: Any) -> None:
        raise _NoNetwork

    monkeypatch.setattr(core, "plan", plan)
    monkeypatch.setattr(core, "run", run)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.chdir(tmp_path)
    strict_guard.consume()

    assert entry.main([command, *VALID[command], "--dry-run", "--json"]) == 0

    assert strict_guard.consume() == []
    assert list(tmp_path.iterdir()) == []
    payload, _err = _one_line(capsys)
    assert payload["dry_run"] is True
    assert (payload["items_total"], payload["items_skipped"]) == (3, 2)


OPEN = WorkCounts(
    total=5, complete=1, no_match=1, needs_review=1, deferred=1, blocked=1
)


def test_open_states_exit_0_and_unfinished_work_exits_1() -> None:
    assert exit_code(OPEN) == 0
    assert exit_code(WorkCounts(total=2, complete=1, failed=1)) == 1
    assert exit_code(WorkCounts(total=2, complete=1, partial=1)) == 1


def test_a_run_with_open_works_exits_0_and_counts_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def plan(spec: CommandSpec, settings: Settings, **_kwargs: Any) -> RunPlan:
        return RunPlan(spec, settings)

    def run(run_plan: RunPlan, events: RunEvents) -> RunResult:
        return RunResult(OPEN)

    monkeypatch.setattr(core, "plan", plan)
    monkeypatch.setattr(core, "run", run)

    assert entry.main(["download", *VALID["download"], "--json"]) == 0
    payload, _err = _one_line(capsys)
    assert payload == {
        "dry_run": False,
        "command": "download",
        "items_total": 5,
        "items_complete": 1,
        "items_partial": 0,
        "items_failed": 0,
        "items_no_match": 1,
        "items_skipped": 0,
        "items_needs_review": 1,
        "items_deferred": 1,
        "items_blocked": 1,
    }


def test_a_partial_work_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def plan(spec: CommandSpec, settings: Settings, **_kwargs: Any) -> RunPlan:
        return RunPlan(spec, settings)

    def run(run_plan: RunPlan, events: RunEvents) -> RunResult:
        return RunResult(WorkCounts(total=1, partial=1))

    monkeypatch.setattr(core, "plan", plan)
    monkeypatch.setattr(core, "run", run)

    assert entry.main(["download", *VALID["download"], "--json"]) == 1
    payload, _err = _one_line(capsys)
    assert payload["items_partial"] == 1
