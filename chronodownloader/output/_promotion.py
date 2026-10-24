"""The disk side of the store's commits: staging, promotion and repair.

Private to :mod:`chronodownloader.output.store`, whose docstring states the
commit and the repair rules these functions carry out. Every function that
moves or deletes a final file takes the store's connection and runs inside
one of its write transactions, so it never interleaves with another
process's promotion.

A staging folder ``<work>/objects/.staging/<generation>/`` holds the staged
files under their staged names, and for each commit begun in it a journal
``.promote-<commit id>.json`` (``commit_id`` and the ``[staged name, final
path, final file existed]`` entries) and a folder ``.replaced-<commit id>/``
of the final files moved aside, under their final paths.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from chronodownloader.net.atomic import atomic_write_json
from chronodownloader.providers.base import StagedFile

from .paths import INSPECT_DIR, STAGING_DIR, staging_dir

__all__ = [
    "STAGING_DIR",
    "Move",
    "check_folder",
    "clear_landed",
    "drop_folder",
    "final_path",
    "generation_dirs",
    "journal_path",
    "promote",
    "relative_path",
    "remove_tree",
    "settle_generation",
    "staging_path",
]

logger = logging.getLogger(__name__)

_RESERVED_FOLDERS: Final = frozenset({INSPECT_DIR})
_JOURNAL_PREFIX: Final = ".promote-"
_ASIDE_PREFIX: Final = ".replaced-"
_RENAME_ATTEMPTS: Final = 5
_RENAME_BASE_SLEEP_S: Final = 0.1

_FILE_OWNER: Final = """
SELECT a.lease_generation FROM files AS f
JOIN attempts AS a ON a.attempt_id = f.attempt_id
WHERE f.entry_id = ? AND f.path = ?
"""


@dataclass(frozen=True)
class Move:
    """One promotion: the staged file, its final path, both on disk."""

    staged: StagedFile
    final: str
    source: Path
    target: Path


# Names and paths


def check_folder(folder: str) -> str:
    """Return *folder* when it is a single plain name directly under a root.

    Raises:
        ValueError: An empty, dotted, reserved or nested name.
    """
    if (
        not folder
        or folder in (".", "..")
        or folder.casefold() in _RESERVED_FOLDERS
        or any(char in folder for char in "/\\:")
        or folder != folder.strip()
    ):
        raise ValueError(f"not a work-folder name: {folder!r}")
    return folder


def relative_path(path: str, what: str) -> PurePosixPath:
    """Return *path* as a normalized relative POSIX path.

    Raises:
        ValueError: An empty, absolute, backslashed or unnormalized path, or
            one with ``.``, ``..`` or a drive.
    """
    pure = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or pure.is_absolute()
        or pure.as_posix() != path
        or any(part in (".", "..") or ":" in part for part in pure.parts)
    ):
        raise ValueError(f"{what} is not a plain relative path: {path!r}")
    return pure


def final_path(path: str) -> PurePosixPath:
    """Return a final path relative to the work folder, outside the staging.

    Raises:
        ValueError: The path is not plain or lies in the staging folder.
    """
    pure = relative_path(path, "final path")
    if pure.parts[:2] == tuple(STAGING_DIR.split("/")):
        raise ValueError(f"final path lies in the staging folder: {path!r}")
    return pure


def staging_path(work_dir: Path, generation: int) -> Path:
    """Return the staging folder of lease *generation* in *work_dir*."""
    return staging_dir(work_dir, generation)


def journal_path(gen_dir: Path, commit_id: str) -> Path:
    """Return the promotion journal of commit *commit_id* in *gen_dir*."""
    return gen_dir / f"{_JOURNAL_PREFIX}{commit_id}.json"


def generation_dirs(work_dir: Path) -> list[tuple[int, Path]]:
    """Return the staging folders of *work_dir* by generation, in order."""
    parent = work_dir.joinpath(*STAGING_DIR.split("/"))
    if not parent.is_dir():
        return []
    found = [
        (int(entry.name), Path(entry.path))
        for entry in os.scandir(parent)
        if entry.is_dir() and entry.name.isdigit()
    ]
    return sorted(found)


# File operations


def rename(source: Path, target: Path) -> None:
    """Move *source* onto *target*, retrying a transient Windows lock.

    Unlike :func:`~chronodownloader.net.atomic.replace_with_retry`, a failure
    leaves *source* in place: it may be a recorded file moved aside.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_RENAME_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _RENAME_ATTEMPTS - 1:
                raise
            time.sleep(_RENAME_BASE_SLEEP_S * (attempt + 1))


def write_journal(path: Path, document: Mapping[str, Any]) -> None:
    """Write a promotion journal atomically (temporary file and replace)."""
    atomic_write_json(path, document)


def remove_tree(path: Path) -> None:
    """Delete *path* and everything below it, ignoring what cannot go."""
    shutil.rmtree(path, ignore_errors=True)


# Promotion


def promote(journal: Path, commit_id: str, moves: Sequence[Move]) -> None:
    """Journal the moves, then rename each staged file to its final path.

    Each journal entry records whether the final file existed before; an
    existing one is first moved aside. A replayed transaction finds the
    journal and resumes: a move whose staged file is gone and whose final
    file exists was done before.

    Raises:
        FileNotFoundError: A staged file is missing.
        OSError: A file could not be moved.
    """
    aside_dir = journal.parent / f"{_ASIDE_PREFIX}{commit_id}"
    if not journal.exists():
        for move in moves:
            if not move.source.is_file():
                raise FileNotFoundError(f"Staged file {move.staged.name!r} is missing.")
        write_journal(
            journal,
            {
                "commit_id": commit_id,
                "entries": [
                    [move.staged.name, move.final, move.target.exists()]
                    for move in moves
                ],
            },
        )
    for move in moves:
        if not move.source.exists():
            if not move.target.is_file():
                raise FileNotFoundError(f"Staged file for {move.final!r} is missing.")
            continue
        aside = aside_dir / move.final
        if move.target.exists() and not aside.exists():
            rename(move.target, aside)
        rename(move.source, move.target)


def clear_landed(gen_dir: Path, commit_id: str) -> None:
    """Delete a landed commit's journal and moved-aside files, then *gen_dir*.

    The staging parent goes too when nothing else is left in it.
    """
    with contextlib.suppress(OSError):
        aside = gen_dir / f"{_ASIDE_PREFIX}{commit_id}"
        if aside.exists():
            shutil.rmtree(aside)
        journal_path(gen_dir, commit_id).unlink(missing_ok=True)
    remove_tree(gen_dir)
    with contextlib.suppress(OSError):
        gen_dir.parent.rmdir()


def drop_folder(
    work_dir: Path, recorded: Sequence[Path], *, delete: bool, gone: set[Path]
) -> tuple[list[Path], list[Path]]:
    """Delete a dropped work's recorded files; return them and the kept files.

    With *delete*, the recorded files are deleted, then the staging folders,
    then every folder left empty, the work folder included; files no row
    records are kept. Without it nothing changes and the lists say what
    would. *gone* collects the files deleted, so that a rerun of the
    transaction still reports them.

    Raises:
        OSError: A recorded file could not be deleted.
    """
    deleted: list[Path] = []
    for path in recorded:
        if path in gone:
            deleted.append(path)
        elif path.is_file():
            if delete:
                path.unlink()
                gone.add(path)
            deleted.append(path)
    staging = work_dir.joinpath(*STAGING_DIR.split("/"))
    if delete:
        remove_tree(staging)
    doomed = set(deleted)
    kept = sorted(
        path
        for path in (work_dir.rglob("*") if work_dir.is_dir() else ())
        if path.is_file() and path not in doomed and staging not in path.parents
    )
    if delete:
        _remove_empty_folders(work_dir)
    return deleted, kept


def _remove_empty_folders(top: Path) -> None:
    """Remove the empty folders below *top*, deepest first, then *top*."""
    if not top.is_dir():
        return
    folders = sorted(
        (path for path in top.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for folder in [*folders, top]:
        with contextlib.suppress(OSError):
            folder.rmdir()


# Repair


def _roll_back_entry(
    connection: sqlite3.Connection,
    entry_id: str,
    generation: int,
    paths: tuple[Path, Path, Path],
    final: str,
    existed: bool,
) -> bool:
    """Undo one rename of an interrupted commit; return True when a file moved.

    *paths* are the staged file, the final file and the moved-aside copy;
    *existed* says whether the final file existed before the commit. Such a
    file is only ever restored from its copy, never deleted, so that a
    recovery interrupted after the restore can run again.
    """
    staged, target, aside = paths
    row = connection.execute(_FILE_OWNER, (entry_id, final)).fetchone()
    if row is not None and int(row[0]) > generation:
        return False
    if aside.exists():
        rename(aside, target)
        return True
    if existed:
        return False
    if not staged.exists() and row is None and target.is_file():
        target.unlink()
        return True
    return False


def _settle_journal(
    connection: sqlite3.Connection,
    entry_id: str,
    generation: int,
    work_dir: Path,
    journal: Path,
) -> list[str]:
    """Settle one promotion journal; return the final paths rolled back.

    Raises:
        OSError: A file could not be moved or deleted.
        ValueError, KeyError, TypeError: The journal cannot be read.
    """
    document = json.loads(journal.read_text(encoding="utf-8"))
    commit_id = str(document["commit_id"])
    gen_dir = journal.parent
    aside_dir = gen_dir / f"{_ASIDE_PREFIX}{commit_id}"
    landed = (
        connection.execute(
            "SELECT 1 FROM attempts WHERE commit_id = ?", (commit_id,)
        ).fetchone()
        is not None
    )
    rolled: list[str] = []
    if not landed:
        for staged_name, final, existed in reversed(document["entries"]):
            paths = (gen_dir / staged_name, work_dir / final, aside_dir / final)
            if _roll_back_entry(
                connection, entry_id, generation, paths, final, bool(existed)
            ):
                rolled.append(str(final))
    if aside_dir.exists():
        shutil.rmtree(aside_dir)
    journal.unlink()
    return rolled


def settle_generation(
    connection: sqlite3.Connection,
    entry_id: str,
    generation: int,
    work_dir: Path,
    gen_dir: Path,
) -> list[str]:
    """Settle every journal of *gen_dir*, then delete the folder.

    Returns the final paths rolled back. A journal that cannot be read keeps
    its folder for inspection; it is logged.

    Raises:
        OSError: A file could not be moved or deleted.
    """
    rolled: list[str] = []
    for journal in sorted(gen_dir.glob(f"{_JOURNAL_PREFIX}*.json")):
        try:
            rolled += _settle_journal(
                connection, entry_id, generation, work_dir, journal
            )
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Promotion journal %s is unreadable: %s", journal, exc)
            return rolled
    remove_tree(gen_dir)
    with contextlib.suppress(OSError):
        gen_dir.parent.rmdir()
    return rolled
