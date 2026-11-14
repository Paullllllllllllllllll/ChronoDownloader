"""The scope one thread's calls run under: a cancel flag and time in flight.

A search runs its provider's calls inside
:meth:`~chronodownloader.net.Network.scoped`. Once :meth:`CallScope.cancel`
is called, the network starts nothing further for those calls (see
:class:`~chronodownloader.net.errors.Cancelled`). :meth:`CallScope.in_flight_s`
gives the seconds the calls' requests were in flight, from just before a
request is sent until its response is handled, so that a caller can tell a
slow provider from a deadline passed while waiting for pacing turns. A scope
built with *reuse_answers* also keeps the answers of its requests, so that
the network sends a request repeated inside the scope only once.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import requests

__all__ = ["CallScope"]


class CallScope:
    """A cancel flag and the in-flight seconds of one thread's calls.

    The calls run on one thread, one request at a time; the flag and the
    seconds are read from any thread. *clock* is a monotonic clock. With
    *reuse_answers* the scope keeps the answers the network hands it; only
    the calling thread reads and writes them.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        reuse_answers: bool = False,
    ) -> None:
        self._clock = clock
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._done_s = 0.0
        self._since: float | None = None
        self._answers: dict[Hashable, requests.Response] | None = (
            {} if reuse_answers else None
        )

    @property
    def reuses_answers(self) -> bool:
        """Return True when the scope keeps the answers of its requests."""
        return self._answers is not None

    def answer(self, key: Hashable) -> requests.Response | None:
        """Return the answer kept under *key*, or None."""
        return None if self._answers is None else self._answers.get(key)

    def keep(self, key: Hashable, response: requests.Response) -> None:
        """Keep *response* under *key*; a scope that reuses none ignores it."""
        if self._answers is not None:
            self._answers[key] = response

    @property
    def cancelled(self) -> bool:
        """Return True once :meth:`cancel` was called."""
        return self._event.is_set()

    def cancel(self) -> None:
        """Stop the calls from starting anything further."""
        self._event.set()

    def begin(self) -> None:
        """Note that a request is being sent."""
        with self._lock:
            self._since = self._clock()

    def end(self) -> None:
        """Note that the request's response was handled or the send failed."""
        with self._lock:
            if self._since is not None:
                self._done_s += self._clock() - self._since
                self._since = None

    def in_flight_s(self) -> float:
        """Return the seconds in flight, a request still running included."""
        with self._lock:
            running = 0.0 if self._since is None else self._clock() - self._since
            return self._done_s + running
