"""The agent contract of inspect, verify, status, drop and providers end to end.

Each command runs through ``chronodownloader.__main__.main`` against a store
that a download with the fakes filled: exactly one JSON summary line with
``--json``, the command's own summary fields, its exit code, and a
``--dry-run`` that writes nothing.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

import pytest

from chronodownloader import __main__ as entry
from chronodownloader import run as core
from chronodownloader.coord import Coordinator, Holder
from chronodownloader.output.store import read_files, read_work_status
from chronodownloader.settings import Settings
from tests.run.support import (
    FakeProvider,
    StubJudge,
    record,
    settings_file,
    wiring,
    write_csv,
)


@pytest.fixture
def mdz() -> FakeProvider:
    return FakeProvider(
        "mdz",
        records={
            "cuisinier": [record("mdz", "bsb1", "Le cuisinier françois")],
            "kochbuch": [record("mdz", "bsb2", "Ein new Kochbuch")],
        },
    )


@pytest.fixture
def judge() -> StubJudge:
    return StubJudge({"mdz:bsb1": 0.97, "mdz:bsb2": 0.5})


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, mdz: FakeProvider, judge: StubJudge) -> None:
    real = core.run
    monkeypatch.setattr(
        core, "run", functools.partial(real, wiring=wiring([mdz], judge))
    )


@pytest.fixture
def settings_path(tmp_path: Path) -> str:
    return str(settings_file(tmp_path, providers=("mdz",)))


@pytest.fixture
def out(tmp_path: Path, settings_path: str, wired: None) -> Path:
    """An output root with E1 complete and E2 in review."""
    root = tmp_path / "out"
    csv = write_csv(
        tmp_path / "works.csv",
        [
            {"entry_id": "E1", "title": "Le cuisinier francois"},
            {"entry_id": "E2", "title": "Ein new Kochbuch"},
        ],
    )
    argv = ["download", "--input", str(csv), "--output", str(root)]
    assert entry.main([*argv, "--settings", settings_path, "--format", "images"]) == 0
    return root


def main(
    capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, Any], str]:
    capsys.readouterr()
    code = entry.main([*argv, "--json"])
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1, captured.out
    return code, json.loads(lines[0]), captured.err


def files_of(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_status_exits_0_and_counts_by_status(
    out: Path, settings_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, err = main(
        capsys, "status", "--output", str(out), "--settings", settings_path
    )

    assert code == 0
    assert payload["command"] == "status"
    assert (payload["items_complete"], payload["items_needs_review"]) == (1, 1)
    assert payload["items_new"] == 0
    assert "Review queue (1):" in err

    capsys.readouterr()
    assert (
        entry.main(["status", "--output", str(out), "--settings", settings_path]) == 0
    )
    assert capsys.readouterr().out.splitlines()[0].split()[:2] == ["work", "status"]


def test_inspect_reports_the_page_check(
    out: Path, settings_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["inspect", "--entry-ids", "E2", "--output", str(out)]

    code, payload, err = main(capsys, *argv, "--settings", settings_path)

    assert code == 0
    assert (payload["items_total"], payload["items_complete"]) == (1, 1)
    assert payload["pages_sampled"] == 3
    assert "mdz:bsb2" in err
    assert "checked" in err


def test_verify_exits_1_when_a_recorded_file_is_missing(
    out: Path, settings_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (row,) = read_work_status(out, ["E1"])
    first = read_files(out, "E1")[0]
    (out / str(row["folder"]) / first.path).unlink()
    before = files_of(out)
    argv = ["verify", "--output", str(out), "--settings", settings_path]

    code, payload, _err = main(capsys, *argv, "--dry-run")
    assert (code, payload["dry_run"], payload["files_missing"]) == (0, True, 1)
    assert files_of(out) == before

    code, payload, err = main(capsys, *argv)
    assert code == 1
    assert (payload["items_failed"], payload["files_missing"]) == (1, 1)
    assert "E1: set from complete to partial" in err


def test_drop_dry_run_writes_nothing_and_drop_deletes(
    out: Path, settings_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = files_of(out)

    code, payload, err = main(
        capsys, "drop", "--output", str(out), "--all", "--dry-run"
    )
    assert (code, payload["dry_run"], payload["items_complete"]) == (0, True, 2)
    assert payload["files_deleted"] == 3
    assert "Would drop 2 work(s): E1, E2" in err
    assert files_of(out) == before

    code, payload, _err = main(
        capsys, "drop", "--output", str(out), "--only", "complete"
    )
    assert (code, payload["items_complete"], payload["files_deleted"]) == (0, 1, 3)
    assert [row["entry_id"] for row in read_work_status(out)] == ["E2"]


def test_providers_counts_open_breakers(
    tmp_path: Path, settings_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings.from_mapping({"state_dir": (tmp_path / "state").as_posix()})
    with Coordinator(
        settings.state_directory(), settings.coordination, Holder.current("t")
    ) as coord:
        coord.record_failure("polona", threshold=1, cooldown_s=600.0, cause="blocked")

    code, payload, err = main(capsys, "providers", "--settings", settings_path)

    assert code == 0
    assert (payload["items_total"], payload["breakers_open"]) == (17, 1)
    polona = next(line for line in err.splitlines() if line.startswith("polona"))
    assert "open until" in polona
