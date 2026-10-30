"""The agent contract of download, search and fetch run end to end.

Each command runs through ``chronodownloader.__main__.main`` with a settings
file in the temporary home and the fakes wired into the real run core:
exit codes 0/1/2/130, exactly one JSON summary line with ``--json`` on every
exit path, and a ``--dry-run`` that sends no request and writes nothing.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from chronodownloader import __main__ as entry
from chronodownloader import run as core
from chronodownloader.cli import command
from chronodownloader.cli.reporter import StderrReporter
from chronodownloader.events import CandidateJudged
from chronodownloader.output.store import read_work_status
from tests.run.support import (
    FakeProvider,
    StubJudge,
    record,
    settings_file,
    wiring,
    write_csv,
)

COOK = record("mdz", "bsb1", "Le cuisinier françois")
KOCH = record("mdz", "bsb2", "Ein new Kochbuch")


@pytest.fixture
def mdz() -> FakeProvider:
    return FakeProvider("mdz", records={"cuisinier": [COOK], "kochbuch": [KOCH]})


@pytest.fixture
def judge() -> StubJudge:
    return StubJudge({"mdz:bsb1": 0.97, "mdz:bsb2": 0.96})


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, mdz: FakeProvider, judge: StubJudge) -> None:
    """Run the real core with the fakes."""
    real = core.run
    monkeypatch.setattr(
        core, "run", functools.partial(real, wiring=wiring([mdz], judge))
    )


@pytest.fixture
def base(tmp_path: Path) -> list[str]:
    """The settings and output flags of every command line."""
    return [
        "--settings",
        str(settings_file(tmp_path, providers=("mdz",))),
        "--output",
        str(tmp_path / "out"),
    ]


def works(tmp_path: Path) -> str:
    rows = [
        {"entry_id": "E1", "title": "Le cuisinier francois"},
        {"entry_id": "E2", "title": "Ein new Kochbuch"},
    ]
    return str(write_csv(tmp_path / "works.csv", rows))


def one_line(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], str]:
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1, captured.out
    return json.loads(lines[0]), captured.err


@pytest.mark.usefixtures("wired")
def test_a_complete_download_exits_0_with_one_summary_line(
    tmp_path: Path, base: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["download", "--input", works(tmp_path), *base, "--json"]

    assert entry.main(argv) == 0

    payload, err = one_line(capsys)
    assert payload["command"] == "download"
    assert payload["dry_run"] is False
    assert (payload["items_total"], payload["items_complete"]) == (2, 2)
    assert "[INFO] E1: complete" in err


@pytest.mark.usefixtures("wired")
def test_a_failed_gate_check_leaves_the_exit_code_and_is_counted(
    tmp_path: Path, base: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The fake page files are no decodable images, so no check is sent.
    argv = ["download", "--input", works(tmp_path), *base, "--decisions", "shadow"]

    assert entry.main([*argv, "--json"]) == 0

    payload, err = one_line(capsys)
    assert (payload["items_complete"], payload["gate_failed"]) == (2, 2)
    assert (payload["gate_checks"], payload["gate_requests"]) == (0, 0)
    assert "Gate (shadow, model typesafe/jev-1.14): 0 request(s)" in err
    assert "mdz:bsb1" in err


@pytest.mark.usefixtures("wired")
def test_a_partial_download_exits_1(
    tmp_path: Path,
    base: list[str],
    mdz: FakeProvider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mdz.failing = {"p00002"}
    argv = ["download", "--input", works(tmp_path), *base, "--json"]

    assert entry.main(argv) == 1

    payload, _err = one_line(capsys)
    assert payload["items_partial"] == 2


@pytest.mark.usefixtures("wired")
def test_a_missing_judge_key_exits_2_naming_fuzzy(
    tmp_path: Path,
    base: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    argv = ["download", "--input", works(tmp_path), *base, "--json"]

    assert entry.main(argv) == 2

    payload, err = one_line(capsys)
    assert payload["items_total"] == 0
    assert "--matching fuzzy" in err
    assert not (tmp_path / "out").exists()


class _Interrupting(StderrReporter):
    """Ctrl+C after the second work was judged."""

    def candidate_judged(self, event: CandidateJudged) -> None:
        super().candidate_judged(event)
        if event.entry_id == "E2":
            raise KeyboardInterrupt


@pytest.mark.usefixtures("wired")
def test_an_interrupted_download_exits_130_and_releases_its_leases(
    tmp_path: Path,
    base: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(command, "StderrReporter", _Interrupting)
    argv = ["download", "--input", works(tmp_path), *base, "--concurrency", "1"]

    assert entry.main([*argv, "--json"]) == 130

    payload, err = one_line(capsys)
    assert payload["command"] == "download"
    assert "Interrupted" in err
    rows = {row["entry_id"]: row for row in read_work_status(tmp_path / "out")}
    assert rows["E1"]["status"] == "complete"
    assert rows["E2"]["status"] == "new"
    assert rows["E2"]["lease_holder"] is None


def _files(root: Path) -> set[Path]:
    return {path for path in root.rglob("*")}


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(lambda csv: ["download", "--input", csv], id="download"),
        pytest.param(lambda csv: ["search", "--input", csv], id="search"),
        pytest.param(
            lambda csv: (
                ["fetch", "--entry-id", "N1", "--title", "X"]
                + ["--provider", "mdz", "--id", "bsb1"]
            ),
            id="fetch",
        ),
    ],
)
def test_a_dry_run_sends_nothing_and_writes_nothing(
    argv: Callable[[str], list[str]],
    tmp_path: Path,
    base: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a dry run must not run")

    monkeypatch.setattr(core, "run", no_run)
    csv = works(tmp_path)
    before = _files(tmp_path)
    flags = base if argv(csv)[0] != "search" else base[:2]

    assert entry.main([*argv(csv), *flags, "--dry-run", "--json"]) == 0

    assert _files(tmp_path) == before
    payload, _err = one_line(capsys)
    assert payload["dry_run"] is True
    assert payload["items_total"] in (1, 2)


@pytest.mark.usefixtures("wired")
def test_search_prints_the_table_on_stdout_and_moves_it_to_stderr_under_json(
    tmp_path: Path, base: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    settings = base[:2]
    assert entry.main(["search", "--title", "Le cuisinier francois", *settings]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split()[:3] == ["work", "item", "year"]
    assert "*mdz:bsb1" in out

    argv = ["search", "--title", "Le cuisinier francois", *settings, "--json"]
    assert entry.main(argv) == 0
    payload, err = one_line(capsys)
    # Live round F4: an accepted candidate is not a complete work.
    assert (payload["items_accepted"], payload["items_complete"]) == (1, 0)
    assert "*mdz:bsb1" in err
    assert not (tmp_path / "out").exists()


@pytest.mark.usefixtures("wired")
def test_fetch_of_a_reviewed_work_without_reason_exits_2(
    tmp_path: Path,
    base: list[str],
    judge: StubJudge,
    capsys: pytest.CaptureFixture[str],
) -> None:
    judge.answers["mdz:bsb1"] = 0.5
    assert entry.main(["download", "--input", works(tmp_path), *base]) == 0
    capsys.readouterr()
    item = ["--entry-id", "E1", "--provider", "mdz", "--id", "bsb1"]

    assert entry.main(["fetch", *item, *base, "--json"]) == 2
    payload, err = one_line(capsys)
    assert payload["command"] == "fetch"
    assert "--reason" in err

    assert entry.main(["fetch", *item, "--reason", "checked", *base, "--json"]) == 0
    payload, _err = one_line(capsys)
    assert payload["items_complete"] == 1


@pytest.mark.usefixtures("wired")
def test_the_log_file_takes_info_and_the_console_only_warnings(
    tmp_path: Path,
    base: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wired_run = core.run

    def logging_run(*args: Any, **kwargs: Any) -> Any:
        log = logging.getLogger("chronodownloader.run")
        log.info("info record for the file")
        log.warning("warning record for both")
        return wired_run(*args, **kwargs)

    monkeypatch.setattr(core, "run", logging_run)
    argv = ["download", "--input", works(tmp_path), *base, "--json"]

    assert entry.main(argv) == 0

    payload, err = one_line(capsys)
    assert payload["command"] == "download"
    assert "[WARNING] warning record for both" in err
    assert "info record for the file" not in err
    text = (tmp_path / "logs" / "application.log").read_text(encoding="utf-8")
    assert "INFO - chronodownloader.run - info record for the file" in text
    assert "WARNING - chronodownloader.run - warning record for both" in text


@pytest.mark.usefixtures("wired")
def test_the_listings_write_no_log(
    tmp_path: Path, base: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    logs = tmp_path / "logs"
    settings = base[base.index("--settings") : base.index("--settings") + 2]
    output = base[base.index("--output") : base.index("--output") + 2]

    assert entry.main(["status", *output, *settings, "--json"]) == 0
    assert entry.main(["providers", *settings, "--json"]) == 0

    assert not logs.exists()


def test_a_missing_works_csv_exits_2(
    tmp_path: Path, base: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["download", "--input", str(tmp_path / "none.csv"), *base, "--json"]

    assert entry.main(argv) == 2

    payload, err = one_line(capsys)
    assert payload["items_total"] == 0
    assert "works CSV not found" in err
