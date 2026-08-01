"""Candidate selection and scoring logic for ChronoDownloader.

This module extracts the candidate collection, scoring, and ranking logic
from the main pipeline to improve modularity and testability.

Supports parallel provider searches via daemon worker threads when
selection.max_parallel_searches > 1 in config.

Ranking combines the title score with a creator bonus, a quality boost, and
an edition-year penalty governed by ``selection.year_tolerance``; only the
pure title score is gated by ``min_title_score``.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any, cast

from api.core.config import (
    DEFAULT_MAX_PARALLEL_SEARCHES,
    get_config,
    get_min_title_score,
    get_provider_setting,
    get_search_timeout,
    get_year_tolerance,
)
from api.matching import creator_score, extract_year, title_score
from api.model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

# Type alias for provider tuple
ProviderTuple = tuple[str, Callable[..., Any], Callable[..., Any], str]

# Shape of the edition-year penalty (see :func:`year_penalty`). Fixed rather
# than configurable: ``selection.year_tolerance`` already expresses the only
# judgement a corpus makes, namely how wide the "same edition period" window
# is. The slope and cap are calibrated against the ranking total, whose other
# terms are the 0-100 title score, a creator bonus of at most 100 *
# creator_weight, and a quality boost of at most 3.5.
YEAR_PENALTY_PER_YEAR = 0.5
MAX_YEAR_PENALTY = 25.0


def _run_with_timeout(
    timeout: float | None,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run ``func(*args, **kwargs)`` enforcing a wall-clock timeout.

    When ``timeout`` is None or non-positive the call runs directly in the
    caller thread with zero overhead (current unbounded behavior). Otherwise
    the call runs in a daemon worker thread; if it exceeds the deadline a
    ``TimeoutError`` is raised. The abandoned daemon worker may linger until
    its blocking HTTP call returns, but being a daemon thread it never blocks
    interpreter exit. A worker exception is re-raised in the caller thread.
    """
    if not timeout or timeout <= 0:
        return func(*args, **kwargs)

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = func(*args, **kwargs)
        except Exception as exc:  # surfaced in the caller thread
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        raise TimeoutError(f"call timed out after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def call_search_function(
    search_func: Callable[..., Any], title: str, creator: str | None, max_results: int
) -> list[Any]:
    """Call a provider search function with the registry's uniform signature.

    Every registered provider exposes ``search_*(title, creator=None,
    max_results=3)``, so the arguments are passed directly. The former
    ``except TypeError`` probing chain is gone: it misinterpreted a
    TypeError raised *inside* a provider (e.g. on a malformed API response)
    as a signature mismatch and re-ran the same search up to four times.

    Args:
        search_func: Provider search function
        title: Work title
        creator: Optional creator name
        max_results: Maximum results to return

    Returns:
        List of search results from the provider
    """
    return cast(list[Any], search_func(title, creator=creator, max_results=max_results))


def prepare_search_result(
    provider_key: str, provider_name: str, item: Any
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
            provider_name, item if isinstance(item, dict) else {}
        )

    sr.provider_key = provider_key
    if not sr.provider:
        sr.provider = provider_name

    return sr


def year_penalty(
    query_year: int | None, candidate_date: str | None, tolerance: int
) -> float:
    """Ranking penalty for a candidate imprint dated outside the query window.

    The research unit is the EDITION, so a modern reprint or e-book must not
    outrank a period edition of the same title. Candidates whose year falls
    inside ``[query_year - tolerance, query_year + tolerance]`` are unpenalized;
    beyond it the penalty grows linearly at :data:`YEAR_PENALTY_PER_YEAR` per
    year of excess distance and is capped at :data:`MAX_YEAR_PENALTY`, so a
    candidate 50 or more years outside the window takes the full penalty and no
    more. This is a ranking penalty only -- never a gate -- so ``min_title_score``
    (which reads the pure title score) is unaffected and a wrongly-dated record
    can still be selected when nothing better exists.

    Fails open in every direction: no query year, no candidate date, or a date
    from which no plausible four-digit year can be read (Roman-numeral
    centuries, verbal dates, empty imprints) all yield 0.0. Since the term is a
    penalty and never a bonus, a candidate WITHOUT a date can never outrank an
    otherwise identical candidate WITH a matching date.

    Args:
        query_year: Year sought, or None when the query carries no year.
        candidate_date: Free-form date string from the provider record.
        tolerance: Half-width of the unpenalized window, in years.

    Returns:
        Penalty in ranking points, 0.0 <= p <= MAX_YEAR_PENALTY.
    """
    if query_year is None:
        return 0.0

    candidate_year = extract_year(candidate_date)
    if candidate_year is None:
        return 0.0

    excess = abs(candidate_year - query_year) - max(0, tolerance)
    if excess <= 0:
        return 0.0

    return min(MAX_YEAR_PENALTY, YEAR_PENALTY_PER_YEAR * excess)


def score_candidate(
    sr: SearchResult,
    query_title: str,
    query_creator: str | None,
    creator_weight: float,
    query_year: int | None = None,
) -> dict[str, Any]:
    """Compute matching score for a search result candidate.

    Args:
        sr: SearchResult to score
        query_title: Query title
        query_creator: Optional query creator
        creator_weight: Weight for creator matching (0.0-1.0)
        query_year: Optional year sought; when None the year term is inert and
            no configuration is read

    Returns:
        Dictionary with keys ``score`` (pure title score, used for the
        ``min_title_score`` gate), ``creator_score``, ``creator_bonus``,
        ``boost`` (quality signals), ``year_penalty``, ``candidate_year``, and
        ``total`` (ranking score).

    Per the matching decision, ``min_title_score`` gates the pure title score
    only; creator similarity contributes a positive ranking bonus and never
    penalizes a candidate that lacks creator metadata. The year term is the
    mirror image: a pure penalty on the ranking total (see
    :func:`year_penalty`), so an undated candidate is never advantaged.
    """
    title = float(title_score(query_title, sr.title, method="token_set"))

    cw = max(0.0, min(1.0, float(creator_weight or 0.0)))
    cs = float(creator_score(query_creator, sr.creators)) if query_creator else 0.0
    creator_bonus = cw * cs

    # Quality signals boost
    boost = 0.0
    if sr.iiif_manifest:
        boost += 3.0
    if sr.item_url:
        boost += 0.5

    # Without a query year the feature is dormant: no config read, no penalty,
    # and a ranking total byte-identical to the pre-feature behavior.
    if query_year is None:
        penalty = 0.0
        candidate_year = None
    else:
        candidate_year = extract_year(sr.date)
        penalty = year_penalty(query_year, sr.date, get_year_tolerance())

    total = title + creator_bonus + boost - penalty

    return {
        "score": title,
        "title_score": title,
        "creator_score": cs,
        "creator_bonus": creator_bonus,
        "boost": boost,
        "year_penalty": penalty,
        "candidate_year": candidate_year,
        "total": total,
    }


def attach_scores(
    sr: SearchResult,
    query_title: str,
    query_creator: str | None,
    creator_weight: float,
    query_year: int | None = None,
) -> None:
    """Compute and attach matching scores to a SearchResult's raw dict.

    Args:
        sr: SearchResult to score
        query_title: Query title
        query_creator: Optional query creator
        creator_weight: Weight for creator matching
        query_year: Optional year sought (see :func:`score_candidate`)
    """
    scores = score_candidate(sr, query_title, query_creator, creator_weight, query_year)
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


def _search_provider_logged(
    pkey: str,
    search_func: Callable[..., Any],
    pname: str,
    title: str,
    creator: str | None,
    max_candidates_per_provider: int,
) -> list[Any]:
    """Search a single provider with the standard logging preamble.

    Logs the search banner, runs the provider's search function, and logs
    either the no-results message or the hit count. Returns the (possibly
    empty) list of raw provider results. When the provider exceeds its
    effective search timeout the call is abandoned, a WARNING is logged, and
    an empty list is returned so the fan-out continues without it.
    """
    logger.info("--- Searching on %s for '%s' ---", pname, title)
    max_results = get_max_results_for_provider(pkey, max_candidates_per_provider)
    timeout = get_search_timeout(pkey)
    try:
        results = cast(
            list[Any],
            _run_with_timeout(
                timeout, call_search_function, search_func, title, creator, max_results
            ),
        )
    except TimeoutError:
        logger.warning(
            "search on %s timed out after %ss; continuing without it", pname, timeout
        )
        return []

    if not results:
        logger.info("No items found for '%s' on %s.", title, pname)
        return []

    logger.info("Found %d item(s) on %s", len(results), pname)
    return results


def collect_candidates_sequential(
    provider_list: list[ProviderTuple],
    title: str,
    creator: str | None,
    min_title_score: float,
    creator_weight: float,
    max_candidates_per_provider: int,
    query_year: int | None = None,
) -> tuple[list[SearchResult], SearchResult | None, ProviderTuple | None]:
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
        query_year: Optional year sought, used for the edition-year penalty

    Returns:
        Tuple of (all_candidates, selected, selected_provider_tuple)
    """
    all_candidates: list[SearchResult] = []
    selected: SearchResult | None = None
    selected_provider_tuple: ProviderTuple | None = None

    for pkey, search_func, download_func, pname in provider_list:
        try:
            results = _search_provider_logged(
                pkey, search_func, pname, title, creator, max_candidates_per_provider
            )
            if not results:
                continue

            # Get per-provider threshold, falling back to global
            provider_threshold = get_min_title_score(pkey, default=min_title_score)

            # Score and collect candidates
            temp: list[tuple[float, SearchResult]] = []
            for it in results:
                sr = prepare_search_result(pkey, pname, it)
                attach_scores(sr, title, creator, creator_weight, query_year)
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
        return max(
            1, int(sel.get("max_parallel_searches", DEFAULT_MAX_PARALLEL_SEARCHES))
        )
    except (TypeError, ValueError):
        return DEFAULT_MAX_PARALLEL_SEARCHES


def _search_single_provider(
    provider_tuple: ProviderTuple,
    title: str,
    creator: str | None,
    max_candidates_per_provider: int,
    creator_weight: float,
    query_year: int | None = None,
) -> tuple[str, str, list[SearchResult]]:
    """Search a single provider and return scored candidates.

    This function is designed to be called from a daemon worker thread.

    Args:
        provider_tuple: (provider_key, search_func, download_func, provider_name)
        title: Work title to search
        creator: Optional creator name
        max_candidates_per_provider: Max results to request
        creator_weight: Weight for creator matching
        query_year: Optional year sought, used for the edition-year penalty

    Returns:
        Tuple of (provider_key, provider_name, list of scored SearchResults)
    """
    pkey, search_func, _download_func, pname = provider_tuple
    candidates: list[SearchResult] = []

    try:
        max_results = get_max_results_for_provider(pkey, max_candidates_per_provider)
        results = call_search_function(search_func, title, creator, max_results)

        if results:
            for it in results:
                sr = prepare_search_result(pkey, pname, it)
                attach_scores(sr, title, creator, creator_weight, query_year)
                candidates.append(sr)
    except Exception:
        logger.exception("Error during search with %s for '%s'", pname, title)

    return pkey, pname, candidates


def collect_candidates_all(
    provider_list: list[ProviderTuple],
    title: str,
    creator: str | None,
    creator_weight: float,
    max_candidates_per_provider: int,
    query_year: int | None = None,
) -> list[SearchResult]:
    """Collect candidates from all providers.

    Supports parallel searches when selection.max_parallel_searches > 1.

    Args:
        provider_list: List of provider tuples
        title: Work title
        creator: Optional creator name
        creator_weight: Weight for creator matching
        max_candidates_per_provider: Max results per provider
        query_year: Optional year sought, used for the edition-year penalty

    Returns:
        List of all SearchResult candidates with scores attached
    """
    max_workers = _get_max_parallel_searches()

    if max_workers <= 1 or len(provider_list) <= 1:
        return _collect_candidates_exhaustive(
            provider_list,
            title,
            creator,
            creator_weight,
            max_candidates_per_provider,
            query_year,
        )

    return _collect_candidates_parallel(
        provider_list,
        title,
        creator,
        creator_weight,
        max_candidates_per_provider,
        max_workers,
        query_year,
    )


def _collect_candidates_exhaustive(
    provider_list: list[ProviderTuple],
    title: str,
    creator: str | None,
    creator_weight: float,
    max_candidates_per_provider: int,
    query_year: int | None = None,
) -> list[SearchResult]:
    """Sequential candidate collection that always queries all providers.

    Unlike ``collect_candidates_sequential`` (which exits on the first acceptable
    match), this variant never short-circuits — it is used by the
    ``collect_and_select`` strategy to gather all candidates before ranking.
    """
    all_candidates: list[SearchResult] = []

    for pkey, search_func, _download_func, pname in provider_list:
        try:
            results = _search_provider_logged(
                pkey, search_func, pname, title, creator, max_candidates_per_provider
            )
            if not results:
                continue

            for it in results:
                sr = prepare_search_result(pkey, pname, it)
                attach_scores(sr, title, creator, creator_weight, query_year)
                all_candidates.append(sr)

        except Exception:
            logger.exception("Error during search with %s for '%s'", pname, title)

    return all_candidates


def _parallel_search_worker(
    provider_tuple: ProviderTuple,
    title: str,
    creator: str | None,
    max_candidates_per_provider: int,
    creator_weight: float,
    semaphore: threading.BoundedSemaphore,
    box: dict[str, Any],
    query_year: int | None = None,
) -> None:
    """Run one provider search in a daemon thread, gated by ``semaphore``.

    Records the actual start time (measured only after a concurrency slot is
    acquired) so the caller measures the timeout from real work start, signals
    the ``started`` and ``done`` events, and always releases the slot.
    ``_search_single_provider`` is itself exception-safe, so no search error
    escapes here.
    """
    semaphore.acquire()
    try:
        box["start"] = time.monotonic()
        box["started"].set()
        _pkey, _pname, candidates = _search_single_provider(
            provider_tuple,
            title,
            creator,
            max_candidates_per_provider,
            creator_weight,
            query_year,
        )
        box["candidates"] = candidates
    finally:
        box["done"].set()
        semaphore.release()


def _collect_candidates_parallel(
    provider_list: list[ProviderTuple],
    title: str,
    creator: str | None,
    creator_weight: float,
    max_candidates_per_provider: int,
    max_workers: int,
    query_year: int | None = None,
) -> list[SearchResult]:
    """Parallel candidate collection using daemon worker threads.

    Searches all providers concurrently (one daemon thread each, gated by a
    ``BoundedSemaphore`` so ``max_parallel_searches`` is still honored), then
    merges results in provider_list order for consistent selection behavior.

    Each provider gets its own deadline (its recorded start time plus its
    effective search timeout). A provider that exceeds its deadline is logged
    at WARNING and dropped, so the total wait is bounded by the largest
    per-provider timeout rather than the slowest provider's real runtime.
    Workers are daemon threads: an abandoned worker still blocked in a slow
    HTTP call lingers harmlessly and never blocks interpreter exit.
    """
    all_candidates: list[SearchResult] = []
    start_time = time.perf_counter()

    logger.info(
        "--- Parallel search across %d providers (max %d workers) for '%s' ---",
        len(provider_list),
        max_workers,
        title,
    )

    semaphore = threading.BoundedSemaphore(max_workers)
    workers: list[tuple[ProviderTuple, dict[str, Any], float | None]] = []
    for provider_tuple in provider_list:
        box: dict[str, Any] = {
            "started": threading.Event(),
            "done": threading.Event(),
            "start": 0.0,
            "candidates": [],
        }
        timeout = get_search_timeout(provider_tuple[0])
        thread = threading.Thread(
            target=_parallel_search_worker,
            args=(
                provider_tuple,
                title,
                creator,
                max_candidates_per_provider,
                creator_weight,
                semaphore,
                box,
                query_year,
            ),
            daemon=True,
        )
        thread.start()
        workers.append((provider_tuple, box, timeout))

    # Bound on how long to wait for a queued worker to acquire a slot and
    # start: a slot pinned by a stalled search is never released, so a later
    # provider may never run. Cap the wait rather than block forever; when
    # every timeout is disabled there is no cap (unbounded, as before).
    finite = [t for _pt, _box, t in workers if t]
    if finite:
        batches = math.ceil(len(workers) / max_workers)
        started_guard: float | None = max(finite) * batches + 5.0
    else:
        started_guard = None

    results_by_provider: dict[str, list[SearchResult]] = {}
    for provider_tuple, box, timeout in workers:
        pkey = provider_tuple[0]
        pname = provider_tuple[3]

        if not box["started"].wait(started_guard):
            logger.warning(
                "search on %s never started; all search slots stalled", pname
            )
            continue

        if timeout:
            remaining = max(0.0, (box["start"] + timeout) - time.monotonic())
            finished = box["done"].wait(remaining)
        else:
            box["done"].wait()
            finished = True

        if not finished:
            logger.warning(
                "search on %s timed out after %ss; continuing without it",
                pname,
                timeout,
            )
            continue

        candidates = box["candidates"]
        if candidates:
            logger.info("Found %d item(s) on %s", len(candidates), pname)
            results_by_provider[pkey] = candidates
        else:
            logger.info("No items found for '%s' on %s.", title, pname)

    for provider_tuple in provider_list:
        pkey = provider_tuple[0]
        if pkey in results_by_provider:
            all_candidates.extend(results_by_provider[pkey])

    elapsed = time.perf_counter() - start_time
    logger.info(
        "Parallel search completed in %.2fs, found %d total candidates",
        elapsed,
        len(all_candidates),
    )

    return all_candidates


def select_best_candidate(
    all_candidates: list[SearchResult],
    provider_list: list[ProviderTuple],
    min_title_score: float,
) -> tuple[SearchResult | None, ProviderTuple | None]:
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
    ranked: list[tuple[int, float, SearchResult]] = []

    for sr in all_candidates:
        sc = sr.raw.get("__matching__", {})
        score = sc.get("score", 0)

        # Use per-provider threshold if configured, else fall back to global
        provider_threshold = get_min_title_score(
            sr.provider_key, default=min_title_score
        )
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
