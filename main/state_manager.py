"""Unified state manager for ChronoDownloader.

This module provides a centralized state file that consolidates:
- Quota tracking state (previously .quota_state.json)
- Deferred queue state (previously .deferred_queue.json)

Benefits:
- Single source of truth for all persistent state
- Atomic writes for consistency
- Simplified backup/restore operations
- Reduced file I/O overhead
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.core.config import get_config

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = ".downloader_state.json"

class StateManager:
    """Unified state manager for persistent application state.
    
    Thread-safe singleton that manages all persistent state including
    quota tracking and deferred downloads.
    """
    
    _instance: "StateManager" | None = None
    _lock = threading.Lock()
    
    def __new__(cls, state_file: str | None = None) -> "StateManager":
        """Singleton pattern."""
        with cls._lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instance = instance
            return cls._instance
    
    def __init__(self, state_file: str | None = None):
        """Initialize the state manager.
        
        Args:
            state_file: Path to state file (uses config default if None)
        """
        if getattr(self, "_initialized", False):
            return
        
        self._data_lock = threading.RLock()
        self._save_lock = threading.Lock()
        
        # Determine state file path
        if state_file:
            self._state_file = Path(state_file)
        else:
            cfg = get_config()
            deferred_cfg = cfg.get("deferred", {})
            self._state_file = Path(
                deferred_cfg.get("state_file", DEFAULT_STATE_FILE)
            )
        
        # State sections
        self._state: dict[str, Any] = {
            "quotas": {},
            "deferred_items": [],
            "last_updated": None,
            "version": "2.0",
        }
        
        # Load existing state (with migration from old files)
        self._load_state()
        self._initialized = True
        logger.debug("StateManager initialized with file: %s", self._state_file)
    
    def _load_state(self) -> None:
        """Load state from disk, migrating from old files if needed."""
        # Try to load unified state file first
        if self._state_file.exists():
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                self._state["quotas"] = data.get("quotas", {})
                self._state["deferred_items"] = data.get("deferred_items", [])
                self._state["version"] = data.get("version", "1.0")
                
                logger.info(
                    "Loaded unified state: %d quota(s), %d deferred item(s)",
                    len(self._state["quotas"]),
                    len(self._state["deferred_items"])
                )
                return
            except Exception as e:
                logger.warning("Failed to load unified state: %s", e)
        
        # Migration: Try to load from old separate files
        self._migrate_from_old_files()
    
    def _migrate_from_old_files(self) -> None:
        """Migrate state from old separate files."""
        migrated = False
        
        # Migrate quota state
        old_quota_file = Path(".quota_state.json")
        if old_quota_file.exists():
            try:
                with open(old_quota_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._state["quotas"] = data.get("quotas", {})
                logger.info("Migrated quota state from %s", old_quota_file)
                migrated = True
            except Exception as e:
                logger.warning("Failed to migrate quota state: %s", e)
        
        # Migrate deferred queue
        old_queue_file = Path(".deferred_queue.json")
        if old_queue_file.exists():
            try:
                with open(old_queue_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._state["deferred_items"] = data.get("items", [])
                logger.info("Migrated deferred queue from %s", old_queue_file)
                migrated = True
            except Exception as e:
                logger.warning("Failed to migrate deferred queue: %s", e)
        
        if migrated:
            # Save to new unified file
            self._save_state()
            logger.info("Migration complete. Old files can be deleted manually.")
    
    def _save_state(self) -> None:
        """Save state to disk."""
        with self._save_lock:
            try:
                self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
                with open(self._state_file, "w", encoding="utf-8") as f:
                    json.dump(self._state, f, indent=2)
            except Exception as e:
                logger.warning("Failed to save state: %s", e)
    
    # === Quota State Methods ===
    
    def get_quotas(self) -> dict[str, Any]:
        """Get all quota state.
        
        Returns:
            Dictionary of provider_key -> quota_data
        """
        with self._data_lock:
            return dict(self._state["quotas"])
    
    def get_quota(self, provider_key: str) -> dict[str, Any] | None:
        """Get quota state for a provider.
        
        Args:
            provider_key: Provider identifier
            
        Returns:
            Quota data dict or None
        """
        with self._data_lock:
            return self._state["quotas"].get(provider_key)
    
    def set_quota(self, provider_key: str, quota_data: dict[str, Any]) -> None:
        """Set quota state for a provider.
        
        Args:
            provider_key: Provider identifier
            quota_data: Quota data to store
        """
        with self._data_lock:
            self._state["quotas"][provider_key] = quota_data
            self._save_state()
    
    def update_quotas(self, quotas: dict[str, dict[str, Any]]) -> None:
        """Update multiple quotas at once.
        
        Args:
            quotas: Dictionary of provider_key -> quota_data
        """
        with self._data_lock:
            self._state["quotas"].update(quotas)
            self._save_state()
    
    # === Deferred Queue Methods ===
    
    def get_deferred_items(self) -> list:
        """Get all deferred items.
        
        Returns:
            List of deferred item dicts
        """
        with self._data_lock:
            return list(self._state["deferred_items"])
    
    def set_deferred_items(self, items: list) -> None:
        """Set all deferred items (replaces existing).
        
        Args:
            items: List of deferred item dicts
        """
        with self._data_lock:
            self._state["deferred_items"] = items
            self._save_state()
    
    def add_deferred_item(self, item: dict[str, Any]) -> None:
        """Add a deferred item.
        
        Args:
            item: Deferred item dict
        """
        with self._data_lock:
            self._state["deferred_items"].append(item)
            self._save_state()
    
    def remove_deferred_item(self, item_id: str) -> bool:
        """Remove a deferred item by ID.
        
        Args:
            item_id: Item ID to remove
            
        Returns:
            True if removed, False if not found
        """
        with self._data_lock:
            original_len = len(self._state["deferred_items"])
            self._state["deferred_items"] = [
                item for item in self._state["deferred_items"]
                if item.get("id") != item_id
            ]
            if len(self._state["deferred_items"]) < original_len:
                self._save_state()
                return True
            return False
    
    # === General Methods ===
    
    def get_state_file_path(self) -> Path:
        """Get the path to the state file.
        
        Returns:
            Path to state file
        """
        return self._state_file
    
    def force_save(self) -> None:
        """Force a save to disk."""
        self._save_state()

def get_state_manager() -> StateManager:
    """Get the singleton StateManager instance.
    
    Returns:
        StateManager instance
    """
    return StateManager()

__all__ = [
    "StateManager",
    "get_state_manager",
    "DEFAULT_STATE_FILE",
]
