"""Run events: how the core reports progress to a front end.

The core never prints. It calls a :class:`RunEvents` implementation instead:
the CLI writes the events to stderr, a test records them, and
:class:`NullEvents` drops them. Events are called on the run's main thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = [
    "CandidateFound",
    "CandidateJudged",
    "Decision",
    "Downloaded",
    "NullEvents",
    "RunEvents",
    "StatusChanged",
    "WorkStarted",
    "WorkStatus",
]

WorkStatus = Literal[
    "complete",
    "partial",
    "failed",
    "no_match",
    "needs_review",
    "deferred",
    "blocked",
]
"""The stored status of a work; needs_review, deferred and blocked are open."""

Decision = Literal["accept", "reject", "review"]
"""The acceptance policy's verdict on a candidate."""


@dataclass(frozen=True)
class WorkStarted:
    """A work is about to be processed; *index* counts from 1 to *total*."""

    entry_id: str
    title: str
    index: int
    total: int


@dataclass(frozen=True)
class CandidateFound:
    """A provider returned *item_id* as a candidate for the work."""

    entry_id: str
    provider: str
    item_id: str
    title: str


@dataclass(frozen=True)
class CandidateJudged:
    """The policy decided on a candidate.

    *same_work* is the judge's probability (None under ``--matching
    fuzzy``); *text_score* the fuzzy title score, computed in every mode.
    """

    entry_id: str
    provider: str
    item_id: str
    decision: Decision
    same_work: float | None
    text_score: float


@dataclass(frozen=True)
class Downloaded:
    """The files of an item were written to the work's folder."""

    entry_id: str
    provider: str
    item_id: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class StatusChanged:
    """A work reached *status*; *reason* says why when it is not complete."""

    entry_id: str
    status: WorkStatus
    reason: str | None = None


class RunEvents(Protocol):
    """The events a run reports."""

    def work_started(self, event: WorkStarted) -> None:
        """Report that the run started a work."""

    def candidate_found(self, event: CandidateFound) -> None:
        """Report a candidate a search found."""

    def candidate_judged(self, event: CandidateJudged) -> None:
        """Report the decision on a candidate."""

    def downloaded(self, event: Downloaded) -> None:
        """Report the files downloaded for a work."""

    def status_changed(self, event: StatusChanged) -> None:
        """Report the status a work reached."""

    def warning(self, message: str) -> None:
        """Report a warning."""


class NullEvents:
    """Drop every event."""

    def work_started(self, event: WorkStarted) -> None:
        """Ignore the event."""

    def candidate_found(self, event: CandidateFound) -> None:
        """Ignore the event."""

    def candidate_judged(self, event: CandidateJudged) -> None:
        """Ignore the event."""

    def downloaded(self, event: Downloaded) -> None:
        """Ignore the event."""

    def status_changed(self, event: StatusChanged) -> None:
        """Ignore the event."""

    def warning(self, message: str) -> None:
        """Ignore the warning."""
