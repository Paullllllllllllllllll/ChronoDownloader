"""Tests for main.state.background — eager deferred-retry helper."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest

from api.model import QuotaDeferredException, SearchResult
from main.state.background import BackgroundRetryScheduler


@pytest.fixture(autouse=True)
def reset_scheduler_singleton() -> Generator[None, None, None]:
    """Reset the BackgroundRetryScheduler singleton between tests."""
    BackgroundRetryScheduler._instance = None
    yield
    BackgroundRetryScheduler._instance = None


# ============================================================================
# Singleton pattern
# ============================================================================


class TestBackgroundSchedulerSingleton:
    """Tests for singleton pattern."""

    def test_singleton_returns_same_instance(self) -> None:
        s1 = BackgroundRetryScheduler()
        s2 = BackgroundRetryScheduler()
        assert s1 is s2

    def test_initialized_once(self) -> None:
        s = BackgroundRetryScheduler()
        assert s._initialized is True


# ============================================================================
# set_provider_download_fn
# ============================================================================


class TestSetProviderDownloadFn:
    """Tests for registering download functions."""

    def test_registers_download_function(self) -> None:
        s = BackgroundRetryScheduler()
        fn = MagicMock()
        s.set_provider_download_fn("ia", fn)
        assert s._provider_download_fns["ia"] is fn


# ============================================================================
# set_callbacks
# ============================================================================


class TestSetCallbacks:
    """Tests for callback registration."""

    def test_sets_callbacks(self) -> None:
        s = BackgroundRetryScheduler()
        on_success = MagicMock()
        on_failure = MagicMock()
        s.set_callbacks(on_success=on_success, on_failure=on_failure)
        assert s._on_retry_success is on_success
        assert s._on_retry_failure is on_failure


# ============================================================================
# get_stats
# ============================================================================


class TestBackgroundSchedulerStats:
    """Tests for statistics tracking."""

    def test_initial_stats(self) -> None:
        s = BackgroundRetryScheduler()
        stats = s.get_stats()
        assert stats["retries_attempted"] == 0
        assert stats["retries_succeeded"] == 0
        assert stats["retries_failed"] == 0


# ============================================================================
# _reconstruct_search_result
# ============================================================================


class TestReconstructSearchResult:
    """Tests for SearchResult reconstruction from DeferredItem."""

    def test_reconstructs_from_deferred_item(self) -> None:
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
        assert result is not None
        assert result.title == "Test Book"
        assert result.creators == ["Author"]
        assert result.source_id == "id123"

    def test_handles_no_creator(self) -> None:
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
        assert result is not None
        assert result.creators == []

    def test_returns_none_on_error(self) -> None:
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


def _mock_item() -> MagicMock:
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
    return item


# ============================================================================
# _retry_item (called by retry_ready_now)
# ============================================================================


class TestRetryItem:
    """Tests for individual item retry logic."""

    def test_succeeds_with_download(self) -> None:
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (True, 0)

        download_fn = MagicMock(return_value=True)
        s._provider_download_fns = {"ia": download_fn}

        result = s._retry_item(_mock_item())
        assert result is True
        s._queue.mark_completed.assert_called_once()

    def test_non_quota_success_does_not_record_quota(self) -> None:
        """A non-quota provider retry must not record a quota unit (BUG-4a)."""
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (True, 0)

        s._provider_download_fns = {"ia": MagicMock(return_value=True)}

        assert s._retry_item(_mock_item()) is True
        s._quota_manager.record_download.assert_not_called()

    def test_skips_when_no_download_fn(self) -> None:
        s = BackgroundRetryScheduler()
        s._provider_download_fns = {}

        item = MagicMock()
        item.title = "Test"
        item.provider_key = "unknown"

        result = s._retry_item(item)
        assert result is False

    def test_skips_when_quota_not_ready(self) -> None:
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

    def test_handles_quota_deferred_exception(self) -> None:
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (True, 0)

        download_fn = MagicMock(side_effect=QuotaDeferredException("ia"))
        s._provider_download_fns = {"ia": download_fn}

        item = _mock_item()
        item.creator = None
        item.entry_id = None

        result = s._retry_item(item)
        assert result is False
        assert s.get_stats()["retries_redeferred"] == 1

    def test_handles_download_failure(self) -> None:
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._queue.mark_retrying.return_value = False
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (True, 0)

        download_fn = MagicMock(return_value=False)
        s._provider_download_fns = {"ia": download_fn}

        on_failure = MagicMock()
        s._on_retry_failure = on_failure

        item = _mock_item()
        item.creator = None
        item.entry_id = None

        result = s._retry_item(item)
        assert result is False
        on_failure.assert_called_once()

    def test_calls_success_callback(self) -> None:
        s = BackgroundRetryScheduler()
        s._queue = MagicMock()
        s._quota_manager = MagicMock()
        s._quota_manager.can_download.return_value = (True, 0)

        download_fn = MagicMock(return_value=True)
        s._provider_download_fns = {"ia": download_fn}
        on_success = MagicMock()
        s._on_retry_success = on_success

        item = _mock_item()
        item.creator = None
        item.entry_id = None

        s._retry_item(item)
        on_success.assert_called_once_with(item)
