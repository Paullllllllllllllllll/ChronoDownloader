"""``--search`` / ``--search-only`` CLI handler: metadata-only provider search.

Runs the normal discovery and matching pipeline but stops before the
download phase, printing one structured result per work. Fully
side-effect-free: no work directories, no work.json, no index.csv rows,
and no source-CSV updates.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

import pandas as pd

from main.data.works_csv import (
    CREATOR_COL,
    ENTRY_ID_COL,
    TITLE_COL,
    load_works_csv,
)
from main.orchestration import pipeline

from ..exit_codes import EXIT_FAILURES, EXIT_OK, EXIT_USAGE
from ..overrides import _dedupe_keep_order, _split_csv_values

MAX_HUMAN_CANDIDATES = 10


def _redirect_logging_to_stderr() -> None:
    """Keep stdout pure NDJSON by moving root stdout log handlers to stderr."""
    for handler in logging.getLogger().handlers:
        if (
            isinstance(handler, logging.StreamHandler)
            and getattr(handler, "stream", None) is sys.stdout
        ):
            handler.setStream(sys.stderr)


def _candidate_score(candidate: dict[str, Any]) -> float:
    try:
        return float((candidate.get("scores") or {}).get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _print_human(result: dict[str, Any]) -> None:
    """Print one work's search result as a readable block."""
    query = result.get("query", {})
    head = str(query.get("title", ""))
    if query.get("creator"):
        head += f" / {query['creator']}"
    entry_id = result.get("entry_id")
    label = f"[{entry_id}] " if entry_id else ""
    candidates = result.get("candidates") or []
    print(f"\n{label}{head} -> {result.get('status')} ({len(candidates)} candidate(s))")

    selected = result.get("selected")
    if selected:
        print(
            f"  selected: {selected.get('provider')} | {selected.get('title')} | "
            f"date={selected.get('date')} | id={selected.get('source_id')} | "
            f"score={(selected.get('scores') or {}).get('score')}"
        )

    ranked = sorted(candidates, key=_candidate_score, reverse=True)
    for candidate in ranked[:MAX_HUMAN_CANDIDATES]:
        print(
            f"    {_candidate_score(candidate):6.1f}  {candidate.get('provider')} | "
            f"{candidate.get('title')} | date={candidate.get('date')} | "
            f"id={candidate.get('source_id')} | {candidate.get('item_url') or ''}"
        )
    overflow = len(ranked) - MAX_HUMAN_CANDIDATES
    if overflow > 0:
        print(f"    ... and {overflow} more candidate(s)")


def _emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        _print_human(result)


def _queries_from_csv(
    args: argparse.Namespace,
    logger: logging.Logger,
) -> list[tuple[str, str | None, str | None]] | None:
    """Build (title, creator, entry_id) queries from the works CSV.

    Search mode deliberately ignores the retrievable/pending status: searching
    is free and idempotent, so every row with a title is queried. Only
    --entry-ids and --limit narrow the set. Returns None on a usage error.
    """
    try:
        works_df = load_works_csv(args.csv_file)
    except FileNotFoundError:
        logger.error("CSV file not found at %s", args.csv_file)
        return None
    except ValueError as e:
        logger.error("CSV validation error: %s", e)
        return None
    except Exception as e:
        logger.error("Error reading CSV file: %s", e)
        return None

    if TITLE_COL not in works_df.columns:
        logger.error("CSV file must contain '%s' for search mode.", TITLE_COL)
        return None

    requested_ids = _dedupe_keep_order(
        _split_csv_values(getattr(args, "entry_ids", None))
    )
    if requested_ids:
        id_set = {str(i) for i in requested_ids}
        works_df = works_df[works_df[ENTRY_ID_COL].astype(str).isin(id_set)]

    limit = getattr(args, "limit", None)
    if limit is not None and limit >= 0:
        works_df = works_df.head(limit)

    queries: list[tuple[str, str | None, str | None]] = []
    skipped = 0
    for _, row in works_df.iterrows():
        title = row.get(TITLE_COL)
        if pd.isna(title) or not str(title).strip():
            skipped += 1
            continue
        creator = row.get(CREATOR_COL) if CREATOR_COL in works_df.columns else None
        creator_str = None if (creator is None or pd.isna(creator)) else str(creator)
        entry_id = row.get(ENTRY_ID_COL)
        entry_id_str = None if pd.isna(entry_id) else str(entry_id)
        queries.append((str(title), creator_str, entry_id_str))

    if skipped:
        logger.info("Skipping %d row(s) without a searchable title.", skipped)
    return queries


def run_search_cli(
    args: argparse.Namespace,
    config: dict[str, Any],
    logger: logging.Logger,
) -> int:
    """Execute the search-only path (ad hoc ``--search`` or CSV ``--search-only``).

    Prints one result per work on stdout: a JSON line each with ``--json``
    (NDJSON; logs move to stderr so stdout stays parseable), otherwise a
    human-readable block.

    Returns:
        A process exit code: 0 when every queried work produced a confident
        match, 1 when at least one work had no match, 2 on usage errors.
    """
    as_json = getattr(args, "json_summary", False)
    if as_json:
        _redirect_logging_to_stderr()

    queries: list[tuple[str, str | None, str | None]]
    if getattr(args, "search", None):
        queries = [(str(args.search), getattr(args, "creator", None), None)]
    else:
        if not args.csv_file:
            logger.error("--search-only requires a CSV file (or use --search TITLE).")
            return EXIT_USAGE
        maybe_queries = _queries_from_csv(args, logger)
        if maybe_queries is None:
            return EXIT_USAGE
        queries = maybe_queries

    if not queries:
        logger.error("No searchable works found.")
        return EXIT_USAGE

    matched = 0
    for title, creator, entry_id in queries:
        result = pipeline.search_work(title, creator, entry_id)
        if result.get("status") == "match":
            matched += 1
        _emit(result, as_json)

    logger.info("Search complete: %d/%d work(s) matched.", matched, len(queries))
    return EXIT_OK if matched == len(queries) else EXIT_FAILURES
