"""What a worker thread does for one work: search, judge, download.

Every function here runs on a worker thread of the run's pool and only
talks to the network and the work's staging folder; none writes to a store
or calls a run event. The run loop (:mod:`chronodownloader._loop`) writes
what they return.

Matching (:func:`match_work`). A work's current stored candidates are
reused unless ``--research``; their stored judgments are reused when the
configured model gave them. Without stored candidates the providers are
searched one after another, each under the per-provider timeout
(:func:`~chronodownloader.net.run_with_deadline`, a daemon thread); a work
with a ``link`` is not searched: the linked IIIF manifest, or the manifest
of the item an item page names (:func:`link_record`), is fetched and its
metadata becomes the one candidate. Every candidate gets its text
score; the candidates :func:`~chronodownloader.matching.policy.to_judge`
names and that carry no reusable judgment are judged. A judge outage
returns a deferral with ``retry_after``; nothing is decided on the text
score instead.

Stored URLs and raw fields carry credentials masked (``wskey=***``); a
reused record has the masked query parameters removed (:func:`unmask`), so
that the provider appends its key again.

Downloads (:func:`download_item`) hold one of the provider's download
slots for the whole transfer. A candidate that came from a ``link`` row or
a ``fetch --url`` with a IIIF manifest is downloaded from that manifest;
every other one through its provider's ``download``. A provider that
offers no download of the item (:class:`UnsupportedCapability`) ends the
download as unsupported, not as failed.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import urlsplit

from chronodownloader.coord import (
    BreakerStatus,
    CoordinationError,
    SlotToken,
    read_snapshot,
)
from chronodownloader.iiif.manifest import extract_manifest_metadata
from chronodownloader.iiif.urls import extract_item_id_from_url, is_iiif_manifest_url
from chronodownloader.matching import fuzzy, policy
from chronodownloader.matching.answers import RELATIONS, WorkAnswer
from chronodownloader.matching.fuzzy import TextScore
from chronodownloader.matching.judge import (
    JudgeRefused,
    JudgeUnavailable,
    MalformedAnswer,
)
from chronodownloader.net import (
    BreakerOpen,
    DeadlineExceeded,
    NetworkError,
    run_with_deadline,
)
from chronodownloader.net.credentials import redact_text
from chronodownloader.output.store import (
    CandidateOrigin,
    CandidateRow,
    Judgment,
    WorkInput,
)
from chronodownloader.providers._query import parse_year
from chronodownloader.providers.base import (
    DownloadRequest,
    DownloadResult,
    MissingApiKey,
    ProviderError,
    SearchQuery,
    SearchRecord,
    UnsupportedCapability,
    item_id_from_url,
    item_manifest_url,
    manifest_url,
)
from chronodownloader.providers.iiif_download import download_manifest, fetch_json
from chronodownloader.providers.registry import host_key

from ._runtime import Services

__all__ = [
    "DownloadOutcome",
    "Match",
    "MatchJob",
    "Scored",
    "answer_of",
    "download_item",
    "judgment_of",
    "link_record",
    "match_work",
    "query_of",
    "unmask",
]

logger = logging.getLogger(__name__)

_MASKED_FIRST = re.compile(r"\?[^?&=#\s\"']+=\*\*\*&")
_MASKED = re.compile(r"[?&][^?&=#\s\"']+=\*\*\*(?=[&#\s\"']|$)")
_LINK_TITLE_KEYS: Final = ("title", "titel", "titre", "titolo", "título")
_LINK_CREATOR_KEYS: Final = ("author", "creator", "autor", "auteur", "contributor")
_LINK_DATE_KEYS: Final = ("date", "datum", "year", "published", "publication date")


# -- records, answers, judgments ---------------------------------------------


def query_of(work: WorkInput) -> SearchQuery:
    """Return the search query of *work*; the year is read from its text."""
    return SearchQuery(
        title=work.title,
        creator=work.creator,
        year=parse_year(work.year),
        place=work.place,
        publisher=work.publisher,
        language=work.language,
        edition=work.edition,
    )


def _strip_masked(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _MASKED_FIRST.sub("?", text)
    return _MASKED.sub("", text)


def _strip_value(value: Any) -> Any:
    if isinstance(value, str):
        return _strip_masked(value) if "=***" in value else value
    if isinstance(value, Mapping):
        return {key: _strip_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_strip_value(item) for item in value]
    return value


def unmask(record: SearchRecord) -> SearchRecord:
    """Return a stored *record* without its masked credential parameters.

    The store masks credentials (``wskey=***``); a provider that finds its
    key parameter present would not append the real key again.
    """
    raw = _strip_value(dict(record.raw))
    return SearchRecord(
        provider=record.provider,
        source_id=record.source_id,
        title=record.title,
        creators=record.creators,
        date=record.date,
        year=record.year,
        place=record.place,
        publisher=record.publisher,
        language=record.language,
        item_url=_strip_value(record.item_url),
        iiif_manifest=_strip_value(record.iiif_manifest),
        raw=MappingProxyType(raw),
    )


def judgment_of(answer: WorkAnswer, decision: str) -> Judgment:
    """Return the stored form of the judge's *answer* and the *decision*."""
    return Judgment(
        probabilities={
            "same_work": answer.same_work,
            "relation": answer.relation,
            "match": dict(answer.relation_probabilities),
            "confidence": answer.confidence,
        },
        decision=decision,
        model=answer.requested_model,
        model_version=answer.model or None,
    )


def answer_of(judgment: Judgment | None, model: str) -> WorkAnswer | None:
    """Return the stored *judgment* as an answer when *model* gave it.

    A judgment of another model, or one that does not read as an answer,
    gives None: the candidate is judged again.
    """
    if judgment is None or judgment.model != model:
        return None
    values = judgment.probabilities
    relation = values.get("relation")
    same_work = values.get("same_work")
    matches = values.get("match") or {}
    if relation not in RELATIONS or not isinstance(same_work, int | float):
        return None
    if not isinstance(matches, Mapping):
        return None
    confidence = values.get("confidence")
    try:
        return WorkAnswer(
            same_work=float(same_work),
            relation=relation,
            relation_probabilities={str(k): float(v) for k, v in matches.items()},
            confidence=None if confidence is None else float(confidence),
            model=judgment.model_version or "",
            requested_model=judgment.model,
        )
    except (TypeError, ValueError):
        return None


# -- matching ------------------------------------------------------------------


@dataclass(frozen=True)
class Scored:
    """A candidate with its text score and, if asked, its answer.

    *found* marks a candidate this run's search or link fetch produced;
    *judged* one whose answer this run obtained.
    """

    record: SearchRecord
    origin: CandidateOrigin
    rank: int | None
    text: TextScore
    iiif: bool
    answer: WorkAnswer | None = None
    found: bool = False
    judged: bool = False

    def candidate(self) -> policy.Candidate:
        """Return the policy's view of the candidate."""
        return policy.Candidate(self.record, self.text, self.iiif, self.answer)


@dataclass(frozen=True)
class Match:
    """What matching produced for a work.

    *notes* are warnings for the run's events; *missing_keys* the
    providers skipped because their key is unset. *failure* is set when the
    work cannot be decided (every search failed, the judge refused);
    *deferral* holds the reason and ``retry_after`` of a judge outage;
    *review* the reason a ``link`` row goes to review without a decision.
    """

    candidates: tuple[Scored, ...] = ()
    notes: tuple[str, ...] = ()
    missing_keys: tuple[str, ...] = ()
    failure: str | None = None
    deferral: tuple[str, float] | None = None
    review: str | None = None


@dataclass(frozen=True)
class MatchJob:
    """The inputs of :func:`match_work` for one work.

    *stored* are the work's current stored candidates (empty under
    ``--research`` or without a store); *providers* the keys to search in
    order; *skip* the providers already known to lack their key.
    """

    work: WorkInput
    stored: tuple[CandidateRow, ...]
    mode: policy.Mode
    providers: tuple[str, ...]
    max_candidates: int
    search_timeout: float
    skip: frozenset[str] = field(default_factory=frozenset)


def _iiif(services: Services, record: SearchRecord) -> bool:
    if record.iiif_manifest:
        return True
    try:
        return services.registry.get(record.provider).capabilities.iiif
    except (ProviderError, ValueError):
        return False


def _scored(
    services: Services,
    query: SearchQuery,
    record: SearchRecord,
    origin: CandidateOrigin,
    rank: int | None,
    *,
    found: bool,
    answer: WorkAnswer | None = None,
) -> Scored:
    return Scored(
        record,
        origin,
        rank,
        fuzzy.score(query, record),
        _iiif(services, record),
        answer,
        found,
    )


def _provider_search(
    services: Services, job: MatchJob, key: str, query: SearchQuery
) -> list[SearchRecord]:
    """Return one provider's records, searching again without the creator.

    A creator search that finds nothing is repeated without the creator.
    Catalogs file a creator under forms the works CSV rarely gives (Gallica
    has "Taillevent" for Guillaume Tirel), so the creator clause can hide every
    copy; the judge still sees the creator in the full query. Both searches
    share one deadline: the run's search timeout, raised to the provider's
    ``min_search_timeout_s``; a run timeout of 0 sets none.
    """
    provider = services.registry.get(key)
    timeout = job.search_timeout
    if timeout:
        floor = services.settings.providers[key].min_search_timeout_s
        timeout = max(timeout, floor)
    variants = (query, replace(query, creator=None)) if query.creator else (query,)

    def search() -> list[SearchRecord]:
        records: list[SearchRecord] = []
        for variant in variants:
            records = provider.search(
                variant, network=services.network, limit=job.max_candidates
            )
            if records:
                break
        return records

    return run_with_deadline(search, timeout)


def _search(
    services: Services, job: MatchJob, query: SearchQuery
) -> tuple[list[Scored], list[str], list[str], int]:
    """Search every provider.

    Returns:
        The candidates, notes, missing keys and the number of providers whose
        search failed.
    """
    found: list[Scored] = []
    notes: list[str] = []
    missing: list[str] = []
    failed = 0
    for key in job.providers:
        if key in job.skip:
            continue
        try:
            records = _provider_search(services, job, key, query)
        except MissingApiKey:
            missing.append(key)
            continue
        except DeadlineExceeded as exc:
            notes.append(f"{job.work.entry_id}: {key} search timed out ({exc})")
            failed += 1
            continue
        except (ProviderError, NetworkError, CoordinationError, OSError) as exc:
            notes.append(f"{job.work.entry_id}: {key} search failed: {exc}")
            failed += 1
            continue
        for rank, record in enumerate(records[: job.max_candidates], start=1):
            found.append(_scored(services, query, record, "search", rank, found=True))
    return found, notes, missing, failed


def _first(metadata: Mapping[str, str], keys: Sequence[str]) -> str | None:
    folded = {name.strip().lower(): value for name, value in metadata.items()}
    for key in keys:
        value = folded.get(key)
        if value:
            return value
    return None


def link_record(services: Services, url: str) -> SearchRecord | str:
    """Return the record of the item at *url*, or why there is none.

    The provider is the one serving the URL's host. A IIIF manifest URL is
    read as it is; any other URL (an item or viewer page) is resolved by
    the provider's :func:`~chronodownloader.providers.base.item_id_from_url`
    hook, and the item's manifest comes from its
    :func:`~chronodownloader.providers.base.item_manifest_url` hook. The
    manifest is fetched through the provider's
    :func:`~chronodownloader.providers.base.manifest_url` hook (Europeana's
    key). The record's title, creator and date come from the manifest's
    label and metadata pairs; ``raw`` holds the label, every metadata pair
    under its own name and the attribution, so the judge's catalog record
    sees publisher, place, language and edition.
    """
    try:
        key = host_key(url)
        if key is None:
            host = urlsplit(url).hostname or url
            return f"link: no provider serves {redact_text(host)}"
        adapter = services.registry.get(key)
        if is_iiif_manifest_url(url):
            item_id = extract_item_id_from_url(url)
            manifest_at = url
        else:
            resolved = item_id_from_url(adapter, url)
            if resolved is None:
                return (
                    f"link: {redact_text(url)} names no {key} item this tool "
                    "can resolve; check the item and fetch it"
                )
            item_id = resolved
            template = item_manifest_url(adapter, item_id)
            if template is None:
                return (
                    f"link: {key}:{item_id} offers no IIIF manifest to read its "
                    f"metadata from; check the item and fetch it with "
                    f"--provider {key} --id {item_id}"
                )
            manifest_at = template
        fetch_url = manifest_url(adapter, manifest_at)
        manifest = fetch_json(services.network, key, fetch_url, item=item_id)
    except ValueError as exc:
        return f"link: not a usable URL: {redact_text(str(exc))}"
    except ProviderError as exc:
        return f"link: manifest not fetched: {exc}"
    meta = extract_manifest_metadata(manifest)
    date = _first(meta.metadata, _LINK_DATE_KEYS)
    creator = _first(meta.metadata, _LINK_CREATOR_KEYS)
    title = meta.label or _first(meta.metadata, _LINK_TITLE_KEYS) or ""
    raw: dict[str, Any] = {"label": meta.label}
    raw.update(meta.metadata)
    raw["attribution"] = meta.attribution
    return SearchRecord(
        provider=key,
        source_id=item_id,
        title=title,
        creators=(creator,) if creator else (),
        date=date,
        year=parse_year(date),
        item_url=None if manifest_at == url else url,
        iiif_manifest=manifest_at,
        raw=MappingProxyType(raw),
    )


def _reused(
    services: Services, job: MatchJob, query: SearchQuery, model: str
) -> list[Scored]:
    reused = []
    for row in job.stored:
        answer = None if job.mode == "fuzzy" else answer_of(row.judgment, model)
        reused.append(
            _scored(
                services,
                query,
                unmask(row.record),
                row.origin,
                row.rank,
                found=False,
                answer=answer,
            )
        )
    return reused


def _judge(
    services: Services, job: MatchJob, query: SearchQuery, scored: list[Scored]
) -> tuple[list[Scored], Match | None]:
    """Judge the candidates that need it; a refusal or outage ends matching."""
    thresholds = services.settings.judge.thresholds
    wanted = {
        id(candidate.record)
        for candidate in policy.to_judge(
            [item.candidate() for item in scored], job.mode, thresholds
        )
    }
    if not any(id(item.record) in wanted and item.answer is None for item in scored):
        return scored, None
    judge = services.judge()
    judged: list[Scored] = []
    for index, item in enumerate(scored):
        if id(item.record) not in wanted or item.answer is not None:
            judged.append(item)
            continue
        try:
            answer = judge.judge_work(query, item.record)
        except JudgeUnavailable as exc:
            rest = judged + scored[index:]
            return rest, Match(deferral=(exc.reason, exc.retry_after))
        except (JudgeRefused, MalformedAnswer) as exc:
            rest = judged + scored[index:]
            return rest, Match(failure=f"judge: {exc}")
        judged.append(_with_answer(item, answer))
    return judged, None


def _with_answer(item: Scored, answer: WorkAnswer) -> Scored:
    return Scored(
        item.record,
        item.origin,
        item.rank,
        item.text,
        item.iiif,
        answer,
        item.found,
        True,
    )


def match_work(services: Services, job: MatchJob) -> Match:
    """Return the scored and judged candidates of *job*'s work.

    Raises:
        MissingJudgeKey: The judge is needed and its key is unusable.
    """
    query = query_of(job.work)
    model = services.settings.judge.model
    notes: list[str] = []
    missing: list[str] = []
    if job.stored:
        scored = _reused(services, job, query, model)
    elif job.work.link:
        linked = link_record(services, job.work.link)
        if isinstance(linked, str):
            return Match(review=linked)
        scored = [_scored(services, query, linked, "link", None, found=True)]
    else:
        scored, notes, missing, failed = _search(services, job, query)
        if not scored and failed:
            return Match(
                notes=tuple(notes),
                missing_keys=tuple(missing),
                failure=f"search failed on {failed} provider(s); nothing found",
            )
    judged, stop = _judge(services, job, query, scored)
    if stop is not None:
        return Match(
            candidates=tuple(judged),
            notes=tuple(notes),
            missing_keys=tuple(missing),
            failure=stop.failure,
            deferral=stop.deferral,
        )
    return Match(tuple(judged), tuple(notes), tuple(missing))


# -- downloads -------------------------------------------------------------------


@dataclass(frozen=True)
class DownloadOutcome:
    """A download's result, or the error that ended it, and when it began.

    *unsupported* marks an error that says the provider offers no download
    of the item (metadata only); *note* says more about a result, such as
    which breaker stopped it.
    """

    provider: str
    source_id: str
    started_at: float
    result: DownloadResult | None = None
    error: str | None = None
    unsupported: bool = False
    note: str | None = None


def _open_breakers(services: Services) -> str | None:
    """Name the open breakers and when each admits a probe again.

    Returns None when no breaker of the coordination database is open or the
    database cannot be read.
    """
    try:
        snapshot = read_snapshot(
            services.settings.state_directory(), clock=services.wiring.clock
        )
    except (CoordinationError, sqlite3.Error, OSError) as exc:
        logger.info("breakers not read: %s", exc)
        return None
    named = [
        str(BreakerOpen(provider, state.until, state.cause))
        for provider, state in snapshot.breakers.items()
        if state.status is not BreakerStatus.CLOSED
    ]
    return "; ".join(named) or None


def _direct_manifest(record: SearchRecord, origin: CandidateOrigin) -> str | None:
    """Return the manifest URL a link or fetch candidate is downloaded from."""
    if origin == "search" or not record.iiif_manifest:
        return None
    return record.iiif_manifest


def download_item(
    services: Services,
    record_or_id: SearchRecord | str,
    *,
    provider: str,
    origin: CandidateOrigin,
    staging_dir: Path,
    request: Callable[[SlotToken], DownloadRequest],
) -> DownloadOutcome:
    """Stage the item's files under one of *provider*'s download slots.

    *request* builds the download request around the held slot. A record
    from a link row or a ``fetch --url`` of a manifest is downloaded from
    that manifest; anything else through the provider. Errors are returned,
    not raised.
    """
    source_id = (
        record_or_id.source_id
        if isinstance(record_or_id, SearchRecord)
        else record_or_id
    )
    started = services.wiring.clock()
    began = time.monotonic()
    try:
        adapter = services.registry.get(provider)
        direct = (
            _direct_manifest(record_or_id, origin)
            if isinstance(record_or_id, SearchRecord)
            else None
        )
        with services.network.hold_slot(provider) as slot:
            if direct is not None:
                manifest = fetch_json(
                    services.network,
                    provider,
                    manifest_url(adapter, direct),
                    slot=slot,
                    item=source_id,
                )
                result = download_manifest(
                    services.network,
                    provider,
                    manifest,
                    source_id=source_id,
                    staging_dir=staging_dir,
                    request=request(slot),
                    manifest_url=direct,
                    image_max_px=adapter.capabilities.image_max_px,
                )
            else:
                result = adapter.download(
                    record_or_id,
                    network=services.network,
                    staging_dir=staging_dir,
                    request=request(slot),
                )
    except UnsupportedCapability as exc:
        logger.info("%s:%s not downloaded: %s", provider, source_id, exc)
        return DownloadOutcome(
            provider, source_id, started, error=str(exc), unsupported=True
        )
    except (ProviderError, NetworkError, CoordinationError, OSError) as exc:
        logger.warning("%s:%s download failed: %s", provider, source_id, exc)
        return DownloadOutcome(provider, source_id, started, error=str(exc))
    logger.info(
        "%s:%s staged %d file(s) in %.1f s",
        provider,
        source_id,
        len(result.files),
        time.monotonic() - began,
    )
    note = _open_breakers(services) if result.stopped == "breaker_open" else None
    return DownloadOutcome(provider, source_id, started, result=result, note=note)
