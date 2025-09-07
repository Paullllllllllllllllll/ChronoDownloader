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
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from api import utils
from api.providers import PROVIDERS
from api.model import SearchResult, convert_to_searchresult
from api.matching import (
    combined_match_score,
    normalize_text,
)

# Providers are registered centrally in api/providers.py as PROVIDERS


def load_enabled_apis(config_path: str) -> List[Tuple[str, Any, Any, str]]:
    """Load enabled providers from a JSON config file.

    Returns a list of tuples: (provider_key, search_func, download_func, provider_friendly_name)

    Config format:
    {
      "providers": { "internet_archive": true, "europeana": false, ... }
    }
    """
    logger = logging.getLogger(__name__)
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
    enabled: List[Tuple[str, Any, Any, str]] = []
    for key, flag in (cfg.get("providers") or {}).items():
        if flag and key in PROVIDERS:
            s, d, n = PROVIDERS[key]
            enabled.append((key, s, d, n))
    if not enabled:
        logging.getLogger(__name__).warning(
            "No providers enabled in config; nothing to do. Enable providers in %s under 'providers'.",
            config_path,
        )
    return enabled


# Default to Internet Archive only; this will be overridden by the CLI using this module
ENABLED_APIS: List[Tuple[str, Any, Any, str]] = [
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
    enabled: List[Tuple[str, Any, Any, str]]
) -> List[Tuple[str, Any, Any, str]]:
    """Filter out providers missing required API keys in the environment.

    - Logs a clear ERROR for each enabled provider missing a required env var.
    - Returns the subset of providers that pass the check.
    """
    req = _required_provider_envvars()
    kept: List[Tuple[str, Any, Any, str]] = []
    missing: List[Tuple[str, str, str]] = []  # (provider_key, provider_name, envvar)
    for pkey, s, d, pname in enabled:
        envvar = req.get(pkey)
        if envvar and not os.getenv(envvar):
            missing.append((pkey, pname, envvar))
            continue
        kept.append((pkey, s, d, pname))
    if missing:
        logger = logging.getLogger(__name__)
        for _pkey, pname, envvar in missing:
            logger.error(
                "Provider '%s' requires environment variable %s; it is not set. Skipping this provider for this run.",
                pname,
                envvar,
            )
        logger.error(
            "Missing required API keys for %d provider(s). See messages above.",
            len(missing),
        )
    return kept


def _get_selection_config() -> Dict[str, Any]:
    cfg = utils.get_config()
    sel = dict(cfg.get("selection", {}) or {})
    # Defaults
    sel.setdefault("strategy", "collect_and_select")  # or "sequential_first_hit"
    sel.setdefault("provider_hierarchy", [])
    sel.setdefault("min_title_score", 85)
    sel.setdefault("creator_weight", 0.2)
    sel.setdefault("year_tolerance", 2)
    sel.setdefault("max_candidates_per_provider", 5)
    sel.setdefault("download_strategy", "selected_only")  # selected_only | selected_plus_metadata | all
    sel.setdefault("keep_non_selected_metadata", True)
    return sel


def _title_slug(title: str, max_len: int = 80) -> str:
    base = "_".join([t for t in normalize_text(title).split() if t])
    if not base:
        base = "untitled"
    base = base[:max_len]
    # sanitize for filesystem
    return utils.sanitize_filename(base)


def _compute_work_id(title: str, creator: Optional[str]) -> str:
    norm = f"{normalize_text(title)}|{normalize_text(creator) if creator else ''}"
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
    return h


def _provider_order(
    enabled: List[Tuple[str, Any, Any, str]], hierarchy: List[str]
) -> List[Tuple[str, Any, Any, str]]:
    if not hierarchy:
        return enabled
    key_to_tuple = {e[0]: e for e in enabled}
    ordered = [key_to_tuple[k] for k in hierarchy if k in key_to_tuple]
    # append any enabled not explicitly listed
    for item in enabled:
        if item[0] not in hierarchy:
            ordered.append(item)
    return ordered


def _get_naming_config() -> Dict[str, Any]:
    cfg = utils.get_config()
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
    """
    logger = logging.getLogger(__name__)
    logger.info("Processing work: '%s'%s", title, f" by '{creator}'" if creator else "")

    sel_cfg = _get_selection_config()
    provider_list = _provider_order(ENABLED_APIS, sel_cfg.get("provider_hierarchy") or [])

    # Build a quick map of provider_key -> (search_func, download_func, provider_name)
    provider_map: Dict[str, Tuple[Any, Any, str]] = {pkey: (s, d, pname) for (pkey, s, d, pname) in provider_list}

    # Helper to call search functions with varying signatures robustly
    def _call_search(search_func, title: str, creator: Optional[str], max_results: int):
        try:
            return search_func(title, creator=creator, max_results=max_results)
        except TypeError:
            try:
                return search_func(title, max_results=max_results)
            except TypeError:
                try:
                    if creator is not None:
                        return search_func(title, creator=creator)
                except TypeError:
                    return search_func(title)
        except Exception:
            # Let the outer try/except handle logging
            raise

    # Gather candidates depending on strategy
    all_candidates: List[SearchResult] = []
    selected: Optional[SearchResult] = None
    selected_provider_tuple: Optional[Tuple[str, Any, Any, str]] = None

    def _score_item(sr: SearchResult) -> Dict[str, Any]:
        score = combined_match_score(
            query_title=title,
            item_title=sr.title,
            query_creator=creator,
            creators=sr.creators,
            creator_weight=float(sel_cfg.get("creator_weight", 0.2)),
            method="token_set",
        )
        # Simple quality signals
        boost = 0.0
        if sr.iiif_manifest:
            boost += 3.0
        if sr.item_url:
            boost += 0.5
        total = score + boost
        return {"score": score, "boost": boost, "total": total}

    def _max_results_for_provider(pkey: str) -> int:
        val = utils.get_provider_setting(pkey, "max_results", None)
        try:
            return int(val) if val is not None else int(sel_cfg.get("max_candidates_per_provider", 5))
        except Exception:
            return int(sel_cfg.get("max_candidates_per_provider", 5))

    min_title_score = float(sel_cfg.get("min_title_score", 85))

    def _prepare_sr(provider_key: str, provider_name: str, item: Any) -> SearchResult:
        if isinstance(item, SearchResult):
            sr = item
        else:
            # Convert legacy dicts to SearchResult for uniform handling
            sr = convert_to_searchresult(provider_name, item if isinstance(item, dict) else {})
        sr.provider_key = provider_key
        if not sr.provider:
            sr.provider = provider_name
        return sr

    def _consider(sr: SearchResult):
        # Compute and attach scores for later JSON export
        sc = _score_item(sr)
        sr.raw.setdefault("__matching__", {}).update(sc)
        all_candidates.append(sr)

    strategy = (sel_cfg.get("strategy") or "collect_and_select").lower()
    if strategy == "sequential_first_hit":
        for pkey, search_func, download_func, pname in provider_list:
            logger.info("--- Searching on %s for '%s' ---", pname, title)
            try:
                max_results = _max_results_for_provider(pkey)
                results = _call_search(search_func, title, creator, max_results)
                if not results:
                    logger.info("No items found for '%s' on %s.", title, pname)
                    continue
                logger.info("Found %d item(s) on %s", len(results), pname)
                # evaluate and pick best acceptable result from this provider
                temp: List[Tuple[float, SearchResult]] = []
                for it in results:
                    sr = _prepare_sr(pkey, pname, it)
                    _consider(sr)
                    sc = sr.raw.get("__matching__", {})
                    if sc.get("score", 0) >= min_title_score:
                        temp.append((sc.get("total", 0.0), sr))
                if temp:
                    temp.sort(key=lambda x: x[0], reverse=True)
                    selected = temp[0][1]
                    selected_provider_tuple = (pkey, search_func, download_func, pname)
                    break
            except Exception:
                logger.exception("Error during search with %s for '%s'", pname, title)
    else:
        # collect_and_select: search all providers, then choose best according to hierarchy and scores
        for pkey, search_func, download_func, pname in provider_list:
            logger.info("--- Searching on %s for '%s' ---", pname, title)
            try:
                max_results = _max_results_for_provider(pkey)
                if creator:
                    try:
                        results = search_func(title, creator=creator, max_results=max_results)
                    except TypeError:
                        results = search_func(title, max_results=max_results)
                else:
                    results = search_func(title, max_results=max_results)
                if not results:
                    logger.info("No items found for '%s' on %s.", title, pname)
                    continue
                logger.info("Found %d item(s) on %s", len(results), pname)
                for it in results:
                    sr = _prepare_sr(pkey, pname, it)
                    _consider(sr)
            except Exception:
                logger.exception("Error during search with %s for '%s'", pname, title)

        # After collection, filter and rank
        # Rank by provider priority then by total matching score
        prov_priority = {pkey: idx for idx, (pkey, *_rest) in enumerate(provider_list)}
        ranked: List[Tuple[int, float, SearchResult]] = []
        for sr in all_candidates:
            sc = sr.raw.get("__matching__", {})
            if sc.get("score", 0) < min_title_score:
                continue
            pprio = prov_priority.get(sr.provider_key or "", 9999)
            ranked.append((pprio, float(sc.get("total", 0.0)), sr))
        ranked.sort(key=lambda x: (x[0], -x[1]))
        if ranked:
            selected = ranked[0][2]
            # find its provider tuple
            for tup in provider_list:
                if tup[0] == (selected.provider_key or ""):
                    selected_provider_tuple = tup
                    break

    if not all_candidates:
        logger.info("No items found for '%s' across all enabled APIs.", title)
        try:
            utils.clear_current_work()
        except Exception:
            pass
        return

    # Build work directory and write metadata
    naming_cfg = _get_naming_config()
    work_id = _compute_work_id(title, creator)
    # New folder scheme: strictly snake_case: <entry_id>_<work_name>
    entry_slug = utils.to_snake_case(str(entry_id)) if entry_id else None
    title_slug_sc = utils.to_snake_case(str(title))
    work_stem = "_".join([p for p in [entry_slug, title_slug_sc] if p])
    work_dir_name = work_stem or _title_slug(title, max_len=int(naming_cfg.get("title_slug_max_len", 80)))
    work_dir = os.path.join(base_output_dir, work_dir_name)
    os.makedirs(work_dir, exist_ok=True)

    # Set per-work context for centralized download budgeting
    utils.set_current_work(work_id)
    # Configure naming for this work and reset per-work counters
    utils.set_current_entry(entry_id)
    utils.set_current_name_stem(work_stem)
    try:
        utils.reset_counters()
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
                    utils.set_current_provider(sr.provider_key)
                    utils.save_json(sr.to_dict(include_raw=True), work_dir, "search_result")
                finally:
                    try:
                        utils.clear_current_provider()
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
    if not dry_run and selected and selected_provider_tuple and selected_dir:
        pkey, _search_func, download_func, pname = selected_provider_tuple
        try:
            logger.info("Downloading selected item from %s into %s", pname, selected_dir)
            try:
                utils.set_current_provider(pkey)
                ok = download_func(selected, selected_dir)
            finally:
                try:
                    utils.clear_current_provider()
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
                            utils.set_current_provider(prov_key)
                            if dfunc(sr, selected_dir):
                                logger.info("Fallback download from %s succeeded.", _pname)
                                break
                        except Exception:
                            logger.exception(
                                "Fallback download error for %s:%s", prov_key, sr.source_id
                            )
                        finally:
                            try:
                                utils.clear_current_provider()
                            except Exception:
                                pass
                except Exception:
                    logger.exception("Error while attempting fallback download candidates.")
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
                            utils.set_current_provider(prov_key)
                            dfunc(sr, dest_dir)
                        finally:
                            try:
                                utils.clear_current_provider()
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
            utils.clear_current_work()
        except Exception:
            pass
        try:
            utils.clear_current_entry()
        except Exception:
            pass
        try:
            utils.clear_current_name_stem()
        except Exception:
            pass
    logger.info("Finished processing '%s'. Check '%s' for results.", title, work_dir)
