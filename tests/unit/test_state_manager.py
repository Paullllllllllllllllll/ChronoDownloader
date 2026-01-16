"""Tests for main/state_manager.py - Unified state management."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestStateManagerSingleton:
    """Tests for StateManager singleton pattern."""

    def test_singleton_returns_same_instance(self, temp_dir, mock_config):
        """Multiple instantiations return the same instance."""
        # Reset singleton for testing
        from main.state_manager import StateManager
        StateManager._instance = None
        
        state_file = os.path.join(temp_dir, "test_state.json")
        manager1 = StateManager(state_file=state_file)
        manager2 = StateManager(state_file=state_file)
        
        assert manager1 is manager2
        
        # Cleanup
        StateManager._instance = None

    def test_singleton_thread_safety(self, temp_dir, mock_config):
        """Singleton creation is thread-safe."""
        from main.state_manager import StateManager
        StateManager._instance = None
        
        state_file = os.path.join(temp_dir, "test_state.json")
        instances = []
        
        def create_instance():
            instances.append(StateManager(state_file=state_file))
        
        threads = [threading.Thread(target=create_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All instances should be the same
        assert all(inst is instances[0] for inst in instances)
        
        # Cleanup
        StateManager._instance = None


class TestStateManagerInit:
    """Tests for StateManager initialization."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from main.state_manager import StateManager
        StateManager._instance = None
        yield
        StateManager._instance = None

    def test_init_with_explicit_state_file(self, temp_dir, mock_config):
        """Initializes with explicit state file path."""
        from main.state_manager import StateManager
        
        state_file = os.path.join(temp_dir, "custom_state.json")
        manager = StateManager(state_file=state_file)
        
        assert manager.get_state_file_path() == Path(state_file)

    def test_init_creates_default_state(self, temp_dir, mock_config):
        """Creates default state structure on init."""
        from main.state_manager import StateManager
        
        state_file = os.path.join(temp_dir, "test_state.json")
        manager = StateManager(state_file=state_file)
        
        assert manager.get_quotas() == {}
        assert manager.get_deferred_items() == []

    def test_init_loads_existing_state(self, temp_dir, mock_config):
        """Loads existing state from file."""
        from main.state_manager import StateManager
        
        state_file = os.path.join(temp_dir, "test_state.json")
        
        # Pre-create state file
        existing_state = {
            "quotas": {"test_provider": {"downloads_used": 5}},
            "deferred_items": [{"id": "item1", "title": "Test"}],
            "version": "2.0"
        }
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(existing_state, f)
        
        manager = StateManager(state_file=state_file)
        
        assert manager.get_quotas() == {"test_provider": {"downloads_used": 5}}
        assert manager.get_deferred_items() == [{"id": "item1", "title": "Test"}]


class TestStateManagerMigration:
    """Tests for migration from old state files."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from main.state_manager import StateManager
        StateManager._instance = None
        yield
        StateManager._instance = None

    def test_migrates_from_old_quota_file(self, temp_dir, mock_config):
        """Migrates quota state from old .quota_state.json file."""
        from main.state_manager import StateManager
        
        # Create old quota file in temp dir (we need to change cwd for this test)
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            old_quota_file = Path(".quota_state.json")
            old_quota_data = {"quotas": {"annas_archive": {"downloads_used": 10}}}
            with open(old_quota_file, "w", encoding="utf-8") as f:
                json.dump(old_quota_data, f)
            
            state_file = os.path.join(temp_dir, "new_state.json")
            manager = StateManager(state_file=state_file)
            
            assert manager.get_quotas() == {"annas_archive": {"downloads_used": 10}}
        finally:
            os.chdir(original_cwd)

    def test_migrates_from_old_queue_file(self, temp_dir, mock_config):
        """Migrates deferred queue from old .deferred_queue.json file."""
        from main.state_manager import StateManager
        
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            old_queue_file = Path(".deferred_queue.json")
            old_queue_data = {"items": [{"id": "def1", "title": "Deferred Work"}]}
            with open(old_queue_file, "w", encoding="utf-8") as f:
                json.dump(old_queue_data, f)
            
            state_file = os.path.join(temp_dir, "new_state.json")
            manager = StateManager(state_file=state_file)
            
            assert manager.get_deferred_items() == [{"id": "def1", "title": "Deferred Work"}]
        finally:
            os.chdir(original_cwd)


class TestStateManagerQuotas:
    """Tests for quota state management."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from main.state_manager import StateManager
        StateManager._instance = None
        yield
        StateManager._instance = None

    @pytest.fixture
    def manager(self, temp_dir, mock_config):
        """Create a fresh StateManager for each test."""
        from main.state_manager import StateManager
        state_file = os.path.join(temp_dir, "test_state.json")
        return StateManager(state_file=state_file)

    def test_get_quota_returns_none_for_unknown(self, manager):
        """get_quota returns None for unknown provider."""
        assert manager.get_quota("unknown_provider") is None

    def test_set_quota_stores_data(self, manager):
        """set_quota stores quota data."""
        quota_data = {"downloads_used": 5, "daily_limit": 100}
        manager.set_quota("test_provider", quota_data)
        
        assert manager.get_quota("test_provider") == quota_data

    def test_set_quota_persists_to_file(self, manager, temp_dir):
        """set_quota persists data to file."""
        quota_data = {"downloads_used": 5}
        manager.set_quota("test_provider", quota_data)
        
        # Read file directly
        state_file = manager.get_state_file_path()
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        assert data["quotas"]["test_provider"] == quota_data

    def test_update_quotas_batch_update(self, manager):
        """update_quotas updates multiple providers at once."""
        quotas = {
            "provider1": {"downloads_used": 10},
            "provider2": {"downloads_used": 20}
        }
        manager.update_quotas(quotas)
        
        assert manager.get_quota("provider1") == {"downloads_used": 10}
        assert manager.get_quota("provider2") == {"downloads_used": 20}

    def test_get_quotas_returns_shallow_copy(self, manager):
        """get_quotas returns a shallow copy of the quotas dict."""
        manager.set_quota("test", {"value": 1})
        quotas = manager.get_quotas()
        
        # Adding a new key to the copy shouldn't affect internal state
        quotas["new_provider"] = {"value": 2}
        
        # The new key should not appear in internal state
        assert manager.get_quota("new_provider") is None


class TestStateManagerDeferredItems:
    """Tests for deferred item state management."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from main.state_manager import StateManager
        StateManager._instance = None
        yield
        StateManager._instance = None

    @pytest.fixture
    def manager(self, temp_dir, mock_config):
        """Create a fresh StateManager for each test."""
        from main.state_manager import StateManager
        state_file = os.path.join(temp_dir, "test_state.json")
        return StateManager(state_file=state_file)

    def test_add_deferred_item(self, manager):
        """add_deferred_item adds item to list."""
        item = {"id": "item1", "title": "Test Work"}
        manager.add_deferred_item(item)
        
        items = manager.get_deferred_items()
        assert len(items) == 1
        assert items[0] == item

    def test_set_deferred_items_replaces_all(self, manager):
        """set_deferred_items replaces entire list."""
        manager.add_deferred_item({"id": "old"})
        
        new_items = [{"id": "new1"}, {"id": "new2"}]
        manager.set_deferred_items(new_items)
        
        items = manager.get_deferred_items()
        assert len(items) == 2
        assert items[0]["id"] == "new1"

    def test_remove_deferred_item_by_id(self, manager):
        """remove_deferred_item removes item by ID."""
        manager.add_deferred_item({"id": "keep"})
        manager.add_deferred_item({"id": "remove"})
        
        result = manager.remove_deferred_item("remove")
        
        assert result is True
        items = manager.get_deferred_items()
        assert len(items) == 1
        assert items[0]["id"] == "keep"

    def test_remove_deferred_item_not_found(self, manager):
        """remove_deferred_item returns False if not found."""
        result = manager.remove_deferred_item("nonexistent")
        assert result is False

    def test_get_deferred_items_returns_copy(self, manager):
        """get_deferred_items returns a copy, not internal list."""
        manager.add_deferred_item({"id": "test"})
        items = manager.get_deferred_items()
        items.append({"id": "extra"})
        
        # Original should be unchanged
        assert len(manager.get_deferred_items()) == 1


class TestStateManagerPersistence:
    """Tests for state persistence and file I/O."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from main.state_manager import StateManager
        StateManager._instance = None
        yield
        StateManager._instance = None

    def test_force_save_writes_to_disk(self, temp_dir, mock_config):
        """force_save explicitly saves state to disk."""
        from main.state_manager import StateManager
        
        state_file = os.path.join(temp_dir, "test_state.json")
        manager = StateManager(state_file=state_file)
        
        # Modify internal state directly (bypassing auto-save)
        manager._state["quotas"]["manual"] = {"test": True}
        manager.force_save()
        
        # Read file to verify
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        assert data["quotas"]["manual"]["test"] is True

    def test_save_includes_timestamp(self, temp_dir, mock_config):
        """Saved state includes last_updated timestamp."""
        from main.state_manager import StateManager
        
        state_file = os.path.join(temp_dir, "test_state.json")
        manager = StateManager(state_file=state_file)
        manager.set_quota("test", {})
        
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        assert "last_updated" in data
        assert data["last_updated"] is not None

    def test_handles_corrupt_state_file(self, temp_dir, mock_config):
        """Handles corrupted state file gracefully."""
        from main.state_manager import StateManager
        
        state_file = os.path.join(temp_dir, "corrupt_state.json")
        with open(state_file, "w", encoding="utf-8") as f:
            f.write("not valid json {{{")
        
        # Should not raise, should create fresh state
        manager = StateManager(state_file=state_file)
        assert manager.get_quotas() == {}


class TestGetStateManager:
    """Tests for get_state_manager helper function."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from main.state_manager import StateManager
        StateManager._instance = None
        yield
        StateManager._instance = None

    def test_get_state_manager_returns_singleton(self, mock_config):
        """get_state_manager returns singleton instance."""
        from main.state_manager import get_state_manager
        
        manager1 = get_state_manager()
        manager2 = get_state_manager()
        
        assert manager1 is manager2


class TestStateManagerThreadSafety:
    """Tests for thread-safe operations."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from main.state_manager import StateManager
        StateManager._instance = None
        yield
        StateManager._instance = None

    def test_concurrent_quota_updates(self, temp_dir, mock_config):
        """Concurrent quota updates don't corrupt state."""
        from main.state_manager import StateManager
        
        state_file = os.path.join(temp_dir, "test_state.json")
        manager = StateManager(state_file=state_file)
        
        def update_quota(provider_id):
            for i in range(10):
                manager.set_quota(f"provider_{provider_id}", {"count": i})
        
        threads = [
            threading.Thread(target=update_quota, args=(i,))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Should have 5 providers
        quotas = manager.get_quotas()
        assert len(quotas) == 5

    def test_concurrent_deferred_item_operations(self, temp_dir, mock_config):
        """Concurrent deferred item operations don't corrupt state."""
        from main.state_manager import StateManager
        
        state_file = os.path.join(temp_dir, "test_state.json")
        manager = StateManager(state_file=state_file)
        
        def add_items(thread_id):
            for i in range(5):
                manager.add_deferred_item({"id": f"t{thread_id}_i{i}"})
        
        threads = [
            threading.Thread(target=add_items, args=(i,))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Should have 20 items total
        items = manager.get_deferred_items()
        assert len(items) == 20
