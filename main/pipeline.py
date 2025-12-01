"""Core orchestration pipeline for ChronoDownloader.

This module encapsulates the reusable logic for:
- Loading enabled providers from configuration
- Enforcing required API-key checks
- Searching across providers and selecting the best candidate per work
- Creating per-work directories and persisting selection metadata
- Delegating actual downloads to provider-specific download_* functions

The CLI in main/downloader.py is now a thin wrapper that parses arguments
and delegates to functions in this module. Import and use:

    from main import pipeline
    providers = pipeline.load_enabled_apis(config_path)
    providers = pipeline.filter_enabled_providers_for_keys(providers)
    pipeline.ENABLED_APIS = providers
    pipeline.process_work(...)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from api import utils
from api.core.config import get_config
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

# Deferred downloads tracking (works that need retry when quota resets)
# Structure: List of dicts with keys: title, creator, entry_id, base_output_dir, selected, provider_tuple, work_dir, reset_time
_deferred_downloads: List[dict] = []


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

    # Build work directory and write metadata
    naming_cfg = _get_naming_config()
    work_id = _compute_work_id(title, creator)
    
    # Build standardized work directory name
    work_dir_name = build_work_directory_name(
        entry_id,
        title,
        max_len=int(naming_cfg.get("title_slug_max_len", 80))
    )
    work_dir = os.path.join(base_output_dir, work_dir_name)
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
            _deferred_downloads.append({
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

    # Update index.csv summary
    try:
        index_path = os.path.join(base_output_dir, "index.csv")
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
        df = pd.DataFrame([row])
        header = not os.path.exists(index_path)
        df.to_csv(index_path, mode="a", header=header, index=False)
    except Exception:
        logger.exception("Failed to update index.csv")

    finally:
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
    
    Returns:
        List of deferred download info dicts
    """
    return list(_deferred_downloads)


def clear_deferred_downloads() -> None:
    """Clear the deferred downloads list."""
    _deferred_downloads.clear()


def process_deferred_downloads(wait_for_reset: bool = True) -> int:
    """Process downloads that were deferred due to quota exhaustion.
    
    This function should be called after processing all works to retry
    downloads that were deferred because a provider's quota was exhausted.
    
    Args:
        wait_for_reset: If True, wait for the quota reset time before retrying.
                       If False, only retry items whose reset time has passed.
    
    Returns:
        Number of successfully processed deferred downloads
    """
    import time
    
    if not _deferred_downloads:
        logger.info("No deferred downloads to process.")
        return 0
    
    logger.info("Processing %d deferred download(s)...", len(_deferred_downloads))
    
    # Find the earliest reset time
    earliest_reset = None
    for item in _deferred_downloads:
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
                logger.info("Deferred downloads: %.1f hours remaining until quota reset...", remaining / 3600)
                time.sleep(sleep_time)
                remaining -= sleep_time
            logger.info("Quota reset time reached. Retrying deferred downloads...")
    
    # Process deferred items
    processed = 0
    failed = []
    
    for item in list(_deferred_downloads):
        title = item.get("title")
        selected = item.get("selected")
        provider_tuple = item.get("provider_tuple")
        work_dir = item.get("work_dir")
        
        if not selected or not provider_tuple or not work_dir:
            logger.warning("Incomplete deferred download info for '%s', skipping.", title)
            continue
        
        pkey, _search_func, download_func, pname = provider_tuple
        logger.info("Retrying deferred download for '%s' from %s", title, pname)
        
        try:
            set_current_provider(pkey)
            ok = download_func(selected, work_dir)
            if ok:
                logger.info("Deferred download succeeded for '%s'", title)
                processed += 1
                _deferred_downloads.remove(item)
            else:
                logger.warning("Deferred download still failed for '%s'", title)
                failed.append(item)
        except QuotaDeferredException as qde:
            logger.warning(
                "Deferred download for '%s' still quota-limited: %s",
                title, qde.message
            )
            # Update reset time
            item["reset_time"] = qde.reset_time
            failed.append(item)
        except Exception:
            logger.exception("Error retrying deferred download for '%s'", title)
            failed.append(item)
        finally:
            try:
                clear_current_provider()
            except Exception:
                pass
    
    if failed:
        logger.info(
            "Deferred downloads: %d succeeded, %d still pending.",
            processed, len(failed)
        )
    else:
        logger.info("All %d deferred downloads completed successfully.", processed)
    
    return processed
