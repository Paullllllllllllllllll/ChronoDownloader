"""Unit tests for api.core.budget module."""

from __future__ import annotations

from unittest.mock import patch

from api.core.budget import DownloadBudget, budget_exhausted, get_budget
from api.core.config import DEFAULT_ON_EXCEED


class TestDownloadBudget:
    """Tests for DownloadBudget class."""

    def test_initialization(self, fresh_budget: DownloadBudget) -> None:
        """Test that budget initializes with zero counters."""
        assert fresh_budget.total_images_bytes == 0
        assert fresh_budget.total_pdfs_bytes == 0
        assert fresh_budget.total_metadata_bytes == 0
        assert fresh_budget.per_work == {}
        assert fresh_budget.exhausted() is False

    def test_gb_to_bytes(self, fresh_budget: DownloadBudget) -> None:
        """Test GB to bytes conversion."""
        assert fresh_budget._gb_to_bytes(1) == 1024 * 1024 * 1024
        assert fresh_budget._gb_to_bytes(0.5) == 512 * 1024 * 1024
        assert fresh_budget._gb_to_bytes(0) is None
        assert fresh_budget._gb_to_bytes(-1) is None
        assert fresh_budget._gb_to_bytes("invalid") is None

    def test_mb_to_bytes(self, fresh_budget: DownloadBudget) -> None:
        """Test MB to bytes conversion."""
        assert fresh_budget._mb_to_bytes(1) == 1024 * 1024
        assert fresh_budget._mb_to_bytes(0.5) == 512 * 1024
        assert fresh_budget._mb_to_bytes(0) is None
        assert fresh_budget._mb_to_bytes(-1) is None

    def test_allow_content_small_amount(self, fresh_budget: DownloadBudget) -> None:
        """Test that small amounts are always allowed."""
        with patch("api.core.budget.get_download_limits", return_value={}):
            assert fresh_budget.allow_content("images", "work_1", 1024) is True
            assert fresh_budget.allow_content("pdfs", "work_1", 1024) is True
            assert fresh_budget.allow_content("metadata", "work_1", 1024) is True

    def test_allow_content_zero_bytes(self, fresh_budget: DownloadBudget) -> None:
        """Test that zero/negative bytes are always allowed."""
        assert fresh_budget.allow_content("images", "work_1", 0) is True
        assert fresh_budget.allow_content("images", "work_1", -1) is True
        assert fresh_budget.allow_content("images", "work_1", None) is True

    def test_allow_content_unknown_type(self, fresh_budget: DownloadBudget) -> None:
        """Test that unknown content types are allowed with warning."""
        assert fresh_budget.allow_content("unknown_type", "work_1", 1024) is True

    def test_record_download(self, fresh_budget: DownloadBudget) -> None:
        """Test recording a download."""
        fresh_budget.record_download("images", "work_1", 1000)
        assert fresh_budget.total_images_bytes == 1000
        assert fresh_budget.per_work["work_1"]["images"] == 1000

    def test_record_download_accumulates(self, fresh_budget: DownloadBudget) -> None:
        """Test that multiple downloads accumulate."""
        fresh_budget.record_download("pdfs", "work_1", 1000)
        fresh_budget.record_download("pdfs", "work_1", 2000)
        assert fresh_budget.total_pdfs_bytes == 3000
        assert fresh_budget.per_work["work_1"]["pdfs"] == 3000

    def test_record_download_multiple_works(self, fresh_budget: DownloadBudget) -> None:
        """Test recording downloads for multiple works."""
        fresh_budget.record_download("images", "work_1", 1000)
        fresh_budget.record_download("images", "work_2", 2000)
        assert fresh_budget.total_images_bytes == 3000
        assert fresh_budget.per_work["work_1"]["images"] == 1000
        assert fresh_budget.per_work["work_2"]["images"] == 2000

    def test_record_download_unknown_type(self, fresh_budget: DownloadBudget) -> None:
        """Test that unknown content type is ignored."""
        fresh_budget.record_download("unknown", "work_1", 1000)
        # Should not raise, but also should not record
        assert "unknown" not in fresh_budget.per_work.get("work_1", {})

    def test_allow_new_file_not_exhausted(self, fresh_budget: DownloadBudget) -> None:
        """Test allow_new_file when not exhausted."""
        assert fresh_budget.allow_new_file("internet_archive", "work_1") is True

    def test_allow_new_file_when_exhausted(self, fresh_budget: DownloadBudget) -> None:
        """Test allow_new_file when exhausted."""
        fresh_budget._exhausted = True
        assert fresh_budget.allow_new_file("internet_archive", "work_1") is False

    def test_allow_new_file_reads_the_flag_under_the_lock(
        self, fresh_budget: DownloadBudget
    ) -> None:
        """The flag is read through ``exhausted()``, not off the attribute.

        ``allow_new_file`` used to read ``_exhausted`` unlocked while every
        other reader took the lock for the same field.
        """
        with patch.object(
            fresh_budget, "exhausted", wraps=fresh_budget.exhausted
        ) as locked_read:
            assert fresh_budget.allow_new_file("internet_archive", "work_1") is True
            locked_read.assert_called_once()

    def test_add_bytes_success(self, fresh_budget: DownloadBudget) -> None:
        """Test adding bytes successfully."""
        with patch("api.core.budget.get_download_limits", return_value={}):
            result = fresh_budget.add_bytes("internet_archive", "work_1", 1000)
            assert result is True
            assert fresh_budget.total_images_bytes == 1000

    def test_refund_returns_bytes(self, fresh_budget: DownloadBudget) -> None:
        """Refund subtracts previously booked bytes globally and per-work."""
        with patch("api.core.budget.get_download_limits", return_value={}):
            fresh_budget.add_bytes("ia", "work_1", 1000, content_type="pdfs")
            assert fresh_budget.total_pdfs_bytes == 1000
            fresh_budget.refund("pdfs", "work_1", 400)
            assert fresh_budget.total_pdfs_bytes == 600
            assert fresh_budget.per_work["work_1"]["pdfs"] == 600

    def test_refund_clamps_at_zero(self, fresh_budget: DownloadBudget) -> None:
        """Refunding more than was booked never drives a counter negative."""
        with patch("api.core.budget.get_download_limits", return_value={}):
            fresh_budget.add_bytes("ia", "work_1", 100, content_type="images")
            fresh_budget.refund("images", "work_1", 999)
            assert fresh_budget.total_images_bytes == 0
            assert fresh_budget.per_work["work_1"]["images"] == 0

    def test_refund_ignores_nonpositive(self, fresh_budget: DownloadBudget) -> None:
        """A zero/negative refund is a no-op (covers the discarded 0-byte case)."""
        with patch("api.core.budget.get_download_limits", return_value={}):
            fresh_budget.add_bytes("ia", "work_1", 500, content_type="images")
            fresh_budget.refund("images", "work_1", 0)
            fresh_budget.refund("images", "work_1", -5)
            assert fresh_budget.total_images_bytes == 500

    def test_refund_unknown_work_is_safe(self, fresh_budget: DownloadBudget) -> None:
        """Refunding a work with no per-work record does not raise or create one."""
        fresh_budget.refund("images", "never_seen", 100)
        assert "never_seen" not in fresh_budget.per_work

    def test_add_file_no_op(self, fresh_budget: DownloadBudget) -> None:
        """Test that add_file is a no-op."""
        # Should not raise
        fresh_budget.add_file("internet_archive", "work_1")

    def test_exhausted_state(self, fresh_budget: DownloadBudget) -> None:
        """Test exhausted state management."""
        assert fresh_budget.exhausted() is False
        fresh_budget._exhausted = True
        assert fresh_budget.exhausted() is True

    def test_policy_default(self, fresh_budget: DownloadBudget) -> None:
        """An absent on_exceed key resolves to the shared default, 'stop'."""
        with patch("api.core.budget.get_download_limits", return_value={}):
            assert fresh_budget._policy() == DEFAULT_ON_EXCEED == "stop"

    def test_policy_unrecognized_value_falls_back_to_default(
        self, fresh_budget: DownloadBudget
    ) -> None:
        """A typo'd policy must not silently become the permissive one."""
        with patch(
            "api.core.budget.get_download_limits",
            return_value={"on_exceed": "halt"},
        ):
            assert fresh_budget._policy() == DEFAULT_ON_EXCEED

    def test_policy_skip_is_honored(self, fresh_budget: DownloadBudget) -> None:
        """An explicit 'skip' still overrides the stricter default."""
        with patch(
            "api.core.budget.get_download_limits", return_value={"on_exceed": "skip"}
        ):
            assert fresh_budget._policy() == "skip"

    def test_policy_stop(self, fresh_budget: DownloadBudget) -> None:
        """Test 'stop' policy."""
        with patch(
            "api.core.budget.get_download_limits", return_value={"on_exceed": "stop"}
        ):
            assert fresh_budget._policy() == "stop"


class TestBudgetLimits:
    """Tests for budget limit enforcement."""

    def test_global_limit_exceeded(self, fresh_budget: DownloadBudget) -> None:
        """Test that global limit is enforced."""
        limits = {
            "total": {"images_gb": 0.001},  # ~1MB
            "on_exceed": "skip",
        }
        with patch("api.core.budget.get_download_limits", return_value=limits):
            # First should be allowed
            assert fresh_budget.allow_content("images", "work_1", 500_000) is True
            fresh_budget.record_download("images", "work_1", 500_000)

            # Second should exceed limit
            assert fresh_budget.allow_content("images", "work_1", 600_000) is False

    def test_per_work_limit_exceeded(self, fresh_budget: DownloadBudget) -> None:
        """Test that per-work limit is enforced."""
        limits = {
            "per_work": {"images_gb": 0.001},  # ~1MB
            "on_exceed": "skip",
        }
        with patch("api.core.budget.get_download_limits", return_value=limits):
            # First work should be allowed
            fresh_budget.record_download("images", "work_1", 500_000)
            assert fresh_budget.allow_content("images", "work_1", 600_000) is False

            # Different work should still be allowed
            assert fresh_budget.allow_content("images", "work_2", 600_000) is True

    def test_stop_policy_sets_exhausted(self, fresh_budget: DownloadBudget) -> None:
        """Test that 'stop' policy sets exhausted flag."""
        limits = {"total": {"images_gb": 0.001}, "on_exceed": "stop"}
        with patch("api.core.budget.get_download_limits", return_value=limits):
            fresh_budget.record_download("images", "work_1", 1_000_000)
            fresh_budget.allow_content("images", "work_1", 500_000)
            assert fresh_budget.exhausted() is True

    def test_per_work_stop_policy_does_not_set_exhausted(
        self, fresh_budget: DownloadBudget
    ) -> None:
        """A per-work 'stop' limit blocks only that work; the run continues.

        Contrasts with test_stop_policy_sets_exhausted above, which covers the
        GLOBAL limit and must still set the exhausted flag: one oversized work
        must not abort every remaining work in the run.
        """
        limits = {
            "per_work": {"images_gb": 0.001},  # ~1MB
            "on_exceed": "stop",
        }
        with patch("api.core.budget.get_download_limits", return_value=limits):
            fresh_budget.record_download("images", "work_1", 1_000_000)

            assert fresh_budget.allow_content("images", "work_1", 500_000) is False
            assert (
                fresh_budget.allow_bytes("ia", "work_1", 500_000, content_type="images")
                is False
            )
            assert fresh_budget.exhausted() is False

            # A different work is unaffected by work_1's per-work cap.
            assert fresh_budget.allow_content("images", "work_2", 500_000) is True


class TestGlobalBudget:
    """Tests for global budget functions."""

    def test_get_budget_returns_singleton(self) -> None:
        """Test that get_budget returns the same instance."""
        budget1 = get_budget()
        budget2 = get_budget()
        assert budget1 is budget2

    def test_budget_exhausted_function(self) -> None:
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

    def test_concurrent_record_download(self, fresh_budget: DownloadBudget) -> None:
        """Test concurrent record_download calls."""
        import threading

        def record_many() -> None:
            for _ in range(100):
                fresh_budget.record_download("images", "work_1", 100)

        threads = [threading.Thread(target=record_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have recorded all downloads
        assert fresh_budget.total_images_bytes == 100 * 100 * 5
