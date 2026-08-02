"""Unit tests for index_manager module."""

from __future__ import annotations

import csv
import os
from typing import Any

import pandas as pd

from main.data.index import build_index_row, read_index_csv, update_index_csv


class TestUpdateIndexCsv:
    """Tests for update_index_csv function."""

    def test_creates_index_csv_with_header(self, temp_output_dir: str) -> None:
        """Test creates index.csv with headers for first row."""
        row = {
            "work_id": "abc123",
            "entry_id": "E0001",
            "title": "Test Title",
            "creator": "Test Author",
        }

        update_index_csv(temp_output_dir, row)

        index_path = os.path.join(temp_output_dir, "index.csv")
        assert os.path.exists(index_path)

        df = pd.read_csv(index_path)
        assert len(df) == 1
        assert df.iloc[0]["work_id"] == "abc123"
        assert df.iloc[0]["title"] == "Test Title"

    def test_appends_to_existing_csv(self, temp_output_dir: str) -> None:
        """Test appends row to existing index.csv."""
        row1 = {"work_id": "abc123", "title": "Title 1"}
        row2 = {"work_id": "def456", "title": "Title 2"}

        update_index_csv(temp_output_dir, row1)
        update_index_csv(temp_output_dir, row2)

        index_path = os.path.join(temp_output_dir, "index.csv")
        df = pd.read_csv(index_path)

        assert len(df) == 2
        assert df.iloc[0]["work_id"] == "abc123"
        assert df.iloc[1]["work_id"] == "def456"

    def test_stable_full_column_header(self, temp_output_dir: str) -> None:
        """Header is always the full stable column set, in a fixed order."""
        from main.data.index import INDEX_COLUMNS

        # A first row missing most columns must not freeze a narrow header.
        row1 = {"work_id": "abc123", "title": "Title 1", "creator": "Author 1"}
        row2 = {"creator": "Author 2", "work_id": "def456", "title": "Title 2"}

        update_index_csv(temp_output_dir, row1)
        update_index_csv(temp_output_dir, row2)

        index_path = os.path.join(temp_output_dir, "index.csv")
        with open(index_path, encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader)

        assert headers == INDEX_COLUMNS
        assert headers[0] == "work_id"

    def test_upsert_replaces_row_by_work_id(self, temp_output_dir: str) -> None:
        """Re-running the same work_id updates its row instead of duplicating."""
        update_index_csv(temp_output_dir, {"work_id": "abc123", "status": "failed"})
        update_index_csv(temp_output_dir, {"work_id": "abc123", "status": "completed"})

        df = pd.read_csv(os.path.join(temp_output_dir, "index.csv"))
        assert len(df) == 1
        assert df.iloc[0]["status"] == "completed"

    def test_thread_safe_updates(self, temp_output_dir: str) -> None:
        """Test multiple updates are thread-safe."""
        import threading

        rows = [{"work_id": f"id_{i}", "title": f"Title {i}"} for i in range(10)]

        threads = [
            threading.Thread(target=update_index_csv, args=(temp_output_dir, row))
            for row in rows
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        index_path = os.path.join(temp_output_dir, "index.csv")
        df = pd.read_csv(index_path)

        assert len(df) == 10

    def test_creates_directory_if_missing(self, temp_dir: str) -> None:
        """Test creates base directory if it doesn't exist."""
        output_dir = os.path.join(temp_dir, "new_dir")
        row = {"work_id": "abc123", "title": "Test"}

        update_index_csv(output_dir, row)

        assert os.path.exists(output_dir)
        assert os.path.exists(os.path.join(output_dir, "index.csv"))

    def test_handles_missing_columns(self, temp_output_dir: str) -> None:
        """Test handles rows with missing columns."""
        row1 = {"work_id": "abc123", "title": "Title 1", "creator": "Author 1"}
        row2 = {"work_id": "def456", "title": "Title 2"}

        update_index_csv(temp_output_dir, row1)
        update_index_csv(temp_output_dir, row2)

        index_path = os.path.join(temp_output_dir, "index.csv")
        df = pd.read_csv(index_path)

        assert len(df) == 2
        assert pd.isna(df.iloc[1]["creator"])

    def test_read_failure_skips_write_and_preserves_existing_rows(
        self, temp_output_dir: str
    ) -> None:
        """A read failure must not be swallowed into an empty list.

        update_index_csv rewrites the whole file, so "starting fresh" after a
        transient read failure (AV scanner, file open in Excel) would replace
        the entire ledger with the single row being written. The failure must
        propagate so update_index_csv skips the write and the ledger survives.
        """
        from unittest.mock import patch

        import main.data.index as index_module

        update_index_csv(temp_output_dir, {"work_id": "abc123", "status": "completed"})
        update_index_csv(temp_output_dir, {"work_id": "def456", "status": "failed"})

        index_path = os.path.join(temp_output_dir, "index.csv")
        with open(index_path, encoding="utf-8") as f:
            original_content = f.read()

        # The module caches rows by (mtime, size); clear it so the patched
        # reader is actually exercised on the next call.
        index_module._index_cache.clear()

        # Patch the shared stdlib csv module object (index.py's own `import
        # csv` binds the same module), rather than main.data.index.csv --
        # mypy strict flags the latter as an attribute main.data.index does
        # not explicitly re-export.
        with patch.object(csv, "DictReader", side_effect=RuntimeError("boom")):
            update_index_csv(
                temp_output_dir, {"work_id": "ghi789", "status": "pending"}
            )

        with open(index_path, encoding="utf-8") as f:
            assert f.read() == original_content


class TestBuildIndexRow:
    """Tests for build_index_row function."""

    def test_builds_complete_row(self, sample_search_result: Any) -> None:
        """Test builds complete index row with all fields."""
        row = build_index_row(
            work_id="abc123",
            entry_id="E0001",
            work_dir="/path/to/work",
            title="Test Title",
            creator="Test Author",
            selected=sample_search_result,
            selected_source_id="source123",
            work_json_path="/path/to/work.json",
            status="completed",
        )

        assert row["work_id"] == "abc123"
        assert row["entry_id"] == "E0001"
        assert row["title"] == "Test Title"
        assert row["creator"] == "Test Author"
        assert row["selected_provider"] == "Internet Archive"
        assert row["selected_source_id"] == "source123"
        assert row["status"] == "completed"

    def test_none_selected(self) -> None:
        """Test builds row when no result is selected."""
        row = build_index_row(
            work_id="abc123",
            entry_id="E0001",
            work_dir="/path/to/work",
            title="Test Title",
            creator="Test Author",
            selected=None,
            selected_source_id=None,
            work_json_path="/path/to/work.json",
        )

        assert row["work_id"] == "abc123"
        assert row["selected_provider"] is None
        assert row["selected_source_id"] is None
        assert row["status"] is None

    def test_none_entry_id(self, sample_search_result: Any) -> None:
        """Test builds row with None entry_id."""
        row = build_index_row(
            work_id="abc123",
            entry_id=None,
            work_dir="/path/to/work",
            title="Test Title",
            creator=None,
            selected=sample_search_result,
            selected_source_id="source123",
            work_json_path="/path/to/work.json",
        )

        assert row["entry_id"] is None
        assert row["creator"] is None

    def test_custom_item_url(self, sample_search_result: Any) -> None:
        """Test uses custom item_url when provided."""
        custom_url = "https://custom.url/item"
        row = build_index_row(
            work_id="abc123",
            entry_id="E0001",
            work_dir="/path/to/work",
            title="Test Title",
            creator="Test Author",
            selected=sample_search_result,
            selected_source_id="source123",
            work_json_path="/path/to/work.json",
            item_url=custom_url,
        )

        assert row["item_url"] == custom_url

    def test_fallback_to_selected_item_url(self, sample_search_result: Any) -> None:
        """Test falls back to selected.item_url when custom not provided."""
        row = build_index_row(
            work_id="abc123",
            entry_id="E0001",
            work_dir="/path/to/work",
            title="Test Title",
            creator="Test Author",
            selected=sample_search_result,
            selected_source_id="source123",
            work_json_path="/path/to/work.json",
        )

        assert row["item_url"] == sample_search_result.item_url


class TestReadIndexCsv:
    """Tests for read_index_csv function."""

    def test_reads_existing_csv(self, temp_output_dir: str) -> None:
        """Test reads existing index.csv."""
        row = {"work_id": "abc123", "title": "Test"}
        update_index_csv(temp_output_dir, row)

        df = read_index_csv(temp_output_dir)

        assert df is not None
        assert len(df) == 1
        assert df.iloc[0]["work_id"] == "abc123"

    def test_returns_none_for_missing_file(self, temp_output_dir: str) -> None:
        """Test returns None when index.csv doesn't exist."""
        df = read_index_csv(temp_output_dir)

        assert df is None

    def test_handles_empty_csv(self, temp_output_dir: str) -> None:
        """Test handles empty CSV file."""
        index_path = os.path.join(temp_output_dir, "index.csv")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write("work_id,title\n")

        df = read_index_csv(temp_output_dir)

        assert df is not None
        assert len(df) == 0


class TestUpsertKeyedOnEntryId:
    """The upsert key is (work_id, entry_id), not work_id alone."""

    def test_same_work_id_different_entry_ids_coexist(
        self, temp_output_dir: str
    ) -> None:
        """Two editions sharing title+creator (same work_id) keep separate rows."""
        update_index_csv(
            temp_output_dir,
            {"work_id": "abc123", "entry_id": "E0001", "status": "completed"},
        )
        update_index_csv(
            temp_output_dir,
            {"work_id": "abc123", "entry_id": "E0002", "status": "failed"},
        )

        df = read_index_csv(temp_output_dir)
        assert df is not None
        assert len(df) == 2
        by_entry = {str(r["entry_id"]): str(r["status"]) for _, r in df.iterrows()}
        assert by_entry == {"E0001": "completed", "E0002": "failed"}

    def test_same_work_and_entry_id_upserts(self, temp_output_dir: str) -> None:
        """A matching (work_id, entry_id) pair still updates in place."""
        update_index_csv(
            temp_output_dir,
            {"work_id": "abc123", "entry_id": "E0001", "status": "failed"},
        )
        update_index_csv(
            temp_output_dir,
            {"work_id": "abc123", "entry_id": "E0001", "status": "completed"},
        )

        df = read_index_csv(temp_output_dir)
        assert df is not None
        assert len(df) == 1
        assert str(df.iloc[0]["status"]) == "completed"

    def test_upsert_clears_stale_fields_when_row_becomes_no_match(
        self, temp_output_dir: str
    ) -> None:
        """A row that becomes no_match must clear stale provider/url fields.

        The merge is keyed on ``col in row``, not on truthiness: build_index_row
        always supplies every column and uses None for "no selection", so a
        value-based test previously left the prior provider, source id, and
        item URL on a row that had since become no_match.
        """
        update_index_csv(
            temp_output_dir,
            {
                "work_id": "abc123",
                "entry_id": "E0001",
                "selected_provider": "Internet Archive",
                "selected_provider_key": "internet_archive",
                "item_url": "https://archive.org/details/abc",
                "status": "completed",
            },
        )
        update_index_csv(
            temp_output_dir,
            {
                "work_id": "abc123",
                "entry_id": "E0001",
                "selected_provider": None,
                "selected_provider_key": None,
                "item_url": None,
                "status": "no_match",
            },
        )

        df = read_index_csv(temp_output_dir)
        assert df is not None
        assert len(df) == 1
        row = df.iloc[0]
        assert pd.isna(row["selected_provider"])
        assert pd.isna(row["selected_provider_key"])
        assert pd.isna(row["item_url"])
        assert row["status"] == "no_match"


class TestBomPrefixedLedger:
    """An Excel "CSV UTF-8" save prefixes a BOM the reader must strip.

    Read as plain utf-8, the first fieldname became BOM + "work_id": the next
    rewrite blanked that column and the upsert, unable to match, appended a
    duplicate row for the work.
    """

    def test_bom_ledger_upserts_instead_of_duplicating(
        self, temp_output_dir: str
    ) -> None:
        index_path = os.path.join(temp_output_dir, "index.csv")
        with open(index_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["work_id", "entry_id", "title", "status"])
            writer.writerow(["abc123", "E0001", "Title 1", "completed"])

        update_index_csv(
            temp_output_dir,
            {"work_id": "abc123", "entry_id": "E0001", "status": "failed"},
        )

        df = pd.read_csv(index_path, dtype=str, keep_default_na=False)
        assert len(df) == 1
        assert df.iloc[0]["work_id"] == "abc123"
        assert df.iloc[0]["status"] == "failed"
