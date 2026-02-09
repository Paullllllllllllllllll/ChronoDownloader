"""Unit tests for main.unified_csv module."""
from __future__ import annotations

import os

import pandas as pd
import pytest

from main.unified_csv import (
    CREATOR_COL,
    ENTRY_ID_COL,
    LINK_COL,
    STATUS_COL,
    TITLE_COL,
    get_completed_entry_ids,
    get_pending_works,
    get_stats,
    load_works_csv,
    mark_deferred,
    mark_failed,
    mark_success,
)


class TestLoadWorksCsv:
    """Tests for load_works_csv function."""
    
    def test_loads_valid_csv(self, sample_csv_file: str):
        """Test loading a valid CSV file."""
        df = load_works_csv(sample_csv_file)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 5
    
    def test_required_columns_present(self, sample_csv_file: str):
        """Test that required columns are present."""
        df = load_works_csv(sample_csv_file)
        assert ENTRY_ID_COL in df.columns
        assert TITLE_COL in df.columns
    
    def test_creates_missing_status_column(self, temp_dir: str):
        """Test that missing status column is created."""
        csv_path = os.path.join(temp_dir, "no_status.csv")
        pd.DataFrame({
            "entry_id": ["E0001"],
            "short_title": ["Test"]
        }).to_csv(csv_path, index=False)
        
        df = load_works_csv(csv_path)
        assert STATUS_COL in df.columns
    
    def test_creates_missing_link_column(self, temp_dir: str):
        """Test that missing link column is created."""
        csv_path = os.path.join(temp_dir, "no_link.csv")
        pd.DataFrame({
            "entry_id": ["E0001"],
            "short_title": ["Test"]
        }).to_csv(csv_path, index=False)
        
        df = load_works_csv(csv_path)
        assert LINK_COL in df.columns
    
    def test_raises_for_missing_file(self, temp_dir: str):
        """Test that FileNotFoundError is raised for missing file."""
        missing_path = os.path.join(temp_dir, "nonexistent.csv")
        with pytest.raises(FileNotFoundError):
            load_works_csv(missing_path)
    
    def test_raises_for_missing_required_columns(self, temp_dir: str):
        """Test that ValueError is raised for missing required columns."""
        csv_path = os.path.join(temp_dir, "invalid.csv")
        pd.DataFrame({
            "other_column": ["value"]
        }).to_csv(csv_path, index=False)
        
        with pytest.raises(ValueError) as exc_info:
            load_works_csv(csv_path)
        assert "entry_id" in str(exc_info.value).lower()
    
    def test_iiif_only_csv_without_title(self, temp_dir: str):
        """Test loading a CSV with direct_link but no short_title column."""
        csv_path = os.path.join(temp_dir, "iiif_only.csv")
        pd.DataFrame({
            "entry_id": ["E0001", "E0002"],
            "direct_link": [
                "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json",
                "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb123/manifest",
            ],
        }).to_csv(csv_path, index=False)
        
        df = load_works_csv(csv_path)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        assert "direct_link" in df.columns
    
    def test_csv_with_both_title_and_direct_link(self, temp_dir: str):
        """Test loading a CSV with both short_title and direct_link columns."""
        csv_path = os.path.join(temp_dir, "mixed.csv")
        pd.DataFrame({
            "entry_id": ["E0001", "E0002"],
            "short_title": ["Work A", ""],
            "direct_link": [
                "",
                "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json",
            ],
        }).to_csv(csv_path, index=False)
        
        df = load_works_csv(csv_path)
        assert len(df) == 2
    
    def test_raises_for_no_title_no_direct_link(self, temp_dir: str):
        """Test that ValueError is raised when neither title nor direct_link column exists."""
        csv_path = os.path.join(temp_dir, "no_title_no_link.csv")
        pd.DataFrame({
            "entry_id": ["E0001"],
            "main_author": ["Someone"],
        }).to_csv(csv_path, index=False)
        
        with pytest.raises(ValueError) as exc_info:
            load_works_csv(csv_path)
        assert "short_title" in str(exc_info.value)
        assert "direct_link" in str(exc_info.value)
    
    def test_raises_for_missing_entry_id(self, temp_dir: str):
        """Test that ValueError is raised when entry_id column is missing."""
        csv_path = os.path.join(temp_dir, "no_entry_id.csv")
        pd.DataFrame({
            "short_title": ["Test Work"],
            "direct_link": ["https://example.org/manifest.json"],
        }).to_csv(csv_path, index=False)
        
        with pytest.raises(ValueError) as exc_info:
            load_works_csv(csv_path)
        assert "entry_id" in str(exc_info.value)


class TestGetPendingWorks:
    """Tests for get_pending_works function."""
    
    def test_returns_na_status_works(self, sample_csv_data: pd.DataFrame):
        """Test that works with NA status are returned."""
        pending = get_pending_works(sample_csv_data)
        # E0001, E0004, E0005 have NA status
        assert len(pending) >= 3
    
    def test_returns_false_status_works(self, sample_csv_data: pd.DataFrame):
        """Test that works with False status are returned."""
        pending = get_pending_works(sample_csv_data)
        # E0003 has False status
        entry_ids = pending[ENTRY_ID_COL].tolist()
        assert "E0003" in entry_ids
    
    def test_excludes_completed_works(self, sample_csv_data: pd.DataFrame):
        """Test that completed works are excluded."""
        pending = get_pending_works(sample_csv_data)
        entry_ids = pending[ENTRY_ID_COL].tolist()
        # E0002 has True status
        assert "E0002" not in entry_ids
    
    def test_handles_string_status(self, temp_dir: str):
        """Test handling of string status values."""
        csv_path = os.path.join(temp_dir, "string_status.csv")
        pd.DataFrame({
            "entry_id": ["E0001", "E0002", "E0003"],
            "short_title": ["A", "B", "C"],
            "retrievable": ["true", "false", "True"]
        }).to_csv(csv_path, index=False)
        
        df = load_works_csv(csv_path)
        pending = get_pending_works(df)
        
        entry_ids = pending[ENTRY_ID_COL].tolist()
        assert "E0002" in entry_ids  # "false" is pending
        assert "E0001" not in entry_ids  # "true" is completed
        assert "E0003" not in entry_ids  # "True" is completed


class TestGetCompletedEntryIds:
    """Tests for get_completed_entry_ids function."""
    
    def test_returns_completed_ids(self, sample_csv_data: pd.DataFrame):
        """Test that completed entry IDs are returned."""
        completed = get_completed_entry_ids(sample_csv_data)
        assert "E0002" in completed
    
    def test_excludes_pending_ids(self, sample_csv_data: pd.DataFrame):
        """Test that pending IDs are excluded."""
        completed = get_completed_entry_ids(sample_csv_data)
        assert "E0001" not in completed
        assert "E0004" not in completed
    
    def test_returns_set(self, sample_csv_data: pd.DataFrame):
        """Test that return type is a set."""
        completed = get_completed_entry_ids(sample_csv_data)
        assert isinstance(completed, set)


class TestMarkSuccess:
    """Tests for mark_success function."""
    
    def test_marks_entry_as_success(self, sample_csv_file: str):
        """Test marking an entry as successful."""
        result = mark_success(sample_csv_file, "E0001", "https://example.com/item")
        assert result is True
        
        # Verify the change
        df = pd.read_csv(sample_csv_file)
        row = df[df[ENTRY_ID_COL] == "E0001"].iloc[0]
        assert row[STATUS_COL] == True
        assert row[LINK_COL] == "https://example.com/item"
    
    def test_adds_provider_column(self, sample_csv_file: str):
        """Test that provider column is added."""
        mark_success(sample_csv_file, "E0001", "https://example.com", provider="Test Provider")
        
        df = pd.read_csv(sample_csv_file)
        assert "download_provider" in df.columns
        row = df[df[ENTRY_ID_COL] == "E0001"].iloc[0]
        assert row["download_provider"] == "Test Provider"
    
    def test_adds_timestamp(self, sample_csv_file: str):
        """Test that timestamp is added."""
        mark_success(sample_csv_file, "E0001", "https://example.com")
        
        df = pd.read_csv(sample_csv_file)
        assert "download_timestamp" in df.columns
        row = df[df[ENTRY_ID_COL] == "E0001"].iloc[0]
        assert pd.notna(row["download_timestamp"])
    
    def test_returns_false_for_missing_entry(self, sample_csv_file: str):
        """Test that False is returned for missing entry."""
        result = mark_success(sample_csv_file, "NONEXISTENT", "https://example.com")
        assert result is False


class TestMarkFailed:
    """Tests for mark_failed function."""
    
    def test_marks_entry_as_failed(self, sample_csv_file: str):
        """Test marking an entry as failed."""
        result = mark_failed(sample_csv_file, "E0001")
        assert result is True
        
        # Verify the change
        df = pd.read_csv(sample_csv_file)
        row = df[df[ENTRY_ID_COL] == "E0001"].iloc[0]
        assert row[STATUS_COL] == False
    
    def test_adds_timestamp(self, sample_csv_file: str):
        """Test that timestamp is added."""
        mark_failed(sample_csv_file, "E0001")
        
        df = pd.read_csv(sample_csv_file)
        assert "download_timestamp" in df.columns
    
    def test_returns_false_for_missing_entry(self, sample_csv_file: str):
        """Test that False is returned for missing entry."""
        result = mark_failed(sample_csv_file, "NONEXISTENT")
        assert result is False


class TestMarkDeferred:
    """Tests for mark_deferred function."""
    
    def test_marks_entry_as_deferred(self, sample_csv_file: str):
        """Test marking an entry as deferred."""
        # First mark as failed
        mark_failed(sample_csv_file, "E0001")
        
        # Then mark as deferred
        result = mark_deferred(sample_csv_file, "E0001")
        assert result is True
        
        # Verify status is NA (pending for retry)
        df = pd.read_csv(sample_csv_file)
        row = df[df[ENTRY_ID_COL] == "E0001"].iloc[0]
        assert pd.isna(row[STATUS_COL])
    
    def test_returns_false_for_missing_entry(self, sample_csv_file: str):
        """Test that False is returned for missing entry."""
        result = mark_deferred(sample_csv_file, "NONEXISTENT")
        assert result is False


class TestGetStats:
    """Tests for get_stats function."""
    
    def test_returns_correct_counts(self, sample_csv_file: str):
        """Test that correct counts are returned."""
        stats = get_stats(sample_csv_file)
        
        assert stats["total"] == 5
        assert stats["completed"] == 1  # E0002
        assert stats["failed"] == 1  # E0003
        assert stats["pending"] == 3  # E0001, E0004, E0005
    
    def test_returns_dict(self, sample_csv_file: str):
        """Test that return type is a dict."""
        stats = get_stats(sample_csv_file)
        assert isinstance(stats, dict)
        assert "total" in stats
        assert "completed" in stats
        assert "failed" in stats
        assert "pending" in stats
    
    def test_handles_missing_file(self, temp_dir: str):
        """Test handling of missing file."""
        missing_path = os.path.join(temp_dir, "nonexistent.csv")
        stats = get_stats(missing_path)
        
        assert stats["total"] == 0
        assert stats["completed"] == 0
        assert stats["failed"] == 0
        assert stats["pending"] == 0
    
    def test_handles_missing_status_column(self, temp_dir: str):
        """Test handling of CSV without status column (all works are pending)."""
        # Create CSV without retrievable column (like essential_monographies CSVs)
        csv_path = os.path.join(temp_dir, "no_status.csv")
        df = pd.DataFrame({
            "entry_id": ["E0001", "E0002", "E0003"],
            "short_title": ["Book One", "Book Two", "Book Three"],
            "main_author": ["Author A", "Author B", "Author C"],
        })
        df.to_csv(csv_path, index=False)
        
        stats = get_stats(csv_path)
        
        assert stats["total"] == 3
        assert stats["completed"] == 0
        assert stats["failed"] == 0
        assert stats["pending"] == 3  # All pending when no status column


class TestColumnConstants:
    """Tests for column name constants."""
    
    def test_entry_id_col(self):
        """Test ENTRY_ID_COL constant."""
        assert ENTRY_ID_COL == "entry_id"
    
    def test_title_col(self):
        """Test TITLE_COL constant."""
        assert TITLE_COL == "short_title"
    
    def test_creator_col(self):
        """Test CREATOR_COL constant."""
        assert CREATOR_COL == "main_author"
    
    def test_status_col(self):
        """Test STATUS_COL constant."""
        assert STATUS_COL == "retrievable"
    
    def test_link_col(self):
        """Test LINK_COL constant."""
        assert LINK_COL == "link"
