"""Unit tests for api.core.context module."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from api.core.context import (
    clear_all_context,
    clear_current_entry,
    clear_current_name_stem,
    clear_current_provider,
    clear_current_work,
    get_counters,
    get_current_entry,
    get_current_name_stem,
    get_current_provider,
    get_current_work,
    increment_counter,
    provider_context,
    reset_counters,
    set_current_entry,
    set_current_name_stem,
    set_current_provider,
    set_current_work,
    work_context,
)


class TestWorkContext:
    """Tests for work ID context management."""
    
    def test_set_and_get_work_id(self):
        """Test setting and getting work ID."""
        set_current_work("work_123")
        assert get_current_work() == "work_123"
    
    def test_clear_work_id(self):
        """Test clearing work ID."""
        set_current_work("work_123")
        clear_current_work()
        assert get_current_work() is None
    
    def test_work_id_none_by_default(self):
        """Test that work ID is None by default."""
        clear_current_work()
        assert get_current_work() is None
    
    def test_overwrite_work_id(self):
        """Test overwriting work ID."""
        set_current_work("work_1")
        set_current_work("work_2")
        assert get_current_work() == "work_2"


class TestEntryContext:
    """Tests for entry ID context management."""
    
    def test_set_and_get_entry_id(self):
        """Test setting and getting entry ID."""
        set_current_entry("E0001")
        assert get_current_entry() == "E0001"
    
    def test_clear_entry_id(self):
        """Test clearing entry ID."""
        set_current_entry("E0001")
        clear_current_entry()
        assert get_current_entry() is None


class TestProviderContext:
    """Tests for provider key context management."""
    
    def test_set_and_get_provider(self):
        """Test setting and getting provider key."""
        set_current_provider("internet_archive")
        assert get_current_provider() == "internet_archive"
    
    def test_clear_provider(self):
        """Test clearing provider key."""
        set_current_provider("internet_archive")
        clear_current_provider()
        assert get_current_provider() is None


class TestNameStemContext:
    """Tests for name stem context management."""
    
    def test_set_and_get_name_stem(self):
        """Test setting and getting name stem."""
        set_current_name_stem("my_work")
        assert get_current_name_stem() == "my_work"
    
    def test_clear_name_stem(self):
        """Test clearing name stem."""
        set_current_name_stem("my_work")
        clear_current_name_stem()
        assert get_current_name_stem() is None


class TestCounters:
    """Tests for file sequencing counters."""
    
    def test_get_counters_returns_dict(self):
        """Test that get_counters returns a dictionary."""
        counters = get_counters()
        assert isinstance(counters, dict)
    
    def test_reset_counters(self):
        """Test resetting counters."""
        counters = get_counters()
        counters[("stem", "ia", "pdf")] = 5
        reset_counters()
        assert get_counters() == {}
    
    def test_increment_counter(self):
        """Test incrementing counter."""
        reset_counters()
        key = ("stem", "ia", "pdf")
        assert increment_counter(key) == 1
        assert increment_counter(key) == 2
        assert increment_counter(key) == 3
    
    def test_increment_different_keys(self):
        """Test incrementing different counter keys."""
        reset_counters()
        key1 = ("stem1", "ia", "pdf")
        key2 = ("stem2", "ia", "pdf")
        
        assert increment_counter(key1) == 1
        assert increment_counter(key2) == 1
        assert increment_counter(key1) == 2


class TestClearAllContext:
    """Tests for clear_all_context function."""
    
    def test_clears_all_values(self):
        """Test that all context values are cleared."""
        set_current_work("work_123")
        set_current_entry("E0001")
        set_current_provider("internet_archive")
        set_current_name_stem("my_work")
        
        clear_all_context()
        
        assert get_current_work() is None
        assert get_current_entry() is None
        assert get_current_provider() is None
        assert get_current_name_stem() is None


class TestWorkContextManager:
    """Tests for work_context context manager."""
    
    def test_sets_context_on_enter(self):
        """Test that context is set when entering context manager."""
        with work_context(
            work_id="work_123",
            entry_id="E0001",
            provider_key="internet_archive",
            name_stem="my_work"
        ):
            assert get_current_work() == "work_123"
            assert get_current_entry() == "E0001"
            assert get_current_provider() == "internet_archive"
            assert get_current_name_stem() == "my_work"
    
    def test_clears_context_on_exit(self):
        """Test that context is cleared when exiting context manager."""
        with work_context(work_id="work_123"):
            pass
        
        assert get_current_work() is None
    
    def test_clears_context_on_exception(self):
        """Test that context is cleared even when exception occurs."""
        try:
            with work_context(work_id="work_123"):
                raise ValueError("test error")
        except ValueError:
            pass
        
        assert get_current_work() is None
    
    def test_resets_counters(self):
        """Test that counters are reset when entering context."""
        counters = get_counters()
        counters[("key",)] = 10
        
        with work_context(work_id="work_123"):
            assert get_counters() == {}
    
    def test_partial_context(self):
        """Test context manager with partial values."""
        with work_context(work_id="work_123"):
            assert get_current_work() == "work_123"
            assert get_current_entry() is None


class TestProviderContextManager:
    """Tests for provider_context context manager."""
    
    def test_sets_provider_on_enter(self):
        """Test that provider is set when entering context."""
        with provider_context("internet_archive"):
            assert get_current_provider() == "internet_archive"
    
    def test_clears_provider_on_exit(self):
        """Test that provider is cleared when exiting context."""
        with provider_context("internet_archive"):
            pass
        assert get_current_provider() is None
    
    def test_clears_provider_on_exception(self):
        """Test that provider is cleared even on exception."""
        try:
            with provider_context("internet_archive"):
                raise ValueError("test error")
        except ValueError:
            pass
        assert get_current_provider() is None


class TestThreadIsolation:
    """Tests for thread-local storage isolation."""
    
    def test_different_threads_have_separate_context(self):
        """Test that different threads have isolated context."""
        results = {}
        
        def thread_task(thread_id: str):
            set_current_work(thread_id)
            # Small delay to ensure threads overlap
            import time
            time.sleep(0.01)
            results[thread_id] = get_current_work()
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(thread_task, f"thread_{i}")
                for i in range(3)
            ]
            for f in futures:
                f.result()
        
        # Each thread should have its own value
        assert results["thread_0"] == "thread_0"
        assert results["thread_1"] == "thread_1"
        assert results["thread_2"] == "thread_2"
    
    def test_counters_are_thread_local(self):
        """Test that counters are isolated per thread."""
        results = {}
        
        def thread_task(thread_id: str):
            reset_counters()
            key = ("stem", "ia", "pdf")
            for _ in range(3):
                increment_counter(key)
            results[thread_id] = get_counters().get(key, 0)
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(thread_task, f"thread_{i}")
                for i in range(3)
            ]
            for f in futures:
                f.result()
        
        # Each thread should have its own counter at 3
        for thread_id in results:
            assert results[thread_id] == 3
