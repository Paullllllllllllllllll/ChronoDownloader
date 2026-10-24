"""Machine-wide coordination of providers across processes and runs.

Every process that talks to a provider opens a :class:`Coordinator` on
``<state_dir>/coordination.sqlite``. The database holds per-provider pacing
reservations, download slots, circuit breakers and quota counters, so that
several agents running at once keep each provider's limits together. Every
operation is one ``BEGIN IMMEDIATE`` transaction through
:func:`chronodownloader.sqlite.transact` (rollback journal, never WAL); the
clock is read inside the transaction, so the order of transactions is the
order of their timestamps. Nothing sleeps inside a transaction or a lock:
callers sleep between transactions.

Pacing. :meth:`Coordinator.reserve` sets
``next_at = max(now, next_at) + delay + jitter`` and returns the send time
``max(now, next_at)`` that it consumed. The caller sleeps until the send
time and sends only while :meth:`Coordinator.still_on_time` holds (the clock
is at most ``pacing_tolerance_ms`` past it); a caller that resumes later was
suspended or scheduled out and reserves again. :meth:`Coordinator.wait_turn`
runs that loop. A retry is a new request and reserves anew.

Slots. A provider has numbered slot rows; :meth:`Coordinator.acquire_slot`
takes the lowest free one while fewer than *limit* are held and unexpired.
Each acquisition increments the row's generation and sets its expiry
``slot_ttl_s`` ahead. Renewal extends the expiry and is refused once it has
passed; release frees the row only for its holder and generation. A dead
holder stops renewing, so its slot is reclaimed once it expires. A
reservation that names a slot checks in the same transaction that the slot
is still held by this holder with this generation and unexpired, else it
raises :class:`SlotLost`: a holder that missed its renewals finishes at most
the request already in flight. :class:`SlotKeeper` renews from a timer
thread.

Breakers. Consecutive failures count per provider; at *threshold* the
breaker opens until ``now + cooldown`` with its cause. After that time the
breaker is half-open: :meth:`Coordinator.breaker_check` grants one probe,
for ``slot_ttl_s``, to exactly one caller and reports the others blocked. A
success closes the breaker; a failure while half-open reopens it. The
failure that opens or reopens a breaker is reported as ``opened``.

Quota. A window opens at the first reservation after the previous window
ended and lasts *reset_hours*. :meth:`Coordinator.reserve_quota` counts one
unit atomically and returns None when the window's units are spent;
:meth:`Coordinator.quota_status` gives the reset time.
:meth:`Coordinator.release_quota` returns a unit, only for a request that
provably was not sent, and only within the window that issued it.

Reading. :func:`read_snapshot` reads the breakers and quota windows through
a read-only connection (the ``providers`` command): it grants no probe and
creates nothing.

Schema (version 1, in ``meta``; another version is refused)::

    meta     (key TEXT PRIMARY KEY, value TEXT)          -- schema_version
    pacing   (provider TEXT PRIMARY KEY, next_at REAL)
    slots    (provider TEXT, slot INTEGER, generation INTEGER,
              pid INTEGER, run_id TEXT, host TEXT, expires_at REAL,
              PRIMARY KEY (provider, slot))              -- NULL holder: free
    breakers (provider TEXT PRIMARY KEY, failures INTEGER,
              open_until REAL, cause TEXT, probe_until REAL)
    quotas   (provider TEXT PRIMARY KEY, used INTEGER, daily_limit INTEGER,
              window_start REAL, reset_at REAL)

Times are wall-clock epoch seconds from the injectable clock
(``time.time`` by default), comparable across processes on one machine.
"""

from __future__ import annotations

import enum
import logging
import os
import random
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, Self, TypeVar

from chronodownloader.sqlite import (
    DatabaseBusyError,
    TableSpec,
    connect,
    read,
    transact,
)

if TYPE_CHECKING:
    from chronodownloader.settings import Coordination

__all__ = [
    "DATABASE_NAME",
    "SCHEMA_VERSION",
    "BreakerState",
    "BreakerStatus",
    "Clock",
    "CoordinationError",
    "Coordinator",
    "DatabaseBusyError",
    "Holder",
    "QuotaStatus",
    "QuotaToken",
    "SlotKeeper",
    "SlotLost",
    "SlotRenewer",
    "SlotToken",
    "Snapshot",
    "read_snapshot",
]

DATABASE_NAME = "coordination.sqlite"
SCHEMA_VERSION = 1

Clock = Callable[[], float]
"""Wall-clock seconds since the epoch."""

T = TypeVar("T")

logger = logging.getLogger(__name__)

TABLES = (
    TableSpec(
        "meta",
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        ("key",),
    ),
    TableSpec(
        "pacing",
        "CREATE TABLE IF NOT EXISTS pacing ("
        "provider TEXT PRIMARY KEY, next_at REAL NOT NULL)",
        ("provider",),
    ),
    TableSpec(
        "slots",
        "CREATE TABLE IF NOT EXISTS slots ("
        "provider TEXT NOT NULL, slot INTEGER NOT NULL, "
        "generation INTEGER NOT NULL DEFAULT 0, "
        "pid INTEGER, run_id TEXT, host TEXT, expires_at REAL, "
        "PRIMARY KEY (provider, slot))",
        ("provider", "slot"),
    ),
    TableSpec(
        "breakers",
        "CREATE TABLE IF NOT EXISTS breakers ("
        "provider TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, "
        "open_until REAL, cause TEXT, probe_until REAL)",
        ("provider",),
    ),
    TableSpec(
        "quotas",
        "CREATE TABLE IF NOT EXISTS quotas ("
        "provider TEXT PRIMARY KEY, used INTEGER NOT NULL, "
        "daily_limit INTEGER NOT NULL, window_start REAL NOT NULL, "
        "reset_at REAL NOT NULL)",
        ("provider",),
    ),
)


class CoordinationError(Exception):
    """Base class of the coordination errors."""


class SlotLost(CoordinationError):
    """The slot is no longer held by this holder with this generation.

    ``quota_charged`` is set by the network client when the loss ended a
    call: True when a quota unit of that call stayed charged.
    """

    quota_charged: bool = False

    def __init__(self, message: str, token: SlotToken) -> None:
        super().__init__(message)
        self.token = token


class SlotRenewer(Protocol):
    """What :class:`SlotKeeper` needs of a coordinator."""

    @property
    def settings(self) -> Coordination:
        """The coordination settings (``slot_renew_s``)."""
        ...

    def renew_slot(self, token: SlotToken) -> SlotToken:
        """Extend the slot's expiry and return the new token."""
        ...


@dataclass(frozen=True)
class Holder:
    """Who holds a slot: process id, run id and host name."""

    pid: int
    run_id: str
    host: str

    @classmethod
    def current(cls, run_id: str) -> Holder:
        """Return the holder for this process and *run_id*."""
        return cls(os.getpid(), run_id, socket.gethostname())


@dataclass(frozen=True)
class SlotToken:
    """A held slot: provider, slot number, generation and expiry (epoch s)."""

    provider: str
    slot: int
    generation: int
    expires_at: float


class BreakerStatus(enum.StrEnum):
    """The state of a provider's circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerState:
    """A breaker check's result.

    ``until`` is when an open breaker turns half-open, or when a half-open
    breaker's probe expires; ``probe`` is True for the one caller granted
    the half-open probe; ``opened`` is True for the one failure that turned
    a closed breaker open, or reopened a half-open one
    (:meth:`Coordinator.record_failure`).
    """

    status: BreakerStatus
    failures: int = 0
    until: float | None = None
    cause: str | None = None
    probe: bool = False
    opened: bool = False

    @property
    def allows(self) -> bool:
        """Return True when the caller may send: closed, or the probe."""
        return self.status is BreakerStatus.CLOSED or self.probe


@dataclass(frozen=True)
class QuotaToken:
    """One reserved quota unit: provider, its window and its unit number.

    ``unit`` can repeat after a refund; ``token_id`` is unique per token.
    """

    provider: str
    window_start: float
    unit: int
    token_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class QuotaStatus:
    """A provider's quota window; ``reset_at`` is None without an open window."""

    provider: str
    used: int
    daily_limit: int | None
    reset_at: float | None

    @property
    def remaining(self) -> int | None:
        """Return the units left in the window; None when no limit is known."""
        if self.daily_limit is None:
            return None
        return max(self.daily_limit - self.used, 0)


class Coordinator:
    """Opens the coordination database of *state_dir* for one holder.

    Each thread uses its own connection, opened on first use; :meth:`close`
    closes them all.

    Raises:
        CoordinationError: The database has another schema version.
        DatabaseBusyError: The database stayed busy for ``busy_timeout_s``.
    """

    def __init__(
        self,
        state_dir: Path,
        settings: Coordination,
        holder: Holder,
        *,
        clock: Clock = time.time,
    ) -> None:
        self.path = Path(state_dir) / DATABASE_NAME
        self.settings = settings
        self.holder = holder
        self._clock = clock
        self._random = random.Random()
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._released: set[str] = set()
        self._closed = False
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        try:
            self._transact(_check_schema)
        except BaseException:  # cleanup on every exit, interrupts too; re-raised
            self.close()
            raise

    def close(self) -> None:
        """Close every connection; later calls raise :class:`CoordinationError`.

        Call it once the threads that use the coordinator have stopped.
        """
        with self._lock:
            self._closed = True
            connections, self._connections = self._connections, []
        for connection in connections:
            connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise CoordinationError(f"Coordinator for {self.path} is closed.")
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is None:
            connection = connect(
                self.path,
                TABLES,
                busy_timeout=self.settings.busy_timeout_s,
                check_same_thread=False,
            )
            with self._lock:
                if self._closed:
                    connection.close()
                    raise CoordinationError(f"Coordinator for {self.path} is closed.")
                self._connections.append(connection)
            self._local.connection = connection
        return connection

    def _transact(self, func: Callable[[sqlite3.Connection], T]) -> T:
        return transact(
            self._connection(), func, busy_timeout=self.settings.busy_timeout_s
        )

    # Pacing

    def reserve(
        self,
        provider: str,
        delay_s: float,
        jitter_s: float,
        *,
        slot: SlotToken | None = None,
    ) -> float:
        """Reserve the next send time of *provider* and return it.

        Sets ``next_at = max(now, next_at) + delay_s + U(0, jitter_s)``. With
        *slot*, the same transaction checks that the slot is still held.

        Raises:
            SlotLost: *slot* expired or passed to another holder.
        """
        if delay_s < 0 or jitter_s < 0:
            raise ValueError("delay_s and jitter_s must not be negative.")
        gap = delay_s + (self._random.uniform(0.0, jitter_s) if jitter_s else 0.0)

        def write(connection: sqlite3.Connection) -> float:
            now = self._clock()
            if slot is not None:
                self._check_slot(connection, slot, now)
            row = connection.execute(
                "SELECT next_at FROM pacing WHERE provider = ?", (provider,)
            ).fetchone()
            send_at = now if row is None else max(now, float(row[0]))
            connection.execute(
                "INSERT INTO pacing (provider, next_at) VALUES (?, ?) "
                "ON CONFLICT (provider) DO UPDATE SET next_at = excluded.next_at",
                (provider, send_at + gap),
            )
            return send_at

        return self._transact(write)

    def still_on_time(self, send_at: float) -> bool:
        """Return True while the clock is at most the tolerance past *send_at*."""
        return self._clock() - send_at <= self.settings.pacing_tolerance_ms / 1000.0

    def wait_turn(
        self,
        provider: str,
        delay_s: float,
        jitter_s: float,
        *,
        slot: SlotToken | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Reserve, sleep until the send time and return when it is still on time.

        A caller that wakes past the tolerance reserves again. *sleep* runs
        at least once after every reservation, outside any transaction. With
        *slot*, a short transaction checks the slot once more after the
        on-time check.

        Raises:
            SlotLost: *slot* was lost before a reservation or during the wait.
        """
        while True:
            send_at = self.reserve(provider, delay_s, jitter_s, slot=slot)
            remaining = max(send_at - self._clock(), 0.0)
            while True:
                sleep(remaining)
                remaining = send_at - self._clock()
                if remaining <= 0:
                    break
            if self.still_on_time(send_at):
                if slot is not None:
                    self._verify_slot(slot)
                return
            logger.debug("Resumed late for %s; reserving again.", provider)

    # Slots

    def acquire_slot(self, provider: str, limit: int) -> SlotToken | None:
        """Take a download slot of *provider*; None while *limit* are held.

        Expired slots, those of dead holders included, count as free. The
        acquisition increments the slot's generation.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        ttl = self.settings.slot_ttl_s
        holder = self.holder

        def write(connection: sqlite3.Connection) -> SlotToken | None:
            now = self._clock()
            rows = connection.execute(
                "SELECT slot, generation, run_id, expires_at FROM slots "
                "WHERE provider = ?",
                (provider,),
            ).fetchall()
            generations = {int(row[0]): int(row[1]) for row in rows}
            held = {
                int(row[0])
                for row in rows
                if row[2] is not None and row[3] is not None and row[3] > now
            }
            if len(held) >= limit:
                return None
            slot = next(index for index in range(len(held) + 1) if index not in held)
            generation = generations.get(slot, 0) + 1
            expires_at = now + ttl
            connection.execute(
                "INSERT INTO slots "
                "(provider, slot, generation, pid, run_id, host, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (provider, slot) DO UPDATE SET "
                "generation = excluded.generation, pid = excluded.pid, "
                "run_id = excluded.run_id, host = excluded.host, "
                "expires_at = excluded.expires_at",
                (
                    provider,
                    slot,
                    generation,
                    holder.pid,
                    holder.run_id,
                    holder.host,
                    expires_at,
                ),
            )
            return SlotToken(provider, slot, generation, expires_at)

        return self._transact(write)

    def renew_slot(self, token: SlotToken) -> SlotToken:
        """Extend the slot's expiry by ``slot_ttl_s`` and return the new token.

        Raises:
            SlotLost: The slot expired or passed to another holder.
        """
        ttl = self.settings.slot_ttl_s

        def write(connection: sqlite3.Connection) -> SlotToken:
            now = self._clock()
            self._check_slot(connection, token, now)
            connection.execute(
                "UPDATE slots SET expires_at = ? WHERE provider = ? AND slot = ?",
                (now + ttl, token.provider, token.slot),
            )
            return replace(token, expires_at=now + ttl)

        return self._transact(write)

    def release_slot(self, token: SlotToken) -> None:
        """Free the slot when this holder still holds it with this generation."""
        holder = self.holder

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE slots SET pid = NULL, run_id = NULL, host = NULL, "
                "expires_at = NULL WHERE provider = ? AND slot = ? "
                "AND generation = ? AND pid = ? AND run_id = ? AND host = ?",
                (
                    token.provider,
                    token.slot,
                    token.generation,
                    holder.pid,
                    holder.run_id,
                    holder.host,
                ),
            )

        self._transact(write)

    def _verify_slot(self, token: SlotToken) -> None:
        def check(connection: sqlite3.Connection) -> None:
            self._check_slot(connection, token, self._clock())

        self._transact(check)

    def _check_slot(
        self, connection: sqlite3.Connection, token: SlotToken, now: float
    ) -> None:
        row = connection.execute(
            "SELECT generation, pid, run_id, host, expires_at FROM slots "
            "WHERE provider = ? AND slot = ?",
            (token.provider, token.slot),
        ).fetchone()
        where = f"slot {token.slot} of {token.provider}"
        if row is None:
            raise SlotLost(f"The {where} does not exist.", token)
        generation, pid, run_id, host, expires_at = row
        holder = self.holder
        if generation != token.generation or (pid, run_id, host) != (
            holder.pid,
            holder.run_id,
            holder.host,
        ):
            raise SlotLost(f"The {where} passed to another holder.", token)
        if expires_at is None or expires_at <= now:
            raise SlotLost(f"The {where} expired.", token)

    # Breakers

    def breaker_check(self, provider: str) -> BreakerState:
        """Return the breaker of *provider*; grant the half-open probe once.

        While half-open, the first caller gets ``probe=True`` for
        ``slot_ttl_s`` seconds; the others see the breaker blocked until the
        probe reports or expires.
        """
        ttl = self.settings.slot_ttl_s

        def write(connection: sqlite3.Connection) -> BreakerState:
            now = self._clock()
            row = connection.execute(
                "SELECT failures, open_until, cause, probe_until FROM breakers "
                "WHERE provider = ?",
                (provider,),
            ).fetchone()
            if row is None:
                return BreakerState(BreakerStatus.CLOSED)
            failures, open_until, cause, probe_until = row
            if open_until is None:
                return BreakerState(BreakerStatus.CLOSED, failures)
            if now < open_until:
                return BreakerState(BreakerStatus.OPEN, failures, open_until, cause)
            if probe_until is not None and probe_until > now:
                return BreakerState(
                    BreakerStatus.HALF_OPEN, failures, probe_until, cause
                )
            connection.execute(
                "UPDATE breakers SET probe_until = ? WHERE provider = ?",
                (now + ttl, provider),
            )
            return BreakerState(
                BreakerStatus.HALF_OPEN, failures, now + ttl, cause, probe=True
            )

        return self._transact(write)

    def record_success(self, provider: str) -> None:
        """Close the breaker of *provider* and reset its failure count."""

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE breakers SET failures = 0, open_until = NULL, cause = NULL, "
                "probe_until = NULL WHERE provider = ?",
                (provider,),
            )

        self._transact(write)

    def record_failure(
        self, provider: str, *, threshold: int, cooldown_s: float, cause: str
    ) -> BreakerState:
        """Count a failure; open the breaker at *threshold* or after a probe.

        An open breaker keeps its reopening time; a failure while half-open
        reopens it for *cooldown_s*. Returns the resulting state, with
        ``opened`` set when this failure opened or reopened the breaker.
        """
        if threshold < 1:
            raise ValueError("threshold must be at least 1.")

        def write(connection: sqlite3.Connection) -> BreakerState:
            now = self._clock()
            row = connection.execute(
                "SELECT failures, open_until, cause FROM breakers WHERE provider = ?",
                (provider,),
            ).fetchone()
            failures = (int(row[0]) if row else 0) + 1
            open_until: float | None = row[1] if row else None
            reason: str | None = row[2] if row else None
            opened = False
            if open_until is None:
                if failures >= threshold:
                    open_until, reason, opened = now + cooldown_s, cause, True
            elif now >= open_until:
                open_until, reason, opened = now + cooldown_s, cause, True
            connection.execute(
                "INSERT INTO breakers "
                "(provider, failures, open_until, cause, probe_until) "
                "VALUES (?, ?, ?, ?, NULL) ON CONFLICT (provider) DO UPDATE SET "
                "failures = excluded.failures, open_until = excluded.open_until, "
                "cause = excluded.cause, probe_until = NULL",
                (provider, failures, open_until, reason),
            )
            if open_until is None:
                return BreakerState(BreakerStatus.CLOSED, failures)
            return BreakerState(
                BreakerStatus.OPEN, failures, open_until, reason, opened=opened
            )

        return self._transact(write)

    # Quota

    def reserve_quota(
        self, provider: str, *, daily_limit: int, reset_hours: float
    ) -> QuotaToken | None:
        """Reserve one quota unit; None when the window's units are spent.

        The window opens at the first reservation after the previous one
        ended and lasts *reset_hours*; :meth:`quota_status` gives its reset
        time.
        """
        if daily_limit < 0:
            raise ValueError("daily_limit must not be negative.")
        if reset_hours <= 0:
            raise ValueError("reset_hours must be positive.")

        def write(connection: sqlite3.Connection) -> QuotaToken | None:
            now = self._clock()
            row = connection.execute(
                "SELECT used, window_start, reset_at FROM quotas WHERE provider = ?",
                (provider,),
            ).fetchone()
            if row is None or now >= row[2]:
                used, window_start, reset_at = 0, now, now + reset_hours * 3600.0
            else:
                used, window_start, reset_at = int(row[0]), row[1], row[2]
            if used >= daily_limit:
                return None
            connection.execute(
                "INSERT INTO quotas "
                "(provider, used, daily_limit, window_start, reset_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (provider) DO UPDATE SET "
                "used = excluded.used, daily_limit = excluded.daily_limit, "
                "window_start = excluded.window_start, reset_at = excluded.reset_at",
                (provider, used + 1, daily_limit, window_start, reset_at),
            )
            return QuotaToken(provider, window_start, used + 1)

        return self._transact(write)

    def release_quota(self, token: QuotaToken) -> None:
        """Return the unit of a request that provably was not sent.

        A token is returned at most once, and only to the window that issued
        it. A refund that fails counts as not made and can be retried.
        """
        with self._lock:
            if token.token_id in self._released:
                return
            self._released.add(token.token_id)

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE quotas SET used = used - 1 "
                "WHERE provider = ? AND window_start = ? AND used > 0",
                (token.provider, token.window_start),
            )

        try:
            self._transact(write)
        except BaseException:  # cleanup on every exit, interrupts too; re-raised
            with self._lock:
                self._released.discard(token.token_id)
            raise

    def quota_status(self, provider: str) -> QuotaStatus:
        """Return the units used in the open window and its reset time."""

        def query(connection: sqlite3.Connection) -> QuotaStatus:
            now = self._clock()
            row = connection.execute(
                "SELECT used, daily_limit, reset_at FROM quotas WHERE provider = ?",
                (provider,),
            ).fetchone()
            if row is None:
                return QuotaStatus(provider, 0, None, None)
            used, limit, reset_at = row
            if now >= reset_at:
                return QuotaStatus(provider, 0, limit, None)
            return QuotaStatus(provider, used, limit, reset_at)

        return self._transact(query)


@dataclass(frozen=True)
class Snapshot:
    """The breakers and quota windows of the coordination database.

    ``breakers`` and ``quotas`` hold the providers with a row; a provider
    without one has a closed breaker and no open window.
    """

    breakers: dict[str, BreakerState] = field(default_factory=dict)
    quotas: dict[str, QuotaStatus] = field(default_factory=dict)


def _breaker_of(row: tuple[Any, ...], now: float) -> BreakerState:
    """Return the breaker a row describes at *now*, granting no probe."""
    failures, open_until, cause, probe_until = row
    if open_until is None:
        return BreakerState(BreakerStatus.CLOSED, failures)
    if now < open_until:
        return BreakerState(BreakerStatus.OPEN, failures, open_until, cause)
    return BreakerState(BreakerStatus.HALF_OPEN, failures, probe_until, cause)


def read_snapshot(
    state_dir: Path, *, clock: Clock = time.time, busy_timeout: float = 30.0
) -> Snapshot:
    """Return the breakers and quotas of *state_dir*'s database, never writing.

    A read-only connection reads them; no probe is granted and nothing is
    created, so a missing database gives an empty snapshot.

    Raises:
        CoordinationError: The database has another schema version.
        DatabaseBusyError: The database stayed busy for *busy_timeout*.
    """
    path = Path(state_dir) / DATABASE_NAME
    if not path.is_file():
        return Snapshot()

    def query(connection: sqlite3.Connection) -> Snapshot:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"meta", "breakers", "quotas"} <= tables:
            return Snapshot()
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if version is not None and version[0] != str(SCHEMA_VERSION):
            raise CoordinationError(
                f"Coordination database schema {version[0]} found, "
                f"{SCHEMA_VERSION} expected."
            )
        now = clock()
        breakers = {
            str(row[0]): _breaker_of(tuple(row[1:]), now)
            for row in connection.execute(
                "SELECT provider, failures, open_until, cause, probe_until "
                "FROM breakers ORDER BY provider"
            )
        }
        quotas: dict[str, QuotaStatus] = {}
        for provider, used, limit, reset_at in connection.execute(
            "SELECT provider, used, daily_limit, reset_at FROM quotas ORDER BY provider"
        ):
            if now >= reset_at:
                quotas[provider] = QuotaStatus(provider, 0, limit, None)
            else:
                quotas[provider] = QuotaStatus(provider, used, limit, reset_at)
        return Snapshot(breakers, quotas)

    return read(path, query, busy_timeout=busy_timeout)


def _check_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    (value,) = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if value != str(SCHEMA_VERSION):
        raise CoordinationError(
            f"Coordination database schema {value} found, {SCHEMA_VERSION} "
            "expected; stop the other runs and delete the file."
        )


class SlotKeeper:
    """Renews a held slot every ``slot_renew_s`` seconds from a timer thread.

    Use it as a context manager around the downloads that use the slot. A
    refused renewal sets :attr:`lost` and ends the thread instead of
    raising; a database error is logged and the next tick tries again; a
    closed coordinator ends the thread. The
    reservations that name the slot refuse it on their own once it has
    expired, so :attr:`lost` serves to stop early.
    """

    def __init__(
        self,
        coordinator: SlotRenewer,
        token: SlotToken,
        *,
        interval: float | None = None,
    ) -> None:
        self.coordinator = coordinator
        self._token = token
        self.interval = (
            coordinator.settings.slot_renew_s if interval is None else interval
        )
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def token(self) -> SlotToken:
        """Return the latest renewed token."""
        return self._token

    @property
    def lost(self) -> bool:
        """Return True once a renewal was refused."""
        return self._lost.is_set()

    def start(self) -> None:
        """Start the renewal thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"slot-keeper-{self._token.provider}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the renewal thread and wait for it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

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

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._token = self.coordinator.renew_slot(self._token)
            except SlotLost as exc:
                logger.warning("Slot lost: %s", exc)
                self._lost.set()
                return
            except CoordinationError:
                return
            except sqlite3.Error as exc:
                logger.warning("Slot renewal failed, retrying: %s", exc)
