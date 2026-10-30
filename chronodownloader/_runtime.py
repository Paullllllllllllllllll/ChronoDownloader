"""The run-owned services of a network command and its worker pool.

:class:`Services` opens, for one run, the machine-wide coordinator, the
run's :class:`~chronodownloader.net.Network` (with the registry's
:func:`~chronodownloader.providers.registry.host_key`, so redirect hops are
paced under the provider serving each host), the provider registry, the
decision-model client on first use, and a :class:`WorkerPool`. The run id
names the run in the coordinator's slots and in the store's leases.

:class:`WorkerPool` runs callables on daemon threads and hands back
futures. Workers search, judge and download; only the run's main thread
writes to a store, and it calls the run events. The threads are daemons so
that Ctrl+C ends the process without waiting for a transfer in flight; the
store's close releases the run's leases and discards their staging folders.

:class:`Wiring` replaces the parts a test fakes: provider factories, the
judge, the HTTP session factory, the clock and the sleep.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self

import requests

from chronodownloader.coord import Coordinator, Holder
from chronodownloader.matching.answers import PageAnswer, WorkAnswer
from chronodownloader.matching.judge import DecisionTarget, JudgeClient
from chronodownloader.net import Network
from chronodownloader.net.session import build_session
from chronodownloader.providers.base import SearchQuery, SearchRecord
from chronodownloader.providers.registry import ProviderFactory, Registry, host_key
from chronodownloader.settings import Settings

__all__ = ["JudgeFactory", "Services", "WorkJudge", "WorkerPool", "Wiring"]

logger = logging.getLogger(__name__)


class WorkJudge(Protocol):
    """What the run asks the decision model.

    It judges one candidate against a work, and checks an item's first pages.
    """

    def judge_work(self, sought: SearchQuery, candidate: SearchRecord) -> WorkAnswer:
        """Return the model's answer on *candidate*."""
        ...

    def judge_pages(
        self, sought: SearchQuery, page_images: Sequence[bytes]
    ) -> PageAnswer:
        """Return the page check over *page_images*, the first pages in order."""
        ...


type JudgeFactory = Callable[..., WorkJudge]
"""Builds the run's judge from the network and settings, and ``page=`` when
a page model is named; raises ``MissingJudgeKey`` without a usable key."""


def _judge_client(
    network: Network, settings: Settings, *, page: DecisionTarget | None = None
) -> WorkJudge:
    return JudgeClient(network, settings, page=page)


@dataclass(frozen=True)
class Wiring:
    """The replaceable parts of a run; the defaults are the real ones.

    *providers* replaces the adapter of each key it names; *judge* builds
    the decision-model client; *session_factory* the HTTP session;
    *clock* reads epoch seconds (leases, deferrals); *sleep* waits out
    pacing and slot polls.
    """

    providers: Mapping[str, ProviderFactory] | None = None
    judge: JudgeFactory = _judge_client
    session_factory: Callable[[], requests.Session] = build_session
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


_STOP = object()


class WorkerPool:
    """*size* daemon threads that run submitted callables in order."""

    def __init__(self, size: int, *, name: str = "chronodownloader-worker") -> None:
        if size < 1:
            raise ValueError(f"a pool needs at least one thread, got {size}")
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._threads = [
            threading.Thread(target=self._work, name=f"{name}-{index}", daemon=True)
            for index in range(1, size + 1)
        ]
        for thread in self._threads:
            thread.start()
        self._closed = False

    def submit[T](self, fn: Callable[[], T]) -> Future[T]:
        """Run *fn* on a worker thread; its future holds the result."""
        if self._closed:
            raise RuntimeError("the worker pool is closed")
        future: Future[T] = Future()
        self._queue.put((future, fn))
        return future

    def close(self) -> None:
        """Stop the threads after their current call; queued calls are cancelled.

        Does not wait: a thread busy with a transfer ends with it.
        """
        if self._closed:
            return
        self._closed = True
        pending: list[Any] = []
        while True:
            try:
                pending.append(self._queue.get_nowait())
            except queue.Empty:
                break
        for item in pending:
            if item is not _STOP:
                item[0].cancel()
        for _ in self._threads:
            self._queue.put(_STOP)

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            future, fn = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001 - handed to the main thread
                future.set_exception(exc)
            else:
                future.set_result(result)


class Services:
    """The coordinator, network, registry, judge and pool of one run.

    Build with :meth:`open`; :meth:`close` stops the pool and closes the
    network and the coordinator.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        run_id: str,
        coordinator: Coordinator,
        network: Network,
        registry: Registry,
        pool: WorkerPool,
        wiring: Wiring,
    ) -> None:
        self.settings = settings
        self.run_id = run_id
        self.coordinator = coordinator
        self.network = network
        self.registry = registry
        self.pool = pool
        self.wiring = wiring
        self._judge: WorkJudge | None = None
        self._lock = threading.Lock()

    @classmethod
    def open(
        cls, settings: Settings, *, concurrency: int = 1, wiring: Wiring | None = None
    ) -> Services:
        """Open the services of a run with *concurrency* worker threads.

        Raises:
            SettingsError: ``state_dir`` is not absolute.
            DatabaseBusyError: The coordination database stayed busy.
        """
        wiring = wiring or Wiring()
        run_id = str(uuid.uuid4())
        coordinator = Coordinator(
            settings.state_directory(),
            settings.coordination,
            Holder.current(run_id),
            clock=wiring.clock,
        )
        try:
            network = Network(
                settings,
                coordinator,
                session_factory=wiring.session_factory,
                sleep=wiring.sleep,
                clock=wiring.clock,
                host_key=host_key,
            )
        except BaseException:  # cleanup on every exit, interrupts too; re-raised
            coordinator.close()
            raise
        registry = Registry(settings, factories=wiring.providers)
        return cls(
            settings,
            run_id=run_id,
            coordinator=coordinator,
            network=network,
            registry=registry,
            pool=WorkerPool(concurrency),
            wiring=wiring,
        )

    def judge(self, page: DecisionTarget | None = None) -> WorkJudge:
        """Return the run's judge, built on the first call.

        *page* names the page check's model (``inspect``, ``verify
        --pages``); the first call's choice holds for the run.

        Raises:
            MissingJudgeKey: The decision model's key is unset or unusable.
        """
        with self._lock:
            if self._judge is None:
                extra = {} if page is None else {"page": page}
                self._judge = self.wiring.judge(self.network, self.settings, **extra)
            return self._judge

    def close(self) -> None:
        """Stop the pool, close the network and the coordinator."""
        self.pool.close()
        self.network.close()
        self.coordinator.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
