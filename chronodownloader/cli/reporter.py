"""Run events written to stderr, one line each."""

from __future__ import annotations

from chronodownloader.cli.console import print_info, print_warning
from chronodownloader.events import (
    CandidateFound,
    CandidateJudged,
    Downloaded,
    StatusChanged,
    WorkStarted,
)

__all__ = ["StderrReporter"]


class StderrReporter:
    """Write each run event as one line to stderr."""

    def work_started(self, event: WorkStarted) -> None:
        """Name the work and its position in the run."""
        print_info(f"[{event.index}/{event.total}] {event.entry_id}: {event.title}")

    def candidate_found(self, event: CandidateFound) -> None:
        """Name the candidate."""
        print_info(
            f"{event.entry_id}: candidate {event.provider}:{event.item_id} "
            f"{event.title}"
        )

    def candidate_judged(self, event: CandidateJudged) -> None:
        """Name the decision and its scores."""
        same_work = "-" if event.same_work is None else f"{event.same_work:.2f}"
        print_info(
            f"{event.entry_id}: {event.decision} {event.provider}:{event.item_id} "
            f"(same_work {same_work}, text score {event.text_score:.2f})"
        )

    def downloaded(self, event: Downloaded) -> None:
        """Count the files written."""
        print_info(
            f"{event.entry_id}: {len(event.files)} file(s) from "
            f"{event.provider}:{event.item_id}"
        )

    def status_changed(self, event: StatusChanged) -> None:
        """Name the new status and its reason."""
        reason = f" ({event.reason})" if event.reason else ""
        print_info(f"{event.entry_id}: {event.status}{reason}")

    def warning(self, message: str) -> None:
        """Write the warning."""
        print_warning(message)
