"""Core orchestration pipeline for ChronoDownloader.

This module encapsulates the reusable logic for:
- Loading enabled providers from configuration
- Enforcing required API-key checks
- Searching across providers and selecting the best candidate per work
- Creating per-work directories and persisting selection metadata
- Delegating actual downloads to provider-specific download_* functions

The pipeline supports both sequential and parallel download modes:
- Sequential (default): process_work() handles search and download in one call
- Parallel: search_and_select() returns a DownloadTask, execute_download() runs in workers

The CLI in main/downloader.py is now a thin wrapper that parses arguments
and delegates to functions in this module. Import and use:

    from main import pipeline
    providers = pipeline.load_enabled_apis(config_path)
    providers = pipeline.filter_enabled_providers_for_keys(providers)
    pipeline.ENABLED_APIS = providers
    
    # Sequential mode:
    pipeline.process_work(...)
    
    # Parallel mode:
    task = pipeline.search_and_select(...)
    if task:
        success = pipeline.execute_download(task)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from main.download_scheduler import DownloadTask

from api import utils
from api.core.config import get_config, get_resume_mode
from api.core.context import (
    clear_current_entry,
    clear_current_name_stem,
    clear_current_provider,
    clear_current_work,
    reset_counters,
    set_current_entry,
    set_current_name_stem,
    set_current_provider,
    set_current_work,
)
from api.core.naming import build_work_directory_name, to_snake_case
from api.matching import normalize_text
from api.model import QuotaDeferredException, SearchResult
from api.providers import PROVIDERS
from main.selection import (
    collect_candidates_all,
    collect_candidates_sequential,
    select_best_candidate,
)

logger = logging.getLogger(__name__)

# Type alias for provider tuple
ProviderTuple = Tuple[str, Callable, Callable, str]

# Thread-safe deferred downloads tracking (works that need retry when quota resets)
# Structure: List of dicts with keys: title, creator, entry_id, base_output_dir, selected, provider_tuple, work_dir, reset_time
_deferred_downloads: List[dict] = []
_deferred_downloads_lock = threading.Lock()

# Thread-safe index.csv updates
_index_csv_lock = threading.Lock()


def load_enabled_apis(config_path: str) -> List[ProviderTuple]:
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
    
    enabled: List[ProviderTuple] = []
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
ENABLED_APIS: List[ProviderTuple] = [
    ("internet_archive",) + PROVIDERS["internet_archive"],
]


def _required_provider_envvars() -> Dict[str, str]:
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
    enabled: List[ProviderTuple]
) -> List[ProviderTuple]:
    """Filter out providers missing required API keys in the environment.

    Args:
        enabled: List of enabled provider tuples

    Returns:
        Filtered list with only providers that have required keys set
    """
    req = _required_provider_envvars()
    kept: List[ProviderTuple] = []
    missing: List[Tuple[str, str, str]] = []  # (provider_key, provider_name, envvar)
    
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


def _get_selection_config() -> Dict[str, Any]:
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


def _title_slug(title: str, max_len: int = 80) -> str:
    """Generate a URL-safe slug from a title.
    
    Args:
        title: Work title to slugify
        max_len: Maximum length of slug
        
    Returns:
        Sanitized filename-safe slug
    """
    base = "_".join([t for t in normalize_text(title).split() if t])
    if not base:
        base = "untitled"
    base = base[:max_len]
    return utils.sanitize_filename(base)


def _compute_work_id(title: str, creator: Optional[str]) -> str:
    """Generate a stable hash-based work ID from title and creator.
    
    Args:
        title: Work title
        creator: Optional creator name
        
    Returns:
        10-character hex hash identifier
    """
    norm = f"{normalize_text(title)}|{normalize_text(creator) if creator else ''}"
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
    return h


def _provider_order(
    enabled: List[ProviderTuple], hierarchy: List[str]
) -> List[ProviderTuple]:
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


def _get_naming_config() -> Dict[str, Any]:
    """Get naming configuration with defaults.
    
    Returns:
        Dictionary with naming preferences for work directories and files
    """
    cfg = get_config()
    nm = dict(cfg.get("naming", {}) or {})
    
    nm.setdefault("include_creator_in_work_dir", True)
    nm.setdefault("include_year_in_work_dir", True)
    nm.setdefault("title_slug_max_len", 80)
    
    return nm


def check_work_status(work_dir: str, resume_mode: str) -> Tuple[bool, str]:
    """Check if a work should be skipped based on resume mode and existing state.
    
    Args:
        work_dir: Path to the work directory
        resume_mode: Resume mode from config ("skip_completed", "reprocess_all", "skip_if_has_objects")
        
    Returns:
        Tuple of (should_skip, reason). If should_skip is True, the work should be skipped.
    """
    if resume_mode == "reprocess_all":
        return False, ""
    
    if not os.path.isdir(work_dir):
        return False, ""
    
    work_json_path = os.path.join(work_dir, "work.json")
    objects_dir = os.path.join(work_dir, "objects")
    
    if resume_mode == "skip_completed":
        # Check work.json for status
        if os.path.exists(work_json_path):
            try:
                with open(work_json_path, "r", encoding="utf-8") as f:
                    work_meta = json.load(f)
                status = work_meta.get("status", "")
                if status == "completed":
                    return True, "status=completed in work.json"
            except Exception:
                pass
    
    elif resume_mode == "skip_if_has_objects":
        # Check if objects directory has any files
        if os.path.isdir(objects_dir):
            try:
                files = [f for f in os.listdir(objects_dir) if os.path.isfile(os.path.join(objects_dir, f))]
                if files:
                    return True, f"objects/ contains {len(files)} file(s)"
            except Exception:
                pass
    
    return False, ""


def update_work_status(work_json_path: str, status: str, download_info: Optional[Dict[str, Any]] = None) -> None:
    """Update the status field in work.json.
    
    Args:
        work_json_path: Path to work.json file
        status: New status ("completed", "partial", "failed", "no_match")
        download_info: Optional dict with download details to merge
    """
    if not os.path.exists(work_json_path):
        return
    
    try:
        with open(work_json_path, "r", encoding="utf-8") as f:
            work_meta = json.load(f)
        
        work_meta["status"] = status
        work_meta["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        
        if download_info:
            work_meta["download"] = download_info
        
        with open(work_json_path, "w", encoding="utf-8") as f:
            json.dump(work_meta, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to update work.json status: %s", e)


def _update_index_csv(base_output_dir: str, row: Dict[str, Any]) -> None:
    """Thread-safe update of index.csv.
    
    Args:
        base_output_dir: Base output directory containing index.csv
        row: Dictionary of row data to append
    """
    with _index_csv_lock:
        try:
            index_path = os.path.join(base_output_dir, "index.csv")
            df = pd.DataFrame([row])
            header = not os.path.exists(index_path)
            df.to_csv(index_path, mode="a", header=header, index=False)
        except Exception:
            logger.exception("Failed to update index.csv")


def _add_deferred_download(item: dict) -> None:
    """Thread-safe addition to deferred downloads list.
    
    Args:
        item: Deferred download info dict
    """
    with _deferred_downloads_lock:
        _deferred_downloads.append(item)


def search_and_select(
    title: str,
    creator: Optional[str] = None,
    entry_id: Optional[str] = None,
    base_output_dir: str = "downloaded_works",
) -> Optional["DownloadTask"]:
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

    # Compute work directory early to enable resume/skip check
    naming_cfg = _get_naming_config()
    work_dir_name = build_work_directory_name(
        entry_id,
        title,
        max_len=int(naming_cfg.get("title_slug_max_len", 80))
    )
    work_dir = os.path.join(base_output_dir, work_dir_name)
    
    # Check if this work should be skipped based on resume mode
    resume_mode = get_resume_mode()
    should_skip, skip_reason = check_work_status(work_dir, resume_mode)
    if should_skip:
        logger.info("Skipping '%s': %s (resume_mode=%s)", title, skip_reason, resume_mode)
        return None

    sel_cfg = _get_selection_config()
    provider_list = _provider_order(ENABLED_APIS, sel_cfg.get("provider_hierarchy") or [])

    # Build a quick map of provider_key -> (search_func, download_func, provider_name)
    provider_map: Dict[str, Tuple[Any, Any, str]] = {pkey: (s, d, pname) for (pkey, s, d, pname) in provider_list}

    # Gather candidates depending on strategy
    all_candidates: List[SearchResult] = []
    selected: Optional[SearchResult] = None
    selected_provider_tuple: Optional[ProviderTuple] = None

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
        # collect_and_select: search all providers, then choose best
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

    if not all_candidates:
        logger.info("No items found for '%s' across all enabled APIs.", title)
        return None

    # Create work directory
    work_id = _compute_work_id(title, creator)
    os.makedirs(work_dir, exist_ok=True)
    work_stem = work_dir_name

    selected_source_id = None
    if selected and selected_provider_tuple:
        selected_source_id = selected.source_id or selected.raw.get("identifier") or selected.raw.get("ark_id")

    # Persist work.json summarizing decision
    work_json_path = os.path.join(work_dir, "work.json")
    work_meta: Dict[str, Any] = {
        "input": {"title": title, "creator": creator, "entry_id": entry_id},
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "pending",  # Will be updated by execute_download
        "selection": sel_cfg,
        "candidates": [
            {
                "provider": sr.provider,
                "provider_key": sr.provider_key,
                "title": sr.title,
                "creators": sr.creators,
                "date": sr.date,
                "source_id": sr.source_id,
                "item_url": sr.item_url,
                "iiif_manifest": sr.iiif_manifest,
                "scores": sr.raw.get("__matching__", {}),
            }
            for sr in all_candidates
        ],
        "selected": (
            {
                "provider": selected.provider if selected else None,
                "provider_key": selected.provider_key if selected else None,
                "source_id": selected_source_id,
                "title": selected.title if selected else None,
            }
            if selected
            else None
        ),
    }
    try:
        with open(work_json_path, "w", encoding="utf-8") as f:
            json.dump(work_meta, f, indent=2, ensure_ascii=False)
    except Exception:
        logger.exception("Failed to write work.json to %s", work_json_path)

    # Optionally persist non-selected metadata
    if sel_cfg.get("keep_non_selected_metadata", True):
        for sr in all_candidates:
            try:
                if not sr.provider_key:
                    continue
                try:
                    set_current_provider(sr.provider_key)
                    utils.save_json(sr.to_dict(include_raw=True), work_dir, "search_result")
                finally:
                    try:
                        clear_current_provider()
                    except Exception:
                        pass
            except Exception:
                logger.exception(
                    "Failed to persist candidate metadata for %s:%s",
                    sr.provider_key,
                    sr.source_id,
                )

    # If no selected candidate, update status and return None
    if not selected or not selected_provider_tuple:
        update_work_status(work_json_path, "no_match")
        logger.info("No matching candidate found for '%s'.", title)
        return None

    # Create and return DownloadTask
    task = DownloadTask(
        work_id=work_id,
        entry_id=entry_id,
        title=title,
        creator=creator,
        work_dir=work_dir,
        work_stem=work_stem,
        selected_result=selected,
        provider_key=selected_provider_tuple[0],
        provider_tuple=selected_provider_tuple,
        work_json_path=work_json_path,
        all_candidates=all_candidates,
        provider_map=provider_map,
        selection_config=sel_cfg,
        base_output_dir=base_output_dir,
    )
    
    logger.info("Created download task for '%s' from %s", title, selected_provider_tuple[3])
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
                ranked_fallbacks: List[Tuple[int, float, SearchResult]] = []
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
        # Quota exhausted for selected provider - defer for later retry
        download_deferred = True
        logger.info("Download deferred for '%s': %s", task.title, qde.message)
        _add_deferred_download({
            "title": task.title,
            "creator": task.creator,
            "entry_id": task.entry_id,
            "base_output_dir": task.base_output_dir,
            "selected": selected,
            "provider_tuple": task.provider_tuple,
            "work_dir": work_dir,
            "reset_time": qde.reset_time,
            "provider": qde.provider,
        })
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
    work_id = task.work_id
    row = {
        "work_id": work_id,
        "entry_id": task.entry_id,
        "work_dir": work_dir,
        "title": task.title,
        "creator": task.creator,
        "selected_provider": selected.provider,
        "selected_provider_key": selected.provider_key,
        "selected_source_id": selected_source_id,
        "selected_dir": work_dir,
        "work_json": work_json_path,
        "status": "completed" if download_succeeded else ("deferred" if download_deferred else "failed"),
    }
    _update_index_csv(task.base_output_dir, row)

    if download_succeeded:
        logger.info("Download completed for '%s'. Check '%s' for results.", task.title, work_dir)
    else:
        logger.info("Download %s for '%s'.", "deferred" if download_deferred else "failed", task.title)
    
    return download_succeeded


def process_work(
    title: str,
    creator: Optional[str] = None,
    entry_id: Optional[str] = None,
    base_output_dir: str = "downloaded_works",
    dry_run: bool = False,
) -> None:
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
    """
    logger = logging.getLogger(__name__)
    logger.info("Processing work: '%s'%s", title, f" by '{creator}'" if creator else "")

    # Compute work directory early to enable resume/skip check
    naming_cfg = _get_naming_config()
    work_dir_name = build_work_directory_name(
        entry_id,
        title,
        max_len=int(naming_cfg.get("title_slug_max_len", 80))
    )
    work_dir = os.path.join(base_output_dir, work_dir_name)
    
    # Check if this work should be skipped based on resume mode
    resume_mode = get_resume_mode()
    should_skip, skip_reason = check_work_status(work_dir, resume_mode)
    if should_skip:
        logger.info("Skipping '%s': %s (resume_mode=%s)", title, skip_reason, resume_mode)
        return

    sel_cfg = _get_selection_config()
    provider_list = _provider_order(ENABLED_APIS, sel_cfg.get("provider_hierarchy") or [])

    # Build a quick map of provider_key -> (search_func, download_func, provider_name)
    provider_map: Dict[str, Tuple[Any, Any, str]] = {pkey: (s, d, pname) for (pkey, s, d, pname) in provider_list}

    # Gather candidates depending on strategy
    all_candidates: List[SearchResult] = []
    selected: Optional[SearchResult] = None
    selected_provider_tuple: Optional[ProviderTuple] = None

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
        # collect_and_select: search all providers, then choose best
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

    if not all_candidates:
        logger.info("No items found for '%s' across all enabled APIs.", title)
        try:
            clear_current_work()
        except Exception:
            pass
        return

    # Create work directory (work_dir_name computed earlier for resume check)
    work_id = _compute_work_id(title, creator)
    os.makedirs(work_dir, exist_ok=True)
    
    # Use work_dir_name as the naming stem for files
    work_stem = work_dir_name

    # Set per-work context for centralized download budgeting
    set_current_work(work_id)
    # Configure naming for this work and reset per-work counters
    set_current_entry(entry_id)
    set_current_name_stem(work_stem)
    try:
        reset_counters()
    except Exception:
        pass

    selected_dir = None
    selected_source_id = None
    if selected and selected_provider_tuple:
        selected_source_id = selected.source_id or selected.raw.get("identifier") or selected.raw.get("ark_id")
        # For the new structure we pass the parent directory; utils will route to 'objects/'
        selected_dir = work_dir

    # Persist work.json summarizing decision
    work_json_path = os.path.join(work_dir, "work.json")
    work_meta: Dict[str, Any] = {
        "input": {"title": title, "creator": creator, "entry_id": entry_id},
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "selection": sel_cfg,
        "candidates": [
            {
                "provider": sr.provider,
                "provider_key": sr.provider_key,
                "title": sr.title,
                "creators": sr.creators,
                "date": sr.date,
                "source_id": sr.source_id,
                "item_url": sr.item_url,
                "iiif_manifest": sr.iiif_manifest,
                "scores": sr.raw.get("__matching__", {}),
            }
            for sr in all_candidates
        ],
        "selected": (
            {
                "provider": selected.provider if selected else None,
                "provider_key": selected.provider_key if selected else None,
                "source_id": selected_source_id,
                "title": selected.title if selected else None,
            }
            if selected
            else None
        ),
    }
    try:
        with open(work_json_path, "w", encoding="utf-8") as f:
            json.dump(work_meta, f, indent=2, ensure_ascii=False)
    except Exception:
        logger.exception("Failed to write work.json to %s", work_json_path)

    # Optionally persist non-selected metadata (SearchResult dict) for auditing into metadata/
    if sel_cfg.get("keep_non_selected_metadata", True):
        for sr in all_candidates:
            try:
                if not sr.provider_key:
                    continue
                try:
                    set_current_provider(sr.provider_key)
                    utils.save_json(sr.to_dict(include_raw=True), work_dir, "search_result")
                finally:
                    try:
                        clear_current_provider()
                    except Exception:
                        pass
            except Exception:
                logger.exception(
                    "Failed to persist candidate metadata for %s:%s",
                    sr.provider_key,
                    sr.source_id,
                )

    # Download according to strategy
    download_strategy = (sel_cfg.get("download_strategy") or "selected_only").lower()
    download_deferred = False  # Track if download was deferred due to quota
    download_succeeded = False  # Track if download completed successfully
    if not dry_run and selected and selected_provider_tuple and selected_dir:
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
                    ranked_fallbacks: List[Tuple[int, float, SearchResult]] = []
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
            # Quota exhausted for selected provider - defer for later retry
            download_deferred = True
            logger.info("Download deferred for '%s': %s", title, qde.message)
            _add_deferred_download({
                "title": title,
                "creator": creator,
                "entry_id": entry_id,
                "base_output_dir": base_output_dir,
                "selected": selected,
                "provider_tuple": selected_provider_tuple,
                "work_dir": work_dir,
                "reset_time": qde.reset_time,
                "provider": qde.provider,
            })
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
            update_work_status(work_json_path, "completed", {
                "provider": selected.provider if selected else None,
                "provider_key": selected.provider_key if selected else None,
                "source_id": selected_source_id,
            })
        elif download_deferred:
            update_work_status(work_json_path, "deferred")
        elif selected:
            update_work_status(work_json_path, "failed")
        else:
            update_work_status(work_json_path, "no_match")

    # Update index.csv summary (thread-safe)
    row = {
        "work_id": work_id,
        "entry_id": entry_id,
        "work_dir": work_dir,
        "title": title,
        "creator": creator,
        "selected_provider": selected.provider if selected else None,
        "selected_provider_key": selected.provider_key if selected else None,
        "selected_source_id": selected_source_id,
        "selected_dir": selected_dir,
        "work_json": work_json_path,
    }
    _update_index_csv(base_output_dir, row)

    # Clear per-work context
    try:
        clear_current_work()
    except Exception:
        pass
    try:
        clear_current_entry()
    except Exception:
        pass
    try:
        clear_current_name_stem()
    except Exception:
        pass
    
    logger.info("Finished processing '%s'. Check '%s' for results.", title, work_dir)


def get_deferred_downloads() -> List[dict]:
    """Get the list of deferred downloads waiting for quota reset.
    
    Thread-safe.
    
    Returns:
        List of deferred download info dicts (copy)
    """
    with _deferred_downloads_lock:
        return list(_deferred_downloads)


def clear_deferred_downloads() -> None:
    """Clear the deferred downloads list.
    
    Thread-safe.
    """
    with _deferred_downloads_lock:
        _deferred_downloads.clear()


def process_deferred_downloads(wait_for_reset: bool = True) -> int:
    """Process downloads that were deferred due to quota exhaustion.
    
    This function loops until all deferred downloads are complete (or permanently
    failed). After each pass, if quota-limited items remain, it waits for the
    quota reset time before retrying.
    
    Thread-safe.
    
    Args:
        wait_for_reset: If True, wait for the quota reset time before retrying.
                       If False, only retry items whose reset time has passed.
    
    Returns:
        Number of successfully processed deferred downloads
    """
    import time
    from api.core.config import get_provider_setting
    
    total_processed = 0
    pass_number = 0
    permanent_failures: set = set()  # Track items that failed for non-quota reasons
    
    while True:
        pass_number += 1
        
        # Get current deferred downloads (excluding permanent failures)
        with _deferred_downloads_lock:
            pending = [
                item for item in _deferred_downloads
                if id(item) not in permanent_failures
            ]
            if not pending:
                if pass_number == 1:
                    logger.info("No deferred downloads to process.")
                break
        
        logger.info(
            "Deferred downloads pass %d: %d item(s) to process...",
            pass_number, len(pending)
        )
        
        # Find the earliest reset time among pending items
        earliest_reset = None
        for item in pending:
            reset_time = item.get("reset_time")
            if reset_time and (earliest_reset is None or reset_time < earliest_reset):
                earliest_reset = reset_time
        
        # Wait for quota reset if needed
        if wait_for_reset and earliest_reset:
            now = datetime.now(timezone.utc)
            # Handle naive datetime from Anna's Archive
            if earliest_reset.tzinfo is None:
                from datetime import timezone as tz
                earliest_reset = earliest_reset.replace(tzinfo=tz.utc)
            
            wait_seconds = (earliest_reset - now).total_seconds()
            if wait_seconds > 0:
                wait_hours = wait_seconds / 3600
                logger.info(
                    "Waiting %.1f hours for quota reset before processing deferred downloads...",
                    wait_hours
                )
                # Sleep in chunks and log progress
                remaining = wait_seconds
                while remaining > 0:
                    sleep_time = min(remaining, 3600)  # Sleep max 1 hour at a time
                    logger.info(
                        "Deferred downloads: %.1f hours remaining until quota reset...",
                        remaining / 3600
                    )
                    time.sleep(sleep_time)
                    remaining -= sleep_time
                logger.info("Quota reset time reached. Retrying deferred downloads...")
        
        # Process pending items in this pass
        pass_processed = 0
        quota_limited = []
        
        for item in pending:
            title = item.get("title")
            selected = item.get("selected")
            provider_tuple = item.get("provider_tuple")
            work_dir = item.get("work_dir")
            
            if not selected or not provider_tuple or not work_dir:
                logger.warning("Incomplete deferred download info for '%s', skipping.", title)
                permanent_failures.add(id(item))
                continue
            
            pkey, _search_func, download_func, pname = provider_tuple
            logger.info("Retrying deferred download for '%s' from %s", title, pname)
            
            try:
                set_current_provider(pkey)
                ok = download_func(selected, work_dir)
                if ok:
                    logger.info("Deferred download succeeded for '%s'", title)
                    pass_processed += 1
                    total_processed += 1
                    # Remove from deferred list (thread-safe)
                    with _deferred_downloads_lock:
                        try:
                            _deferred_downloads.remove(item)
                        except ValueError:
                            pass  # Already removed
                else:
                    logger.warning("Deferred download failed for '%s' (non-quota reason)", title)
                    permanent_failures.add(id(item))
            except QuotaDeferredException as qde:
                logger.info(
                    "Deferred download for '%s' quota-limited: %s",
                    title, qde.message
                )
                # Update reset time for next pass
                item["reset_time"] = qde.reset_time
                quota_limited.append(item)
            except Exception:
                logger.exception("Error retrying deferred download for '%s'", title)
                permanent_failures.add(id(item))
            finally:
                try:
                    clear_current_provider()
                except Exception:
                    pass
        
        logger.info(
            "Pass %d complete: %d succeeded, %d quota-limited, %d permanently failed",
            pass_number, pass_processed, len(quota_limited), len(permanent_failures)
        )
        
        # If no quota-limited items remain, we're done
        if not quota_limited:
            break
        
        # Check if we should continue waiting for quota resets
        if not wait_for_reset:
            logger.info(
                "wait_for_reset=False; %d quota-limited items will remain deferred.",
                len(quota_limited)
            )
            break
    
    # Final summary
    with _deferred_downloads_lock:
        remaining = len(_deferred_downloads)
    
    if remaining > 0:
        logger.info(
            "Deferred downloads complete: %d succeeded, %d still pending.",
            total_processed, remaining
        )
    else:
        logger.info("All deferred downloads processed: %d succeeded.", total_processed)
    
    return total_processed
