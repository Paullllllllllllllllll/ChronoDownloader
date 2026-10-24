"""The size limits of a run, as the downloads consult them.

One :class:`SizeBudget` per run counts the bytes staged per file kind
(``image``, ``pdf``, ``metadata``) for the run and for each work;
:meth:`SizeBudget.for_work` gives a download its
:class:`~chronodownloader.providers.base.Budget`. A kind is allowed while
the bytes counted so far stay below both its run limit and the work's limit
(0 or None: no limit); the file that crosses a limit is kept, the next one
is refused. Thread-safe: the run's workers charge it concurrently.

``on_exceed``: under ``stop`` a run limit reached ends the run (no further
work starts, :attr:`SizeBudget.stopped`); under ``skip`` only the files
over a limit are skipped and the run goes on. A work limit never ends the
run; the work stays partial.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from chronodownloader.providers.base import FileKind
from chronodownloader.settings import DownloadLimits
from chronodownloader.spec import SizeLimits

__all__ = ["KINDS", "SizeBudget", "WorkBudget"]

KINDS: Final[tuple[FileKind, ...]] = ("image", "pdf", "metadata")
GB: Final = 1024**3
MB: Final = 1024**2


def _bytes(value: float | None, unit: int) -> int | None:
    if value is None or value <= 0:
        return None
    return int(value * unit)


class SizeBudget:
    """The run's size limits and the bytes counted against them.

    *total* and *per_work* map each kind to its limit in bytes (None: no
    limit); *on_exceed* is ``stop`` or ``skip``.
    """

    def __init__(
        self,
        total: Mapping[FileKind, int | None],
        per_work: Mapping[FileKind, int | None],
        on_exceed: str = "stop",
    ) -> None:
        self.total: Mapping[FileKind, int | None] = MappingProxyType(dict(total))
        self.per_work: Mapping[FileKind, int | None] = MappingProxyType(dict(per_work))
        self.on_exceed = on_exceed
        self._lock = threading.Lock()
        self._run: dict[FileKind, int] = dict.fromkeys(KINDS, 0)
        self._works: dict[str, dict[FileKind, int]] = {}
        self._reached = False

    @classmethod
    def of(cls, limits: SizeLimits, settings: DownloadLimits) -> SizeBudget:
        """Return the budget of the run's flags over the settings' limits."""

        def pick(flag: float | None, setting: float) -> float:
            return setting if flag is None else flag

        total = settings.total
        work = settings.per_work
        return cls(
            total={
                "image": _bytes(pick(limits.total_images_gb, total.images_gb), GB),
                "pdf": _bytes(pick(limits.total_pdfs_gb, total.pdfs_gb), GB),
                "metadata": _bytes(
                    pick(limits.total_metadata_gb, total.metadata_gb), GB
                ),
            },
            per_work={
                "image": _bytes(pick(limits.work_images_gb, work.images_gb), GB),
                "pdf": _bytes(pick(limits.work_pdfs_gb, work.pdfs_gb), GB),
                "metadata": _bytes(pick(limits.work_metadata_mb, work.metadata_mb), MB),
            },
            on_exceed=limits.on_exceed or settings.on_exceed,
        )

    @property
    def stopped(self) -> bool:
        """Return True once a run limit was reached under ``stop``."""
        with self._lock:
            return self._reached and self.on_exceed == "stop"

    def used(self, kind: FileKind, entry_id: str | None = None) -> int:
        """Return the bytes of *kind* counted for the run (or one work)."""
        with self._lock:
            if entry_id is None:
                return self._run[kind]
            return self._works.get(entry_id, {}).get(kind, 0)

    def for_work(self, entry_id: str) -> WorkBudget:
        """Return the budget one download of work *entry_id* consults."""
        return WorkBudget(self, entry_id)

    def allows(self, kind: FileKind, entry_id: str) -> bool:
        """Return True when another file of *kind* may be staged for the work."""
        with self._lock:
            limit = self.total[kind]
            if limit is not None and self._run[kind] >= limit:
                self._reached = True
                return False
            work_limit = self.per_work[kind]
            used = self._works.get(entry_id, {}).get(kind, 0)
            return work_limit is None or used < work_limit

    def charge(self, kind: FileKind, size: int, entry_id: str) -> None:
        """Count a staged file of *kind* and *size* bytes for the work."""
        with self._lock:
            self._run[kind] += size
            work = self._works.setdefault(entry_id, dict.fromkeys(KINDS, 0))
            work[kind] += size
            limit = self.total[kind]
            if limit is not None and self._run[kind] >= limit:
                self._reached = True


class WorkBudget:
    """The run budget as one work's download sees it."""

    def __init__(self, budget: SizeBudget, entry_id: str) -> None:
        self._budget = budget
        self.entry_id = entry_id

    def allows(self, kind: FileKind) -> bool:
        """Return True when another file of *kind* may be staged."""
        return self._budget.allows(kind, self.entry_id)

    def charge(self, kind: FileKind, size: int) -> None:
        """Count a staged file of *kind* and *size* bytes."""
        self._budget.charge(kind, size, self.entry_id)
