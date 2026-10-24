"""Settings schema: typed reads from a parsed settings mapping.

:class:`Reader` reads typed values from a parsed settings mapping and names
the key in every error. The block readers parse ``state_dir`` and
``logs_dir``; a tool builds its other blocks from the reader's methods with
its own code defaults. A missing key keeps the default.

Copied from ChronoTranscriber v1.0.0 ``common/settings_schema.py`` on
19.10.2026, trimmed: removed the LLM blocks and their constants
(``api_keys``, ``endpoints``, ``timeouts``, ``rate_limits``, ``retry``,
``concurrency``, ``images``, ``prompt_caching``), the extra-block seam
(``BlockParser``, ``check_blocks``, ``read_extensions``) and
``Reader.command``. Changed: :meth:`Reader.number` refuses NaN and the
infinities.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from .settings_loader import SettingsError

__all__ = [
    "LOG_FILE_NAME",
    "Reader",
    "ValueKind",
    "log_file",
    "read_logs_dir",
    "read_state_dir",
    "state_directory",
]

LOG_FILE_NAME = "application.log"

type ValueKind = Literal["number", "integer", "positive", "flag"]
"""The :class:`Reader` method that reads one value of a flat block."""


class Reader:
    """Typed reads from a parsed settings mapping with errors naming the key.

    *where* names the file in error messages; relative paths resolve against
    *base*, by default the current directory.
    """

    def __init__(self, where: str, base: Path | None = None) -> None:
        self.where = where
        self.base = base if base is not None else Path.cwd()

    def fail(self, path: str, message: str) -> SettingsError:
        """Return the error for the key at *path*."""
        return SettingsError(f"{self.where}: {path}: {message}")

    def section(
        self, raw: Any, path: str, allowed: frozenset[str] | None = None
    ) -> Mapping[str, Any]:
        """Return a mapping section ({} when absent), checking its keys."""
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise self.fail(path, "must be a mapping")
        if allowed is not None:
            self.keys(raw, allowed, path)
        return raw

    def keys(self, raw: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
        """Raise when *raw* holds a key outside *allowed*."""
        unknown = sorted(str(key) for key in raw if key not in allowed)
        if unknown:
            raise self.fail(path or "top level", f"unknown keys {', '.join(unknown)}")

    def text(self, raw: Mapping[str, Any], key: str, default: str, path: str) -> str:
        """Return a stripped string."""
        value = raw.get(key)
        if value is None:
            return default
        if not isinstance(value, str):
            raise self.fail(path + key, "must be a string")
        return value.strip()

    def choice(
        self,
        raw: Mapping[str, Any],
        key: str,
        default: str,
        path: str,
        choices: tuple[str, ...],
    ) -> str:
        """Return a string that is one of *choices*."""
        value = self.text(raw, key, default, path)
        if value not in choices:
            raise self.fail(path + key, f"must be one of {', '.join(choices)}")
        return value

    def number(
        self, raw: Mapping[str, Any], key: str, default: float, path: str
    ) -> float:
        """Return a finite number that is not negative, as a float."""
        value = raw.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self.fail(path + key, "must be a number")
        if not math.isfinite(value):
            raise self.fail(path + key, "must be a finite number")
        if value < 0:
            raise self.fail(path + key, "must not be negative")
        return float(value)

    def integer(self, raw: Mapping[str, Any], key: str, default: int, path: str) -> int:
        """Return an integer that is not negative."""
        value = raw.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise self.fail(path + key, "must be a non-negative integer")
        return value

    def positive(
        self, raw: Mapping[str, Any], key: str, default: int, path: str
    ) -> int:
        """Return an integer of at least 1."""
        value = self.integer(raw, key, default, path)
        if value < 1:
            raise self.fail(path + key, "must be at least 1")
        return value

    def flag(self, raw: Mapping[str, Any], key: str, default: bool, path: str) -> bool:
        """Return true or false."""
        value = raw.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise self.fail(path + key, "must be true or false")
        return value

    def path(self, raw: Mapping[str, Any], key: str, path: str) -> Path | None:
        """Return a path resolved against the base, or None for ''."""
        text = self.text(raw, key, "", path)
        if not text:
            return None
        value = Path(text).expanduser()
        return value if value.is_absolute() else self.base / value

    def flat(
        self,
        raw: Mapping[str, Any],
        keys: Sequence[tuple[str, ValueKind]],
        base: Any,
        path: str,
    ) -> dict[str, Any]:
        """Return the values of *keys* in order, each read with its kind.

        A missing key keeps the attribute of *base* of the same name.
        """
        return {
            key: getattr(self, kind)(raw, key, getattr(base, key), path)
            for key, kind in keys
        }


def read_state_dir(reader: Reader, data: Mapping[str, Any]) -> Path | None:
    """Return ``state_dir`` as written, or None for '' (the default folder)."""
    text = reader.text(data, "state_dir", "", "")
    return Path(text) if text else None


def state_directory(state_dir: Path | None, default_name: str) -> Path:
    """Return the state folder: *state_dir*, else ``~/<default_name>``."""
    if state_dir is not None:
        return state_dir.expanduser()
    return Path.home() / default_name


def read_logs_dir(reader: Reader, data: Mapping[str, Any]) -> Path | None:
    """Return ``logs_dir`` resolved against the reader's base, or None."""
    return reader.path(data, "logs_dir", "")


def log_file(logs_dir: Path | None, default_dir: Path) -> Path:
    """Return the log file: *logs_dir* names a ``.log`` file or its folder.

    Without *logs_dir* the file is ``application.log`` in *default_dir*.
    """
    if logs_dir is None:
        return default_dir / LOG_FILE_NAME
    if logs_dir.suffix == ".log":
        return logs_dir
    return logs_dir / LOG_FILE_NAME
