import os
import argparse
import logging
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from api import utils
from api.providers import PROVIDERS
from api.model import SearchResult, convert_to_searchresult
from api.matching import (
    combined_match_score,
    parse_year,
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
        logger.info("Config file %s not found; using default providers: Internet Archive only", config_path)
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


# Default to Internet Archive only; this will be overridden by --config if present
ENABLED_APIS: List[Tuple[str, Any, Any, str]] = [
    ("internet_archive",) + PROVIDERS["internet_archive"],
]


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


def _provider_order(enabled: List[Tuple[str, Any, Any, str]], hierarchy: List[str]) -> List[Tuple[str, Any, Any, str]]:
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


def process_work(title, creator: Optional[str] = None, base_output_dir: str = "downloaded_works", dry_run: bool = False):
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
    title_slug = _title_slug(title, max_len=int(naming_cfg.get("title_slug_max_len", 80)))
    parts = [work_id, title_slug]
    if naming_cfg.get("include_creator_in_work_dir", True) and creator:
        parts.append(_title_slug(creator, max_len=40))
    if naming_cfg.get("include_year_in_work_dir", True) and selected and selected.date:
        y = parse_year(selected.date)
        if y:
            parts.append(str(y))
    work_dir_name = "_".join([p for p in parts if p])
    work_dir = os.path.join(base_output_dir, work_dir_name)
    os.makedirs(work_dir, exist_ok=True)

    # Set per-work context for centralized download budgeting
    utils.set_current_work(work_id)

    selected_dir = None
    selected_provider_name = None
    selected_source_id = None
    if selected and selected_provider_tuple:
        selected_provider_name = selected.provider or selected_provider_tuple[3]
        selected_source_id = selected.source_id or selected.raw.get("identifier") or selected.raw.get("ark_id")
        selected_dir = os.path.join(work_dir, "selected", selected.provider_key or "unknown")
        os.makedirs(selected_dir, exist_ok=True)

    # Persist work.json summarizing decision
    work_json_path = os.path.join(work_dir, "work.json")
    work_meta: Dict[str, Any] = {
        "input": {"title": title, "creator": creator},
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

    # Optionally persist non-selected metadata (SearchResult dict) for auditing
    if sel_cfg.get("keep_non_selected_metadata", True):
        for sr in all_candidates:
            try:
                prov_dir = os.path.join(work_dir, "sources", sr.provider_key or "unknown", sr.source_id or "unknown")
                os.makedirs(prov_dir, exist_ok=True)
                meta_path = os.path.join(prov_dir, "search_result.json")
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(sr.to_dict(include_raw=True), f, indent=2, ensure_ascii=False)
            except Exception:
                logger.exception("Failed to persist candidate metadata for %s:%s", sr.provider_key, sr.source_id)

    # Download according to strategy
    download_strategy = (sel_cfg.get("download_strategy") or "selected_only").lower()
    if not dry_run and selected and selected_provider_tuple and selected_dir:
        pkey, _search_func, download_func, pname = selected_provider_tuple
        try:
            logger.info("Downloading selected item from %s into %s", pname, selected_dir)
            ok = download_func(selected, selected_dir)
            if not ok:
                logger.warning("Download function reported failure for %s:%s", pkey, selected.source_id)
        except Exception:
            logger.exception("Error during download for %s:%s", pkey, selected.source_id)
        # If 'all', also download other candidates into sources/ subfolders
        if download_strategy == "all":
            for sr in all_candidates:
                try:
                    if selected and sr.provider_key == selected.provider_key and sr.source_id == selected.source_id:
                        continue
                    prov_key = sr.provider_key or "unknown"
                    dest_dir = os.path.join(work_dir, "sources", prov_key, sr.source_id or "unknown")
                    os.makedirs(dest_dir, exist_ok=True)
                    if prov_key in provider_map:
                        _s, dfunc, pname2 = provider_map[prov_key]
                        logger.info("Downloading additional candidate from %s into %s", pname2, dest_dir)
                        dfunc(sr, dest_dir)
                except Exception:
                    logger.exception("Failed to download additional candidate %s:%s", sr.provider_key, sr.source_id)
    elif dry_run:
        logger.info("Dry-run: skipping download for selected item.")

    # Update index.csv summary
    try:
        index_path = os.path.join(base_output_dir, "index.csv")
        row = {
            "work_id": work_id,
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
    logger.info("Finished processing '%s'. Check '%s' for results.", title, work_dir)


def main():
    parser = argparse.ArgumentParser(description="Download historical sources from various digital libraries.")
    parser.add_argument("csv_file", help="Path to the CSV file containing works to download. Must have a 'Title' column. Optional 'Creator' column.")
    parser.add_argument("--output_dir", default="downloaded_works", help="Directory to save downloaded files.")
    parser.add_argument("--dry-run", action="store_true", help="Run searches and create folders, but skip downloads.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to JSON config file to enable/disable providers.",
    )
    args = parser.parse_args()

    # Configure base logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    logger = logging.getLogger(__name__)

    # Ensure utils.get_config() reads the same config path
    try:
        os.environ["CHRONO_CONFIG_PATH"] = args.config
    except Exception:
        pass

    # Load providers from config (if exists), otherwise defaults remain (IA only)
    global ENABLED_APIS
    ENABLED_APIS = load_enabled_apis(args.config)
    if not ENABLED_APIS:
        logger.warning("No providers are enabled. Update %s to enable providers.", args.config)

    if not os.path.exists(args.csv_file):
        logger.error("CSV file not found at %s", args.csv_file)
        return
    try:
        works_df = pd.read_csv(args.csv_file)
    except Exception as e:
        logger.error("Error reading CSV file: %s", e)
        return
    if "Title" not in works_df.columns:
        logger.error("CSV file must contain a 'Title' column.")
        return
    logger.info("Starting downloader. Output directory: %s", args.output_dir)
    for index, row in works_df.iterrows():
        title = row["Title"]
        creator = row.get("Creator")
        if pd.isna(title) or not str(title).strip():
            logger.warning("Skipping row %d due to missing or empty title.", index + 1)
            continue
        process_work(str(title), None if pd.isna(creator) else str(creator), args.output_dir, dry_run=args.dry_run)
        logger.info("%s", "-" * 50)
        # Stop early if the global download budget has been exhausted
        try:
            if utils.budget_exhausted():
                logger.warning("Download budget exhausted; stopping further processing.")
                break
        except Exception:
            pass
    logger.info("All works processed.")


if __name__ == "__main__":
    main()
