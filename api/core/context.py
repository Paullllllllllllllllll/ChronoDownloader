"""Thread-local context management for tracking work, entry, and provider state.

This module provides thread-safe context variables used throughout the download pipeline
to track the current work ID, entry ID, provider key, and naming stem for file generation.
"""
from __future__ import annotations

import threading

# Thread-local storage for current work context
_TLS = threading.local()


def _init_tls() -> None:
    """Initialize thread-local storage attributes if not present."""
    if not hasattr(_TLS, "work_id"):
        _TLS.work_id = None
    if not hasattr(_TLS, "entry_id"):
        _TLS.entry_id = None
    if not hasattr(_TLS, "provider_key"):
        _TLS.provider_key = None
    if not hasattr(_TLS, "name_stem"):
        _TLS.name_stem = None
    if not hasattr(_TLS, "counters"):
        _TLS.counters = {}


# Work ID context
def set_current_work(work_id: str | None) -> None:
    """Set the current work ID in thread-local storage."""
    _init_tls()
    _TLS.work_id = work_id


def get_current_work() -> str | None:
    """Get the current work ID from thread-local storage."""
    _init_tls()
    return getattr(_TLS, "work_id", None)


def clear_current_work() -> None:
    """Clear the current work ID from thread-local storage."""
    _init_tls()
    _TLS.work_id = None


# Entry ID context
def set_current_entry(entry_id: str | None) -> None:
    """Set the current entry ID in thread-local storage."""
    _init_tls()
    _TLS.entry_id = entry_id


def get_current_entry() -> str | None:
    """Get the current entry ID from thread-local storage."""
    _init_tls()
    return getattr(_TLS, "entry_id", None)


def clear_current_entry() -> None:
    """Clear the current entry ID from thread-local storage."""
    _init_tls()
    _TLS.entry_id = None


# Provider key context
def set_current_provider(provider_key: str | None) -> None:
    """Set the current provider key in thread-local storage."""
    _init_tls()
    _TLS.provider_key = provider_key


def get_current_provider() -> str | None:
    """Get the current provider key from thread-local storage."""
    _init_tls()
    return getattr(_TLS, "provider_key", None)


def clear_current_provider() -> None:
    """Clear the current provider key from thread-local storage."""
    _init_tls()
    _TLS.provider_key = None


# Name stem context
def set_current_name_stem(stem: str | None) -> None:
    """Set the current naming stem in thread-local storage."""
    _init_tls()
    _TLS.name_stem = stem


def get_current_name_stem() -> str | None:
    """Get the current naming stem from thread-local storage."""
    _init_tls()
    return getattr(_TLS, "name_stem", None)


def clear_current_name_stem() -> None:
    """Clear the current naming stem from thread-local storage."""
    _init_tls()
    _TLS.name_stem = None


# Counters for file sequencing
def get_counters() -> dict[tuple[str, str, str], int]:
    """Get the per-work file counters dictionary.
    
    Returns:
        Dictionary mapping (stem, provider_slug, type_key) to sequence number
    """
    _init_tls()
    if not hasattr(_TLS, "counters") or _TLS.counters is None:
        _TLS.counters = {}
    return _TLS.counters


def reset_counters() -> None:
    """Reset the file counters (typically called at the start of a new work)."""
    _init_tls()
    _TLS.counters = {}


def increment_counter(key: tuple[str, str, str]) -> int:
    """Increment and return the counter for a specific file type.
    
    Args:
        key: Tuple of (stem, provider_slug, type_key)
        
    Returns:
        The new counter value after incrementing
    """
    counters = get_counters()
    counters[key] = counters.get(key, 0) + 1
    return counters[key]
