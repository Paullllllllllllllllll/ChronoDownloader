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

import contextlib
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from api.core.atomic import atomic_write_json
from api.core.config import get_config

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = ".downloader_state.json"

# User-level default state directory (decision 4). A CWD-relative state file
# made quota counters silently reset whenever the tool ran from a different
# directory; the user-level location is stable across working directories.
DEFAULT_STATE_DIR = Path.home() / ".chronodownloader"


def resolve_state_file_path() -> Path:
    """Resolve the state-file path: config override, else user-level default.

    Resolution order:
    1. ``deferred.state_file`` in config (absolute or relative path) -- full
       override, kept for backward compatibility when explicitly set.
    2. ``deferred.state_dir`` in config -- directory override; the standard
       filename is appended.
    3. ``~/.chronodownloader/.downloader_state.json`` (default). When the
       user-level file does not exist but a legacy CWD file does, the legacy
       file is adopted (copied) once.
    """
    try:
        cfg = get_config()
    except Exception:
        cfg = {}
    deferred_cfg = cfg.get("deferred", {}) if isinstance(cfg, dict) else {}

    explicit_file = deferred_cfg.get("state_file")
    if explicit_file:
        return Path(explicit_file)

    state_dir_override = deferred_cfg.get("state_dir")
    state_dir = Path(state_dir_override) if state_dir_override else DEFAULT_STATE_DIR

    target = state_dir / DEFAULT_STATE_FILE

    # One-time adoption of a legacy CWD state file.
    legacy = Path(DEFAULT_STATE_FILE)
    if not target.exists() and legacy.exists():
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copy2(legacy, target)
            logger.info(
                "Adopted legacy state file %s into user-level location %s",
                legacy.resolve(),
                target,
            )
        except Exception as e:
            logger.warning("Failed to adopt legacy state file: %s", e)
            return legacy

    return target


class StateManager:
    """Unified state manager for persistent application state.

    Thread-safe singleton that manages all persistent state including
    quota tracking and deferred downloads.
    """

    _instance: StateManager | None = None
    _lock = threading.Lock()

    # Instance attribute declarations (assigned under the class lock in
    # __init__; declared here so mypy can type them across the early-return
    # branch).
    _initialized: bool
    _state_file: Path
    _save_disabled: bool

    def __new__(cls, state_file: str | None = None) -> StateManager:
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
        # The class lock serializes first initialization: without it a second
        # thread constructing the singleton while the first is still inside
        # __init__ would observe _initialized=False and re-run the body,
        # discarding loaded state and replacing the locks mid-use.
        with self.__class__._lock:
            if getattr(self, "_initialized", False):
                if state_file and Path(state_file) != self._state_file:
                    logger.warning(
                        "StateManager already initialized with %s; ignoring "
                        "differing state_file %s",
                        self._state_file,
                        state_file,
                    )
                return

            try:
                self._data_lock = threading.RLock()
                self._save_lock = threading.Lock()
                # When the state file could not be read for reasons other than
                # corruption (e.g. a transient Windows share violation), saves
                # are disabled so the intact on-disk state is never overwritten
                # with the empty in-memory default.
                self._save_disabled = False

                # Determine state file path (config override or user-level
                # default, with one-time legacy CWD adoption).
                if state_file:
                    self._state_file = Path(state_file)
                else:
                    self._state_file = resolve_state_file_path()

                with contextlib.suppress(OSError):
                    self._state_file.parent.mkdir(parents=True, exist_ok=True)

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
            except BaseException:
                # Do not leave a half-built singleton behind: a later
                # construction attempt must start fresh.
                type(self)._instance = None
                raise
        logger.debug("StateManager initialized with file: %s", self._state_file)

    def _read_state_file(self) -> dict[str, Any]:
        """Read and parse the state file, retrying transient OS errors.

        On Windows a transiently open state file (AV scanner, file viewer)
        raises PermissionError; a short bounded retry mirrors
        ``api.core.atomic._atomic_replace`` so a momentary lock is not
        misdiagnosed as corruption.
        """
        import time

        attempts = 5
        for attempt in range(attempts):
            try:
                with open(self._state_file, encoding="utf-8") as f:
                    return cast(dict[str, Any], json.load(f))
            except OSError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.1 * (attempt + 1))
        raise OSError("unreachable")  # pragma: no cover

    def _load_state(self) -> None:
        """Load state from disk, migrating from old files if needed."""
        # Try to load unified state file first
        if self._state_file.exists():
            try:
                data = self._read_state_file()
            except OSError as e:
                # The file exists but cannot be read (permissions, transient
                # lock). This is NOT corruption: keep the on-disk file intact
                # by disabling saves for this process rather than overwriting
                # it later with the empty in-memory default.
                self._save_disabled = True
                logger.error(
                    "Cannot read state file %s (%s); state persistence is "
                    "disabled for this run to protect the existing file.",
                    self._state_file,
                    e,
                )
                return
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
                # Preserve the corrupt file for inspection instead of
                # silently resetting quota counters and the deferred queue.
                corrupt_path = self._state_file.with_suffix(".corrupt")
                try:
                    import shutil

                    shutil.copy2(self._state_file, corrupt_path)
                    logger.error(
                        "State file %s is corrupt (%s); preserved a copy at "
                        "%s and starting with fresh state.",
                        self._state_file,
                        e,
                        corrupt_path,
                    )
                except Exception:
                    logger.error(
                        "State file %s is corrupt (%s); starting with fresh state.",
                        self._state_file,
                        e,
                    )
                # Deliberately no fall-through to legacy migration: stale
                # legacy files must not replace the (corrupt but preserved)
                # unified state.
                return
            else:
                self._state["quotas"] = data.get("quotas", {})
                self._state["deferred_items"] = data.get("deferred_items", [])
                self._state["version"] = data.get("version", "1.0")

                logger.info(
                    "Loaded unified state: %d quota(s), %d deferred item(s)",
                    len(self._state["quotas"]),
                    len(self._state["deferred_items"]),
                )
                return

        # Migration: Try to load from old separate files
        self._migrate_from_old_files()

    def _migrate_from_old_files(self) -> None:
        """Migrate state from old separate files."""
        migrated = False

        # Migrate quota state
        old_quota_file = Path(".quota_state.json")
        if old_quota_file.exists():
            try:
                with open(old_quota_file, encoding="utf-8") as f:
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
                with open(old_queue_file, encoding="utf-8") as f:
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
        """Save state to disk atomically (temp file + os.replace).

        The data lock is held while serializing so a concurrent mutator (or a
        ``force_save`` from another thread) cannot change ``self._state``
        mid-``json.dumps`` (which would raise and silently drop the save).
        """
        if self._save_disabled:
            logger.debug("State persistence disabled; skipping save.")
            return
        with self._data_lock, self._save_lock:
            try:
                self._state["last_updated"] = datetime.now(UTC).isoformat()
                atomic_write_json(str(self._state_file), self._state)
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
            return cast(dict[str, Any] | None, self._state["quotas"].get(provider_key))

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

    def get_deferred_items(self) -> list[Any]:
        """Get all deferred items.

        Returns:
            List of deferred item dicts
        """
        with self._data_lock:
            return list(self._state["deferred_items"])

    def set_deferred_items(self, items: list[Any]) -> None:
        """Set all deferred items (replaces existing).

        Args:
            items: List of deferred item dicts
        """
        with self._data_lock:
            self._state["deferred_items"] = items
            self._save_state()

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
    "resolve_state_file_path",
    "DEFAULT_STATE_FILE",
    "DEFAULT_STATE_DIR",
]
