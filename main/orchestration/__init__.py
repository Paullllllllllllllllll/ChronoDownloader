"""Search-select-download orchestration package.

Consolidates the four modules that together implement the
candidate-collection, scoring, selection, and download-dispatch pipeline:

- :mod:`main.orchestration.pipeline` -- per-work orchestration
- :mod:`main.orchestration.selection` -- candidate scoring and selection
- :mod:`main.orchestration.scheduler` -- ThreadPoolExecutor-based parallel
  download scheduler (previously main.download_scheduler)
- :mod:`main.orchestration.execution` -- batch execution controller

Public surface (stable imports for CLI and UI layers):

- :func:`run_batch_downloads`, :func:`process_direct_iiif`,
  :func:`create_interactive_callbacks` (from execution)
- :func:`process_work`, :func:`search_and_select`,
  :func:`execute_download`, :func:`load_enabled_apis`,
  :func:`filter_enabled_providers_for_keys`,
  :data:`QUOTA_LIMITED_PROVIDERS` (from pipeline)
- :class:`DownloadTask`, :class:`DownloadScheduler`,
  :func:`get_parallel_download_config` (from scheduler)
- :func:`collect_candidates_all`,
  :func:`collect_candidates_sequential`,
  :func:`select_best_candidate` (from selection)
"""
from __future__ import annotations

from .execution import (
    create_interactive_callbacks,
    process_direct_iiif,
    run_batch_downloads,
)
from .pipeline import (
    QUOTA_LIMITED_PROVIDERS,
    execute_download,
    filter_enabled_providers_for_keys,
    load_enabled_apis,
    process_work,
    search_and_select,
)
from .scheduler import (
    DownloadScheduler,
    DownloadTask,
    get_parallel_download_config,
)
from .selection import (
    collect_candidates_all,
    collect_candidates_sequential,
    select_best_candidate,
)

__all__ = [
    "run_batch_downloads",
    "process_direct_iiif",
    "create_interactive_callbacks",
    "process_work",
    "search_and_select",
    "execute_download",
    "load_enabled_apis",
    "filter_enabled_providers_for_keys",
    "QUOTA_LIMITED_PROVIDERS",
    "DownloadTask",
    "DownloadScheduler",
    "get_parallel_download_config",
    "collect_candidates_all",
    "collect_candidates_sequential",
    "select_best_candidate",
]
