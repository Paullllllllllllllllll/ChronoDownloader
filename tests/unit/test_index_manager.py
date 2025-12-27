"""Unit tests for index_manager module."""
from __future__ import annotations

import csv
import os

import pandas as pd
import pytest

from main.index_manager import (
    update_index_csv,
    build_index_row,
    read_index_csv,
    get_processed_work_ids,
)


class TestUpdateIndexCsv:
    """Tests for update_index_csv function."""
    
    def test_creates_index_csv_with_header(self, temp_output_dir):
        """Test creates index.csv with headers for first row."""
        row = {
            "work_id": "abc123",
            "entry_id": "E0001",
            "title": "Test Title",
            "creator": "Test Author"
        }
        
        update_index_csv(temp_output_dir, row)
        
        index_path = os.path.join(temp_output_dir, "index.csv")
        assert os.path.exists(index_path)
        
        df = pd.read_csv(index_path)
        assert len(df) == 1
        assert df.iloc[0]["work_id"] == "abc123"
        assert df.iloc[0]["title"] == "Test Title"
    
    def test_appends_to_existing_csv(self, temp_output_dir):
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
    
    def test_preserves_column_order(self, temp_output_dir):
        """Test preserves column order from existing CSV."""
        row1 = {"work_id": "abc123", "title": "Title 1", "creator": "Author 1"}
        row2 = {"creator": "Author 2", "work_id": "def456", "title": "Title 2"}
        
        update_index_csv(temp_output_dir, row1)
        update_index_csv(temp_output_dir, row2)
        
        index_path = os.path.join(temp_output_dir, "index.csv")
        with open(index_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader)
        
        assert headers[0] == "work_id"
        assert headers[1] == "title"
        assert headers[2] == "creator"
    
    def test_thread_safe_updates(self, temp_output_dir):
        """Test multiple updates are thread-safe."""
        import threading
        
        rows = [
            {"work_id": f"id_{i}", "title": f"Title {i}"}
            for i in range(10)
        ]
        
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
    
    def test_creates_directory_if_missing(self, temp_dir):
        """Test creates base directory if it doesn't exist."""
        output_dir = os.path.join(temp_dir, "new_dir")
        row = {"work_id": "abc123", "title": "Test"}
        
        update_index_csv(output_dir, row)
        
        assert os.path.exists(output_dir)
        assert os.path.exists(os.path.join(output_dir, "index.csv"))
    
    def test_handles_missing_columns(self, temp_output_dir):
        """Test handles rows with missing columns."""
        row1 = {"work_id": "abc123", "title": "Title 1", "creator": "Author 1"}
        row2 = {"work_id": "def456", "title": "Title 2"}
        
        update_index_csv(temp_output_dir, row1)
        update_index_csv(temp_output_dir, row2)
        
        index_path = os.path.join(temp_output_dir, "index.csv")
        df = pd.read_csv(index_path)
        
        assert len(df) == 2
        assert pd.isna(df.iloc[1]["creator"])


class TestBuildIndexRow:
    """Tests for build_index_row function."""
    
    def test_builds_complete_row(self, sample_search_result):
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
            status="completed"
        )
        
        assert row["work_id"] == "abc123"
        assert row["entry_id"] == "E0001"
        assert row["title"] == "Test Title"
        assert row["creator"] == "Test Author"
        assert row["selected_provider"] == "Internet Archive"
        assert row["selected_source_id"] == "source123"
        assert row["status"] == "completed"
    
    def test_none_selected(self):
        """Test builds row when no result is selected."""
        row = build_index_row(
            work_id="abc123",
            entry_id="E0001",
            work_dir="/path/to/work",
            title="Test Title",
            creator="Test Author",
            selected=None,
            selected_source_id=None,
            work_json_path="/path/to/work.json"
        )
        
        assert row["work_id"] == "abc123"
        assert row["selected_provider"] is None
        assert row["selected_source_id"] is None
        assert "status" not in row
    
    def test_none_entry_id(self, sample_search_result):
        """Test builds row with None entry_id."""
        row = build_index_row(
            work_id="abc123",
            entry_id=None,
            work_dir="/path/to/work",
            title="Test Title",
            creator=None,
            selected=sample_search_result,
            selected_source_id="source123",
            work_json_path="/path/to/work.json"
        )
        
        assert row["entry_id"] is None
        assert row["creator"] is None
    
    def test_custom_item_url(self, sample_search_result):
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
            item_url=custom_url
        )
        
        assert row["item_url"] == custom_url
    
    def test_fallback_to_selected_item_url(self, sample_search_result):
        """Test falls back to selected.item_url when custom not provided."""
        row = build_index_row(
            work_id="abc123",
            entry_id="E0001",
            work_dir="/path/to/work",
            title="Test Title",
            creator="Test Author",
            selected=sample_search_result,
            selected_source_id="source123",
            work_json_path="/path/to/work.json"
        )
        
        assert row["item_url"] == sample_search_result.item_url


class TestReadIndexCsv:
    """Tests for read_index_csv function."""
    
    def test_reads_existing_csv(self, temp_output_dir):
        """Test reads existing index.csv."""
        row = {"work_id": "abc123", "title": "Test"}
        update_index_csv(temp_output_dir, row)
        
        df = read_index_csv(temp_output_dir)
        
        assert df is not None
        assert len(df) == 1
        assert df.iloc[0]["work_id"] == "abc123"
    
    def test_returns_none_for_missing_file(self, temp_output_dir):
        """Test returns None when index.csv doesn't exist."""
        df = read_index_csv(temp_output_dir)
        
        assert df is None
    
    def test_handles_empty_csv(self, temp_output_dir):
        """Test handles empty CSV file."""
        index_path = os.path.join(temp_output_dir, "index.csv")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write("work_id,title\n")
        
        df = read_index_csv(temp_output_dir)
        
        assert df is not None
        assert len(df) == 0


class TestGetProcessedWorkIds:
    """Tests for get_processed_work_ids function."""
    
    def test_returns_work_ids_from_csv(self, temp_output_dir):
        """Test returns set of work IDs from index.csv."""
        rows = [
            {"work_id": "abc123", "title": "Title 1"},
            {"work_id": "def456", "title": "Title 2"},
            {"work_id": "ghi789", "title": "Title 3"}
        ]
        
        for row in rows:
            update_index_csv(temp_output_dir, row)
        
        work_ids = get_processed_work_ids(temp_output_dir)
        
        assert work_ids == {"abc123", "def456", "ghi789"}
    
    def test_returns_empty_set_for_missing_csv(self, temp_output_dir):
        """Test returns empty set when index.csv doesn't exist."""
        work_ids = get_processed_work_ids(temp_output_dir)
        
        assert work_ids == set()
    
    def test_handles_missing_work_id_column(self, temp_output_dir):
        """Test handles CSV without work_id column."""
        index_path = os.path.join(temp_output_dir, "index.csv")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write("title,creator\n")
            f.write("Test,Author\n")
        
        work_ids = get_processed_work_ids(temp_output_dir)
        
        assert work_ids == set()
    
    def test_skips_na_values(self, temp_output_dir):
        """Test skips NA/None values in work_id column."""
        index_path = os.path.join(temp_output_dir, "index.csv")
        df = pd.DataFrame({
            "work_id": ["abc123", pd.NA, "def456", None],
            "title": ["T1", "T2", "T3", "T4"]
        })
        df.to_csv(index_path, index=False)
        
        work_ids = get_processed_work_ids(temp_output_dir)
        
        assert work_ids == {"abc123", "def456"}
