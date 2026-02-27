"""Tests for main.background_scheduler module — background retry scheduling."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.model import QuotaDeferredException, SearchResult
from main.background_scheduler import BackgroundRetryScheduler


@pytest.fixture(autouse=True)
def reset_scheduler_singleton():
    """Reset the BackgroundRetryScheduler singleton."""
    BackgroundRetryScheduler._instance = None
    yield
    # Stop any running schedulers
    if BackgroundRetryScheduler._instance is not None:
        instance = BackgroundRetryScheduler._instance
        if hasattr(instance, "_stop_event"):
            instance._stop_event.set()
        if hasattr(instance, "_thread") and instance._thread and instance._thread.is_alive():
            instance._thread.join(timeout=5)
    BackgroundRetryScheduler._instance = None


# ============================================================================
# Singleton pattern
# ============================================================================

class TestBackgroundSchedulerSingleton:
    """Tests for singleton pattern."""

    @patch("main.background_scheduler.get_config", return_value={})
    def test_singleton_returns_same_instance(self, mock_cfg):
        s1 = BackgroundRetryScheduler()
        s2 = BackgroundRetryScheduler()
        assert s1 is s2

    @patch("main.background_scheduler.get_config", return_value={})
    def test_initialized_once(self, mock_cfg):
        s = BackgroundRetryScheduler()
        assert s._initialized is True


# ============================================================================
# Configuration
# ============================================================================

class TestBackgroundSchedulerConfig:
    """Tests for scheduler configuration."""

    @patch("main.background_scheduler.get_config", return_value={
        "deferred": {
            "check_interval_minutes": 5,
            "background_enabled": True,
        }
    })
    def test_reads_check_interval(self, mock_cfg):
        s = BackgroundRetryScheduler()
        assert s._check_interval_s == 5 * 60

    @patch("main.background_scheduler.get_config", return_value={
        "deferred": {"background_enabled": False}
    })
    def test_disabled_by_config(self, mock_cfg):
        s = BackgroundRetryScheduler()
        assert s._enabled is False


# ============================================================================
# set_provider_download_fn
# ============================================================================

class TestSetProviderDownloadFn:
    """Tests for registering download functions."""

    @patch("main.background_scheduler.get_config", return_value={})
    def test_registers_download_function(self, mock_cfg):
        s = BackgroundRetryScheduler()
        fn = MagicMock()
        s.set_provider_download_fn("ia", fn)
        assert s._provider_download_fns["ia"] is fn


# ============================================================================
# set_callbacks
# ============================================================================

class TestSetCallbacks:
    """Tests for callback registration."""

    @patch("main.background_scheduler.get_config", return_value={})
    def test_sets_callbacks(self, mock_cfg):
        s = BackgroundRetryScheduler()
        on_success = MagicMock()
        on_failure = MagicMock()
        s.set_callbacks(on_success=on_success, on_failure=on_failure)
        assert s._on_retry_success is on_success
        assert s._on_retry_failure is on_failure


# ============================================================================
# start / stop / pause / resume
# ============================================================================

class TestBackgroundSchedulerLifecycle:
    """Tests for scheduler lifecycle management."""

    @patch("main.background_scheduler.get_config", return_value={
        "deferred": {"background_enabled": False}
    })
    def test_start_returns_false_when_disabled(self, mock_cfg):
        s = BackgroundRetryScheduler()
        assert s.start() is False

    @patch("main.background_scheduler.get_deferred_queue")
    @patch("main.background_scheduler.get_quota_manager")
    @patch("main.background_scheduler.get_config", return_value={
        "deferred": {"background_enabled": True, "check_interval_minutes": 0.01}
    })
    def test_start_and_stop(self, mock_cfg, mock_qm, mock_dq):
        s = BackgroundRetryScheduler()
        assert s.start() is True
        assert s.is_running() is True
        s.stop(wait=True, timeout=5)
        assert s.is_running() is False

    @patch("main.background_scheduler.get_deferred_queue")
    @patch("main.background_scheduler.get_quota_manager")
    @patch("main.background_scheduler.get_config", return_value={
        "deferred": {"background_enabled": True, "check_interval_minutes": 0.01}
    })
    def test_start_returns_false_when_already_running(self, mock_cfg, mock_qm, mock_dq):
        s = BackgroundRetryScheduler()
        assert s.start() is True
        assert s.start() is False
        s.stop(wait=True, timeout=5)

    @patch("main.background_scheduler.get_config", return_value={})
    def test_stop_when_not_started(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s.stop()  # Should not raise

    @patch("main.background_scheduler.get_deferred_queue")
    @patch("main.background_scheduler.get_quota_manager")
    @patch("main.background_scheduler.get_config", return_value={
        "deferred": {"background_enabled": True, "check_interval_minutes": 0.01}
    })
    def test_pause_and_resume(self, mock_cfg, mock_qm, mock_dq):
        s = BackgroundRetryScheduler()
        s.start()
        s.pause()
        assert s.is_paused() is True
        s.resume()
        assert s.is_paused() is False
        s.stop(wait=True, timeout=5)


# ============================================================================
# get_stats
# ============================================================================

class TestBackgroundSchedulerStats:
    """Tests for statistics tracking."""

    @patch("main.background_scheduler.get_config", return_value={})
    def test_initial_stats(self, mock_cfg):
        s = BackgroundRetryScheduler()
        stats = s.get_stats()
        assert stats["checks"] == 0
        assert stats["retries_attempted"] == 0
        assert stats["retries_succeeded"] == 0
        assert stats["retries_failed"] == 0


# ============================================================================
# _reconstruct_search_result
# ============================================================================

class TestReconstructSearchResult:
    """Tests for SearchResult reconstruction from DeferredItem."""

    @patch("main.background_scheduler.get_config", return_value={})
    def test_reconstructs_from_deferred_item(self, mock_cfg):
        s = BackgroundRetryScheduler()
        item = MagicMock()
        item.title = "Test Book"
        item.creator = "Author"
        item.source_id = "id123"
        item.provider_name = "Internet Archive"
        item.provider_key = "ia"
        item.item_url = "https://example.org/item"
        item.raw_data = {"identifier": "id123"}

        result = s._reconstruct_search_result(item)
        assert isinstance(result, SearchResult)
        assert result.title == "Test Book"
        assert result.creators == ["Author"]
        assert result.source_id == "id123"

    @patch("main.background_scheduler.get_config", return_value={})
    def test_handles_no_creator(self, mock_cfg):
        s = BackgroundRetryScheduler()
        item = MagicMock()
        item.title = "Test"
        item.creator = None
        item.source_id = "id"
        item.provider_name = "IA"
        item.provider_key = "ia"
        item.item_url = None
        item.raw_data = {}

        result = s._reconstruct_search_result(item)
        assert result.creators == []

    @patch("main.background_scheduler.get_config", return_value={})
    def test_returns_none_on_error(self, mock_cfg):
        s = BackgroundRetryScheduler()
        item = MagicMock()
        item.title = None
        item.creator = None
        item.source_id = None
        item.provider_name = None
        item.provider_key = None
        item.item_url = None
        item.raw_data = None

        result = s._reconstruct_search_result(item)
        assert isinstance(result, SearchResult)


# ============================================================================
# _check_and_retry
# ============================================================================

class TestCheckAndRetry:
    """Tests for the check-and-retry cycle."""

    @patch("main.background_scheduler.get_config", return_value={})
    def test_increments_check_count(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._queue.get_ready.return_value = []
        s._check_and_retry()
        assert s.get_stats()["checks"] == 1

    @patch("main.background_scheduler.get_config", return_value={})
    def test_skips_when_no_queue(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s._queue = None
        s._check_and_retry()
        assert s.get_stats()["checks"] == 1

    @patch("main.background_scheduler.get_config", return_value={})
    def test_retries_ready_items(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        item = MagicMock()
        item.title = "Test"
        item.provider_key = "ia"
        s._queue.get_ready.return_value = [item]
        s._stop_event = threading.Event()

        with patch.object(s, "_retry_item", return_value=True):
            s._check_and_retry()
        assert s.get_stats()["checks"] == 1


# ============================================================================
# _retry_item
# ============================================================================

class TestRetryItem:
    """Tests for individual item retry logic."""

    @patch("main.background_scheduler.get_config", return_value={})
    def test_succeeds_with_download(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (True, 0)

        download_fn = MagicMock(return_value=True)
        s._provider_download_fns = {"ia": download_fn}

        item = MagicMock()
        item.title = "Test"
        item.creator = "Author"
        item.source_id = "id"
        item.provider_key = "ia"
        item.provider_name = "Internet Archive"
        item.item_url = "https://example.org"
        item.entry_id = "E001"
        item.work_dir = "/out"
        item.raw_data = {}
        item.id = "item1"

        result = s._retry_item(item)
        assert result is True
        s._queue.mark_completed.assert_called_once()

    @patch("main.background_scheduler.get_config", return_value={})
    def test_skips_when_no_download_fn(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s._provider_download_fns = {}

        item = MagicMock()
        item.title = "Test"
        item.provider_key = "unknown"

        result = s._retry_item(item)
        assert result is False

    @patch("main.background_scheduler.get_config", return_value={})
    def test_skips_when_quota_not_ready(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (False, None)
        s._provider_download_fns = {"ia": MagicMock()}

        item = MagicMock()
        item.title = "Test"
        item.provider_key = "ia"
        item.reset_time = None

        result = s._retry_item(item)
        assert result is False

    @patch("main.background_scheduler.get_config", return_value={})
    def test_handles_quota_deferred_exception(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (True, 0)

        download_fn = MagicMock(side_effect=QuotaDeferredException("ia"))
        s._provider_download_fns = {"ia": download_fn}

        item = MagicMock()
        item.title = "Test"
        item.creator = None
        item.source_id = "id"
        item.provider_key = "ia"
        item.provider_name = "IA"
        item.item_url = None
        item.entry_id = None
        item.work_dir = "/out"
        item.raw_data = {}
        item.id = "item1"

        result = s._retry_item(item)
        assert result is False
        assert s.get_stats()["retries_redeferred"] == 1

    @patch("main.background_scheduler.get_config", return_value={})
    def test_handles_download_failure(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._queue.mark_retrying.return_value = False
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (True, 0)

        download_fn = MagicMock(return_value=False)
        s._provider_download_fns = {"ia": download_fn}

        on_failure = MagicMock()
        s._on_retry_failure = on_failure

        item = MagicMock()
        item.title = "Test"
        item.creator = None
        item.source_id = "id"
        item.provider_key = "ia"
        item.provider_name = "IA"
        item.item_url = None
        item.entry_id = None
        item.work_dir = "/out"
        item.raw_data = {}
        item.id = "item1"

        result = s._retry_item(item)
        assert result is False
        on_failure.assert_called_once()

    @patch("main.background_scheduler.get_config", return_value={})
    def test_calls_success_callback(self, mock_cfg):
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (True, 0)

        download_fn = MagicMock(return_value=True)
        s._provider_download_fns = {"ia": download_fn}
        on_success = MagicMock()
        s._on_retry_success = on_success

        item = MagicMock()
        item.title = "Test"
        item.creator = None
        item.source_id = "id"
        item.provider_key = "ia"
        item.provider_name = "IA"
        item.item_url = None
        item.entry_id = None
        item.work_dir = "/out"
        item.raw_data = {}
        item.id = "item1"

        s._retry_item(item)
        on_success.assert_called_once_with(item)
