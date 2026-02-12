"""Core orchestration pipeline for ChronoDownloader.

This module encapsulates the reusable logic for:
- Loading enabled providers from configuration
- Enforcing required API-key checks
- Searching across providers and selecting the best candidate per work
- Delegating actual downloads to provider-specific download_* functions

The pipeline supports both sequential and parallel download modes:
- Sequential (default): process_work() handles search and download in one call
- Parallel: search_and_select() returns a DownloadTask, execute_download() runs in workers

Quota Handling:
- QuotaDeferredException is raised by quota-limited providers (e.g., Anna's Archive)
- Pipeline catches the exception and adds work to deferred queue for later retry
- Unlimited providers (MDZ, Gallica) never raise quota exceptions, only use rate limiting

The CLI in main/downloader.py is now a thin wrapper that parses arguments
and delegates to functions in this module.

Refactored modules:
- main.work_manager: Work directory and status management
- main.index_manager: Index CSV operations
- main.deferred_queue: Deferred download tracking
- main.quota_manager: Quota state for quota-limited providers
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from main.download_scheduler import DownloadTask

from api import utils
from api.core.config import get_config, get_resume_mode
from api.core.context import (
    clear_all_context,
    clear_current_provider,
    clear_current_work,
    provider_context,
    reset_counters,
    set_current_entry,
    set_current_name_stem,
    set_current_provider,
    set_current_work,
    work_context,
)
from api.model import QuotaDeferredException, SearchResult
from api.providers import PROVIDERS
from main.deferred_queue import get_deferred_queue
from main.index_manager import build_index_row, update_index_csv
from main.selection import (
    collect_candidates_all,
    collect_candidates_sequential,
    select_best_candidate,
)
from main.work_manager import (
    check_work_status,
    compute_work_dir,
    compute_work_id,
    create_work_json,
    format_candidates_for_json,
    format_selected_for_json,
    update_work_status,
)

logger = logging.getLogger(__name__)

# Type alias for provider tuple
ProviderTuple = tuple[str, Callable, Callable, str]

@dataclasses.dataclass
class _PreparedWork:
    title: str
    creator: str | None
    entry_id: str | None
    base_output_dir: str
    work_dir: str
    work_dir_name: str
    work_stem: str
    work_id: str
    work_json_path: str
    sel_cfg: dict[str, Any]
    provider_list: list[ProviderTuple]
    provider_map: dict[str, tuple[Any, Any, str]]
    all_candidates: list[SearchResult]
    selected: SearchResult | None
    selected_provider_tuple: ProviderTuple | None
    selected_source_id: str | None

def _compute_selected_source_id(selected: SearchResult | None) -> str | None:
    if not selected:
        return None
    return selected.source_id or selected.raw.get("identifier") or selected.raw.get("ark_id")

def _collect_and_select(
    title: str,
    creator: str | None,
    sel_cfg: dict[str, Any],
    provider_list: list[ProviderTuple],
) -> tuple[list[SearchResult], SearchResult | None, ProviderTuple | None]:
    all_candidates: list[SearchResult] = []
    selected: SearchResult | None = None
    selected_provider_tuple: ProviderTuple | None = None

    min_title_score = float(sel_cfg.get("min_title_score", 85))
    creator_weight = float(sel_cfg.get("creator_weight", 0.2))
    max_candidates_per_provider = int(sel_cfg.get("max_candidates_per_provider", 5))

    strategy = (sel_cfg.get("strategy") or "collect_and_select").lower()
    if strategy == "sequential_first_hit":
        all_candidates, selected, selected_provider_tuple = collect_candidates_sequential(
            provider_list,
            title,
            creator,
            min_title_score,
            creator_weight,
            max_candidates_per_provider,
        )
    else:
        all_candidates = collect_candidates_all(
            provider_list,
            title,
            creator,
            creator_weight,
            max_candidates_per_provider,
        )
        selected, selected_provider_tuple = select_best_candidate(
            all_candidates,
            provider_list,
            min_title_score,
        )

    return all_candidates, selected, selected_provider_tuple

def _prepare_work(
    title: str,
    creator: str | None,
    entry_id: str | None,
    base_output_dir: str,
) -> _PreparedWork | None:
    work_dir, work_dir_name = compute_work_dir(base_output_dir, entry_id, title)

    resume_mode = get_resume_mode()
    should_skip, skip_reason = check_work_status(work_dir, resume_mode)
    if should_skip:
        logger.info("Skipping '%s': %s (resume_mode=%s)", title, skip_reason, resume_mode)
        return None

    sel_cfg = _get_selection_config()
    provider_list = _provider_order(ENABLED_APIS, sel_cfg.get("provider_hierarchy") or [])
    provider_map: dict[str, tuple[Any, Any, str]] = {pkey: (s, d, pname) for (pkey, s, d, pname) in provider_list}

    all_candidates, selected, selected_provider_tuple = _collect_and_select(
        title,
        creator,
        sel_cfg,
        provider_list,
    )

    work_id = compute_work_id(title, creator)
    work_json_path = os.path.join(work_dir, "work.json")
    selected_source_id = _compute_selected_source_id(selected if selected and selected_provider_tuple else None)

    return _PreparedWork(
        title=title,
        creator=creator,
        entry_id=entry_id,
        base_output_dir=base_output_dir,
        work_dir=work_dir,
        work_dir_name=work_dir_name,
        work_stem=work_dir_name,
        work_id=work_id,
        work_json_path=work_json_path,
        sel_cfg=sel_cfg,
        provider_list=provider_list,
        provider_map=provider_map,
        all_candidates=all_candidates,
        selected=selected,
        selected_provider_tuple=selected_provider_tuple,
        selected_source_id=selected_source_id,
    )

def _persist_work_json(prep: _PreparedWork, status: str = "pending") -> None:
    create_work_json(
        work_json_path=prep.work_json_path,
        title=prep.title,
        creator=prep.creator,
        entry_id=prep.entry_id,
        selection_config=prep.sel_cfg,
        candidates=format_candidates_for_json(prep.all_candidates),
        selected=format_selected_for_json(prep.selected, prep.selected_source_id),
        status=status,
    )

def _persist_candidates_metadata(prep: _PreparedWork) -> None:
    if not prep.sel_cfg.get("keep_non_selected_metadata", True):
        return
    with work_context(work_id=prep.work_id, entry_id=prep.entry_id, name_stem=prep.work_stem):
        for sr in prep.all_candidates:
            if not sr.provider_key:
                continue
            try:
                with provider_context(sr.provider_key):
                    utils.save_json(sr.to_dict(include_raw=True), prep.work_dir, "search_result")
            except Exception:
                logger.exception(
                    "Failed to persist candidate metadata for %s:%s",
                    sr.provider_key,
                    sr.source_id,
                )

def load_enabled_apis(config_path: str) -> list[ProviderTuple]:
    """Load enabled providers from a JSON config file.

    Args:
        config_path: Path to configuration JSON file

    Returns:
        List of tuples: (provider_key, search_func, download_func, provider_friendly_name)

    Config format:
        {
          "providers": { "internet_archive": true, "europeana": false, ... }
        }
    """
    if not os.path.exists(config_path):
        logger.info(
            "Config file %s not found; using default providers: Internet Archive only",
            config_path,
        )
        s, d, n = PROVIDERS["internet_archive"]
        return [("internet_archive", s, d, n)]
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logger.error(
            "Failed to read config file %s: %s; using default providers (Internet Archive only)",
            config_path,
            e,
        )
        s, d, n = PROVIDERS["internet_archive"]
        return [("internet_archive", s, d, n)]
    
    enabled: list[ProviderTuple] = []
    for key, flag in (cfg.get("providers") or {}).items():
        if flag and key in PROVIDERS:
            s, d, n = PROVIDERS[key]
            enabled.append((key, s, d, n))
    
    if not enabled:
        logger.warning(
            "No providers enabled in config; nothing to do. Enable providers in %s under 'providers'.",
            config_path,
        )
    
    return enabled

# Default to Internet Archive only; this will be overridden by the CLI using this module
ENABLED_APIS: list[ProviderTuple] = [
    ("internet_archive",) + PROVIDERS["internet_archive"],
]

def _required_provider_envvars() -> dict[str, str]:
    """Return mapping of provider_key -> required environment variable name for API keys.

    Only providers listed here will be treated as requiring keys; others work without keys.
    """
    return {
        "europeana": "EUROPEANA_API_KEY",
        "dpla": "DPLA_API_KEY",
        "ddb": "DDB_API_KEY",
        "google_books": "GOOGLE_BOOKS_API_KEY",
        # HathiTrust key is optional and not required for search (HATHI_API_KEY)
    }

def filter_enabled_providers_for_keys(
    enabled: list[ProviderTuple]
) -> list[ProviderTuple]:
    """Filter out providers missing required API keys in the environment.

    Args:
        enabled: List of enabled provider tuples

    Returns:
        Filtered list with only providers that have required keys set
    """
    req = _required_provider_envvars()
    kept: list[ProviderTuple] = []
    missing: list[tuple[str, str, str]] = []  # (provider_key, provider_name, envvar)
    
    for pkey, s, d, pname in enabled:
        envvar = req.get(pkey)
        if envvar and not os.getenv(envvar):
            missing.append((pkey, pname, envvar))
            continue
        kept.append((pkey, s, d, pname))
    
    if missing:
        for _pkey, pname, envvar in missing:
            logger.warning(
                "Provider '%s' requires environment variable %s; it is not set. Skipping this provider for this run.",
                pname,
                envvar,
            )
        logger.warning(
            "Missing required API keys for %d provider(s). See messages above.",
            len(missing),
        )
    
    return kept

def _get_selection_config() -> dict[str, Any]:
    """Get selection configuration with defaults.
    
    Returns:
        Dictionary with selection strategy, thresholds, and download preferences
    """
    cfg = get_config()
    sel = dict(cfg.get("selection", {}) or {})
    
    # Defaults
    sel.setdefault("strategy", "collect_and_select")  # or "sequential_first_hit"
    sel.setdefault("max_parallel_searches", 1)  # 1 = sequential, >1 = parallel provider searches
    sel.setdefault("provider_hierarchy", [])
    sel.setdefault("min_title_score", 85)
    sel.setdefault("creator_weight", 0.2)
    sel.setdefault("year_tolerance", 2)
    sel.setdefault("max_candidates_per_provider", 5)
    sel.setdefault("download_strategy", "selected_only")
    sel.setdefault("keep_non_selected_metadata", True)
    
    return sel

def _provider_order(
    enabled: list[ProviderTuple], hierarchy: list[str]
) -> list[ProviderTuple]:
    """Reorder providers according to hierarchy preference.
    
    Args:
        enabled: List of enabled provider tuples
        hierarchy: Ordered list of preferred provider keys
        
    Returns:
        Reordered provider list with hierarchy-specified providers first
    """
    if not hierarchy:
        return enabled
    
    key_to_tuple = {e[0]: e for e in enabled}
    ordered = [key_to_tuple[k] for k in hierarchy if k in key_to_tuple]
    
    # Append any enabled not explicitly listed
    for item in enabled:
        if item[0] not in hierarchy:
            ordered.append(item)
    
    return ordered

def search_and_select(
    title: str,
    creator: str | None = None,
    entry_id: str | None = None,
    base_output_dir: str = "downloaded_works",
) -> "DownloadTask" | None:
    """Phase 1: Search providers and select best candidate.
    
    This function performs the search phase and returns a DownloadTask
    that can be executed by a worker thread. It creates the work directory
    and persists work.json but does NOT perform the actual download.
    
    Args:
        title: Work title to search for
        creator: Optional creator/author name
        entry_id: Optional unique identifier for this work
        base_output_dir: Base directory for downloaded works
        
    Returns:
        DownloadTask if a candidate was found, None otherwise.
    """
    from main.download_scheduler import DownloadTask
    
    logger.info("Searching for work: '%s'%s", title, f" by '{creator}'" if creator else "")

    prep = _prepare_work(title, creator, entry_id, base_output_dir)
    if prep is None:
        return None

    if not prep.all_candidates:
        logger.info("No items found for '%s' across all enabled APIs.", title)
        return None

    os.makedirs(prep.work_dir, exist_ok=True)
    _persist_work_json(prep, status="pending")
    _persist_candidates_metadata(prep)

    if not prep.selected or not prep.selected_provider_tuple:
        update_work_status(prep.work_json_path, "no_match")
        logger.info("No matching candidate found for '%s'.", title)
        return None

    task = DownloadTask(
        work_id=prep.work_id,
        entry_id=entry_id,
        title=title,
        creator=creator,
        work_dir=prep.work_dir,
        work_stem=prep.work_stem,
        selected_result=prep.selected,
        provider_key=prep.selected_provider_tuple[0],
        provider_tuple=prep.selected_provider_tuple,
        work_json_path=prep.work_json_path,
        all_candidates=prep.all_candidates,
        provider_map=prep.provider_map,
        selection_config=prep.sel_cfg,
        base_output_dir=base_output_dir,
    )

    logger.info("Created download task for '%s' from %s", title, prep.selected_provider_tuple[3])
    return task

def execute_download(task: "DownloadTask", dry_run: bool = False) -> bool:
    """Phase 2: Execute download for a task.
    
    This function is designed to be called by worker threads. Thread-local
    context should be set by the scheduler before calling this function.
    
    Args:
        task: DownloadTask from search_and_select()
        dry_run: If True, skip actual downloads
        
    Returns:
        True if download succeeded, False otherwise.
    """
    if dry_run:
        logger.info("Dry-run: skipping download for '%s'", task.title)
        return True
    
    pkey, _search_func, download_func, pname = task.provider_tuple
    selected = task.selected_result
    work_dir = task.work_dir
    work_json_path = task.work_json_path
    sel_cfg = task.selection_config
    provider_map = task.provider_map
    all_candidates = task.all_candidates
    
    download_deferred = False
    download_succeeded = False
    selected_source_id = selected.source_id or selected.raw.get("identifier") or selected.raw.get("ark_id")
    
    try:
        logger.info("Downloading selected item for '%s' from %s into %s", task.title, pname, work_dir)
        try:
            set_current_provider(pkey)
            ok = download_func(selected, work_dir)
        finally:
            try:
                clear_current_provider()
            except Exception:
                pass
        
        if ok:
            download_succeeded = True
        else:
            logger.warning(
                "Download function reported failure for %s:%s",
                pkey,
                selected.source_id,
            )
            # Fallback: try the next-best candidate based on provider priority and score
            try:
                provider_list = ENABLED_APIS
                prov_priority = {k: idx for idx, (k, *_r) in enumerate(provider_list)}
                ranked_fallbacks: list[tuple[int, float, SearchResult]] = []
                for sr in all_candidates:
                    try:
                        if sr.provider_key == pkey and sr.source_id == selected.source_id:
                            continue
                        sc = sr.raw.get("__matching__", {})
                        if float(sc.get("score", 0.0)) < float(sel_cfg.get("min_title_score", 85)):
                            continue
                        pprio = prov_priority.get(sr.provider_key or "", 9999)
                        ranked_fallbacks.append((pprio, float(sc.get("total", 0.0)), sr))
                    except Exception:
                        continue
                ranked_fallbacks.sort(key=lambda x: (x[0], -x[1]))
                for _pprio, _tot, sr in ranked_fallbacks:
                    prov_key = sr.provider_key or "unknown"
                    if prov_key not in provider_map:
                        continue
                    _s, dfunc, _pname = provider_map[prov_key]
                    logger.info("Attempting fallback download from %s", _pname)
                    try:
                        set_current_provider(prov_key)
                        if dfunc(sr, work_dir):
                            logger.info("Fallback download from %s succeeded.", _pname)
                            download_succeeded = True
                            break
                    except QuotaDeferredException as qde:
                        logger.info(
                            "Fallback provider %s quota exhausted: %s", _pname, qde.message
                        )
                        continue
                    except Exception:
                        logger.exception(
                            "Fallback download error for %s:%s", prov_key, sr.source_id
                        )
                    finally:
                        try:
                            clear_current_provider()
                        except Exception:
                            pass
            except Exception:
                logger.exception("Error while attempting fallback download candidates.")
    except QuotaDeferredException as qde:
        # Quota exhausted for selected provider - add to persistent deferred queue
        download_deferred = True
        logger.info("Download deferred for '%s': %s", task.title, qde.message)
        queue = get_deferred_queue()
        queue.add(
            title=task.title,
            creator=task.creator,
            entry_id=task.entry_id,
            provider_key=pkey,
            provider_name=pname,
            source_id=selected_source_id,
            work_dir=work_dir,
            base_output_dir=task.base_output_dir,
            item_url=selected.item_url,
            reset_time=qde.reset_time,
            raw_data=selected.raw,
        )
    except Exception:
        logger.exception("Error during download for %s:%s", pkey, selected.source_id)
    
    # If 'all', also download other candidates
    download_strategy = (sel_cfg.get("download_strategy") or "selected_only").lower()
    if download_strategy == "all":
        for sr in all_candidates:
            try:
                if sr.provider_key == pkey and sr.source_id == selected.source_id:
                    continue
                prov_key = sr.provider_key or "unknown"
                if prov_key in provider_map:
                    _s, dfunc, pname2 = provider_map[prov_key]
                    logger.info(
                        "Downloading additional candidate from %s into %s",
                        pname2,
                        work_dir,
                    )
                    try:
                        set_current_provider(prov_key)
                        dfunc(sr, work_dir)
                    finally:
                        try:
                            clear_current_provider()
                        except Exception:
                            pass
            except Exception:
                logger.exception(
                    "Failed to download additional candidate %s:%s",
                    sr.provider_key,
                    sr.source_id,
                )

    # Update work.json status based on outcome
    if download_succeeded:
        update_work_status(work_json_path, "completed", {
            "provider": selected.provider,
            "provider_key": selected.provider_key,
            "source_id": selected_source_id,
        })
    elif download_deferred:
        update_work_status(work_json_path, "deferred")
    else:
        update_work_status(work_json_path, "failed")

    # Update index.csv summary (thread-safe)
    final_status = "completed" if download_succeeded else ("deferred" if download_deferred else "failed")
    row = build_index_row(
        work_id=task.work_id,
        entry_id=task.entry_id,
        work_dir=work_dir,
        title=task.title,
        creator=task.creator,
        selected=selected,
        selected_source_id=selected_source_id,
        work_json_path=work_json_path,
        status=final_status,
        item_url=selected.item_url,
    )
    update_index_csv(task.base_output_dir, row)

    try:
        task.status = final_status
        task.item_url = selected.item_url
        task.provider = selected.provider
    except Exception:
        pass

    if download_succeeded:
        logger.info("Download completed for '%s'. Check '%s' for results.", task.title, work_dir)
    else:
        logger.info("Download %s for '%s'.", "deferred" if download_deferred else "failed", task.title)
    
    return download_succeeded

def process_work(
    title: str,
    creator: str | None = None,
    entry_id: str | None = None,
    base_output_dir: str = "downloaded_works",
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Search, select, persist metadata, and download one work.

    This function orchestrates provider searches and the download phase for a
    single input work (title/creator), writing a per-work folder under
    `base_output_dir` and adding an entry to `index.csv`.
    
    Args:
        title: Work title to search for
        creator: Optional creator/author name
        entry_id: Optional unique identifier for this work
        base_output_dir: Base directory for downloaded works
        dry_run: If True, skip actual downloads
        
    Returns:
        Dict with 'status', 'item_url', 'provider' on success/failure,
        None if skipped or deferred
    """
    logger = logging.getLogger(__name__)
    logger.info("Processing work: '%s'%s", title, f" by '{creator}'" if creator else "")

    prep = _prepare_work(title, creator, entry_id, base_output_dir)
    if prep is None:
        return None

    if not prep.all_candidates:
        logger.info("No items found for '%s' across all enabled APIs.", title)
        try:
            clear_current_work()
        except Exception:
            pass
        return {"status": "failed", "item_url": "", "provider": "", "reason": "no_candidates"}

    os.makedirs(prep.work_dir, exist_ok=True)

    set_current_work(prep.work_id)
    set_current_entry(entry_id)
    set_current_name_stem(prep.work_stem)
    try:
        reset_counters()
    except Exception:
        pass

    selected_dir = prep.work_dir if (prep.selected and prep.selected_provider_tuple) else None
    selected = prep.selected
    _persist_work_json(prep, status="pending")
    if prep.sel_cfg.get("keep_non_selected_metadata", True):
        for sr in prep.all_candidates:
            if not sr.provider_key:
                continue
            try:
                with provider_context(sr.provider_key):
                    utils.save_json(sr.to_dict(include_raw=True), prep.work_dir, "search_result")
            except Exception:
                logger.exception(
                    "Failed to persist candidate metadata for %s:%s",
                    sr.provider_key,
                    sr.source_id,
                )

    # Download according to strategy
    download_strategy = (prep.sel_cfg.get("download_strategy") or "selected_only").lower()
    download_deferred = False  # Track if download was deferred due to quota
    download_succeeded = False  # Track if download completed successfully
    if not dry_run and prep.selected and prep.selected_provider_tuple and selected_dir:
        selected = prep.selected
        selected_provider_tuple = prep.selected_provider_tuple
        provider_list = prep.provider_list
        provider_map = prep.provider_map
        sel_cfg = prep.sel_cfg
        all_candidates = prep.all_candidates
        work_dir = prep.work_dir
        work_json_path = prep.work_json_path
        selected_source_id = prep.selected_source_id

        pkey, _search_func, download_func, pname = selected_provider_tuple
        try:
            logger.info("Downloading selected item from %s into %s", pname, selected_dir)
            try:
                set_current_provider(pkey)
                ok = download_func(selected, selected_dir)
            finally:
                try:
                    clear_current_provider()
                except Exception:
                    pass
            if ok:
                download_succeeded = True
            if not ok:
                logger.warning(
                    "Download function reported failure for %s:%s",
                    pkey,
                    selected.source_id,
                )
                # Fallback: try the next-best candidate based on provider priority and score
                try:
                    prov_priority = {k: idx for idx, (k, *_r) in enumerate(provider_list)}
                    ranked_fallbacks: list[tuple[int, float, SearchResult]] = []
                    for sr in all_candidates:
                        try:
                            if selected and sr.provider_key == selected.provider_key and sr.source_id == selected.source_id:
                                continue
                            sc = sr.raw.get("__matching__", {})
                            if float(sc.get("score", 0.0)) < float(sel_cfg.get("min_title_score", 85)):
                                continue
                            pprio = prov_priority.get(sr.provider_key or "", 9999)
                            ranked_fallbacks.append((pprio, float(sc.get("total", 0.0)), sr))
                        except Exception:
                            continue
                    ranked_fallbacks.sort(key=lambda x: (x[0], -x[1]))
                    for _pprio, _tot, sr in ranked_fallbacks:
                        prov_key = sr.provider_key or "unknown"
                        if prov_key not in provider_map:
                            continue
                        _s, dfunc, _pname = provider_map[prov_key]
                        logger.info("Attempting fallback download from %s", _pname)
                        try:
                            set_current_provider(prov_key)
                            if dfunc(sr, selected_dir):
                                logger.info("Fallback download from %s succeeded.", _pname)
                                download_succeeded = True
                                break
                        except QuotaDeferredException as qde:
                            logger.info(
                                "Fallback provider %s quota exhausted: %s", _pname, qde.message
                            )
                            continue  # Try next fallback provider
                        except Exception:
                            logger.exception(
                                "Fallback download error for %s:%s", prov_key, sr.source_id
                            )
                        finally:
                            try:
                                clear_current_provider()
                            except Exception:
                                pass
                except Exception:
                    logger.exception("Error while attempting fallback download candidates.")
        except QuotaDeferredException as qde:
            # Quota exhausted for selected provider - add to persistent deferred queue
            download_deferred = True
            logger.info("Download deferred for '%s': %s", title, qde.message)
            queue = get_deferred_queue()
            queue.add(
                title=title,
                creator=creator,
                entry_id=entry_id,
                provider_key=pkey,
                provider_name=pname,
                source_id=selected_source_id,
                work_dir=work_dir,
                base_output_dir=base_output_dir,
                item_url=selected.item_url if selected else None,
                reset_time=qde.reset_time,
                raw_data=selected.raw if selected else {},
            )
        except Exception:
            logger.exception("Error during download for %s:%s", pkey, selected.source_id)
        # If 'all', also download other candidates into sources/ subfolders
        if download_strategy == "all":
            for sr in all_candidates:
                try:
                    if selected and sr.provider_key == selected.provider_key and sr.source_id == selected.source_id:
                        continue
                    prov_key = sr.provider_key or "unknown"
                    dest_dir = work_dir
                    if prov_key in provider_map:
                        _s, dfunc, pname2 = provider_map[prov_key]
                        logger.info(
                            "Downloading additional candidate from %s into %s",
                            pname2,
                            dest_dir,
                        )
                        try:
                            set_current_provider(prov_key)
                            dfunc(sr, dest_dir)
                        finally:
                            try:
                                clear_current_provider()
                            except Exception:
                                pass
                except Exception:
                    logger.exception(
                        "Failed to download additional candidate %s:%s",
                        sr.provider_key,
                        sr.source_id,
                    )
    elif dry_run:
        logger.info("Dry-run: skipping download for selected item.")

    # Update work.json status based on outcome
    if not dry_run:
        if download_succeeded:
            update_work_status(prep.work_json_path, "completed", {
                "provider": selected.provider if selected else None,
                "provider_key": selected.provider_key if selected else None,
                "source_id": prep.selected_source_id,
            })
        elif download_deferred:
            update_work_status(prep.work_json_path, "deferred")
        elif selected:
            update_work_status(prep.work_json_path, "failed")
        else:
            update_work_status(prep.work_json_path, "no_match")

    # Update index.csv summary (thread-safe)
    final_status = None
    if not dry_run:
        if download_succeeded:
            final_status = "completed"
        elif download_deferred:
            final_status = "deferred"
        elif selected:
            final_status = "failed"
        else:
            final_status = "no_match"
    row = build_index_row(
        work_id=prep.work_id,
        entry_id=entry_id,
        work_dir=prep.work_dir,
        title=title,
        creator=creator,
        selected=prep.selected,
        selected_source_id=prep.selected_source_id,
        work_json_path=prep.work_json_path,
        status=final_status,
        item_url=(prep.selected.item_url if prep.selected else None),
    )
    update_index_csv(base_output_dir, row)

    # Clear per-work context
    clear_all_context()
    
    logger.info("Finished processing '%s'. Check '%s' for results.", title, prep.work_dir)
    
    # Return result for unified CSV updates
    if dry_run:
        return {
            "status": "dry_run",
            "item_url": selected.item_url if selected else "",
            "provider": selected.provider if selected else "",
        }
    if download_succeeded:
        return {
            "status": "completed",
            "item_url": selected.item_url if selected else "",
            "provider": selected.provider if selected else "",
        }
    elif download_deferred:
        return None  # Deferred - don't update CSV yet
    elif selected:
        return {"status": "failed", "item_url": "", "provider": selected.provider, "reason": "download_failed"}
    else:
        return {"status": "failed", "item_url": "", "provider": "", "reason": "no_selection"}
