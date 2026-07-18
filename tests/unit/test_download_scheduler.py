"""Unit tests for main.download_scheduler module."""

from __future__ import annotations

import threading
import time
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

    def test_init_does_not_mutate_caller_provider_limits(self) -> None:
        """Regression: __init__ must not pop 'default' from the caller's dict."""
        from main.orchestration.scheduler import DownloadScheduler

        caller_limits = {"default": 5, "mdz": 2}
        scheduler = DownloadScheduler(max_workers=2, provider_limits=caller_limits)

        assert caller_limits == {"default": 5, "mdz": 2}
        assert scheduler._default_concurrency == 5
        assert scheduler._semaphores.get_limit("mdz") == 2

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

        scheduler.submit(task, slow_download)

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

    def test_run_task_skipped_on_shutdown_releases_pending(self) -> None:
        """A task already submitted (pending incremented) but skipped because
        shutdown was signalled before it ran must decrement the pending count
        rather than leak it; on_complete is not called for a task that never
        ran (its CSV row stays pending for the next run)."""
        from main.orchestration.scheduler import DownloadScheduler, DownloadTask

        on_complete_calls: list[Any] = []
        scheduler = DownloadScheduler(
            max_workers=1,
            on_complete=lambda t, s, e: on_complete_calls.append((t, s, e)),
        )

        task = MagicMock(spec=DownloadTask)
        task.title = "Test"
        task.provider_key = "ia"

        # Simulate submit() having incremented the pending count, then a
        # shutdown request landing before the worker picks the task up.
        scheduler._pending_count = 1
        scheduler._shutdown_event.set()

        result = scheduler._run_task(task, lambda t: True)

        assert result is False
        assert scheduler.pending_count == 0
        assert on_complete_calls == []

    def test_shutdown_timeout_drops_queued_and_reclaims_pending(self) -> None:
        """shutdown(timeout=...) must return promptly when an in-flight task
        blocks past the deadline: queued (never-started) tasks are cancelled and
        their pending count reclaimed, and once the in-flight task finishes the
        pending count settles at 0 (no leak)."""
        from main.orchestration.scheduler import DownloadScheduler, DownloadTask

        scheduler = DownloadScheduler(max_workers=1)
        scheduler.start()

        started = threading.Event()
        release = threading.Event()
        a_done = threading.Event()

        def blocking(_task: Any) -> bool:
            started.set()
            # Bounded wait so the worker thread cannot dangle forever.
            release.wait(timeout=5.0)
            a_done.set()
            return True

        def queued(_task: Any) -> bool:  # pragma: no cover - must be cancelled
            return True

        task_a = MagicMock(spec=DownloadTask)
        task_a.title = "A"
        task_a.provider_key = "ia"
        task_a.work_id = "work_a"
        task_a.entry_id = "E_A"
        task_a.work_stem = "a"
        task_b = MagicMock(spec=DownloadTask)
        task_b.title = "B"
        task_b.provider_key = "ia"
        task_b.work_id = "work_b"
        task_b.entry_id = "E_B"
        task_b.work_stem = "b"

        scheduler.submit(task_a, blocking)
        # A occupies the single worker; B is then queued behind it.
        assert started.wait(timeout=2.0)
        scheduler.submit(task_b, queued)
        assert scheduler.pending_count == 2

        start = time.monotonic()
        scheduler.shutdown(wait=True, timeout=0.3)
        elapsed = time.monotonic() - start

        # Returned promptly despite A still blocked (timeout honored, not the
        # unbounded executor.shutdown(wait=True)).
        assert elapsed < 3.0
        # B was queued and must have been cancelled, reclaiming its pending
        # count; only the in-flight A remains.
        assert scheduler.pending_count == 1

        # Let A finish and confirm the pending count settles at 0.
        release.set()
        assert a_done.wait(timeout=5.0)
        deadline = time.monotonic() + 2.0
        while scheduler.pending_count > 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert scheduler.pending_count == 0


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

    def test_merges_with_config(self) -> None:
        """Test that config values are used."""
        from main.orchestration.scheduler import get_parallel_download_config

        mock_config = {
            "download": {
                "max_parallel_downloads": 4,
            }
        }

        with patch("main.orchestration.scheduler.get_config", return_value=mock_config):
            config = get_parallel_download_config()

        assert config["max_parallel_downloads"] == 4
