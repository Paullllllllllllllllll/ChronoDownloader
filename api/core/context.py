"""Thread-local context management for tracking work, entry, and provider state.

This module provides thread-safe context variables used throughout the download pipeline
to track the current work ID, entry ID, provider key, and naming stem for file generation.

Key features:
- Thread-local storage for per-work context
- Context manager for automatic setup/cleanup
- File sequencing counters for standardized naming
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Generator

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


def clear_all_context() -> None:
    """Clear all thread-local context variables.
    
    This is a convenience function that clears work, entry, provider,
    and name_stem contexts in a single call, suppressing any errors.
    """
    for clear_fn in (clear_current_work, clear_current_entry, clear_current_provider, clear_current_name_stem):
        try:
            clear_fn()
        except Exception:
            pass


@contextmanager
def work_context(
    work_id: str | None = None,
    entry_id: str | None = None,
    provider_key: str | None = None,
    name_stem: str | None = None,
) -> Generator[None, None, None]:
    """Context manager for setting up and tearing down work context.
    
    Automatically sets the provided context values on entry and clears
    all context on exit, even if an exception occurs.
    
    Args:
        work_id: Optional work ID to set
        entry_id: Optional entry ID to set
        provider_key: Optional provider key to set
        name_stem: Optional naming stem to set
        
    Yields:
        None
        
    Example:
        with work_context(work_id="abc123", entry_id="E0001", name_stem="my_work"):
            # Do work here - context is automatically managed
            pass
        # Context is automatically cleared here
    """
    try:
        if work_id is not None:
            set_current_work(work_id)
        if entry_id is not None:
            set_current_entry(entry_id)
        if provider_key is not None:
            set_current_provider(provider_key)
        if name_stem is not None:
            set_current_name_stem(name_stem)
        reset_counters()
        yield
    finally:
        clear_all_context()


@contextmanager
def provider_context(provider_key: str | None) -> Generator[None, None, None]:
    """Context manager for setting provider context only.
    
    Useful for wrapping download operations where only provider context is needed.
    
    Args:
        provider_key: Provider key to set
        
    Yields:
        None
    """
    try:
        if provider_key is not None:
            set_current_provider(provider_key)
        yield
    finally:
        try:
            clear_current_provider()
        except Exception:
            pass
