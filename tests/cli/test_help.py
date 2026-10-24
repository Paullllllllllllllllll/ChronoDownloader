"""Help of the console script and of every command at two widths."""

from __future__ import annotations

import re

import pytest

from chronodownloader import __main__ as entry
from chronodownloader.spec import COMMANDS

WIDTHS = (80, 100)


def _help(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit) as exc:
        entry.main([*argv, "--help"])
    assert exc.value.code == 0
    return capsys.readouterr().out


@pytest.fixture(params=WIDTHS, ids=[f"columns{w}" for w in WIDTHS])
def columns(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> int:
    width: int = request.param
    monkeypatch.setenv("COLUMNS", str(width))
    return width


def test_the_bare_command_prints_help_and_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert entry.main([]) == 0
    bare = capsys.readouterr().out
    assert "usage: chronodownloader" in bare
    for command in COMMANDS:
        assert re.search(rf"^    {command}\b", bare, re.MULTILINE), command
    assert _help(capsys) == bare


@pytest.mark.parametrize("command", COMMANDS)
def test_help_fits_the_width_and_never_splits_a_flag(
    command: str, columns: int, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _help(capsys, command)
    for line in out.splitlines():
        assert len(line) <= columns, line
        assert not re.search(r"[\w-]-$", line), line


@pytest.mark.parametrize("command", [c for c in COMMANDS if c != "ui"])
def test_command_help_names_the_exit_codes(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _help(capsys, command)
    assert "Exit codes:" in out
    for code in ("0", "2", "130"):
        assert re.search(rf"^  {code} ", out, re.MULTILINE), code


def test_removed_flags_are_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["download", "--input", "w.csv", "--pending-mode", "new"]) == 2
    assert "unrecognized arguments" in capsys.readouterr().err
