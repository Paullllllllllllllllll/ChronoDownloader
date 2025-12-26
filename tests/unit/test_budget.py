"""Unit tests for api.core.budget module."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from api.core.budget import DownloadBudget, budget_exhausted, get_budget


class TestDownloadBudget:
    """Tests for DownloadBudget class."""
    
    def test_initialization(self, fresh_budget: DownloadBudget):
        """Test that budget initializes with zero counters."""
        assert fresh_budget.total_images_bytes == 0
        assert fresh_budget.total_pdfs_bytes == 0
        assert fresh_budget.total_metadata_bytes == 0
        assert fresh_budget.per_work == {}
        assert fresh_budget.exhausted() is False
    
    def test_gb_to_bytes(self, fresh_budget: DownloadBudget):
        """Test GB to bytes conversion."""
        assert fresh_budget._gb_to_bytes(1) == 1024 * 1024 * 1024
        assert fresh_budget._gb_to_bytes(0.5) == 512 * 1024 * 1024
        assert fresh_budget._gb_to_bytes(0) is None
        assert fresh_budget._gb_to_bytes(-1) is None
        assert fresh_budget._gb_to_bytes("invalid") is None
    
    def test_mb_to_bytes(self, fresh_budget: DownloadBudget):
        """Test MB to bytes conversion."""
        assert fresh_budget._mb_to_bytes(1) == 1024 * 1024
        assert fresh_budget._mb_to_bytes(0.5) == 512 * 1024
        assert fresh_budget._mb_to_bytes(0) is None
        assert fresh_budget._mb_to_bytes(-1) is None
    
    def test_allow_content_small_amount(self, fresh_budget: DownloadBudget):
        """Test that small amounts are always allowed."""
        with patch("api.core.budget.get_download_limits", return_value={}):
            assert fresh_budget.allow_content("images", "work_1", 1024) is True
            assert fresh_budget.allow_content("pdfs", "work_1", 1024) is True
            assert fresh_budget.allow_content("metadata", "work_1", 1024) is True
    
    def test_allow_content_zero_bytes(self, fresh_budget: DownloadBudget):
        """Test that zero/negative bytes are always allowed."""
        assert fresh_budget.allow_content("images", "work_1", 0) is True
        assert fresh_budget.allow_content("images", "work_1", -1) is True
        assert fresh_budget.allow_content("images", "work_1", None) is True
    
    def test_allow_content_unknown_type(self, fresh_budget: DownloadBudget):
        """Test that unknown content types are allowed with warning."""
        assert fresh_budget.allow_content("unknown_type", "work_1", 1024) is True
    
    def test_record_download(self, fresh_budget: DownloadBudget):
        """Test recording a download."""
        fresh_budget.record_download("images", "work_1", 1000)
        assert fresh_budget.total_images_bytes == 1000
        assert fresh_budget.per_work["work_1"]["images"] == 1000
    
    def test_record_download_accumulates(self, fresh_budget: DownloadBudget):
        """Test that multiple downloads accumulate."""
        fresh_budget.record_download("pdfs", "work_1", 1000)
        fresh_budget.record_download("pdfs", "work_1", 2000)
        assert fresh_budget.total_pdfs_bytes == 3000
        assert fresh_budget.per_work["work_1"]["pdfs"] == 3000
    
    def test_record_download_multiple_works(self, fresh_budget: DownloadBudget):
        """Test recording downloads for multiple works."""
        fresh_budget.record_download("images", "work_1", 1000)
        fresh_budget.record_download("images", "work_2", 2000)
        assert fresh_budget.total_images_bytes == 3000
        assert fresh_budget.per_work["work_1"]["images"] == 1000
        assert fresh_budget.per_work["work_2"]["images"] == 2000
    
    def test_record_download_unknown_type(self, fresh_budget: DownloadBudget):
        """Test that unknown content type is ignored."""
        fresh_budget.record_download("unknown", "work_1", 1000)
        # Should not raise, but also should not record
        assert "unknown" not in fresh_budget.per_work.get("work_1", {})
    
    def test_allow_new_file_not_exhausted(self, fresh_budget: DownloadBudget):
        """Test allow_new_file when not exhausted."""
        assert fresh_budget.allow_new_file("internet_archive", "work_1") is True
    
    def test_allow_new_file_when_exhausted(self, fresh_budget: DownloadBudget):
        """Test allow_new_file when exhausted."""
        fresh_budget._exhausted = True
        assert fresh_budget.allow_new_file("internet_archive", "work_1") is False
    
    def test_add_bytes_success(self, fresh_budget: DownloadBudget):
        """Test adding bytes successfully."""
        with patch("api.core.budget.get_download_limits", return_value={}):
            result = fresh_budget.add_bytes("internet_archive", "work_1", 1000)
            assert result is True
            assert fresh_budget.total_images_bytes == 1000
    
    def test_add_file_no_op(self, fresh_budget: DownloadBudget):
        """Test that add_file is a no-op."""
        # Should not raise
        fresh_budget.add_file("internet_archive", "work_1")
    
    def test_exhausted_state(self, fresh_budget: DownloadBudget):
        """Test exhausted state management."""
        assert fresh_budget.exhausted() is False
        fresh_budget._exhausted = True
        assert fresh_budget.exhausted() is True
    
    def test_policy_default(self, fresh_budget: DownloadBudget):
        """Test default policy is 'skip'."""
        with patch("api.core.budget.get_download_limits", return_value={}):
            assert fresh_budget._policy() == "skip"
    
    def test_policy_stop(self, fresh_budget: DownloadBudget):
        """Test 'stop' policy."""
        with patch("api.core.budget.get_download_limits", return_value={"on_exceed": "stop"}):
            assert fresh_budget._policy() == "stop"


class TestBudgetLimits:
    """Tests for budget limit enforcement."""
    
    def test_global_limit_exceeded(self, fresh_budget: DownloadBudget):
        """Test that global limit is enforced."""
        limits = {
            "total": {"images_gb": 0.001},  # ~1MB
            "on_exceed": "skip"
        }
        with patch("api.core.budget.get_download_limits", return_value=limits):
            # First should be allowed
            assert fresh_budget.allow_content("images", "work_1", 500_000) is True
            fresh_budget.record_download("images", "work_1", 500_000)
            
            # Second should exceed limit
            assert fresh_budget.allow_content("images", "work_1", 600_000) is False
    
    def test_per_work_limit_exceeded(self, fresh_budget: DownloadBudget):
        """Test that per-work limit is enforced."""
        limits = {
            "per_work": {"images_gb": 0.001},  # ~1MB
            "on_exceed": "skip"
        }
        with patch("api.core.budget.get_download_limits", return_value=limits):
            # First work should be allowed
            fresh_budget.record_download("images", "work_1", 500_000)
            assert fresh_budget.allow_content("images", "work_1", 600_000) is False
            
            # Different work should still be allowed
            assert fresh_budget.allow_content("images", "work_2", 600_000) is True
    
    def test_stop_policy_sets_exhausted(self, fresh_budget: DownloadBudget):
        """Test that 'stop' policy sets exhausted flag."""
        limits = {
            "total": {"images_gb": 0.001},
            "on_exceed": "stop"
        }
        with patch("api.core.budget.get_download_limits", return_value=limits):
            fresh_budget.record_download("images", "work_1", 1_000_000)
            fresh_budget.allow_content("images", "work_1", 500_000)
            assert fresh_budget.exhausted() is True


class TestGlobalBudget:
    """Tests for global budget functions."""
    
    def test_get_budget_returns_singleton(self):
        """Test that get_budget returns the same instance."""
        budget1 = get_budget()
        budget2 = get_budget()
        assert budget1 is budget2
    
    def test_budget_exhausted_function(self):
        """Test the budget_exhausted function."""
        budget = get_budget()
        original_state = budget._exhausted
        
        budget._exhausted = False
        assert budget_exhausted() is False
        
        budget._exhausted = True
        assert budget_exhausted() is True
        
        # Restore original state
        budget._exhausted = original_state


class TestBudgetThreadSafety:
    """Tests for budget thread safety."""
    
    def test_concurrent_record_download(self, fresh_budget: DownloadBudget):
        """Test concurrent record_download calls."""
        import threading
        
        def record_many():
            for _ in range(100):
                fresh_budget.record_download("images", "work_1", 100)
        
        threads = [threading.Thread(target=record_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Should have recorded all downloads
        assert fresh_budget.total_images_bytes == 100 * 100 * 5
