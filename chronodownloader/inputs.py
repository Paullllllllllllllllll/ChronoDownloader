"""The works CSV: one work per row, read and checked, never rewritten.

Columns. ``entry_id`` and ``title`` are required; ``creator``, ``year``,
``place``, ``publisher``, ``language``, ``edition`` and ``link`` are
optional. ``short_title`` stands for ``title`` and ``main_author`` for
``creator`` (v1.30.3 names); where a row has both, the canonical column wins
and the alias fills it only when it is empty. Header names are compared
without surrounding spaces and case. Other columns are ignored and named in
a warning, which the caller reports; this module never prints.

Values are stripped; an empty cell is None. Every row needs an entry id and
a title, and no entry id may repeat. The file is read as UTF-8 (a byte
order mark is accepted).
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from chronodownloader.output.store import WorkInput
from chronodownloader.process.contract import ExitCode

__all__ = [
    "ALIASES",
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "InputError",
    "WorksInput",
    "read_works",
]

REQUIRED_COLUMNS: Final = ("entry_id", "title")
OPTIONAL_COLUMNS: Final = (
    "creator",
    "year",
    "place",
    "publisher",
    "language",
    "edition",
    "link",
)
ALIASES: Final[Mapping[str, str]] = {"short_title": "title", "main_author": "creator"}
"""Alias column -> the column it stands for."""


class InputError(ValueError):
    """The works CSV cannot be used; the CLI exits with code 2."""

    exit_code: ExitCode = ExitCode.USAGE


@dataclass(frozen=True)
class WorksInput:
    """The works of a CSV in file order and the warnings its reading raised."""

    path: Path
    works: tuple[WorkInput, ...]
    warnings: tuple[str, ...] = ()


def _header(fieldnames: Sequence[str] | None, path: Path) -> dict[str, str]:
    """Return the file's header names keyed by their normalized names.

    Raises:
        InputError: No header, a repeated column, or a required column
            missing (directly or through its alias).
    """
    if not fieldnames:
        raise InputError(f"{path}: the works CSV has no header row")
    names: dict[str, str] = {}
    for raw in fieldnames:
        name = (raw or "").strip().lower()
        if name in names:
            raise InputError(f"{path}: column {name!r} appears twice")
        names[name] = raw
    present = set(names) | {ALIASES[name] for name in names if name in ALIASES}
    missing = [column for column in REQUIRED_COLUMNS if column not in present]
    if missing:
        raise InputError(
            f"{path}: missing required column(s): {', '.join(missing)} "
            "(the works CSV needs entry_id and title)"
        )
    return names


def _cell(row: Mapping[str, str | None], names: Mapping[str, str], key: str) -> str:
    raw = names.get(key)
    value = row.get(raw) if raw is not None else None
    return (value or "").strip()


def _value(
    row: Mapping[str, str | None], names: Mapping[str, str], column: str
) -> str | None:
    value = _cell(row, names, column)
    if not value:
        for alias, target in ALIASES.items():
            if target == column:
                value = _cell(row, names, alias)
    return value or None


def read_works(path: Path) -> WorksInput:
    """Read the works CSV at *path*.

    Raises:
        InputError: The file cannot be read or decoded, a required column
            is missing, a row lacks its entry id or title, or an entry id
            repeats.
    """
    path = Path(path)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            names = _header(reader.fieldnames, path)
            rows = list(reader)
    except FileNotFoundError:
        raise InputError(f"works CSV not found: {path}") from None
    except UnicodeDecodeError as exc:
        raise InputError(f"{path}: the works CSV is not UTF-8 ({exc})") from exc
    except (OSError, csv.Error) as exc:
        raise InputError(f"{path}: the works CSV cannot be read ({exc})") from exc
    known = {*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS, *ALIASES}
    unknown = [names[name] for name in names if name not in known]
    warnings = []
    if unknown:
        warnings.append(f"{path}: unknown columns ignored: {', '.join(unknown)}")
    works: list[WorkInput] = []
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for line, row in enumerate(rows, start=2):
        entry_id = _value(row, names, "entry_id")
        title = _value(row, names, "title")
        if entry_id is None or title is None:
            missing = "entry_id" if entry_id is None else "title"
            raise InputError(f"{path}, line {line}: the row has no {missing}")
        if entry_id in seen:
            duplicates.append(entry_id)
        seen.setdefault(entry_id, line)
        works.append(
            WorkInput(
                entry_id=entry_id,
                title=title,
                creator=_value(row, names, "creator"),
                year=_value(row, names, "year"),
                place=_value(row, names, "place"),
                publisher=_value(row, names, "publisher"),
                language=_value(row, names, "language"),
                edition=_value(row, names, "edition"),
                link=_value(row, names, "link"),
            )
        )
    if duplicates:
        listed = ", ".join(dict.fromkeys(duplicates))
        raise InputError(f"{path}: duplicate entry_id values: {listed}")
    return WorksInput(path, tuple(works), tuple(warnings))
