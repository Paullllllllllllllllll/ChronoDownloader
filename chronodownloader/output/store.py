"""The store of an output root: ``<root>/chronodownloader.sqlite``.

The store is the only status record of an output root. It holds the runs, the
works with their input fields and status, the candidates found for each work
with their scores and judgments, every download attempt with its completeness,
the files each work's folder holds and the page type of each page image.
External readers use the views ``work_status``, ``work_candidates`` and
``title_page_picks`` (the read contract); the tables behind them may change in
a later schema version.

Schema (version 2, ``meta.schema_version``)::

    meta       (key TEXT PRIMARY KEY, value TEXT)  -- schema_version,
                                                   -- generation_floor
    runs       (run_id TEXT PRIMARY KEY, command, tool_version, pid, host,
                process_created REAL,       -- epoch s; NULL where unknown
                spec JSON, started_at, ended_at, exit_code)
    works      (entry_id TEXT PRIMARY KEY, title, creator, year, place,
                publisher, language, edition, link,     -- input fields, text
                folder UNIQUE NOCASE,                  -- relative to the root
                status, status_reason, retry_after REAL,
                selected_candidate -> candidates,
                lease_holder, lease_generation, lease_expires_at REAL,
                first_run_id, last_run_id, created_at, updated_at)
    candidates (candidate_id INTEGER PRIMARY KEY, entry_id -> works,
                run_id -> runs, origin (search|link|fetch), rank,
                provider, source_id, title, creators JSON, date, year,
                place, publisher, language, item_url, iiif_manifest, raw JSON,
                text_score, judge_probabilities JSON, judge_decision,
                judge_model, judge_model_version, judged_at,
                superseded_at,                         -- NULL: current
                created_at, updated_at)
    attempts   (attempt_id INTEGER PRIMARY KEY, entry_id -> works,
                run_id -> runs, lease_generation, commit_id UNIQUE,
                candidate_id -> candidates, provider, source_id,
                format (images|pdf|both), outcome (complete|partial|failed),
                parts JSON, stopped, quota_charged, error,
                started_at, finished_at)
    files      (entry_id -> works, path,              -- relative to the folder
                kind (image|pdf|metadata), item, size, content_type,
                source_url, label, declared_format,
                attempt_id -> attempts, recorded_at,
                PRIMARY KEY (entry_id, path))
    page_labels (entry_id, path,              -- the files row labelled
                attempt_id,                   -- that row's attempt when asked
                page_index,                   -- source position (Page index)
                page_type, probabilities JSON, taxonomy_version, profile,
                model, dialect, fingerprint,  -- of the decision request
                run_id -> runs, created_at,
                PRIMARY KEY (entry_id, path))  -- version 2

    work_status     one row per work: the works columns, ``status``
                    (``blocked`` while a run holds an unexpired lease by the
                    database's wall clock, else the stored status, or ``new``
                    for a work with no outcome yet), ``stored_status``, the
                    selected candidate (``selected_provider``,
                    ``selected_source_id``, ``selected_item_url``), the latest
                    attempt (``last_*``, ``pages_expected``,
                    ``pages_present``), ``file_count`` and ``file_bytes``.
                    ``retry_after`` and ``lease_expires_at`` are ISO 8601.
    work_candidates one row per candidate, history included, with the work's
                    ``folder``, ``selected`` and ``current`` (0 or 1).
    title_page_picks one row per work with page images: ``entry_id``,
                    ``pages_total`` (its image files), ``pages_leading``
                    (the leading pages a complete work has: the smaller of
                    10 and the highest ``page_index`` plus one),
                    ``pages_labelled`` (current labels among the pages of
                    ``page_index`` below 10) and the title page the shared
                    rule picks
                    (:func:`~chronodownloader.common.page_types.title_page_index`):
                    among the pages of ``page_index`` below 10, the labelled
                    page of the highest ``title_page`` probability (the
                    earlier one on a tie), if it reaches .5; ``path``,
                    ``page_index`` and ``probability`` of that page, else
                    NULL. A label is current when its ``attempt_id`` is the
                    files row's; a stale one never counts.
                    ``pages_labelled`` below ``pages_leading`` marks a
                    leading page missing on disk or not yet classified, not
                    a work without a title page.

Page index. A page's ``page_index`` is its position in the source: item
``pNNNNN`` is page NNNNN - 1, so a partial download keeps the gaps of the
pages it lacks. When any image item of a work is not of that form (``p``
and a number from 1), the work's images are numbered 0, 1, ... in item order
instead. ``page_labels.page_index`` stores the same number.

Page labels. A label keys to its ``files`` row and the attempt that wrote
the row; the commit that replaces the row, the reconciliation that deletes it,
:meth:`Store.remove_files` and :meth:`Store.drop` delete the label in the same
transaction. :meth:`Store.record_page_labels` inserts a label only while the
row still carries the attempt the label was asked for. A label of a row an
older process replaced stays in the table but is stale, so no reader takes it
for current. The table has no foreign keys to ``works``, ``attempts`` or
``files``, so that a version 1 process still open on a migrated store can
delete those rows.

Times are ISO 8601 UTC text (``2026-10-23T12:00:00.000Z``) except
``retry_after`` and ``lease_expires_at``, which are epoch seconds from the
injectable clock. ``parts`` maps each part (``images``, ``pdf``) to its
``expected`` and ``present`` items, ``page_limit``, ``available`` and
``causes`` (:class:`~chronodownloader.providers.base.Completeness`). Items
are not constrained: multi-file renderings (``rendering_00001``,
``rendering_p00001_00025``) sit next to ``rendering`` and pages
``p00001``. Stored statuses are ``complete``, ``partial``, ``failed``,
``no_match``, ``needs_review`` and ``deferred``; a deferred work carries
``retry_after`` and no other does. ``blocked`` is never stored: a run whose
:meth:`Store.acquire` returns None counts the work as blocked, and the view
derives it. URLs, ``raw`` and error texts are stored with credential values
masked; error texts are cut to :data:`ERROR_TEXT_LIMIT` characters.

Opening. A database with tables but no schema version, one with a newer
version and one SQLite cannot read raise a :class:`StoreError` (exit code
2) and are left as they are; only a missing or empty database is created. A
version 1 store is migrated in place, in the opening's write transaction: the
version 2 table and view are created and ``meta.schema_version`` becomes 2.
While a work of it is under an unexpired lease, which only a version 1
process can hold, the opening refuses with :class:`StoreBusyError` and
migrates nothing.
The read-only functions read a version 1 store as one without page labels.

Writers and leases. Every write is one short ``BEGIN IMMEDIATE`` transaction
through :func:`chronodownloader.sqlite.transact` (rollback journal, bounded
busy waits) and never spans a network call. Only the run loop's thread
writes; workers return their results to it. The one exception is the
:class:`LeaseKeeper` thread, which only extends the expiry of held leases.
A work is changed only under its lease (holder = run id, generation,
expiry): every write on a work checks inside its transaction, against the
clock read there, that the lease names this holder and generation and has
not expired, and raises :class:`LeaseLostError` otherwise. A write that
waited for the database lock past the expiry is therefore refused, and so
is a renewal after expiry. An acquisition succeeds when the lease is free,
expired or this run's own, and increments the generation. A lease is live
while it is unexpired and its holder runs: a holder whose ``runs`` row
names this host and a process that is not running (on Windows, also a
running process whose creation time differs from ``process_created``, which
reused the pid; elsewhere pid reuse is not detected) is dead, and its lease
counts as expired for acquisition and reconciliation alike. A holder on
another host, or of unknown identity, keeps its lease until the expiry.

Commit of a download. A download stages into
``<folder>/objects/.staging/<generation>/`` (:meth:`Store.staging_dir`,
which also removes the staging folders of older generations, as only the
current holder may). :meth:`Store.commit_download` runs one transaction:
the lease check, then a promotion journal
``.promote-<commit id>.json`` written into the staging folder, then for
each file a same-volume rename to its final path (an existing final file is
first moved aside to ``.replaced-<commit id>/`` in the staging folder), then
the attempt row (carrying the commit id) and the ``files`` rows, then
COMMIT. Afterwards the journal, the moved-aside files and the staging folder
are deleted. A commit that fails, for a lost lease or any other reason,
rolls its renames back under the rules below and discards the staging
folder; a refused lease check happens before any rename, so no final file
is touched.

Repair rules. Renames are not transactional; the journal records what a
commit may have renamed. A journal whose commit id is in ``attempts``
landed: only its moved-aside files are deleted. For a journal that did not
land (a crash between a rename and COMMIT), every entry is rolled back in
reverse order, in a write transaction: a final path recorded in ``files``
by a later lease generation belongs to the successor and is left alone;
otherwise a moved-aside file is moved back to its final path, and a final
file this commit created (the journal records that none existed before,
its staged file is gone, no row records the path) is deleted. A file that
existed before the commit is only ever restored, never deleted, so an
interrupted repair can run again; a file no journal names is never
deleted. Generations are never reused: a drop raises
``meta.generation_floor``, from which a work created again counts on.

Reconciliation (:meth:`Store.reconcile`, run when a store opens). For each
work not leased by a live run (judged in a transaction by the clock and the
holder's process, as above), the staging folders of every generation are
settled by the repair rules and removed; then every ``files`` row whose
file is missing on disk is deleted, and a ``complete`` work that lost a
file becomes ``partial`` with the reason recorded, so that a later run
downloads the item again. Files on
disk that no row records and no journal names are left alone. Works leased
by a live run are skipped; their holder cleans its own staging folders.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Final, Literal, Self, TypeVar

from chronodownloader.coord import Clock
from chronodownloader.net.credentials import redact_text
from chronodownloader.process.contract import ExitCode
from chronodownloader.providers.base import (
    Completeness,
    DownloadResult,
    Part,
    SearchRecord,
    StagedFile,
)
from chronodownloader.sqlite import (
    DEFAULT_BUSY_TIMEOUT,
    connect,
    is_busy,
    read,
    transact,
)

from . import _promotion
from ._liveness import own_creation, process_alive
from ._promotion import Move
from .paths import STAGING_DIR, inspect_dir

__all__ = [
    "DATABASE_NAME",
    "ERROR_TEXT_LIMIT",
    "LEASE_RENEW_INTERVAL",
    "LEASE_TTL",
    "SCHEMA_VERSION",
    "STAGING_DIR",
    "STORED_STATUSES",
    "AttemptRow",
    "CandidateInput",
    "CandidateOrigin",
    "CandidateRow",
    "DropRefusal",
    "DropReport",
    "FileRow",
    "FolderConflictError",
    "Judgment",
    "Lease",
    "LeaseKeeper",
    "LeaseLostError",
    "NewerSchemaError",
    "NotSelectedError",
    "PICK_SEARCH",
    "PICK_THRESHOLD",
    "PageLabel",
    "Promotion",
    "Reconciliation",
    "Store",
    "StoreBusyError",
    "StoreError",
    "StoredStatus",
    "UnreadableDatabaseError",
    "VersionlessDatabaseError",
    "WorkInput",
    "WorkRow",
    "bounded_error",
    "check_schema",
    "read_candidates",
    "read_files",
    "read_page_labels",
    "read_title_page_picks",
    "read_work_candidates",
    "read_work_status",
    "unstored_folders",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")

DATABASE_NAME: Final = "chronodownloader.sqlite"
SCHEMA_VERSION: Final = 2
PICK_THRESHOLD: Final = 0.5
PICK_SEARCH: Final = 10
"""The ``title_page_picks`` rule; equal to ``page_types.TITLE_PAGE_THRESHOLD``
and ``TITLE_PAGE_SEARCH``, but fixed here, since a stored view keeps its SQL
and a change needs a schema version."""
LEASE_TTL: Final = 600.0
"""Seconds a work lease lasts after its acquisition or last renewal."""
LEASE_RENEW_INTERVAL: Final = 60.0
"""Seconds between the :class:`LeaseKeeper`'s renewals."""
ERROR_TEXT_LIMIT: Final = 2000
"""Characters of error text an attempt row keeps."""

type StoredStatus = Literal[
    "complete", "partial", "failed", "no_match", "needs_review", "deferred"
]
"""A status the store records; ``blocked`` is derived, never stored."""

type CandidateOrigin = Literal["search", "link", "fetch"]
"""How a candidate was found: a search, the input's ``link``, or ``fetch``."""

STORED_STATUSES: Final[tuple[StoredStatus, ...]] = (
    "complete",
    "partial",
    "failed",
    "no_match",
    "needs_review",
    "deferred",
)
_ORIGINS: Final[tuple[CandidateOrigin, ...]] = ("search", "link", "fetch")
_OUTCOMES: Final = ("complete", "partial", "failed")
_FORMATS: Final = ("images", "pdf", "both")
_KINDS: Final = ("image", "pdf", "metadata")


def _choices(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


_WALL_NOW = "((julianday('now') - 2440587.5) * 86400.0)"
_ISO_FORMAT = "'%Y-%m-%dT%H:%M:%fZ'"

_SCHEMA_V1: Final = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    tool_version TEXT,
    pid INTEGER,
    host TEXT,
    process_created REAL,
    spec TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    exit_code INTEGER
)""",
    f"""CREATE TABLE works (
    entry_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    creator TEXT,
    year TEXT,
    place TEXT,
    publisher TEXT,
    language TEXT,
    edition TEXT,
    link TEXT,
    folder TEXT NOT NULL UNIQUE COLLATE NOCASE,
    status TEXT CHECK (status IS NULL OR status IN ({_choices(STORED_STATUSES)})),
    status_reason TEXT,
    retry_after REAL,
    selected_candidate INTEGER REFERENCES candidates (candidate_id),
    lease_holder TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_expires_at REAL,
    first_run_id TEXT REFERENCES runs (run_id),
    last_run_id TEXT REFERENCES runs (run_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (CASE WHEN status = 'deferred' THEN retry_after IS NOT NULL
           ELSE retry_after IS NULL END)
)""",
    f"""CREATE TABLE candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL REFERENCES works (entry_id),
    run_id TEXT NOT NULL REFERENCES runs (run_id),
    origin TEXT NOT NULL CHECK (origin IN ({_choices(_ORIGINS)})),
    rank INTEGER,
    provider TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    creators TEXT NOT NULL DEFAULT '[]',
    date TEXT,
    year INTEGER,
    place TEXT,
    publisher TEXT,
    language TEXT,
    item_url TEXT,
    iiif_manifest TEXT,
    raw TEXT,
    text_score REAL,
    judge_probabilities TEXT,
    judge_decision TEXT,
    judge_model TEXT,
    judge_model_version TEXT,
    judged_at TEXT,
    superseded_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX candidates_current ON candidates "
    "(entry_id, provider, source_id) WHERE superseded_at IS NULL",
    "CREATE INDEX candidates_by_work ON candidates (entry_id)",
    f"""CREATE TABLE attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL REFERENCES works (entry_id),
    run_id TEXT NOT NULL REFERENCES runs (run_id),
    lease_generation INTEGER NOT NULL,
    commit_id TEXT UNIQUE,
    candidate_id INTEGER REFERENCES candidates (candidate_id),
    provider TEXT NOT NULL,
    source_id TEXT NOT NULL,
    format TEXT NOT NULL CHECK (format IN ({_choices(_FORMATS)})),
    outcome TEXT NOT NULL CHECK (outcome IN ({_choices(_OUTCOMES)})),
    parts TEXT NOT NULL DEFAULT '{{}}',
    stopped TEXT,
    quota_charged INTEGER NOT NULL DEFAULT 0 CHECK (quota_charged IN (0, 1)),
    error TEXT,
    started_at TEXT,
    finished_at TEXT NOT NULL
)""",
    "CREATE INDEX attempts_by_work ON attempts (entry_id, attempt_id)",
    f"""CREATE TABLE files (
    entry_id TEXT NOT NULL REFERENCES works (entry_id),
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ({_choices(_KINDS)})),
    item TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    content_type TEXT,
    source_url TEXT,
    label TEXT,
    declared_format TEXT,
    attempt_id INTEGER NOT NULL REFERENCES attempts (attempt_id),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (entry_id, path)
)""",
    "CREATE INDEX files_by_attempt ON files (attempt_id)",
    f"""CREATE VIEW work_status AS
SELECT
    w.entry_id, w.title, w.creator, w.year, w.place, w.publisher, w.language,
    w.edition, w.link, w.folder,
    CASE
        WHEN w.lease_holder IS NOT NULL AND w.lease_expires_at > {_WALL_NOW}
        THEN 'blocked'
        ELSE COALESCE(w.status, 'new')
    END AS status,
    w.status AS stored_status,
    w.status_reason,
    strftime({_ISO_FORMAT}, w.retry_after, 'unixepoch') AS retry_after,
    w.lease_holder,
    strftime({_ISO_FORMAT}, w.lease_expires_at, 'unixepoch') AS lease_expires_at,
    c.provider AS selected_provider,
    c.source_id AS selected_source_id,
    c.item_url AS selected_item_url,
    a.attempt_id AS last_attempt_id,
    a.provider AS last_provider,
    a.source_id AS last_source_id,
    a.format AS last_format,
    a.outcome AS last_outcome,
    a.stopped AS last_stopped,
    a.finished_at AS last_attempt_at,
    json_array_length(a.parts, '$.images.expected') AS pages_expected,
    json_array_length(a.parts, '$.images.present') AS pages_present,
    (SELECT count(*) FROM files AS f WHERE f.entry_id = w.entry_id) AS file_count,
    (SELECT COALESCE(sum(f.size), 0) FROM files AS f
     WHERE f.entry_id = w.entry_id) AS file_bytes,
    w.first_run_id, w.last_run_id, w.created_at, w.updated_at
FROM works AS w
LEFT JOIN candidates AS c ON c.candidate_id = w.selected_candidate
LEFT JOIN attempts AS a ON a.attempt_id = (
    SELECT max(attempt_id) FROM attempts WHERE entry_id = w.entry_id
)""",
    """CREATE VIEW work_candidates AS
SELECT
    c.candidate_id, c.entry_id, w.folder, c.origin, c.rank, c.provider,
    c.source_id, c.title, c.creators, c.date, c.year, c.place, c.publisher,
    c.language, c.item_url, c.iiif_manifest, c.text_score,
    c.judge_probabilities, c.judge_decision, c.judge_model,
    c.judge_model_version, c.judged_at,
    (c.candidate_id IS w.selected_candidate) AS selected,
    (c.superseded_at IS NULL) AS current,
    c.superseded_at, c.run_id, c.created_at, c.updated_at
FROM candidates AS c
JOIN works AS w ON w.entry_id = c.entry_id""",
)

_PICKED = f"labelled AND page_index < {PICK_SEARCH} AND probability >= {PICK_THRESHOLD}"

_SCHEMA_V2: Final = (
    """CREATE TABLE page_labels (
    entry_id TEXT NOT NULL,
    path TEXT NOT NULL,
    attempt_id INTEGER NOT NULL,
    page_index INTEGER NOT NULL CHECK (page_index >= 0),
    page_type TEXT NOT NULL,
    probabilities TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    profile TEXT NOT NULL,
    model TEXT NOT NULL,
    dialect TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs (run_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (entry_id, path)
)""",
    f"""CREATE VIEW title_page_picks AS
WITH images AS (
    SELECT f.entry_id, f.path, f.attempt_id, f.item,
        f.item GLOB 'p[0-9]*' AND substr(f.item, 2) NOT GLOB '*[^0-9]*'
            AND CAST(substr(f.item, 2) AS INTEGER) > 0 AS sourced,
        row_number() OVER (PARTITION BY f.entry_id ORDER BY f.item, f.path) - 1
            AS position,
        count(*) OVER (PARTITION BY f.entry_id) AS pages_total
    FROM files AS f
    WHERE f.kind = 'image'
),
pages AS (
    SELECT entry_id, path, attempt_id, pages_total,
        CASE WHEN min(sourced) OVER (PARTITION BY entry_id)
            THEN CAST(substr(item, 2) AS INTEGER) - 1 ELSE position END
            AS page_index
    FROM images
),
leading AS (
    SELECT p.entry_id, p.path, p.page_index, p.pages_total,
        max(p.page_index) OVER (PARTITION BY p.entry_id) AS last_index,
        l.entry_id IS NOT NULL AS labelled,
        COALESCE(json_extract(l.probabilities, '$.title_page'), 0.0)
            AS probability
    FROM pages AS p
    LEFT JOIN page_labels AS l
        ON l.entry_id = p.entry_id AND l.path = p.path
        AND l.attempt_id = p.attempt_id
),
ranked AS (
    SELECT entry_id, path, page_index, pages_total, labelled, probability,
        min({PICK_SEARCH}, last_index + 1) AS pages_leading,
        sum(labelled AND page_index < {PICK_SEARCH})
            OVER (PARTITION BY entry_id) AS pages_labelled,
        row_number() OVER (
            PARTITION BY entry_id
            ORDER BY labelled AND page_index < {PICK_SEARCH} DESC,
                probability DESC, page_index
        ) AS position
    FROM leading
)
SELECT
    entry_id, pages_total, pages_leading, pages_labelled,
    CASE WHEN {_PICKED} THEN path END AS path,
    CASE WHEN {_PICKED} THEN page_index END AS page_index,
    CASE WHEN {_PICKED} THEN probability END AS probability
FROM ranked
WHERE position = 1""",
)
"""The tables and views version 2 adds; a version 1 store is migrated by them."""

_GENERATION_FLOOR = (
    "COALESCE((SELECT CAST(value AS INTEGER) FROM meta "
    "WHERE key = 'generation_floor'), 0)"
)
"""The generation a new work row starts from (raised by every drop)."""

_ACQUIRE = """
UPDATE works SET
    lease_holder = :holder, lease_expires_at = :until,
    lease_generation = lease_generation + 1,
    title = COALESCE(:title, title),
    creator = COALESCE(:creator, creator), year = COALESCE(:year, year),
    place = COALESCE(:place, place), publisher = COALESCE(:publisher, publisher),
    language = COALESCE(:language, language),
    edition = COALESCE(:edition, edition), link = COALESCE(:link, link),
    last_run_id = :holder, updated_at = :stamp
WHERE entry_id = :entry_id
AND (lease_holder IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= :now
     OR lease_holder = :holder OR lease_holder = :dead)
RETURNING lease_generation, folder
"""
_UPSERT_CANDIDATE = """
INSERT INTO candidates (
    entry_id, run_id, origin, rank, provider, source_id, title, creators, date,
    year, place, publisher, language, item_url, iiif_manifest, raw, text_score,
    judge_probabilities, judge_decision, judge_model, judge_model_version,
    judged_at, created_at, updated_at
) VALUES (
    :entry_id, :run_id, :origin, :rank, :provider, :source_id, :title,
    :creators, :date, :year, :place, :publisher, :language, :item_url,
    :iiif_manifest, :raw, :text_score, :judge_probabilities, :judge_decision,
    :judge_model, :judge_model_version, :judged_at, :stamp, :stamp
)
ON CONFLICT (entry_id, provider, source_id) WHERE superseded_at IS NULL
DO UPDATE SET
    run_id = excluded.run_id, origin = excluded.origin, rank = excluded.rank,
    title = excluded.title, creators = excluded.creators, date = excluded.date,
    year = excluded.year, place = excluded.place,
    publisher = excluded.publisher, language = excluded.language,
    item_url = excluded.item_url, iiif_manifest = excluded.iiif_manifest,
    raw = excluded.raw,
    text_score = COALESCE(excluded.text_score, candidates.text_score),
    judge_probabilities = CASE WHEN excluded.judged_at IS NULL
        THEN candidates.judge_probabilities ELSE excluded.judge_probabilities END,
    judge_decision = CASE WHEN excluded.judged_at IS NULL
        THEN candidates.judge_decision ELSE excluded.judge_decision END,
    judge_model = CASE WHEN excluded.judged_at IS NULL
        THEN candidates.judge_model ELSE excluded.judge_model END,
    judge_model_version = CASE WHEN excluded.judged_at IS NULL
        THEN candidates.judge_model_version ELSE excluded.judge_model_version END,
    judged_at = COALESCE(excluded.judged_at, candidates.judged_at),
    updated_at = excluded.updated_at
RETURNING candidate_id
"""
_INSERT_ATTEMPT = """
INSERT INTO attempts (
    entry_id, run_id, lease_generation, commit_id, candidate_id, provider,
    source_id, format, outcome, parts, stopped, quota_charged, error,
    started_at, finished_at
) VALUES (
    :entry_id, :run_id, :generation, :commit_id, :candidate_id, :provider,
    :source_id, :format, :outcome, :parts, :stopped, :quota_charged, :error,
    :started_at, :finished_at
)
"""
_UPSERT_FILE = """
INSERT INTO files (
    entry_id, path, kind, item, size, content_type, source_url, label,
    declared_format, attempt_id, recorded_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (entry_id, path) DO UPDATE SET
    kind = excluded.kind, item = excluded.item, size = excluded.size,
    content_type = excluded.content_type, source_url = excluded.source_url,
    label = excluded.label, declared_format = excluded.declared_format,
    attempt_id = excluded.attempt_id, recorded_at = excluded.recorded_at
"""
_UPSERT_LABEL = """
INSERT INTO page_labels (
    entry_id, path, attempt_id, page_index, page_type, probabilities,
    taxonomy_version, profile, model, dialect, fingerprint, run_id, created_at
)
SELECT :entry_id, :path, :attempt_id, :page_index, :page_type, :probabilities,
    :taxonomy_version, :profile, :model, :dialect, :fingerprint, :run_id, :stamp
WHERE EXISTS (
    SELECT 1 FROM files WHERE entry_id = :entry_id AND path = :path
    AND attempt_id = :attempt_id AND kind = 'image'
)
ON CONFLICT (entry_id, path) DO UPDATE SET
    attempt_id = excluded.attempt_id, page_index = excluded.page_index,
    page_type = excluded.page_type, probabilities = excluded.probabilities,
    taxonomy_version = excluded.taxonomy_version, profile = excluded.profile,
    model = excluded.model, dialect = excluded.dialect,
    fingerprint = excluded.fingerprint, run_id = excluded.run_id,
    created_at = excluded.created_at
"""
"""Lands only while the files row still carries the label's attempt."""


# Errors


class StoreError(Exception):
    """A database the store refuses to open; the CLI exits with code 2."""

    exit_code: ExitCode = ExitCode.USAGE


class NewerSchemaError(StoreError):
    """The database carries a schema version newer than this tool knows."""


class StoreBusyError(StoreError):
    """A version 1 store awaits migration while an older run holds works."""


class VersionlessDatabaseError(StoreError):
    """The database has tables but no schema version: it is not a store."""


class UnreadableDatabaseError(StoreError):
    """SQLite cannot read the file as a database."""


class LeaseLostError(RuntimeError):
    """This holder no longer holds the work's lease; the write was refused."""

    def __init__(self, message: str, lease: Lease) -> None:
        super().__init__(message)
        self.lease = lease


class FolderConflictError(ValueError):
    """Another work already has the folder name (compared without case)."""


class NotSelectedError(RuntimeError):
    """The work's stored state no longer fits the run; nothing was written.

    *row* is the work's ``work_status`` row as the acquisition found it.
    """

    def __init__(self, entry_id: str, row: Mapping[str, Any]) -> None:
        super().__init__(f"Work {entry_id!r} no longer fits the run's selection.")
        self.entry_id = entry_id
        self.row = row


# Inputs and rows


@dataclass(frozen=True)
class WorkInput:
    """A work as the input names it; every field but the title is optional."""

    entry_id: str
    title: str
    creator: str | None = None
    year: str | None = None
    place: str | None = None
    publisher: str | None = None
    language: str | None = None
    edition: str | None = None
    link: str | None = None


@dataclass(frozen=True)
class Lease:
    """A work lease: holder (a run id), generation, expiry (epoch s), folder."""

    entry_id: str
    holder: str
    generation: int
    expires_at: float
    folder: str


@dataclass(frozen=True)
class Judgment:
    """The judge's answer on a candidate: probabilities, decision and model."""

    probabilities: Mapping[str, Any]
    decision: str
    model: str
    model_version: str | None = None


@dataclass(frozen=True)
class CandidateInput:
    """A candidate to record: the record, how it was found, its scores."""

    record: SearchRecord
    origin: CandidateOrigin = "search"
    rank: int | None = None
    text_score: float | None = None
    judgment: Judgment | None = None


@dataclass(frozen=True)
class Promotion:
    """A staged file and its final path relative to the work folder."""

    staged: StagedFile
    path: str


@dataclass(frozen=True)
class WorkRow:
    """A ``works`` row."""

    entry_id: str
    title: str
    creator: str | None
    year: str | None
    place: str | None
    publisher: str | None
    language: str | None
    edition: str | None
    link: str | None
    folder: str
    status: StoredStatus | None
    status_reason: str | None
    retry_after: float | None
    selected_candidate: int | None
    lease_holder: str | None
    lease_generation: int
    lease_expires_at: float | None
    first_run_id: str | None
    last_run_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CandidateRow:
    """A ``candidates`` row; ``record`` rebuilds the search record."""

    candidate_id: int
    entry_id: str
    run_id: str
    origin: CandidateOrigin
    rank: int | None
    record: SearchRecord
    text_score: float | None
    judgment: Judgment | None
    judged_at: str | None
    superseded_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AttemptRow:
    """An ``attempts`` row; ``parts`` rebuilds each part's completeness."""

    attempt_id: int
    entry_id: str
    run_id: str
    lease_generation: int
    candidate_id: int | None
    provider: str
    source_id: str
    format: str
    outcome: str
    parts: tuple[Completeness, ...]
    stopped: str | None
    quota_charged: bool
    error: str | None
    started_at: str | None
    finished_at: str


@dataclass(frozen=True)
class FileRow:
    """A ``files`` row; ``path`` is relative to the work folder."""

    entry_id: str
    path: str
    kind: str
    item: str
    size: int
    content_type: str | None
    source_url: str | None
    label: str | None
    declared_format: str | None
    attempt_id: int
    recorded_at: str


@dataclass(frozen=True)
class PageLabel:
    """The page type of one page image, keyed to its ``files`` row.

    *attempt_id* is the attempt of that row when the page was asked;
    *page_index* its position in the source (module docstring, Page index);
    *fingerprint* the decision request's (``request_fingerprint``). A label
    read back carries *run_id* and *created_at*.
    """

    path: str
    attempt_id: int
    page_index: int
    page_type: str
    probabilities: Mapping[str, float]
    taxonomy_version: str
    profile: str
    model: str
    dialect: str
    fingerprint: str
    run_id: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class Reconciliation:
    """What :meth:`Store.reconcile` repaired.

    ``missing`` names the ``files`` rows deleted because their file was gone
    (entry id, path); ``rolled_back`` the final files of interrupted commits
    deleted or restored; ``staging_removed`` the staging folders removed
    (entry id, generation); ``downgraded`` the works set from ``complete``
    to ``partial``; ``live`` the works skipped because a live run holds them.
    """

    missing: tuple[tuple[str, str], ...] = ()
    rolled_back: tuple[tuple[str, str], ...] = ()
    staging_removed: tuple[tuple[str, int], ...] = ()
    downgraded: tuple[str, ...] = ()
    live: tuple[str, ...] = ()


@dataclass(frozen=True)
class DropRefusal:
    """A work :meth:`Store.drop` left in place, and why."""

    entry_id: str
    reason: str


@dataclass(frozen=True)
class DropReport:
    """The outcome of :meth:`Store.drop`.

    ``dropped`` names the works deleted, ``deleted_files`` their recorded
    files removed and ``kept_files`` the files in their folders no row
    recorded, which stay; in a dry run they name what a real drop would do.
    """

    dry_run: bool
    dropped: tuple[str, ...] = ()
    refused: tuple[DropRefusal, ...] = ()
    deleted_files: tuple[Path, ...] = ()
    kept_files: tuple[Path, ...] = ()


# Values


def _iso(timestamp: float) -> str:
    stamp = datetime.fromtimestamp(timestamp, tz=UTC)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _masked(text: str | None) -> str | None:
    return None if text is None else redact_text(text)


def bounded_error(text: str | None, limit: int = ERROR_TEXT_LIMIT) -> str | None:
    """Return *text* cut to its first *limit* characters, marked when cut."""
    if text is None or len(text) <= limit:
        return text
    return f"{text[:limit]} [... {len(text) - limit:,} more characters]"


def _parts_json(parts: Sequence[Completeness]) -> str:
    return json.dumps(
        {
            part.part: {
                "expected": list(part.expected),
                "present": list(part.present),
                "page_limit": part.page_limit,
                "available": part.available,
                "causes": dict(part.causes),
            }
            for part in parts
        },
        ensure_ascii=False,
    )


def _parts(text: str) -> tuple[Completeness, ...]:
    decoded: dict[str, dict[str, Any]] = json.loads(text)
    parts: list[Completeness] = []
    for name, value in decoded.items():
        part: Part = "images" if name == "images" else "pdf"
        parts.append(
            Completeness(
                part=part,
                expected=tuple(value["expected"]),
                present=tuple(value["present"]),
                page_limit=value["page_limit"],
                available=value["available"],
                causes=value["causes"],
            )
        )
    return tuple(parts)


def _outcome(result: DownloadResult) -> str:
    if result.complete:
        return "complete"
    if any(part.present for part in result.parts):
        return "partial"
    return "failed"


# Schema


def _schema_version(connection: sqlite3.Connection, path: Path) -> int | None:
    """Return the schema version, None for an empty database; raise to refuse."""
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables:
        return None
    row = None
    if "meta" in tables:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    try:
        version = int(row[0]) if row is not None else 0
    except ValueError:
        version = 0
    if version < 1:
        raise VersionlessDatabaseError(
            f"{path} holds tables but no schema version: it is not a "
            "chronodownloader store and is left as it is."
        )
    if version > SCHEMA_VERSION:
        raise NewerSchemaError(
            f"{path} carries schema version {version}; this tool knows version "
            f"{SCHEMA_VERSION}. Use a newer chronodownloader."
        )
    return version


def check_schema(
    path: Path, *, busy_timeout: float = DEFAULT_BUSY_TIMEOUT
) -> int | None:
    """Return the schema version of the database at *path* without writing.

    None means no database, or an empty one.

    Raises:
        VersionlessDatabaseError: Tables but no schema version.
        NewerSchemaError: A schema version newer than :data:`SCHEMA_VERSION`.
        UnreadableDatabaseError: SQLite cannot read the file.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        return read(
            path,
            lambda connection: _schema_version(connection, path),
            busy_timeout=busy_timeout,
        )
    except sqlite3.DatabaseError as exc:
        if is_busy(exc):
            raise
        raise UnreadableDatabaseError(
            f"{path} is not a readable SQLite database ({exc}); it is left as it is."
        ) from exc
    except OSError as exc:
        raise UnreadableDatabaseError(
            f"{path} cannot be opened as a database ({exc}); it is left as it is."
        ) from exc


def _initialize(
    connection: sqlite3.Connection, path: Path, now: float, *, migrate: bool = True
) -> None:
    """Create a missing schema or migrate a version 1 store, in one transaction.

    Without *migrate* an existing store stays at its version. A version 1
    store is not migrated while a work's lease is unexpired at *now*: only a
    version 1 process can hold it, and it keeps writing as version 1.

    Raises:
        StoreBusyError: A version 1 store has a work under an unexpired lease.
    """
    version = _schema_version(connection, path)
    if version == SCHEMA_VERSION or (version is not None and not migrate):
        return
    if version is not None:
        leased = connection.execute(
            "SELECT count(*) FROM works WHERE lease_holder IS NOT NULL "
            "AND lease_expires_at > ?",
            (now,),
        ).fetchone()[0]
        if leased:
            raise StoreBusyError(
                f"{path}: a run of an older chronodownloader holds works in this "
                "store; let it finish, then rerun."
            )
    if version is None:
        for statement in _SCHEMA_V1:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '1')"
        )
    for statement in _SCHEMA_V2:
        connection.execute(statement)
    connection.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        (str(SCHEMA_VERSION),),
    )
    if version is not None:
        logger.info("Store %s migrated to schema version %d.", path, SCHEMA_VERSION)


# Leases and staging


def _work_status_row(
    connection: sqlite3.Connection, entry_id: str
) -> dict[str, Any] | None:
    """Return the work's ``work_status`` row, or None for an unknown work."""
    cursor = connection.execute(
        "SELECT * FROM work_status WHERE entry_id = ?", (entry_id,)
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _Row([description[0] for description in cursor.description], row)


def _dead_holder(
    connection: sqlite3.Connection, entry_id: str, holder: str | None, now: float
) -> str | None:
    """Return the run id of the work's lease holder when its process is dead.

    Only an unexpired lease whose holder runs on this host counts; else None.
    *holder* is this run, which is never dead.

    This is the one liveness rule of acquisition and reconciliation: a lease
    is live while it is unexpired and its holder is on another host, or its
    process on this host is alive. A holder in this process is alive; a
    holder without a ``runs`` row, or whose row names no pid, keeps its
    lease until expiry.
    """
    row = connection.execute(
        "SELECT w.lease_holder, r.pid, r.host, r.process_created FROM works AS w "
        "JOIN runs AS r ON r.run_id = w.lease_holder "
        "WHERE w.entry_id = ? AND w.lease_holder IS NOT ? "
        "AND w.lease_expires_at > ?",
        (entry_id, holder, now),
    ).fetchone()
    if row is None:
        return None
    other, pid, host, created = row
    if pid is None or host != socket.gethostname() or pid == os.getpid():
        return None
    if process_alive(int(pid), None if created is None else float(created)):
        return None
    logger.info("Work %s: its lease holder %s (pid %s) is gone.", entry_id, other, pid)
    return str(other)


def _check_lease(connection: sqlite3.Connection, lease: Lease, now: float) -> None:
    """Raise unless *lease* holds and has not expired at *now*.

    Holder, generation and expiry are compared with the row read inside the
    transaction; *now* is the clock reading taken inside it.
    """
    row = connection.execute(
        "SELECT lease_holder, lease_generation, lease_expires_at FROM works "
        "WHERE entry_id = ?",
        (lease.entry_id,),
    ).fetchone()
    if row is None:
        raise LeaseLostError(f"Work {lease.entry_id!r} does not exist.", lease)
    holder, generation, expires_at = row
    if holder != lease.holder or generation != lease.generation:
        raise LeaseLostError(
            f"The lease on work {lease.entry_id!r} is no longer this run's.", lease
        )
    if expires_at is None or expires_at <= now:
        raise LeaseLostError(f"The lease on work {lease.entry_id!r} expired.", lease)


# The store


class Store:
    """The store of the output root *root*, opened for one process.

    Opening creates the root and a missing or empty database and refuses one
    it does not know (:class:`StoreError`). With *reconcile* (the default),
    :meth:`reconcile` runs once and its report is :attr:`reconciliation`.
    Without *migrate* a version 1 store is not migrated: such a store serves
    reads and a dry-run :meth:`drop`, nothing that touches page labels.
    Call :meth:`begin_run` before acquiring leases. Each thread uses its own
    connection, opened on first use; :meth:`close` stops the lease keepers,
    releases this run's leases and closes the connections. *clock* returns
    epoch seconds and is injectable for tests.

    Raises:
        StoreError: The database is versionless, newer or unreadable.
        DatabaseBusyError: The database stayed busy for *busy_timeout*.
    """

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock = time.time,
        lease_ttl: float = LEASE_TTL,
        busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
        reconcile: bool = True,
        migrate: bool = True,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / DATABASE_NAME
        self.clock = clock
        self.lease_ttl = lease_ttl
        self.busy_timeout = busy_timeout
        self.reconciliation: Reconciliation | None = None
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._keepers: list[LeaseKeeper] = []
        self._held: dict[str, Lease] = {}
        self._run_id: str | None = None
        self._closed = False
        check_schema(self.path, busy_timeout=busy_timeout)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self._transact(
                lambda connection: _initialize(
                    connection, self.path, self.clock(), migrate=migrate
                )
            )
            if reconcile:
                self.reconciliation = self.reconcile()
        except BaseException:  # cleanup on every exit, interrupts too; re-raised
            self._close_connections()
            raise

    # Connections

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def run_id(self) -> str | None:
        """Return the id of the run begun by this store, or None."""
        return self._run_id

    def close(self, *, settle: bool = True) -> None:
        """Stop the keepers, release this run's leases and close the store.

        Without *settle* (an interrupted run) the leases' staging folders
        stay as they are (see :meth:`release`). Later calls raise
        :class:`RuntimeError`.
        """
        with self._lock:
            keepers, self._keepers = self._keepers, []
        for keeper in keepers:
            keeper.stop()
        if not self._closed and self._run_id is not None:
            for lease in list(self.held_leases().values()):
                with contextlib.suppress(sqlite3.Error, OSError):
                    self.release(lease, settle=settle)
            holder = self._run_id
            with contextlib.suppress(sqlite3.Error):
                self._transact(
                    lambda connection: connection.execute(
                        "UPDATE works SET lease_holder = NULL, "
                        "lease_expires_at = NULL WHERE lease_holder = ?",
                        (holder,),
                    )
                )
        self._close_connections()

    def _close_connections(self) -> None:
        with self._lock:
            self._closed = True
            connections, self._connections = self._connections, []
            self._held.clear()
        for connection in connections:
            connection.close()

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError(f"The store at {self.path} is closed.")
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is None:
            connection = connect(
                self.path, busy_timeout=self.busy_timeout, check_same_thread=False
            )
            with self._lock:
                if self._closed:
                    connection.close()
                    raise RuntimeError(f"The store at {self.path} is closed.")
                self._connections.append(connection)
            self._local.connection = connection
        return connection

    def _transact(self, func: Callable[[sqlite3.Connection], T]) -> T:
        return transact(self._connection(), func, busy_timeout=self.busy_timeout)

    def _leased(self, lease: Lease, func: Callable[[sqlite3.Connection], T]) -> T:
        def write(connection: sqlite3.Connection) -> T:
            _check_lease(connection, lease, self.clock())
            return func(connection)

        try:
            return self._transact(write)
        except LeaseLostError:
            self._forget(lease)
            raise

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[_Row]:
        connection = self._connection()
        cursor = connection.execute(sql, tuple(params))
        names = [description[0] for description in cursor.description]
        return [_Row(names, row) for row in cursor.fetchall()]

    def _require_run(self) -> str:
        if self._run_id is None:
            raise RuntimeError("Begin a run before acquiring leases or writing.")
        return self._run_id

    def _work_dir(self, folder: str) -> Path:
        return self.root / folder

    def _staging_path(self, lease: Lease) -> Path:
        return _promotion.staging_path(self._work_dir(lease.folder), lease.generation)

    # Runs

    def begin_run(
        self,
        command: str,
        *,
        run_id: str | None = None,
        spec: Mapping[str, Any] | None = None,
        tool_version: str | None = None,
    ) -> str:
        """Record a run and return its id, the holder of its leases.

        *run_id* defaults to a new UUID; pass the coordination holder's to
        share it.
        """
        if self._run_id is not None:
            raise RuntimeError(f"Run {self._run_id} already began on this store.")
        run = run_id or str(uuid.uuid4())
        row = (
            run,
            command,
            tool_version,
            os.getpid(),
            socket.gethostname(),
            own_creation(),
            _masked(_json(spec)),
            _iso(self.clock()),
        )
        self._transact(
            lambda connection: connection.execute(
                "INSERT INTO runs (run_id, command, tool_version, pid, host, "
                "process_created, spec, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        )
        self._run_id = run
        return run

    def end_run(self, exit_code: int) -> None:
        """Record the end time and exit code of the current run."""
        row = (_iso(self.clock()), int(exit_code), self._require_run())
        self._transact(
            lambda connection: connection.execute(
                "UPDATE runs SET ended_at = ?, exit_code = ? WHERE run_id = ?", row
            )
        )

    # Leases

    def acquire(
        self,
        work: WorkInput,
        folder: str,
        *,
        select: Callable[[Mapping[str, Any] | None], bool] | None = None,
        update: bool = True,
    ) -> Lease | None:
        """Lease *work* for this run; None while another run holds it.

        A missing work row is created with *folder* (its name under the
        root); an existing work keeps its stored folder. The lease is taken
        when it is free, expired or this run's own, or when its holder is a
        dead process on this host (see the module docstring); the acquisition
        increments the generation and updates the input fields (a field the
        input leaves empty keeps its stored value). Without *update* the
        stored input fields stay as they are.

        *select*, when given, is asked inside the acquisition's transaction,
        once the lease is obtainable, with the work's ``work_status`` row as
        it stood before (None for a work the store does not hold yet). When
        it answers False the transaction is rolled back, so nothing is
        written, and :class:`NotSelectedError` is raised.

        Raises:
            FolderConflictError: A new work's *folder* belongs to another.
            NotSelectedError: *select* refused the work.
            ValueError: *folder* is not a single plain name.
        """
        holder = self._require_run()
        _promotion.check_folder(folder)
        if not work.entry_id or not work.title:
            raise ValueError("A work needs an entry_id and a title.")

        def write(connection: sqlite3.Connection) -> Lease | None:
            now = self.clock()
            stamp = _iso(now)
            before = _work_status_row(connection, work.entry_id)
            try:
                connection.execute(
                    "INSERT INTO works (entry_id, title, folder, lease_generation, "
                    "first_run_id, last_run_id, created_at, updated_at) VALUES "
                    f"(?, ?, ?, {_GENERATION_FLOOR}, ?, ?, ?, ?) "
                    "ON CONFLICT (entry_id) DO NOTHING",
                    (work.entry_id, work.title, folder, holder, holder, stamp, stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise FolderConflictError(
                    f"Folder {folder!r} of work {work.entry_id!r} belongs to "
                    "another work."
                ) from exc
            fields = {
                "title": work.title,
                "creator": work.creator,
                "year": work.year,
                "place": work.place,
                "publisher": work.publisher,
                "language": work.language,
                "edition": work.edition,
                "link": _masked(work.link),
            }
            params = {
                "holder": holder,
                "until": now + self.lease_ttl,
                "now": now,
                "stamp": stamp,
                "entry_id": work.entry_id,
                **(fields if update else dict.fromkeys(fields)),
                "dead": _dead_holder(connection, work.entry_id, holder, now),
            }
            row = connection.execute(_ACQUIRE, params).fetchone()
            if row is None:
                return None
            if select is not None and not select(before):
                raise NotSelectedError(work.entry_id, before or {})
            return Lease(
                work.entry_id, holder, int(row[0]), now + self.lease_ttl, str(row[1])
            )

        lease = self._transact(write)
        if lease is not None:
            with self._lock:
                self._held[lease.entry_id] = lease
        return lease

    def renew(self, lease: Lease) -> Lease:
        """Extend *lease* by the lease TTL counted from the clock now.

        Raises:
            LeaseLostError: The lease expired or passed to another holder.
        """

        def write(connection: sqlite3.Connection) -> Lease:
            now = self.clock()
            cursor = connection.execute(
                "UPDATE works SET lease_expires_at = ? WHERE entry_id = ? "
                "AND lease_holder = ? AND lease_generation = ? "
                "AND lease_expires_at > ?",
                (
                    now + self.lease_ttl,
                    lease.entry_id,
                    lease.holder,
                    lease.generation,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                raise LeaseLostError(
                    f"The lease on work {lease.entry_id!r} cannot be renewed: it "
                    "expired or passed to another holder.",
                    lease,
                )
            return Lease(
                lease.entry_id,
                lease.holder,
                lease.generation,
                now + self.lease_ttl,
                lease.folder,
            )

        renewed = self._transact(write)
        with self._lock:
            held = self._held.get(lease.entry_id)
            if held is not None and held.generation == lease.generation:
                self._held[lease.entry_id] = renewed
        return renewed

    def release(self, lease: Lease, *, settle: bool = True) -> None:
        """Give *lease* up if this holder still has it; discard its staging.

        The staging folder of the lease's generation is settled (see the
        repair rules) and removed. Without *settle* (an interrupted run, which
        must not touch files while a worker may still write them) the folder
        stays; the next holder's :meth:`staging_dir` or the reconciliation of
        a later open settles it.
        """
        self._forget(lease)
        work_dir = self._work_dir(lease.folder)
        gen_dir = self._staging_path(lease)

        def write(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "UPDATE works SET lease_holder = NULL, lease_expires_at = NULL "
                "WHERE entry_id = ? AND lease_holder = ? AND lease_generation = ?",
                (lease.entry_id, lease.holder, lease.generation),
            )
            if settle and cursor.rowcount and gen_dir.is_dir():
                _promotion.settle_generation(
                    connection, lease.entry_id, lease.generation, work_dir, gen_dir
                )

        self._transact(write)

    def held_leases(self) -> dict[str, Lease]:
        """Return the leases this store holds, by entry id."""
        with self._lock:
            return dict(self._held)

    def _forget(self, lease: Lease) -> None:
        with self._lock:
            held = self._held.get(lease.entry_id)
            if held is not None and held.generation == lease.generation:
                del self._held[lease.entry_id]

    def keeper(
        self,
        *,
        interval: float = LEASE_RENEW_INTERVAL,
        on_lost: Callable[[Lease], None] | None = None,
    ) -> LeaseKeeper:
        """Return a keeper that renews the held leases every *interval* s."""
        return LeaseKeeper(self, interval=interval, on_lost=on_lost)

    def _register(self, keeper: LeaseKeeper) -> None:
        with self._lock:
            self._keepers.append(keeper)

    # Writes under a lease

    def staging_dir(self, lease: Lease) -> Path:
        """Return the staging folder of *lease*, created; clear older ones.

        The staging folders of other generations of the work are settled by
        the repair rules and removed, in the same lease-checked transaction.

        Raises:
            LeaseLostError: The lease expired or passed to another holder.
        """
        work_dir = self._work_dir(lease.folder)
        path = self._staging_path(lease)

        def write(connection: sqlite3.Connection) -> Path:
            for generation, gen_dir in _promotion.generation_dirs(work_dir):
                if generation != lease.generation:
                    _promotion.settle_generation(
                        connection, lease.entry_id, generation, work_dir, gen_dir
                    )
            path.mkdir(parents=True, exist_ok=True)
            return path

        return self._leased(lease, write)

    def set_status(
        self,
        lease: Lease,
        status: StoredStatus | None,
        *,
        reason: str | None = None,
        retry_after: float | None = None,
        selected: int | None = None,
    ) -> None:
        """Record the work's status and its reason.

        *retry_after* (epoch s) is required for ``deferred`` and refused for
        any other status. *selected* names the chosen candidate of the work;
        None keeps the stored choice. Status None clears the outcome: the
        work is pending again (``new`` in ``work_status``), as after a
        ``search`` that accepted a candidate without downloading it.

        Raises:
            LeaseLostError: The lease expired or passed to another holder.
            ValueError: An unknown status, a misplaced *retry_after*, or a
                candidate of another work.
        """
        params = self._status_params(status, reason, retry_after)

        def write(connection: sqlite3.Connection) -> None:
            self._apply_status(connection, lease, params, selected)

        self._leased(lease, write)

    @staticmethod
    def _status_params(
        status: StoredStatus | None, reason: str | None, retry_after: float | None
    ) -> tuple[str | None, str | None, float | None]:
        if status is not None and status not in STORED_STATUSES:
            raise ValueError(f"not a stored status: {status!r}")
        if (status == "deferred") != (retry_after is not None):
            raise ValueError("retry_after goes with status deferred, and only with it")
        return status, reason, retry_after

    def _apply_status(
        self,
        connection: sqlite3.Connection,
        lease: Lease,
        params: tuple[str | None, str | None, float | None],
        selected: int | None,
    ) -> None:
        if selected is not None:
            owner = connection.execute(
                "SELECT entry_id FROM candidates WHERE candidate_id = ?", (selected,)
            ).fetchone()
            if owner is None or owner[0] != lease.entry_id:
                raise ValueError(
                    f"Candidate {selected} is not a candidate of {lease.entry_id!r}."
                )
        status, reason, retry_after = params
        connection.execute(
            "UPDATE works SET status = ?, status_reason = ?, retry_after = ?, "
            "selected_candidate = COALESCE(?, selected_candidate), "
            "last_run_id = ?, updated_at = ? WHERE entry_id = ?",
            (
                status,
                reason,
                retry_after,
                selected,
                lease.holder,
                _iso(self.clock()),
                lease.entry_id,
            ),
        )

    def add_candidates(
        self,
        lease: Lease,
        candidates: Sequence[CandidateInput],
        *,
        research: bool = False,
    ) -> tuple[int, ...]:
        """Record candidates of the work; return their ids in order.

        A candidate already current for the same provider and source id is
        updated in place; its text score and judgment are kept unless new
        ones are given. With *research*, the current candidates first become
        history (``superseded_at``) and the work's selection is cleared.

        Raises:
            LeaseLostError: The lease expired or passed to another holder.
            ValueError: An unknown origin.
        """
        run_id = self._require_run()
        for candidate in candidates:
            if candidate.origin not in _ORIGINS:
                raise ValueError(f"unknown candidate origin {candidate.origin!r}")

        def write(connection: sqlite3.Connection) -> tuple[int, ...]:
            stamp = _iso(self.clock())
            if research:
                connection.execute(
                    "UPDATE candidates SET superseded_at = ?, updated_at = ? "
                    "WHERE entry_id = ? AND superseded_at IS NULL",
                    (stamp, stamp, lease.entry_id),
                )
                connection.execute(
                    "UPDATE works SET selected_candidate = NULL WHERE entry_id = ?",
                    (lease.entry_id,),
                )
            ids: list[int] = []
            for candidate in candidates:
                params = _candidate_params(lease.entry_id, run_id, candidate, stamp)
                (candidate_id,) = connection.execute(
                    _UPSERT_CANDIDATE, params
                ).fetchone()
                ids.append(int(candidate_id))
            return tuple(ids)

        return self._leased(lease, write)

    def record_judgment(
        self, lease: Lease, candidate_id: int, judgment: Judgment
    ) -> None:
        """Store the judge's answer on one candidate of the work.

        Raises:
            LeaseLostError: The lease expired or passed to another holder.
            KeyError: The work has no candidate *candidate_id*.
        """

        def write(connection: sqlite3.Connection) -> None:
            stamp = _iso(self.clock())
            cursor = connection.execute(
                "UPDATE candidates SET judge_probabilities = ?, judge_decision = ?, "
                "judge_model = ?, judge_model_version = ?, judged_at = ?, "
                "updated_at = ? WHERE candidate_id = ? AND entry_id = ?",
                (
                    _json(dict(judgment.probabilities)),
                    judgment.decision,
                    judgment.model,
                    judgment.model_version,
                    stamp,
                    stamp,
                    candidate_id,
                    lease.entry_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(
                    f"Work {lease.entry_id!r} has no candidate {candidate_id}."
                )

        self._leased(lease, write)

    def record_page_labels(
        self, lease: Lease, labels: Sequence[PageLabel]
    ) -> tuple[int, int]:
        """Store page labels of the work; return the labels written and dropped.

        One lease-checked transaction writes each label, replacing the
        path's earlier one, only while the path's ``files`` row is a page
        image that still carries the label's ``attempt_id``; a label whose
        row changed or went since it was asked is dropped as stale.

        Raises:
            LeaseLostError: The lease expired or passed to another holder;
                nothing was written.
        """
        run_id = self._require_run()

        def write(connection: sqlite3.Connection) -> tuple[int, int]:
            stamp = _iso(self.clock())
            written = 0
            for label in labels:
                cursor = connection.execute(
                    _UPSERT_LABEL,
                    {
                        "entry_id": lease.entry_id,
                        "path": label.path,
                        "attempt_id": label.attempt_id,
                        "page_index": label.page_index,
                        "page_type": label.page_type,
                        "probabilities": json.dumps(dict(label.probabilities)),
                        "taxonomy_version": label.taxonomy_version,
                        "profile": label.profile,
                        "model": label.model,
                        "dialect": label.dialect,
                        "fingerprint": label.fingerprint,
                        "run_id": run_id,
                        "stamp": stamp,
                    },
                )
                written += cursor.rowcount
            return written, len(labels) - written

        return self._leased(lease, write)

    def drop_page_labels(self, lease: Lease, fingerprint: str) -> int:
        """Delete the work's labels of another request fingerprint; return how many.

        Raises:
            LeaseLostError: The lease expired or passed to another holder;
                nothing was deleted.
        """

        def write(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM page_labels WHERE entry_id = ? AND fingerprint != ?",
                (lease.entry_id, fingerprint),
            )
            return cursor.rowcount

        return self._leased(lease, write)

    def remove_files(self, lease: Lease, paths: Sequence[str]) -> tuple[str, ...]:
        """Delete recorded files of the work and their rows; return the paths.

        One lease-checked transaction deletes the ``files`` rows of *paths*
        (relative to the work folder) and then the files on disk, before
        COMMIT, as :meth:`drop` does; a path no row records is left alone.
        Should the commit fail, the reconciliation of a later open removes
        the rows of the files that are gone.

        Raises:
            LeaseLostError: The lease expired or passed to another holder;
                nothing was deleted.
        """
        work_dir = self._work_dir(lease.folder)

        def write(connection: sqlite3.Connection) -> tuple[str, ...]:
            removed: list[str] = []
            for path in dict.fromkeys(paths):
                cursor = connection.execute(
                    "DELETE FROM files WHERE entry_id = ? AND path = ?",
                    (lease.entry_id, path),
                )
                if cursor.rowcount:
                    removed.append(path)
            connection.executemany(
                "DELETE FROM page_labels WHERE entry_id = ? AND path = ?",
                [(lease.entry_id, path) for path in removed],
            )
            for path in removed:
                (work_dir / _promotion.final_path(path)).unlink(missing_ok=True)
            return tuple(removed)

        return self._leased(lease, write)

    def commit_download(
        self,
        lease: Lease,
        result: DownloadResult,
        promotions: Sequence[Promotion] = (),
        *,
        status: StoredStatus | None = None,
        reason: str | None = None,
        candidate_id: int | None = None,
        started_at: float | None = None,
        error: str | None = None,
    ) -> int:
        """Promote staged files and record the attempt; return its id.

        In one lease-checked transaction, each promotion's staged file (in
        :meth:`staging_dir`) moves to its final path in the work folder and
        is recorded in ``files``, with the attempt row and, when *status* is
        given, the work's status and *reason* (as :meth:`set_status`; a
        deferral, which needs ``retry_after``, goes through
        :meth:`set_status`). The staging folder is removed afterwards, staged
        files left unpromoted included. A failed commit rolls back its
        renames and discards the staging folder (module docstring). A result
        with no parts and no promotions records a failed attempt; *error*
        says why.

        Raises:
            LeaseLostError: The lease expired or passed to another holder;
                no final file was touched.
            ValueError: A promotion names a file the result did not stage,
                twice, two promotions share a final path, a path is not
                plain, or *status* is ``deferred``.
            FileNotFoundError: A staged file is missing.
        """
        run_id = self._require_run()
        status_params = (
            None if status is None else self._status_params(status, reason, None)
        )
        moves = self._plan_moves(lease, result, promotions)
        commit_id = uuid.uuid4().hex
        gen_dir = self._staging_path(lease)
        journal = _promotion.journal_path(gen_dir, commit_id)
        attempt = {
            "entry_id": lease.entry_id,
            "run_id": run_id,
            "generation": lease.generation,
            "commit_id": commit_id,
            "candidate_id": candidate_id,
            "provider": result.provider,
            "source_id": result.source_id,
            "format": result.format,
            "outcome": _outcome(result),
            "parts": _parts_json(result.parts),
            "stopped": result.stopped,
            "quota_charged": int(result.quota_charged),
            "error": bounded_error(_masked(error)),
            "started_at": None if started_at is None else _iso(started_at),
        }

        def write(connection: sqlite3.Connection) -> int:
            stamp = _iso(self.clock())
            if moves:
                _promotion.promote(journal, commit_id, moves)
            cursor = connection.execute(
                _INSERT_ATTEMPT, {**attempt, "finished_at": stamp}
            )
            attempt_id = int(cursor.lastrowid or 0)
            connection.executemany(
                "DELETE FROM page_labels WHERE entry_id = ? AND path = ?",
                [(lease.entry_id, move.final) for move in moves],
            )
            connection.executemany(
                _UPSERT_FILE,
                [
                    (
                        lease.entry_id,
                        move.final,
                        move.staged.kind,
                        move.staged.item,
                        move.staged.size,
                        move.staged.content_type,
                        _masked(move.staged.source_url),
                        move.staged.label,
                        move.staged.declared_format,
                        attempt_id,
                        stamp,
                    )
                    for move in moves
                ],
            )
            if status_params is not None:
                self._apply_status(connection, lease, status_params, None)
            else:
                connection.execute(
                    "UPDATE works SET last_run_id = ?, updated_at = ? "
                    "WHERE entry_id = ?",
                    (lease.holder, stamp, lease.entry_id),
                )
            return attempt_id

        try:
            attempt_id = self._leased(lease, write)
        except BaseException:  # cleanup on every exit, interrupts too; re-raised
            self._discard(lease)
            raise
        if gen_dir.is_dir():
            _promotion.clear_landed(gen_dir, commit_id)
        return attempt_id

    def _plan_moves(
        self, lease: Lease, result: DownloadResult, promotions: Sequence[Promotion]
    ) -> list[Move]:
        staged_names = {staged.name for staged in result.files}
        gen_dir = self._staging_path(lease)
        work_dir = self._work_dir(lease.folder)
        sources: set[str] = set()
        finals: set[str] = set()
        moves: list[Move] = []
        for promotion in promotions:
            staged, final = promotion.staged, promotion.path
            _promotion.relative_path(staged.name, "staged name")
            _promotion.final_path(final)
            if staged.name not in staged_names:
                raise ValueError(f"{staged.name!r} is not a file the result staged.")
            if staged.name in sources:
                raise ValueError(f"{staged.name!r} is promoted twice.")
            if final.casefold() in finals:
                raise ValueError(f"Two promotions share the final path {final!r}.")
            sources.add(staged.name)
            finals.add(final.casefold())
            moves.append(Move(staged, final, gen_dir / staged.name, work_dir / final))
        return moves

    def _discard(self, lease: Lease) -> None:
        """Roll back and remove the staging folder of a failed commit."""
        work_dir = self._work_dir(lease.folder)
        gen_dir = self._staging_path(lease)
        if not gen_dir.is_dir():
            return
        try:
            self._transact(
                lambda connection: _promotion.settle_generation(
                    connection, lease.entry_id, lease.generation, work_dir, gen_dir
                )
            )
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            logger.warning(
                "Staging folder %s could not be discarded; the next "
                "reconciliation settles it: %s",
                gen_dir,
                exc,
            )

    # Reconciliation

    def reconcile(self) -> Reconciliation:
        """Repair the store against the disk (module docstring, Reconciliation).

        Each work needing repair is settled in its own transaction, which
        re-reads its lease and skips it while a live run holds it.
        """
        works = self._query("SELECT entry_id, folder FROM works ORDER BY entry_id")
        recorded: dict[str, list[str]] = {}
        for row in self._query("SELECT entry_id, path FROM files"):
            recorded.setdefault(row["entry_id"], []).append(row["path"])
        report: dict[str, list[Any]] = {
            "missing": [],
            "rolled_back": [],
            "staging_removed": [],
            "downgraded": [],
            "live": [],
        }
        for work in works:
            entry_id, folder = str(work["entry_id"]), str(work["folder"])
            work_dir = self._work_dir(folder)
            gaps = [
                path
                for path in recorded.get(entry_id, [])
                if not (work_dir / path).is_file()
            ]
            if not gaps and not _promotion.generation_dirs(work_dir):
                continue
            found = self._transact(
                functools.partial(
                    self._reconcile_work, entry_id=entry_id, work_dir=work_dir
                )
            )
            for key, values in found.items():
                report[key] += values
        return Reconciliation(**{key: tuple(value) for key, value in report.items()})

    def _reconcile_work(
        self, connection: sqlite3.Connection, *, entry_id: str, work_dir: Path
    ) -> dict[str, list[Any]]:
        """Settle one work's staging and missing files; return what changed."""
        now = self.clock()
        holder, expires_at, status = connection.execute(
            "SELECT lease_holder, lease_expires_at, status FROM works "
            "WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        live = holder is not None and expires_at is not None and expires_at > now
        if live and _dead_holder(connection, entry_id, self._run_id, now) is None:
            return {"live": [entry_id]}
        found: dict[str, list[Any]] = {
            "rolled_back": [],
            "staging_removed": [],
            "downgraded": [],
        }
        for generation, gen_dir in _promotion.generation_dirs(work_dir):
            rolled = _promotion.settle_generation(
                connection, entry_id, generation, work_dir, gen_dir
            )
            found["rolled_back"] += [(entry_id, path) for path in rolled]
            if not gen_dir.exists():
                found["staging_removed"].append((entry_id, generation))
        missing = [
            str(row[0])
            for row in connection.execute(
                "SELECT path FROM files WHERE entry_id = ? ORDER BY path", (entry_id,)
            )
            if not (work_dir / str(row[0])).is_file()
        ]
        for table in ("page_labels", "files"):
            connection.executemany(
                f"DELETE FROM {table} WHERE entry_id = ? AND path = ?",  # noqa: S608
                [(entry_id, path) for path in missing],
            )
        if missing and status == "complete":
            connection.execute(
                "UPDATE works SET status = 'partial', status_reason = ?, "
                "updated_at = ? WHERE entry_id = ?",
                (
                    f"reconcile: {len(missing)} recorded files missing on disk",
                    _iso(now),
                    entry_id,
                ),
            )
            found["downgraded"].append(entry_id)
        found["missing"] = [(entry_id, path) for path in missing]
        return found

    # Drop

    def drop(
        self,
        entry_ids: Sequence[str],
        *,
        dry_run: bool = False,
        statuses: Collection[str] | None = None,
    ) -> DropReport:
        """Delete works with every row keyed to them and their recorded files.

        A work is refused while a run other than this store's own holds an
        unexpired lease on it; an entry id that names no work is refused as
        well, and with *statuses* a work whose stored status (``new``
        without one) is not among them, checked in the deleting transaction.
        For the rest, the ``works``, ``candidates``, ``attempts``, ``files``
        and ``page_labels`` rows are deleted, then the recorded files, the staging
        folders and the ``inspect`` scratch (:func:`.paths.inspect_dir`), and
        the folders left empty; files no row records stay and are reported.
        Runs stay.

        One write transaction classifies the works, deletes their rows and
        then the files before it commits, so no other run can lease a work in
        between; should the commit fail, the rows still describe the files,
        and a second drop finishes. A dry run changes nothing.
        """
        holder = self._run_id
        ids = list(dict.fromkeys(entry_ids))
        gone: set[Path] = set()

        def classify(
            connection: sqlite3.Connection,
        ) -> tuple[list[_Doomed], list[DropRefusal]]:
            now = self.clock()
            dropped: list[_Doomed] = []
            refused: list[DropRefusal] = []
            for entry_id in ids:
                row = connection.execute(
                    "SELECT folder, lease_holder, lease_expires_at, status "
                    "FROM works WHERE entry_id = ?",
                    (entry_id,),
                ).fetchone()
                if row is None:
                    refused.append(DropRefusal(entry_id, "no such work"))
                    continue
                folder, lease_holder, expires_at, status = row
                if statuses is not None and (status or "new") not in statuses:
                    refused.append(
                        DropRefusal(entry_id, f"its status is now {status or 'new'}")
                    )
                    continue
                if (
                    lease_holder is not None
                    and lease_holder != holder
                    and expires_at is not None
                    and expires_at > now
                    and _dead_holder(connection, entry_id, holder, now) is None
                ):
                    refused.append(
                        DropRefusal(entry_id, f"leased by run {lease_holder}")
                    )
                    continue
                work_dir = self._work_dir(str(folder))
                recorded = [
                    work_dir / str(path)
                    for (path,) in connection.execute(
                        "SELECT path FROM files WHERE entry_id = ? ORDER BY path",
                        (entry_id,),
                    )
                ]
                dropped.append((entry_id, work_dir, recorded))
            return dropped, refused

        def settle(
            connection: sqlite3.Connection, *, delete: bool
        ) -> tuple[list[_Doomed], list[DropRefusal], list[Path], list[Path]]:
            dropped, refused = classify(connection)
            if delete and dropped:
                _delete_works(connection, [entry_id for entry_id, _, _ in dropped])
            deleted: list[Path] = []
            kept: list[Path] = []
            for entry_id, work_dir, recorded in dropped:
                files, extra = _promotion.drop_folder(
                    work_dir, recorded, delete=delete, gone=gone
                )
                deleted += files
                kept += extra
                if delete:
                    _promotion.remove_tree(inspect_dir(self.root, entry_id))
            return dropped, refused, deleted, kept

        if dry_run:
            dropped, refused, deleted, kept = read(
                self.path,
                functools.partial(settle, delete=False),
                busy_timeout=self.busy_timeout,
            )
        else:
            dropped, refused, deleted, kept = self._transact(
                functools.partial(settle, delete=True)
            )
            with self._lock:
                for entry_id, _, _ in dropped:
                    self._held.pop(entry_id, None)
        return DropReport(
            dry_run=dry_run,
            dropped=tuple(entry_id for entry_id, _, _ in dropped),
            refused=tuple(refused),
            deleted_files=tuple(deleted),
            kept_files=tuple(kept),
        )

    # Reads

    def work(self, entry_id: str) -> WorkRow | None:
        """Return the ``works`` row of *entry_id*, or None."""
        rows = self._query("SELECT * FROM works WHERE entry_id = ?", (entry_id,))
        return WorkRow(**dict(rows[0])) if rows else None

    def candidates(self, entry_id: str, *, history: bool = False) -> list[CandidateRow]:
        """Return the work's current candidates (with *history*, all), by id."""
        where = "" if history else " AND superseded_at IS NULL"
        rows = self._query(
            f"SELECT * FROM candidates WHERE entry_id = ?{where} ORDER BY candidate_id",
            (entry_id,),
        )
        return [_candidate_row(row) for row in rows]

    def attempts(self, entry_id: str) -> list[AttemptRow]:
        """Return the work's attempts in order."""
        rows = self._query(
            "SELECT * FROM attempts WHERE entry_id = ? ORDER BY attempt_id",
            (entry_id,),
        )
        return [_attempt_row(row) for row in rows]

    def files(self, entry_id: str) -> list[FileRow]:
        """Return the work's recorded files by path."""
        rows = self._query(
            "SELECT * FROM files WHERE entry_id = ? ORDER BY path", (entry_id,)
        )
        return [FileRow(**dict(row)) for row in rows]

    def page_labels(self, entry_id: str) -> list[PageLabel]:
        """Return the work's page labels by page index, stale ones included."""
        rows = self._query(_LABELS, (entry_id,))
        return [_page_label(row) for row in rows]

    def present_items(self, entry_id: str) -> frozenset[str]:
        """Return the page and rendering items the work's files hold."""
        rows = self._query(
            "SELECT DISTINCT item FROM files WHERE entry_id = ? AND kind != 'metadata'",
            (entry_id,),
        )
        return frozenset(str(row["item"]) for row in rows)


type _Doomed = tuple[str, Path, list[Path]]
"""A work to drop: entry id, work folder, recorded files."""


def _delete_works(connection: sqlite3.Connection, entry_ids: Sequence[str]) -> None:
    """Delete the works' rows in every table keyed to them (runs stay).

    The generation floor rises to the highest dropped generation first, so a
    work created again never reuses a generation, nor its staging folder.
    """
    ids = (json.dumps(list(entry_ids)),)
    selected = "entry_id IN (SELECT value FROM json_each(?))"
    connection.execute(
        "INSERT INTO meta (key, value) SELECT 'generation_floor', "
        f"max({_GENERATION_FLOOR}, max(lease_generation)) FROM works WHERE {selected} "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        ids,
    )
    connection.execute(
        f"UPDATE works SET selected_candidate = NULL WHERE {selected}", ids
    )
    for table in ("page_labels", "files", "attempts", "candidates", "works"):
        connection.execute(f"DELETE FROM {table} WHERE {selected}", ids)  # noqa: S608


class _Row(dict[str, Any]):
    """A result row as a dict of column names to values."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        super().__init__(zip(names, values, strict=True))


def _candidate_params(
    entry_id: str, run_id: str, candidate: CandidateInput, stamp: str
) -> dict[str, Any]:
    record, judgment = candidate.record, candidate.judgment
    return {
        "entry_id": entry_id,
        "run_id": run_id,
        "origin": candidate.origin,
        "rank": candidate.rank,
        "provider": record.provider,
        "source_id": record.source_id,
        "title": record.title,
        "creators": json.dumps(list(record.creators), ensure_ascii=False),
        "date": record.date,
        "year": record.year,
        "place": record.place,
        "publisher": record.publisher,
        "language": record.language,
        "item_url": _masked(record.item_url),
        "iiif_manifest": _masked(record.iiif_manifest),
        "raw": _masked(_json(dict(record.raw))),
        "text_score": candidate.text_score,
        "judge_probabilities": None
        if judgment is None
        else _json(dict(judgment.probabilities)),
        "judge_decision": None if judgment is None else judgment.decision,
        "judge_model": None if judgment is None else judgment.model,
        "judge_model_version": None if judgment is None else judgment.model_version,
        "judged_at": None if judgment is None else stamp,
        "stamp": stamp,
    }


def _candidate_row(row: Mapping[str, Any]) -> CandidateRow:
    record = SearchRecord(
        provider=row["provider"],
        source_id=row["source_id"],
        title=row["title"],
        creators=tuple(json.loads(row["creators"])),
        date=row["date"],
        year=row["year"],
        place=row["place"],
        publisher=row["publisher"],
        language=row["language"],
        item_url=row["item_url"],
        iiif_manifest=row["iiif_manifest"],
        raw=MappingProxyType(json.loads(row["raw"]) if row["raw"] else {}),
    )
    judgment = None
    if row["judged_at"] is not None:
        judgment = Judgment(
            probabilities=MappingProxyType(json.loads(row["judge_probabilities"])),
            decision=row["judge_decision"],
            model=row["judge_model"],
            model_version=row["judge_model_version"],
        )
    return CandidateRow(
        candidate_id=row["candidate_id"],
        entry_id=row["entry_id"],
        run_id=row["run_id"],
        origin=row["origin"],
        rank=row["rank"],
        record=record,
        text_score=row["text_score"],
        judgment=judgment,
        judged_at=row["judged_at"],
        superseded_at=row["superseded_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _attempt_row(row: Mapping[str, Any]) -> AttemptRow:
    return AttemptRow(
        attempt_id=row["attempt_id"],
        entry_id=row["entry_id"],
        run_id=row["run_id"],
        lease_generation=row["lease_generation"],
        candidate_id=row["candidate_id"],
        provider=row["provider"],
        source_id=row["source_id"],
        format=row["format"],
        outcome=row["outcome"],
        parts=_parts(row["parts"]),
        stopped=row["stopped"],
        quota_charged=bool(row["quota_charged"]),
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


_LABELS = (
    "SELECT path, attempt_id, page_index, page_type, probabilities, "
    "taxonomy_version, profile, model, dialect, fingerprint, run_id, created_at "
    "FROM page_labels WHERE entry_id = ? ORDER BY page_index, path"
)


def _page_label(row: Mapping[str, Any]) -> PageLabel:
    values = dict(row)
    values["probabilities"] = MappingProxyType(json.loads(values["probabilities"]))
    return PageLabel(**values)


# Read-only access for other processes and commands


def _read_view(
    root: Path,
    sql: str,
    params: Sequence[Any],
    busy_timeout: float,
    *,
    since: int = 1,
) -> list[dict[str, Any]]:
    """Return the rows of *sql*; a store older than version *since* gives none."""
    path = Path(root) / DATABASE_NAME
    version = check_schema(path, busy_timeout=busy_timeout)
    if version is None or version < since:
        return []

    def query(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        cursor = connection.execute(sql, tuple(params))
        names = [description[0] for description in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    return read(path, query, busy_timeout=busy_timeout)


def read_work_status(
    root: Path,
    entry_ids: Sequence[str] | None = None,
    *,
    busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return the ``work_status`` rows of *root* by entry id, never writing.

    *entry_ids* limits the rows; a root without a store gives no rows.

    Raises:
        StoreError: The database is versionless, newer or unreadable.
    """
    if entry_ids is None:
        return _read_view(
            root, "SELECT * FROM work_status ORDER BY entry_id", (), busy_timeout
        )
    return _read_view(
        root,
        "SELECT * FROM work_status WHERE entry_id IN "
        "(SELECT value FROM json_each(?)) ORDER BY entry_id",
        (json.dumps(list(entry_ids)),),
        busy_timeout,
    )


def read_work_candidates(
    root: Path,
    entry_id: str | None = None,
    *,
    busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return the ``work_candidates`` rows of *root* (of *entry_id*), by id.

    Raises:
        StoreError: The database is versionless, newer or unreadable.
    """
    if entry_id is None:
        return _read_view(
            root,
            "SELECT * FROM work_candidates ORDER BY candidate_id",
            (),
            busy_timeout,
        )
    return _read_view(
        root,
        "SELECT * FROM work_candidates WHERE entry_id = ? ORDER BY candidate_id",
        (entry_id,),
        busy_timeout,
    )


def read_candidates(
    root: Path, entry_id: str, *, busy_timeout: float = DEFAULT_BUSY_TIMEOUT
) -> list[CandidateRow]:
    """Return the current candidates of *entry_id* in *root*, never writing.

    The rows carry the full record (``raw`` included), as
    :meth:`Store.candidates` returns them; a root without a store gives none.

    Raises:
        StoreError: The database is versionless, newer or unreadable.
    """
    rows = _read_view(
        root,
        "SELECT * FROM candidates WHERE entry_id = ? AND superseded_at IS NULL "
        "ORDER BY candidate_id",
        (entry_id,),
        busy_timeout,
    )
    return [_candidate_row(row) for row in rows]


def read_files(
    root: Path, entry_id: str, *, busy_timeout: float = DEFAULT_BUSY_TIMEOUT
) -> list[FileRow]:
    """Return the recorded files of *entry_id* in *root* by path, never writing.

    Raises:
        StoreError: The database is versionless, newer or unreadable.
    """
    rows = _read_view(
        root,
        "SELECT * FROM files WHERE entry_id = ? ORDER BY path",
        (entry_id,),
        busy_timeout,
    )
    return [FileRow(**row) for row in rows]


def read_page_labels(
    root: Path, entry_id: str, *, busy_timeout: float = DEFAULT_BUSY_TIMEOUT
) -> list[PageLabel]:
    """Return the page labels of *entry_id* in *root*, never writing.

    The labels come by page index, stale ones included; a version 1 store
    has none.

    Raises:
        StoreError: The database is versionless, newer or unreadable.
    """
    rows = _read_view(root, _LABELS, (entry_id,), busy_timeout, since=2)
    return [_page_label(row) for row in rows]


def read_title_page_picks(
    root: Path,
    entry_ids: Sequence[str] | None = None,
    *,
    busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return the ``title_page_picks`` rows of *root* by entry id, never writing.

    *entry_ids* limits the rows; a version 1 store gives none.

    Raises:
        StoreError: The database is versionless, newer or unreadable.
    """
    sql = "SELECT * FROM title_page_picks"
    params: tuple[str, ...] = ()
    if entry_ids is not None:
        sql += " WHERE entry_id IN (SELECT value FROM json_each(?))"
        params = (json.dumps(list(entry_ids)),)
    return _read_view(root, sql + " ORDER BY entry_id", params, busy_timeout, since=2)


def unstored_folders(
    root: Path, *, busy_timeout: float = DEFAULT_BUSY_TIMEOUT
) -> list[str]:
    """Return the work folders under *root* that no store row names, sorted.

    A work folder is a directory directly under the root whose name starts
    with neither ``_`` (``_inspect``) nor ``.``. Names are compared without
    case. Such folders are skipped unless ``--force``.

    Raises:
        StoreError: The database is versionless, newer or unreadable.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    stored = {
        str(row["folder"]).casefold()
        for row in _read_view(root, "SELECT folder FROM works", (), busy_timeout)
    }
    return sorted(
        entry.name
        for entry in os.scandir(root)
        if entry.is_dir()
        and not entry.name.startswith(("_", "."))
        and entry.name.casefold() not in stored
    )


# Lease renewal


class LeaseKeeper:
    """Renews a store's held leases every *interval* seconds from a thread.

    Use it as a context manager around the work on the leases; the store
    stops it on :meth:`Store.close`. A lease whose renewal is refused leaves
    the held leases, joins :attr:`lost` and is passed to *on_lost*; a lease
    released or reacquired meanwhile is skipped. A database error is logged
    and the next tick tries again. The thread only extends lease expiries
    (the exception to the single-writer rule).
    """

    def __init__(
        self,
        store: Store,
        *,
        interval: float = LEASE_RENEW_INTERVAL,
        on_lost: Callable[[Lease], None] | None = None,
    ) -> None:
        self._store = store
        self.interval = interval
        self._on_lost = on_lost
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.renewals = 0
        self.lost: list[Lease] = []

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        """Return True while the renewal thread runs."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the renewal thread."""
        if self._thread is not None:
            return
        self._store._register(self)
        self._thread = threading.Thread(
            target=self._run, name="store-lease-keeper", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the renewal thread and wait for it."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def renew_all(self) -> None:
        """Renew every held lease once."""
        store = self._store
        for entry_id in list(store.held_leases()):
            lease = store.held_leases().get(entry_id)
            if lease is None:
                continue
            try:
                store.renew(lease)
            except LeaseLostError:
                if self._still_held(lease):
                    store._forget(lease)
                    self.lost.append(lease)
                    if self._on_lost is not None:
                        self._on_lost(lease)
            except sqlite3.Error as exc:
                logger.warning("Lease renewal for %s failed: %s", entry_id, exc)
            else:
                self.renewals += 1

    def _still_held(self, lease: Lease) -> bool:
        held = self._store.held_leases().get(lease.entry_id)
        return held is not None and held.generation == lease.generation

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.renew_all()
            except RuntimeError:
                return
