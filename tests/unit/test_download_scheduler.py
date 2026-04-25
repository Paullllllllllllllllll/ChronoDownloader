"""Unit tests for main.download_scheduler module."""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestProviderSemaphoreManager:
    """Tests for ProviderSemaphoreManager class."""
    
    def test_default_concurrency(self) -> None:
        """Test default concurrency limit."""
        from main.orchestration.scheduler import ProviderSemaphoreManager

        mgr = ProviderSemaphoreManager({}, default=3)

        # Should be able to acquire 3 times
        mgr.acquire("test_provider")
        mgr.acquire("test_provider")
        mgr.acquire("test_provider")

        # Release all
        mgr.release("test_provider")
        mgr.release("test_provider")
        mgr.release("test_provider")

    def test_custom_provider_limit(self) -> None:
        """Test custom per-provider limit."""
        from main.orchestration.scheduler import ProviderSemaphoreManager

        mgr = ProviderSemaphoreManager({"limited_provider": 1}, default=5)

        # Should only allow 1 acquisition
        mgr.acquire("limited_provider")

        # Verify it's blocking by trying in a thread
        acquired = threading.Event()

        def try_acquire() -> None:
            mgr.acquire("limited_provider")
            acquired.set()

        t = threading.Thread(target=try_acquire)
        t.start()

        # Should not acquire within short timeout
        assert not acquired.wait(timeout=0.1)

        # Release and thread should acquire
        mgr.release("limited_provider")
        assert acquired.wait(timeout=1.0)

        mgr.release("limited_provider")
        t.join()


class TestDownloadTask:
    """Tests for DownloadTask dataclass."""
    
    def test_basic_creation(self) -> None:
        """Test basic DownloadTask creation."""
        from main.orchestration.scheduler import DownloadTask

        task = DownloadTask(
            work_id="work_123",
            entry_id="E0001",
            title="Test Title",
            creator="Test Author",
            work_dir="/path/to/work",
            work_stem="test_title",
            selected_result=MagicMock(),
            provider_key="internet_archive",
            provider_tuple=("ia", None, None, "IA"),  # type: ignore[arg-type]
            work_json_path="/path/to/work.json",
            all_candidates=[],
            provider_map={},
            selection_config={},
            base_output_dir="/output",
        )

        assert task.work_id == "work_123"
        assert task.title == "Test Title"
        assert task.provider_key == "internet_archive"

    def test_optional_fields(self) -> None:
        """Test optional fields default values."""
        from main.orchestration.scheduler import DownloadTask

        task = DownloadTask(
            work_id="work_123",
            entry_id=None,
            title="Test",
            creator=None,
            work_dir="/path",
            work_stem="test",
            selected_result=MagicMock(),
            provider_key="ia",
            provider_tuple=("ia", None, None, "IA"),  # type: ignore[arg-type]
            work_json_path="/path/work.json",
            all_candidates=[],
            provider_map={},
            selection_config={},
            base_output_dir="/output",
        )

        assert task.entry_id is None
        assert task.creator is None


class TestDownloadScheduler:
    """Tests for DownloadScheduler class."""
    
    def test_initialization(self) -> None:
        """Test scheduler initialization."""
        from main.orchestration.scheduler import DownloadScheduler

        scheduler = DownloadScheduler(max_workers=4)

        assert scheduler._max_workers == 4
        assert scheduler.pending_count == 0
        assert scheduler.completed_count == 0

    def test_start_creates_executor(self) -> None:
        """Test that start() creates thread pool executor."""
        from main.orchestration.scheduler import DownloadScheduler

        scheduler = DownloadScheduler(max_workers=2)

        assert scheduler._executor is None
        scheduler.start()
        assert scheduler._executor is not None

        scheduler.shutdown(wait=True)

    def test_submit_requires_start(self) -> None:
        """Test that submit raises error if not started."""
        from main.orchestration.scheduler import DownloadScheduler, DownloadTask

        scheduler = DownloadScheduler()
        task = MagicMock(spec=DownloadTask)

        with pytest.raises(RuntimeError):
            scheduler.submit(task, lambda t: True)

    def test_submit_increments_pending(self) -> None:
        """Test that submit increments pending count."""
        from main.orchestration.scheduler import DownloadScheduler, DownloadTask

        scheduler = DownloadScheduler(max_workers=2)
        scheduler.start()

        task = MagicMock(spec=DownloadTask)
        task.title = "Test"
        task.provider_key = "ia"

        # Slow download function to keep task pending
        def slow_download(t: Any) -> bool:
            time.sleep(0.5)
            return True

        future = scheduler.submit(task, slow_download)

        assert scheduler.pending_count >= 1 or scheduler.completed_count >= 1

        scheduler.shutdown(wait=True)

    def test_callbacks_called(self) -> None:
        """Test that callbacks are called."""
        from main.orchestration.scheduler import DownloadScheduler, DownloadTask

        on_submit_called = threading.Event()
        on_complete_called = threading.Event()

        def on_submit(task: Any) -> None:
            on_submit_called.set()

        def on_complete(task: Any, success: bool, error: Exception | None) -> None:
            on_complete_called.set()

        scheduler = DownloadScheduler(
            max_workers=1,
            on_submit=on_submit,
            on_complete=on_complete,
        )
        scheduler.start()

        task = MagicMock(spec=DownloadTask)
        task.title = "Test"
        task.provider_key = "ia"
        task.work_id = "work_1"
        task.entry_id = "E0001"
        task.work_stem = "test"

        scheduler.submit(task, lambda t: True)

        # Wait for completion
        scheduler.wait_all(timeout=5.0)
        scheduler.shutdown(wait=True)

        assert on_submit_called.is_set()
        assert on_complete_called.is_set()

    def test_shutdown_rejects_new_tasks(self) -> None:
        """Test that shutdown rejects new tasks."""
        from main.orchestration.scheduler import DownloadScheduler, DownloadTask

        scheduler = DownloadScheduler(max_workers=1)
        scheduler.start()

        # Trigger shutdown event
        scheduler._shutdown_event.set()

        task = MagicMock(spec=DownloadTask)
        task.title = "Test"

        future = scheduler.submit(task, lambda t: True)

        assert future is None

        scheduler.shutdown(wait=True)


class TestGetParallelDownloadConfig:
    """Tests for get_parallel_download_config function."""
    
    def test_returns_dict(self) -> None:
        """Test that function returns a dictionary."""
        from main.orchestration.scheduler import get_parallel_download_config

        with patch("main.orchestration.scheduler.get_config", return_value={}):
            config = get_parallel_download_config()

        assert isinstance(config, dict)

    def test_default_values(self) -> None:
        """Test default configuration values."""
        from main.orchestration.scheduler import get_parallel_download_config

        with patch("main.orchestration.scheduler.get_config", return_value={}):
            config = get_parallel_download_config()

        assert config["max_parallel_downloads"] == 1
        assert "provider_concurrency" in config
        assert config["queue_size"] == 100

    def test_merges_with_config(self) -> None:
        """Test that config values are used."""
        from main.orchestration.scheduler import get_parallel_download_config

        mock_config = {
            "download": {
                "max_parallel_downloads": 4,
                "queue_size": 200,
            }
        }

        with patch("main.orchestration.scheduler.get_config", return_value=mock_config):
            config = get_parallel_download_config()

        assert config["max_parallel_downloads"] == 4
        assert config["queue_size"] == 200
