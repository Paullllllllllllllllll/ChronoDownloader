"""The jev_eval matching fixture as typed queries, records and judge answers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from chronodownloader.matching.answers import Relation, WorkAnswer
from chronodownloader.matching.fuzzy import score
from chronodownloader.matching.policy import Candidate
from chronodownloader.providers.base import SearchQuery, SearchRecord

PAIRS = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "matching"
    / "jev_eval"
    / "pairs.jsonl"
)

PROVIDER_ORDER = ("mdz", "internet_archive", "wellcome", "europeana", "polona")
"""The fixture's providers in the order the harvest searched them."""


@cache
def pairs() -> tuple[Mapping[str, Any], ...]:
    """Return every fixture pair (294)."""
    with PAIRS.open(encoding="utf-8") as fh:
        return tuple(json.loads(line) for line in fh if line.strip())


def decided(rows: tuple[Mapping[str, Any], ...]) -> list[Mapping[str, Any]]:
    """Return the pairs whose label is not ``insufficient`` (280)."""
    return [r for r in rows if r["label"]["relation"] != "insufficient"]


def query(pair: Mapping[str, Any], *, with_year: bool = True) -> SearchQuery:
    """Return the search query of the pair's work."""
    work = pair["work"]
    return SearchQuery(
        title=work["query_title"],
        creator=work["query_creator"],
        year=work["year"] if with_year else None,
    )


def record(pair: Mapping[str, Any]) -> SearchRecord:
    """Return the pair's candidate as a search record, its year left to the date."""
    cand = pair["candidate"]
    return SearchRecord(
        provider=cand["provider"],
        source_id=cand["source_id"],
        title=cand["title"],
        creators=tuple(cand["creators"]),
        date=cand["date"],
        item_url=cand["item_url"],
        iiif_manifest=cand["iiif_manifest"],
    )


def answer(pair: Mapping[str, Any], condition: str, model: str) -> WorkAnswer:
    """Return the recorded judge answer under *condition* from *model*."""
    entry = pair["answers"][condition][model]
    match = entry["answers"]["match"]
    relation: Relation = match["choice"]
    return WorkAnswer(
        same_work=float(entry["answers"]["same_work"]["noul"]),
        relation=relation,
        relation_probabilities=match["probabilities"],
        confidence=match.get("confidence"),
        model=entry["model"],
        requested_model=entry["requested_model"],
    )


def candidate(
    pair: Mapping[str, Any], condition: str | None = None, model: str = "jev"
) -> Candidate:
    """Return the pair's candidate scored against its work, judged if asked."""
    rec = record(pair)
    return Candidate(
        record=rec,
        text=score(query(pair), rec),
        iiif=rec.iiif_manifest is not None,
        answer=answer(pair, condition, model) if condition else None,
    )
