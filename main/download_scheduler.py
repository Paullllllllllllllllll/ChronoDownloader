"""Parallel download scheduler with per-provider concurrency control.

This module provides a ThreadPoolExecutor-based scheduler for parallelizing
downloads across multiple works while respecting per-provider rate limits
and concurrency constraints.

Key components:
    - DownloadTask: Immutable descriptor for a download job
    - ProviderSemaphoreManager: Per-provider concurrency limits
    - DownloadScheduler: ThreadPoolExecutor wrapper with graceful shutdown
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from api.core.config import get_config
from api.core.context import (
    clear_current_entry,
    clear_current_name_stem,
    clear_current_provider,
    clear_current_work,
    reset_counters,
    set_current_entry,
    set_current_name_stem,
    set_current_provider,
    set_current_work,
)
from api.model import SearchResult

logger = logging.getLogger(__name__)


@dataclass
class DownloadTask:
    """Descriptor for a download job to be executed by a worker thread.
    
    Attributes:
        work_id: Unique identifier for this work (hash-based)
        entry_id: Optional entry ID from CSV
        title: Work title
        creator: Optional creator name
        work_dir: Path to work directory
        work_stem: Naming stem for files
        selected_result: The selected SearchResult to download
        provider_key: Provider key for the selected result
        provider_tuple: Full provider tuple (key, search_fn, download_fn, name)
        work_json_path: Path to work.json file
        all_candidates: List of all candidates for fallback
        provider_map: Map of provider_key to (search_fn, download_fn, name)
        selection_config: Selection configuration dict
        base_output_dir: Base output directory
    """
    work_id: str
    entry_id: Optional[str]
    title: str
    creator: Optional[str]
    work_dir: str
    work_stem: str
    selected_result: SearchResult
    provider_key: str
    provider_tuple: Tuple[str, Callable, Callable, str]
    work_json_path: str
    all_candidates: List[SearchResult] = field(default_factory=list)
    provider_map: Dict[str, Tuple[Callable, Callable, str]] = field(default_factory=dict)
    selection_config: Dict[str, Any] = field(default_factory=dict)
    base_output_dir: str = "downloaded_works"


class ProviderSemaphoreManager:
    """Manages per-provider semaphores for concurrency control.
    
    Each provider has a semaphore that limits concurrent downloads to that provider.
    This ensures we don't overwhelm rate-limited providers even when using multiple workers.
    """
    
    def __init__(self, config: Optional[Dict[str, int]] = None, default: int = 2):
        """Initialize the semaphore manager.
        
        Args:
            config: Mapping of provider_key -> max concurrent downloads
            default: Default concurrency limit if provider not in config
        """
        self._semaphores: Dict[str, threading.Semaphore] = {}
        self._config = config or {}
        self._default = default
        self._lock = threading.Lock()
    
    def acquire(self, provider_key: str) -> None:
        """Acquire semaphore for provider (blocks if at limit).
        
        Args:
            provider_key: Provider identifier
        """
        sem = self._get_or_create(provider_key)
        sem.acquire()
    
    def release(self, provider_key: str) -> None:
        """Release semaphore for provider.
        
        Args:
            provider_key: Provider identifier
        """
        sem = self._semaphores.get(provider_key)
        if sem:
            sem.release()
    
    def _get_or_create(self, provider_key: str) -> threading.Semaphore:
        """Get or create a semaphore for the provider.
        
        Args:
            provider_key: Provider identifier
            
        Returns:
            Semaphore for the provider
        """
        with self._lock:
            if provider_key not in self._semaphores:
                limit = self._config.get(provider_key, self._default)
                self._semaphores[provider_key] = threading.Semaphore(limit)
                logger.debug("Created semaphore for %s with limit %d", provider_key, limit)
            return self._semaphores[provider_key]
    
    def get_limit(self, provider_key: str) -> int:
        """Get the concurrency limit for a provider.
        
        Args:
            provider_key: Provider identifier
            
        Returns:
            Concurrency limit
        """
        return self._config.get(provider_key, self._default)


def get_parallel_download_config() -> Dict[str, Any]:
    """Get parallel download configuration from config.json.
    
    Returns:
        Dictionary with parallel download settings
    """
    cfg = get_config()
    dl = dict(cfg.get("download", {}) or {})
    
    # Defaults for parallel downloads
    dl.setdefault("max_parallel_downloads", 1)  # 1 = sequential (backward compatible)
    dl.setdefault("provider_concurrency", {
        "default": 2,
        "annas_archive": 1,  # Strict quota
        "bnf_gallica": 1,    # Rate limited
        "google_books": 1,   # Rate limited
    })
    dl.setdefault("queue_size", 100)
    # 0 (or omitted) = wait indefinitely for downloads to finish; set a
    # positive value to enforce a hard ceiling per batch wait.
    dl.setdefault("worker_timeout_s", 0)
    
    return dl


class DownloadScheduler:
    """Manages parallel download workers with graceful shutdown.
    
    Uses ThreadPoolExecutor to run multiple download tasks concurrently,
    with per-provider semaphores to limit concurrent access to each provider.
    """
    
    def __init__(
        self,
        max_workers: int = 4,
        provider_limits: Optional[Dict[str, int]] = None,
        on_complete: Optional[Callable[[DownloadTask, bool, Optional[Exception]], None]] = None,
        on_submit: Optional[Callable[[DownloadTask], None]] = None,
    ):
        """Initialize the scheduler.
        
        Args:
            max_workers: Maximum number of concurrent download workers
            provider_limits: Per-provider concurrency limits
            on_complete: Callback when a download completes (task, success, error)
            on_submit: Callback when a task is submitted
        """
        self._max_workers = max_workers
        self._provider_limits = provider_limits or {}
        self._default_concurrency = self._provider_limits.pop("default", 2)
        self._semaphores = ProviderSemaphoreManager(
            self._provider_limits, 
            self._default_concurrency
        )
        self._executor: Optional[ThreadPoolExecutor] = None
        self._futures: Dict[Future, DownloadTask] = {}
        self._on_complete = on_complete
        self._on_submit = on_submit
        self._shutdown_event = threading.Event()
        self._lock = threading.Lock()
        self._pending_count = 0
        self._completed_count = 0
        self._success_count = 0
        self._failure_count = 0
    
    @property
    def pending_count(self) -> int:
        """Number of tasks currently pending or running."""
        with self._lock:
            return self._pending_count
    
    @property
    def completed_count(self) -> int:
        """Number of tasks that have completed (success or failure)."""
        with self._lock:
            return self._completed_count
    
    @property
    def success_count(self) -> int:
        """Number of tasks that completed successfully."""
        with self._lock:
            return self._success_count
    
    @property
    def failure_count(self) -> int:
        """Number of tasks that failed."""
        with self._lock:
            return self._failure_count
    
    def start(self) -> None:
        """Initialize the worker pool."""
        if self._executor is not None:
            logger.warning("Scheduler already started")
            return
        
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="dl_worker"
        )
        self._shutdown_event.clear()
        logger.info(
            "Download scheduler started with %d workers (provider limits: %s)",
            self._max_workers,
            self._provider_limits or f"default={self._default_concurrency}"
        )
    
    def submit(
        self, 
        task: DownloadTask, 
        download_fn: Callable[[DownloadTask], bool]
    ) -> Optional[Future]:
        """Submit a download task to the pool.
        
        Args:
            task: Download task descriptor
            download_fn: Function to execute the download
            
        Returns:
            Future for the task, or None if scheduler is shut down
        """
        if self._executor is None:
            raise RuntimeError("Scheduler not started - call start() first")
        
        if self._shutdown_event.is_set():
            logger.warning("Scheduler shutting down; rejecting task for '%s'", task.title)
            return None
        
        future = self._executor.submit(self._run_task, task, download_fn)
        
        with self._lock:
            self._futures[future] = task
            self._pending_count += 1
        
        if self._on_submit:
            try:
                self._on_submit(task)
            except Exception as e:
                logger.warning("on_submit callback error: %s", e)
        
        logger.debug("Submitted download task for '%s' from %s", task.title, task.provider_key)
        return future
    
    def _run_task(
        self, 
        task: DownloadTask, 
        download_fn: Callable[[DownloadTask], bool]
    ) -> bool:
        """Execute download with provider semaphore and context setup.
        
        Args:
            task: Download task descriptor
            download_fn: Function to execute the download
            
        Returns:
            True if download succeeded, False otherwise
        """
        if self._shutdown_event.is_set():
            logger.debug("Shutdown in progress; skipping task for '%s'", task.title)
            return False
        
        provider = task.provider_key
        error: Optional[Exception] = None
        success = False
        
        try:
            # Acquire provider semaphore (blocks if at limit)
            logger.debug("Acquiring semaphore for %s...", provider)
            self._semaphores.acquire(provider)
            logger.debug("Acquired semaphore for %s", provider)
            
            try:
                # Set thread-local context for this work
                set_current_work(task.work_id)
                set_current_entry(task.entry_id)
                set_current_provider(provider)
                set_current_name_stem(task.work_stem)
                reset_counters()
                
                try:
                    success = download_fn(task)
                except Exception as e:
                    logger.exception("Error executing download for '%s': %s", task.title, e)
                    error = e
                    success = False
                finally:
                    # Clear thread-local context
                    try:
                        clear_current_work()
                    except Exception:
                        pass
                    try:
                        clear_current_entry()
                    except Exception:
                        pass
                    try:
                        clear_current_provider()
                    except Exception:
                        pass
                    try:
                        clear_current_name_stem()
                    except Exception:
                        pass
            finally:
                # Always release semaphore
                self._semaphores.release(provider)
                logger.debug("Released semaphore for %s", provider)
        except Exception as e:
            logger.exception("Unexpected error in task runner for '%s': %s", task.title, e)
            error = e
            success = False
        finally:
            # Update statistics
            with self._lock:
                self._pending_count -= 1
                self._completed_count += 1
                if success:
                    self._success_count += 1
                else:
                    self._failure_count += 1
            
            # Call completion callback
            if self._on_complete:
                try:
                    self._on_complete(task, success, error)
                except Exception as e:
                    logger.warning("on_complete callback error: %s", e)
        
        return success
    
    def wait_all(self, timeout: Optional[float] = None) -> List[Tuple[DownloadTask, bool, Optional[Exception]]]:
        """Wait for all pending downloads to complete.
        
        Args:
            timeout: Maximum seconds to wait per task (None = no timeout)
            
        Returns:
            List of (task, success, error) tuples
        """
        results: List[Tuple[DownloadTask, bool, Optional[Exception]]] = []
        
        with self._lock:
            futures_copy = dict(self._futures)
        
        for future in as_completed(futures_copy.keys(), timeout=timeout):
            task = futures_copy[future]
            error: Optional[Exception] = None
            success = False
            
            try:
                success = future.result(timeout=1.0)  # Short timeout, already completed
            except Exception as e:
                error = e
                success = False
            
            results.append((task, success, error))
            
            # Remove from tracking
            with self._lock:
                self._futures.pop(future, None)
        
        return results
    
    def get_pending_tasks(self) -> List[DownloadTask]:
        """Get list of currently pending/running tasks.
        
        Returns:
            List of pending DownloadTask objects
        """
        with self._lock:
            return list(self._futures.values())
    
    def request_shutdown(self) -> None:
        """Signal that no more tasks should be accepted."""
        self._shutdown_event.set()
        logger.info("Scheduler shutdown requested")
    
    def shutdown(self, wait: bool = True, timeout: Optional[float] = None) -> None:
        """Gracefully shutdown the scheduler.
        
        Args:
            wait: If True, wait for pending tasks to complete
            timeout: Maximum seconds to wait (None = wait forever)
        """
        self._shutdown_event.set()
        
        if self._executor:
            pending = self.pending_count
            if pending > 0:
                logger.info("Shutting down scheduler with %d pending task(s)...", pending)
            
            self._executor.shutdown(wait=wait)
            self._executor = None
            
            logger.info(
                "Download scheduler shut down. Stats: %d completed, %d succeeded, %d failed",
                self._completed_count,
                self._success_count,
                self._failure_count
            )
    
    def get_stats(self) -> Dict[str, int]:
        """Get scheduler statistics.
        
        Returns:
            Dictionary with pending, completed, success, failure counts
        """
        with self._lock:
            return {
                "pending": self._pending_count,
                "completed": self._completed_count,
                "succeeded": self._success_count,
                "failed": self._failure_count,
            }
