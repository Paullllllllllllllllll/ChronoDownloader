"""Persistent download-state package.

Consolidates the four previously flat modules (state_manager, quota_manager,
deferred_queue, background_scheduler) into one deep package exposing a single
public surface:

- :class:`StateManager` / :func:`get_state_manager` - persistent JSON store
- :class:`QuotaManager` / :func:`get_quota_manager` - provider quota tracking
- :class:`DeferredQueue` / :func:`get_deferred_queue` - deferred-download queue
- :class:`BackgroundRetryScheduler` / :func:`get_background_scheduler` /
  :func:`start_background_scheduler` / :func:`stop_background_scheduler`
"""
from __future__ import annotations

from .background import (
    BackgroundRetryScheduler,
    get_background_scheduler,
    start_background_scheduler,
    stop_background_scheduler,
)
from .deferred import DeferredItem, DeferredQueue, get_deferred_queue
from .quota import ProviderQuota, QuotaManager, get_quota_manager
from .store import StateManager, get_state_manager

__all__ = [
    "StateManager",
    "get_state_manager",
    "QuotaManager",
    "ProviderQuota",
    "get_quota_manager",
    "DeferredQueue",
    "DeferredItem",
    "get_deferred_queue",
    "BackgroundRetryScheduler",
    "get_background_scheduler",
    "start_background_scheduler",
    "stop_background_scheduler",
]
