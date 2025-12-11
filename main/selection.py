"""Candidate selection and scoring logic for ChronoDownloader.

This module extracts the candidate collection, scoring, and ranking logic
from the main pipeline to improve modularity and testability.

Supports parallel provider searches via ThreadPoolExecutor when
selection.max_parallel_searches > 1 in config.
"""
from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from api.core.config import get_config, get_provider_setting, get_min_title_score
from api.matching import combined_match_score
from api.model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

# Type alias for provider tuple
ProviderTuple = Tuple[str, Callable, Callable, str]


def call_search_function(
    search_func: Callable,
    title: str,
    creator: Optional[str],
    max_results: int
) -> List[Any]:
    """Call a search function with varying signatures robustly.
    
    Args:
        search_func: Provider search function
        title: Work title
        creator: Optional creator name
        max_results: Maximum results to return
        
    Returns:
        List of search results from the provider
    """
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
        raise


def prepare_search_result(
    provider_key: str,
    provider_name: str,
    item: Any
) -> SearchResult:
    """Convert provider-specific result to SearchResult.
    
    Args:
        provider_key: Provider key (e.g., 'internet_archive')
        provider_name: Provider display name (e.g., 'Internet Archive')
        item: Raw provider result (SearchResult or dict)
        
    Returns:
        Normalized SearchResult instance
    """
    if isinstance(item, SearchResult):
        sr = item
    else:
        sr = convert_to_searchresult(
            provider_name,
            item if isinstance(item, dict) else {}
        )
    
    sr.provider_key = provider_key
    if not sr.provider:
        sr.provider = provider_name
    
    return sr


def score_candidate(
    sr: SearchResult,
    query_title: str,
    query_creator: Optional[str],
    creator_weight: float
) -> Dict[str, Any]:
    """Compute matching score for a search result candidate.
    
    Args:
        sr: SearchResult to score
        query_title: Query title
        query_creator: Optional query creator
        creator_weight: Weight for creator matching (0.0-1.0)
        
    Returns:
        Dictionary with 'score', 'boost', and 'total' keys
    """
    score = combined_match_score(
        query_title=query_title,
        item_title=sr.title,
        query_creator=query_creator,
        creators=sr.creators,
        creator_weight=float(creator_weight),
        method="token_set",
    )
    
    # Quality signals boost
    boost = 0.0
    if sr.iiif_manifest:
        boost += 3.0
    if sr.item_url:
        boost += 0.5
    
    total = score + boost
    
    return {"score": score, "boost": boost, "total": total}


def attach_scores(
    sr: SearchResult,
    query_title: str,
    query_creator: Optional[str],
    creator_weight: float
) -> None:
    """Compute and attach matching scores to a SearchResult's raw dict.
    
    Args:
        sr: SearchResult to score
        query_title: Query title
        query_creator: Optional query creator
        creator_weight: Weight for creator matching
    """
    scores = score_candidate(sr, query_title, query_creator, creator_weight)
    sr.raw.setdefault("__matching__", {}).update(scores)


def get_max_results_for_provider(provider_key: str, default: int = 5) -> int:
    """Get max_results setting for a provider.
    
    Args:
        provider_key: Provider identifier
        default: Default value if not configured
        
    Returns:
        Maximum number of results to request
    """
    val = get_provider_setting(provider_key, "max_results", None)
    try:
        return int(val) if val is not None else default
    except Exception:
        return default


def collect_candidates_sequential(
    provider_list: List[ProviderTuple],
    title: str,
    creator: Optional[str],
    min_title_score: float,
    creator_weight: float,
    max_candidates_per_provider: int
) -> Tuple[List[SearchResult], Optional[SearchResult], Optional[ProviderTuple]]:
    """Collect candidates using sequential first-hit strategy.
    
    Searches providers in order and stops at the first acceptable match.
    Uses per-provider min_title_score thresholds when configured.
    
    Args:
        provider_list: Ordered list of provider tuples
        title: Work title
        creator: Optional creator name
        min_title_score: Global minimum score threshold (used as fallback)
        creator_weight: Weight for creator matching
        max_candidates_per_provider: Max results per provider
        
    Returns:
        Tuple of (all_candidates, selected, selected_provider_tuple)
    """
    all_candidates: List[SearchResult] = []
    selected: Optional[SearchResult] = None
    selected_provider_tuple: Optional[ProviderTuple] = None
    
    for pkey, search_func, download_func, pname in provider_list:
        logger.info("--- Searching on %s for '%s' ---", pname, title)
        try:
            max_results = get_max_results_for_provider(pkey, max_candidates_per_provider)
            results = call_search_function(search_func, title, creator, max_results)
            
            if not results:
                logger.info("No items found for '%s' on %s.", title, pname)
                continue
            
            logger.info("Found %d item(s) on %s", len(results), pname)
            
            # Get per-provider threshold, falling back to global
            provider_threshold = get_min_title_score(pkey, default=min_title_score)
            
            # Score and collect candidates
            temp: List[Tuple[float, SearchResult]] = []
            for it in results:
                sr = prepare_search_result(pkey, pname, it)
                attach_scores(sr, title, creator, creator_weight)
                all_candidates.append(sr)
                
                sc = sr.raw.get("__matching__", {})
                if sc.get("score", 0) >= provider_threshold:
                    temp.append((sc.get("total", 0.0), sr))
            
            if temp:
                temp.sort(key=lambda x: x[0], reverse=True)
                selected = temp[0][1]
                selected_provider_tuple = (pkey, search_func, download_func, pname)
                break
                
        except Exception:
            logger.exception("Error during search with %s for '%s'", pname, title)
    
    return all_candidates, selected, selected_provider_tuple


def _get_max_parallel_searches() -> int:
    """Get max_parallel_searches from config.
    
    Returns:
        Number of parallel search workers (1 = sequential)
    """
    cfg = get_config()
    sel = cfg.get("selection", {})
    try:
        return max(1, int(sel.get("max_parallel_searches", 1)))
    except (TypeError, ValueError):
        return 1


def _search_single_provider(
    provider_tuple: ProviderTuple,
    title: str,
    creator: Optional[str],
    max_candidates_per_provider: int,
    creator_weight: float,
) -> Tuple[str, str, List[SearchResult]]:
    """Search a single provider and return scored candidates.
    
    This function is designed to be called from a ThreadPoolExecutor.
    
    Args:
        provider_tuple: (provider_key, search_func, download_func, provider_name)
        title: Work title to search
        creator: Optional creator name
        max_candidates_per_provider: Max results to request
        creator_weight: Weight for creator matching
        
    Returns:
        Tuple of (provider_key, provider_name, list of scored SearchResults)
    """
    pkey, search_func, _download_func, pname = provider_tuple
    candidates: List[SearchResult] = []
    
    try:
        max_results = get_max_results_for_provider(pkey, max_candidates_per_provider)
        results = call_search_function(search_func, title, creator, max_results)
        
        if results:
            for it in results:
                sr = prepare_search_result(pkey, pname, it)
                attach_scores(sr, title, creator, creator_weight)
                candidates.append(sr)
    except Exception:
        logger.exception("Error during search with %s for '%s'", pname, title)
    
    return pkey, pname, candidates


def collect_candidates_all(
    provider_list: List[ProviderTuple],
    title: str,
    creator: Optional[str],
    creator_weight: float,
    max_candidates_per_provider: int
) -> List[SearchResult]:
    """Collect candidates from all providers.
    
    Supports parallel searches when selection.max_parallel_searches > 1.
    
    Args:
        provider_list: List of provider tuples
        title: Work title
        creator: Optional creator name
        creator_weight: Weight for creator matching
        max_candidates_per_provider: Max results per provider
        
    Returns:
        List of all SearchResult candidates with scores attached
    """
    max_workers = _get_max_parallel_searches()
    
    if max_workers <= 1 or len(provider_list) <= 1:
        return _collect_candidates_sequential(provider_list, title, creator, creator_weight, max_candidates_per_provider)
    
    return _collect_candidates_parallel(provider_list, title, creator, creator_weight, max_candidates_per_provider, max_workers)


def _collect_candidates_sequential(
    provider_list: List[ProviderTuple],
    title: str,
    creator: Optional[str],
    creator_weight: float,
    max_candidates_per_provider: int
) -> List[SearchResult]:
    """Sequential candidate collection (collect-and-select mode without early exit).
    
    This is a simplified version of collect_candidates_sequential that doesn't
    perform early exit on first match - it always collects from all providers.
    """
    all_candidates: List[SearchResult] = []
    
    for pkey, search_func, _download_func, pname in provider_list:
        logger.info("--- Searching on %s for '%s' ---", pname, title)
        try:
            max_results = get_max_results_for_provider(pkey, max_candidates_per_provider)
            results = call_search_function(search_func, title, creator, max_results)
            
            if not results:
                logger.info("No items found for '%s' on %s.", title, pname)
                continue
            
            logger.info("Found %d item(s) on %s", len(results), pname)
            
            for it in results:
                sr = prepare_search_result(pkey, pname, it)
                attach_scores(sr, title, creator, creator_weight)
                all_candidates.append(sr)
                
        except Exception:
            logger.exception("Error during search with %s for '%s'", pname, title)
    
    return all_candidates


def _collect_candidates_parallel(
    provider_list: List[ProviderTuple],
    title: str,
    creator: Optional[str],
    creator_weight: float,
    max_candidates_per_provider: int,
    max_workers: int,
) -> List[SearchResult]:
    """Parallel candidate collection using ThreadPoolExecutor.
    
    Searches all providers concurrently, then merges results.
    Provider order is preserved for consistent selection behavior.
    """
    all_candidates: List[SearchResult] = []
    start_time = time.perf_counter()
    
    logger.info(
        "--- Parallel search across %d providers (max %d workers) for '%s' ---",
        len(provider_list), max_workers, title
    )
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_provider: Dict[concurrent.futures.Future, ProviderTuple] = {}
        
        for provider_tuple in provider_list:
            future = executor.submit(
                _search_single_provider,
                provider_tuple,
                title,
                creator,
                max_candidates_per_provider,
                creator_weight,
            )
            future_to_provider[future] = provider_tuple
        
        results_by_provider: Dict[str, List[SearchResult]] = {}
        
        for future in concurrent.futures.as_completed(future_to_provider):
            provider_tuple = future_to_provider[future]
            pkey = provider_tuple[0]
            pname = provider_tuple[3]
            
            try:
                _pkey, _pname, candidates = future.result()
                if candidates:
                    logger.info("Found %d item(s) on %s", len(candidates), pname)
                    results_by_provider[pkey] = candidates
                else:
                    logger.info("No items found for '%s' on %s.", title, pname)
            except Exception:
                logger.exception("Error retrieving results from %s for '%s'", pname, title)
    
    for provider_tuple in provider_list:
        pkey = provider_tuple[0]
        if pkey in results_by_provider:
            all_candidates.extend(results_by_provider[pkey])
    
    elapsed = time.perf_counter() - start_time
    logger.info(
        "Parallel search completed in %.2fs, found %d total candidates",
        elapsed, len(all_candidates)
    )
    
    return all_candidates


def select_best_candidate(
    all_candidates: List[SearchResult],
    provider_list: List[ProviderTuple],
    min_title_score: float
) -> Tuple[Optional[SearchResult], Optional[ProviderTuple]]:
    """Select the best candidate based on provider hierarchy and scores.
    
    Uses per-provider min_title_score thresholds when configured,
    falling back to the global min_title_score parameter.
    
    Args:
        all_candidates: List of scored SearchResult candidates
        provider_list: Ordered list of provider tuples (defines priority)
        min_title_score: Global minimum score threshold (used as fallback)
        
    Returns:
        Tuple of (selected_result, selected_provider_tuple)
    """
    prov_priority = {pkey: idx for idx, (pkey, *_rest) in enumerate(provider_list)}
    ranked: List[Tuple[int, float, SearchResult]] = []
    
    for sr in all_candidates:
        sc = sr.raw.get("__matching__", {})
        score = sc.get("score", 0)
        
        # Use per-provider threshold if configured, else fall back to global
        provider_threshold = get_min_title_score(sr.provider_key, default=min_title_score)
        if score < provider_threshold:
            continue
        
        pprio = prov_priority.get(sr.provider_key or "", 9999)
        ranked.append((pprio, float(sc.get("total", 0.0)), sr))
    
    ranked.sort(key=lambda x: (x[0], -x[1]))
    
    if not ranked:
        return None, None
    
    selected = ranked[0][2]
    
    # Find provider tuple for selected result
    selected_provider_tuple = None
    for tup in provider_list:
        if tup[0] == (selected.provider_key or ""):
            selected_provider_tuple = tup
            break
    
    return selected, selected_provider_tuple
