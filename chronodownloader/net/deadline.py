"""A wall-clock deadline for a blocking call that cannot be interrupted.

:func:`run_with_deadline` runs the call in a daemon thread and stops waiting
when the deadline passes. The abandoned thread may linger until its blocking
HTTP call returns, but as a daemon it never delays interpreter exit. Searches
use it for the per-provider timeout.

Rewritten from ChronoDownloader v1.30.3
``main/orchestration/selection.py:_run_with_timeout`` on 19.10.2026.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

__all__ = ["DeadlineExceeded", "run_with_deadline"]


class DeadlineExceeded(TimeoutError):
    """The call did not return within its deadline; it may still be running."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        super().__init__(f"call did not finish within {timeout_s:g} s")


def run_with_deadline[T](fn: Callable[[], T], timeout_s: float | None) -> T:
    """Return ``fn()``, or raise :class:`DeadlineExceeded` after *timeout_s*.

    A *timeout_s* of None or at most 0 calls *fn* in the calling thread. An
    exception *fn* raises is raised in the calling thread.

    Raises:
        DeadlineExceeded: *fn* did not return in time.
    """
    if timeout_s is None or timeout_s <= 0:
        return fn()
    results: list[T] = []
    errors: list[Exception] = []

    def work() -> None:
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - handed to the caller, who re-raises it
            errors.append(exc)

    worker = threading.Thread(target=work, name="deadline-call", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise DeadlineExceeded(timeout_s)
    if errors:
        raise errors[0]
    return results[0]
