"""Tests for main/deferred_queue.py - Persistent deferred download queue."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


class TestDeferredItem:
    """Tests for DeferredItem dataclass."""

    def test_to_dict_serialization(self):
        """to_dict returns serializable dictionary."""
        from main.deferred_queue import DeferredItem
        
        item = DeferredItem(
            id="test-id",
            title="Test Work",
            creator="Test Author",
            entry_id="E001",
            provider_key="test_provider",
            provider_name="Test Provider",
            source_id="src123",
            work_dir="/path/to/work",
            base_output_dir="/output"
        )
        
        result = item.to_dict()
        
        assert result["id"] == "test-id"
        assert result["title"] == "Test Work"
        assert result["creator"] == "Test Author"

    def test_from_dict_deserialization(self):
        """from_dict creates DeferredItem from dict."""
        from main.deferred_queue import DeferredItem
        
        data = {
            "id": "item1",
            "title": "Some Work",
            "creator": "Author",
            "entry_id": "E002",
            "provider_key": "ia",
            "provider_name": "Internet Archive",
            "source_id": "ia12345",
            "work_dir": "/work",
            "base_output_dir": "/out",
            "retry_count": 2
        }
        
        item = DeferredItem.from_dict(data)
        
        assert item.id == "item1"
        assert item.title == "Some Work"
        assert item.retry_count == 2

    def test_from_dict_with_missing_fields(self):
        """from_dict handles missing fields with defaults."""
        from main.deferred_queue import DeferredItem
        
        item = DeferredItem.from_dict({})
        
        assert item.title == "Unknown"
        assert item.provider_key == "unknown"
        assert item.retry_count == 0
        assert item.status == "pending"

    def test_get_reset_datetime_parses_iso(self):
        """get_reset_datetime parses ISO format string."""
        from main.deferred_queue import DeferredItem
        
        reset_time = "2024-06-15T12:00:00+00:00"
        item = DeferredItem(
            id="test",
            title="Test",
            creator=None,
            entry_id=None,
            provider_key="test",
            provider_name="Test",
            source_id=None,
            work_dir="",
            base_output_dir="",
            reset_time=reset_time
        )
        
        dt = item.get_reset_datetime()
        
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 6
        assert dt.day == 15

    def test_get_reset_datetime_returns_none_if_not_set(self):
        """get_reset_datetime returns None if reset_time is None."""
        from main.deferred_queue import DeferredItem
        
        item = DeferredItem(
            id="test", title="Test", creator=None, entry_id=None,
            provider_key="test", provider_name="Test", source_id=None,
            work_dir="", base_output_dir=""
        )
        
        assert item.get_reset_datetime() is None

    def test_is_ready_for_retry_true_when_past_reset(self):
        """is_ready_for_retry returns True when reset time passed."""
        from main.deferred_queue import DeferredItem
        
        past_reset = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        item = DeferredItem(
            id="test", title="Test", creator=None, entry_id=None,
            provider_key="test", provider_name="Test", source_id=None,
            work_dir="", base_output_dir="",
            reset_time=past_reset,
            status="pending"
        )
        
        assert item.is_ready_for_retry() is True

    def test_is_ready_for_retry_false_when_future_reset(self):
        """is_ready_for_retry returns False when reset time in future."""
        from main.deferred_queue import DeferredItem
        
        future_reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        item = DeferredItem(
            id="test", title="Test", creator=None, entry_id=None,
            provider_key="test", provider_name="Test", source_id=None,
            work_dir="", base_output_dir="",
            reset_time=future_reset,
            status="pending"
        )
        
        assert item.is_ready_for_retry() is False

    def test_is_ready_for_retry_false_when_completed(self):
        """is_ready_for_retry returns False for completed items."""
        from main.deferred_queue import DeferredItem
        
        item = DeferredItem(
            id="test", title="Test", creator=None, entry_id=None,
            provider_key="test", provider_name="Test", source_id=None,
            work_dir="", base_output_dir="",
            status="completed"
        )
        
        assert item.is_ready_for_retry() is False

    def test_seconds_until_ready_future(self):
        """seconds_until_ready returns positive value for future reset."""
        from main.deferred_queue import DeferredItem
        
        future_reset = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        item = DeferredItem(
            id="test", title="Test", creator=None, entry_id=None,
            provider_key="test", provider_name="Test", source_id=None,
            work_dir="", base_output_dir="",
            reset_time=future_reset
        )
        
        seconds = item.seconds_until_ready()
        assert seconds > 0
        assert seconds < 3700  # Less than 1 hour + 100 seconds buffer

    def test_seconds_until_ready_past(self):
        """seconds_until_ready returns 0 for past reset time."""
        from main.deferred_queue import DeferredItem
        
        past_reset = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        item = DeferredItem(
            id="test", title="Test", creator=None, entry_id=None,
            provider_key="test", provider_name="Test", source_id=None,
            work_dir="", base_output_dir="",
            reset_time=past_reset
        )
        
        assert item.seconds_until_ready() == 0


class TestDeferredQueueSingleton:
    """Tests for DeferredQueue singleton pattern."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config):
        """Reset singletons before and after each test."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        
        DeferredQueue._instance = None
        StateManager._instance = None
        yield
        DeferredQueue._instance = None
        StateManager._instance = None

    def test_singleton_returns_same_instance(self):
        """Multiple instantiations return same instance."""
        from main.deferred_queue import DeferredQueue
        
        queue1 = DeferredQueue()
        queue2 = DeferredQueue()
        
        assert queue1 is queue2

    def test_singleton_thread_safety(self):
        """Singleton creation is thread-safe."""
        from main.deferred_queue import DeferredQueue
        
        instances = []
        
        def create_instance():
            instances.append(DeferredQueue())
        
        threads = [threading.Thread(target=create_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert all(inst is instances[0] for inst in instances)


class TestDeferredQueueOperations:
    """Tests for DeferredQueue core operations."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config, temp_dir):
        """Reset singletons before and after each test."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        
        DeferredQueue._instance = None
        StateManager._instance = None
        yield
        DeferredQueue._instance = None
        StateManager._instance = None

    @pytest.fixture
    def queue(self, temp_dir):
        """Create fresh DeferredQueue with isolated state."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        import os
        
        state_file = os.path.join(temp_dir, "ops_state.json")
        StateManager._instance = None
        sm = StateManager(state_file=state_file)
        
        DeferredQueue._instance = None
        q = DeferredQueue()
        q._items.clear()
        return q

    def test_add_creates_item(self, queue):
        """add creates and returns a DeferredItem."""
        item = queue.add(
            title="Test Work",
            creator="Author",
            entry_id="E001",
            provider_key="test",
            provider_name="Test Provider",
            source_id="src123",
            work_dir="/work",
            base_output_dir="/out"
        )
        
        assert item.title == "Test Work"
        assert item.status == "pending"
        assert len(queue) == 1

    def test_add_prevents_duplicates(self, queue):
        """add returns existing item for duplicate entry_id + provider."""
        item1 = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        item2 = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        assert item1.id == item2.id
        assert len(queue) == 1

    def test_add_with_reset_time(self, queue):
        """add stores reset_time correctly."""
        reset = datetime.now(timezone.utc) + timedelta(hours=6)
        
        item = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o",
            reset_time=reset
        )
        
        assert item.reset_time is not None
        reset_dt = item.get_reset_datetime()
        assert reset_dt is not None

    def test_remove_deletes_item(self, queue):
        """remove deletes item by ID."""
        item = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        result = queue.remove(item.id)
        
        assert result is True
        assert len(queue) == 0

    def test_remove_returns_false_if_not_found(self, queue):
        """remove returns False if ID not found."""
        result = queue.remove("nonexistent-id")
        assert result is False

    def test_get_returns_item(self, queue):
        """get returns item by ID."""
        item = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        retrieved = queue.get(item.id)
        
        assert retrieved is not None
        assert retrieved.id == item.id

    def test_get_returns_none_if_not_found(self, queue):
        """get returns None if ID not found."""
        assert queue.get("nonexistent") is None


class TestDeferredQueueStatusOperations:
    """Tests for status-related operations."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config):
        """Reset singletons."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        
        DeferredQueue._instance = None
        StateManager._instance = None
        yield
        DeferredQueue._instance = None
        StateManager._instance = None

    @pytest.fixture
    def queue(self):
        """Create fresh DeferredQueue."""
        from main.deferred_queue import DeferredQueue
        return DeferredQueue()

    def test_mark_completed(self, queue):
        """mark_completed sets status to completed."""
        item = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        result = queue.mark_completed(item.id)
        
        assert result is True
        assert queue.get(item.id).status == "completed"

    def test_mark_completed_returns_false_if_not_found(self, queue):
        """mark_completed returns False if ID not found."""
        assert queue.mark_completed("nonexistent") is False

    def test_mark_failed(self, queue):
        """mark_failed sets status and error message."""
        item = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        result = queue.mark_failed(item.id, "Download error")
        
        assert result is True
        updated = queue.get(item.id)
        assert updated.status == "failed"
        assert updated.error_message == "Download error"

    def test_mark_retrying_increments_count(self, queue):
        """mark_retrying increments retry_count."""
        item = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        result = queue.mark_retrying(item.id)
        
        assert result is True
        assert queue.get(item.id).retry_count == 1
        assert queue.get(item.id).status == "retrying"

    def test_mark_retrying_fails_when_max_exceeded(self, queue):
        """mark_retrying marks as failed when max retries exceeded."""
        item = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        # Retry until max exceeded
        for i in range(queue._max_retries):
            queue.mark_retrying(item.id)
        
        updated = queue.get(item.id)
        assert updated.status == "failed"
        assert "Max retries" in updated.error_message

    def test_mark_retrying_updates_reset_time(self, queue):
        """mark_retrying updates reset_time if provided."""
        item = queue.add(
            title="Test", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        new_reset = datetime.now(timezone.utc) + timedelta(hours=12)
        queue.mark_retrying(item.id, new_reset_time=new_reset)
        
        updated = queue.get(item.id)
        assert updated.reset_time is not None


class TestDeferredQueueQueries:
    """Tests for query operations."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config, temp_dir):
        """Reset singletons."""
        import os
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        
        DeferredQueue._instance = None
        StateManager._instance = None
        # Use temp dir for state file
        os.environ["CHRONO_STATE_FILE"] = os.path.join(temp_dir, "state.json")
        yield
        DeferredQueue._instance = None
        StateManager._instance = None

    @pytest.fixture
    def queue(self, temp_dir):
        """Create fresh DeferredQueue with isolated state."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        import os
        
        # Create isolated state manager
        state_file = os.path.join(temp_dir, "queue_state.json")
        StateManager._instance = None
        sm = StateManager(state_file=state_file)
        
        DeferredQueue._instance = None
        q = DeferredQueue()
        q._items.clear()  # Ensure clean start
        return q

    def test_get_pending_returns_pending_items(self, queue):
        """get_pending returns only pending and retrying items."""
        queue.add(
            title="Pending", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        item2 = queue.add(
            title="Completed", creator=None, entry_id="E002",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        queue.mark_completed(item2.id)
        
        pending = queue.get_pending()
        
        assert len(pending) == 1
        assert pending[0].title == "Pending"

    def test_get_ready_returns_ready_items(self, queue):
        """get_ready returns items past their reset time."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        
        queue.add(
            title="Ready", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o",
            reset_time=past
        )
        queue.add(
            title="NotReady", creator=None, entry_id="E002",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o",
            reset_time=future
        )
        
        ready = queue.get_ready()
        
        assert len(ready) == 1
        assert ready[0].title == "Ready"

    def test_get_by_provider_filters_correctly(self, queue):
        """get_by_provider returns items for specific provider."""
        queue.add(
            title="Provider1", creator=None, entry_id="E001",
            provider_key="provider1", provider_name="P1",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        queue.add(
            title="Provider2", creator=None, entry_id="E002",
            provider_key="provider2", provider_name="P2",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        p1_items = queue.get_by_provider("provider1")
        
        assert len(p1_items) == 1
        assert p1_items[0].title == "Provider1"

    def test_get_next_ready_time_returns_earliest(self, queue):
        """get_next_ready_time returns earliest reset time."""
        later = datetime.now(timezone.utc) + timedelta(hours=6)
        earlier = datetime.now(timezone.utc) + timedelta(hours=2)
        
        queue.add(
            title="Later", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o",
            reset_time=later
        )
        queue.add(
            title="Earlier", creator=None, entry_id="E002",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o",
            reset_time=earlier
        )
        
        next_ready = queue.get_next_ready_time()
        
        assert next_ready is not None
        # Should be within a few seconds of 'earlier'
        delta = abs((next_ready - earlier).total_seconds())
        assert delta < 5

    def test_count_by_status(self, queue):
        """count_by_status returns correct counts."""
        item1 = queue.add(
            title="Pending", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        item2 = queue.add(
            title="Completed", creator=None, entry_id="E002",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        queue.mark_completed(item2.id)
        
        counts = queue.count_by_status()
        
        assert counts.get("pending", 0) == 1
        assert counts.get("completed", 0) == 1


class TestDeferredQueueCleanup:
    """Tests for cleanup operations."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config, temp_dir):
        """Reset singletons."""
        import os
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        
        DeferredQueue._instance = None
        StateManager._instance = None
        os.environ["CHRONO_STATE_FILE"] = os.path.join(temp_dir, "state.json")
        yield
        DeferredQueue._instance = None
        StateManager._instance = None

    @pytest.fixture
    def queue(self, temp_dir):
        """Create fresh DeferredQueue with isolated state."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        import os
        
        state_file = os.path.join(temp_dir, "cleanup_state.json")
        StateManager._instance = None
        sm = StateManager(state_file=state_file)
        
        DeferredQueue._instance = None
        q = DeferredQueue()
        q._items.clear()
        return q

    def test_clear_completed_removes_completed(self, queue):
        """clear_completed removes all completed items."""
        item1 = queue.add(
            title="Completed", creator=None, entry_id="E001",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        queue.mark_completed(item1.id)
        queue.add(
            title="Pending", creator=None, entry_id="E002",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        
        removed = queue.clear_completed()
        
        assert removed == 1
        assert len(queue) == 1

    def test_cleanup_old_items_removes_old(self, queue):
        """cleanup_old_items removes old completed/failed items."""
        from main.deferred_queue import DeferredItem
        
        # Directly add an old item
        old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        old_item = DeferredItem(
            id="old-item",
            title="Old",
            creator=None,
            entry_id="E001",
            provider_key="test",
            provider_name="Test",
            source_id="src",
            work_dir="/w",
            base_output_dir="/o",
            status="completed",
            deferred_at=old_time
        )
        queue._items[old_item.id] = old_item
        
        # Add a recent completed item
        recent = queue.add(
            title="Recent", creator=None, entry_id="E002",
            provider_key="test", provider_name="Test",
            source_id="src", work_dir="/w", base_output_dir="/o"
        )
        queue.mark_completed(recent.id)
        
        removed = queue.cleanup_old_items(max_age_days=7)
        
        assert removed == 1
        assert queue.get("old-item") is None
        assert queue.get(recent.id) is not None

    def test_clear_all_removes_everything(self, queue):
        """clear_all removes all items."""
        for i in range(5):
            queue.add(
                title=f"Item{i}", creator=None, entry_id=f"E{i}",
                provider_key="test", provider_name="Test",
                source_id="src", work_dir="/w", base_output_dir="/o"
            )
        
        removed = queue.clear_all()
        
        assert removed == 5
        assert len(queue) == 0


class TestDeferredQueueIteration:
    """Tests for iteration and length."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config, temp_dir):
        """Reset singletons."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        
        DeferredQueue._instance = None
        StateManager._instance = None
        yield
        DeferredQueue._instance = None
        StateManager._instance = None

    @pytest.fixture
    def queue(self, temp_dir):
        """Create fresh DeferredQueue with isolated state."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        import os
        
        state_file = os.path.join(temp_dir, "iter_state.json")
        StateManager._instance = None
        sm = StateManager(state_file=state_file)
        
        DeferredQueue._instance = None
        q = DeferredQueue()
        q._items.clear()
        return q

    def test_len_returns_count(self, queue):
        """__len__ returns number of items."""
        for i in range(3):
            queue.add(
                title=f"Item{i}", creator=None, entry_id=f"E{i}",
                provider_key="test", provider_name="Test",
                source_id="src", work_dir="/w", base_output_dir="/o"
            )
        
        assert len(queue) == 3

    def test_iter_yields_items(self, queue):
        """__iter__ yields all items."""
        for i in range(3):
            queue.add(
                title=f"Item{i}", creator=None, entry_id=f"E{i}",
                provider_key="test", provider_name="Test",
                source_id="src", work_dir="/w", base_output_dir="/o"
            )
        
        items = list(queue)
        
        assert len(items) == 3


class TestGetDeferredQueue:
    """Tests for get_deferred_queue helper."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config):
        """Reset singletons."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        
        DeferredQueue._instance = None
        StateManager._instance = None
        yield
        DeferredQueue._instance = None
        StateManager._instance = None

    def test_get_deferred_queue_returns_singleton(self):
        """get_deferred_queue returns singleton instance."""
        from main.deferred_queue import get_deferred_queue
        
        queue1 = get_deferred_queue()
        queue2 = get_deferred_queue()
        
        assert queue1 is queue2
