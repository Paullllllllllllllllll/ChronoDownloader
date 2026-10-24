"""Tests of the ``index.csv`` render from the store's ``work_status`` view."""

from __future__ import annotations

import csv
from pathlib import Path

from chronodownloader.output.index import INDEX_COLUMNS, render_index, write_index
from chronodownloader.output.store import Store, WorkInput
from tests.core.coord_child import FakeClock


def test_a_root_without_a_store_gets_no_index(tmp_path: Path) -> None:
    assert write_index(tmp_path) is None
    assert not (tmp_path / "index.csv").exists()


def test_the_index_lists_every_work_with_its_status(tmp_path: Path) -> None:
    with Store(tmp_path, clock=FakeClock()) as store:
        store.begin_run("download", run_id="a")
        for entry_id, title in (("e2", "Zweites, Buch"), ("e1", 'Le "premier"')):
            lease = store.acquire(WorkInput(entry_id, title), f"{entry_id}_work")
            assert lease is not None
            if entry_id == "e1":
                store.set_status(lease, "no_match", reason="nothing found")
            store.release(lease)

    path = write_index(tmp_path)

    assert path == tmp_path / "index.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == INDEX_COLUMNS
    assert [(r["entry_id"], r["title"], r["status"]) for r in rows] == [
        ("e1", 'Le "premier"', "no_match"),
        ("e2", "Zweites, Buch", "new"),
    ]
    assert rows[0]["status_reason"] == "nothing found"
    assert rows[1]["status_reason"] == ""


def test_render_writes_missing_values_as_empty_cells() -> None:
    text = render_index([{"entry_id": "e1", "file_count": 0}])
    header, row = text.splitlines()
    assert header.split(",") == list(INDEX_COLUMNS)
    assert row.split(",")[0] == "e1"
    assert row.split(",")[INDEX_COLUMNS.index("file_count")] == "0"
    assert row.split(",")[INDEX_COLUMNS.index("title")] == ""
