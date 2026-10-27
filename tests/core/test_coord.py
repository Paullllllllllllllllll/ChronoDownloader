"""Machine-wide coordination: pacing, slots, breakers and quota.

Deterministic schedules run several coordinators (one per simulated process,
each with its own holder and connection) on one database with a shared
:class:`FakeClock`; pause points are the ``sleep`` callbacks of
:meth:`Coordinator.wait_turn`. The multi-process tests spawn
``tests/core/coord_child.py`` and measure arrival times at a local
``ThreadingHTTPServer``.
"""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from chronodownloader import coord
from chronodownloader import sqlite as sqlite_module
from chronodownloader.coord import (
    BreakerStatus,
    CoordinationError,
    Coordinator,
    Holder,
    SlotKeeper,
    SlotLost,
)
from chronodownloader.settings import Coordination
from tests.core.coord_child import FakeClock
from tests.core.spawn import CHILD_TIMEOUT, child, expect, line, send

PROVIDER = "mdz"
DELAY = 0.1
TOLERANCE_MS = 40
ARRIVAL_SLACK = 0.05  # seconds; covers connect and accept latency at the server
SETTINGS = Coordination(
    pacing_tolerance_ms=TOLERANCE_MS,
    slot_ttl_s=10.0,
    slot_renew_s=1.0,
    busy_timeout_s=10.0,
)
EPSILON = 1e-6
"""Float resolution of fake epoch times near 1.8e9 s."""


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def opened(tmp_path: Path, clock: FakeClock) -> Iterator[Callable[..., Coordinator]]:
    """Open coordinators on one state folder; each name is its own holder."""
    coordinators: list[Coordinator] = []

    def open_(name: str, settings: Coordination = SETTINGS) -> Coordinator:
        holder = Holder(pid=1000 + len(coordinators), run_id=name, host="test")
        coordinator = Coordinator(tmp_path / "state", settings, holder, clock=clock)
        coordinators.append(coordinator)
        return coordinator

    yield open_
    for coordinator in coordinators:
        coordinator.close()


def _gaps(times: list[float]) -> list[float]:
    ordered = sorted(times)
    return [later - earlier for earlier, later in pairwise(ordered)]


# Database and connections


def test_the_state_folder_is_created_and_holds_the_database(
    tmp_path: Path, clock: FakeClock
) -> None:
    state = tmp_path / "missing" / "state"
    with Coordinator(state, SETTINGS, Holder.current("run"), clock=clock) as first:
        assert first.path == state / "coordination.sqlite"
    assert sqlite_module.table_names(first.path) == [
        "breakers",
        "meta",
        "pacing",
        "quotas",
        "slots",
    ]
    rows = sqlite_module.read_rows(first.path, "meta")
    assert rows == [{"key": "schema_version", "value": str(coord.SCHEMA_VERSION)}]


def test_every_connection_reports_the_rollback_journal(
    opened: Callable[..., Coordinator],
) -> None:
    coordinator = opened("a")
    modes: list[str] = []

    def mode() -> None:
        connection = coordinator._connection()
        modes.append(connection.execute("PRAGMA journal_mode").fetchone()[0])
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2

    mode()
    worker = threading.Thread(target=mode)
    worker.start()
    worker.join()
    coordinator.reserve(PROVIDER, 0.0, 0.0)

    assert modes == ["delete", "delete"]
    assert len(coordinator._connections) == 2
    assert sqlite_module.read(
        coordinator.path, lambda c: c.execute("PRAGMA journal_mode").fetchone()[0]
    ) == ("delete")


def test_a_closed_coordinator_refuses_calls(
    opened: Callable[..., Coordinator],
) -> None:
    coordinator = opened("a")
    coordinator.close()
    with pytest.raises(CoordinationError, match="closed"):
        coordinator.reserve(PROVIDER, 0.0, 0.0)


def test_another_schema_version_is_refused(
    tmp_path: Path, opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    first = opened("a")
    with contextlib.closing(sqlite3.connect(first.path)) as connection:
        connection.execute("UPDATE meta SET value = '99'")
        connection.commit()
    with pytest.raises(CoordinationError, match="schema 99"):
        Coordinator(tmp_path / "state", SETTINGS, Holder.current("b"), clock=clock)


def test_a_locked_database_raises_the_typed_busy_error(
    opened: Callable[..., Coordinator],
) -> None:
    coordinator = opened(
        "a", Coordination(busy_timeout_s=0.2, pacing_tolerance_ms=TOLERANCE_MS)
    )
    blocker = sqlite3.connect(coordinator.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(coord.DatabaseBusyError):
            coordinator.reserve(PROVIDER, DELAY, 0.0)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert coord.DatabaseBusyError is sqlite_module.DatabaseBusyError
    assert coordinator.reserve(PROVIDER, DELAY, 0.0) > 0


# Pacing


def test_reservations_space_send_times_by_the_delay(
    opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    first, second = opened("a"), opened("b")
    start = clock()

    assert first.reserve(PROVIDER, DELAY, 0.0) == start
    assert second.reserve(PROVIDER, DELAY, 0.0) == pytest.approx(start + DELAY)
    assert first.reserve(PROVIDER, DELAY, 0.0) == pytest.approx(start + 2 * DELAY)
    assert first.reserve("loc", DELAY, 0.0) == start
    clock.advance(10.0)
    assert second.reserve(PROVIDER, DELAY, 0.0) == clock()


def test_jitter_adds_up_to_its_bound(
    opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    coordinator = opened("a")
    times = [coordinator.reserve(PROVIDER, DELAY, 0.05) for _ in range(30)]

    gaps = _gaps(times)
    assert all(DELAY - EPSILON <= gap <= DELAY + 0.05 + EPSILON for gap in gaps)
    assert len({round(gap, 5) for gap in gaps}) > 1
    with pytest.raises(ValueError):
        coordinator.reserve(PROVIDER, -1.0, 0.0)


def test_still_on_time_allows_the_tolerance_only(
    opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    coordinator = opened("a")
    send_at = clock()

    assert coordinator.still_on_time(send_at)
    clock.advance(TOLERANCE_MS / 1000)
    assert coordinator.still_on_time(send_at)
    clock.advance(0.001)
    assert not coordinator.still_on_time(send_at)


@pytest.mark.parametrize(
    ("pause", "reservations"),
    [(0.02, 1), (0.04, 1), (0.25, 2), (0.5, 2)],
    ids=["within-tolerance", "at-tolerance", "late", "very-late"],
)
def test_a_sender_paused_after_reserving_resumes_alongside_another(
    opened: Callable[..., Coordinator],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    pause: float,
    reservations: int,
) -> None:
    paused, other = opened("paused"), opened("other")
    arrivals: list[float] = []
    reserved: list[float] = []
    sleeps: list[float] = []
    reserve = paused.reserve

    def counting(*args: Any, **kwargs: Any) -> float:
        reserved.append(reserve(*args, **kwargs))
        return reserved[-1]

    def other_sends() -> None:
        other.wait_turn(PROVIDER, DELAY, 0.0, sleep=clock.advance)
        arrivals.append(clock())

    def pause_point(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 1:
            clock.advance(seconds)
            return
        resume_at = clock() + pause
        for _ in range(int(pause / DELAY + 1e-9)):
            other_sends()
        clock.now = max(clock.now, resume_at)

    monkeypatch.setattr(paused, "reserve", counting)
    paused.wait_turn(PROVIDER, DELAY, 0.0, sleep=pause_point)
    arrivals.append(clock())
    while len(arrivals) < 6:
        other_sends()

    assert len(reserved) == reservations
    assert min(_gaps(arrivals)) >= DELAY - TOLERANCE_MS / 1000 - EPSILON


# Slots


def test_slots_hold_the_limit_and_count_generations(
    opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    first, second = opened("a"), opened("b")

    one = first.acquire_slot(PROVIDER, 2)
    two = second.acquire_slot(PROVIDER, 2)
    assert one is not None and two is not None
    assert (one.slot, one.generation) == (0, 1)
    assert (two.slot, two.generation) == (1, 1)
    assert one.expires_at == clock() + SETTINGS.slot_ttl_s
    assert first.acquire_slot(PROVIDER, 2) is None
    assert first.acquire_slot("loc", 2) is not None

    second.release_slot(one)  # not the holder: nothing happens
    assert second.acquire_slot(PROVIDER, 2) is None
    first.release_slot(one)
    again = second.acquire_slot(PROVIDER, 2)
    assert again is not None and (again.slot, again.generation) == (0, 2)
    with pytest.raises(SlotLost):
        first.renew_slot(one)
    with pytest.raises(ValueError):
        first.acquire_slot(PROVIDER, 0)


@pytest.mark.parametrize("released", [False, True], ids=["expired", "released"])
def test_a_slot_lost_during_the_wait_is_refused_on_an_on_time_wake(
    opened: Callable[..., Coordinator], clock: FakeClock, released: bool
) -> None:
    holder, other = opened("holder"), opened("other")
    token = holder.acquire_slot(PROVIDER, 1)
    assert token is not None
    clock.advance(SETTINGS.slot_ttl_s - 0.01)
    taken: list[Any] = []

    def sleep(seconds: float) -> None:
        if not taken:
            if released:
                holder.release_slot(token)
            clock.advance(0.02)
            taken.append(other.acquire_slot(PROVIDER, 1))
        else:
            clock.advance(seconds)

    with pytest.raises(SlotLost):
        holder.wait_turn(PROVIDER, 0.0, 0.0, slot=token, sleep=sleep)
    assert taken[0] is not None and taken[0].generation == 2


def test_renewal_extends_and_is_refused_after_expiry(
    opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    holder, other = opened("a"), opened("b")
    token = holder.acquire_slot(PROVIDER, 1)
    assert token is not None

    clock.advance(SETTINGS.slot_ttl_s - 1)
    renewed = holder.renew_slot(token)
    assert renewed.expires_at == clock() + SETTINGS.slot_ttl_s
    clock.advance(SETTINGS.slot_ttl_s - 1)
    assert other.acquire_slot(PROVIDER, 1) is None
    clock.advance(1.0)
    with pytest.raises(SlotLost, match="expired") as raised:
        holder.renew_slot(renewed)
    assert raised.value.token == renewed
    taken = other.acquire_slot(PROVIDER, 1)
    assert taken is not None and taken.generation == 2
    with pytest.raises(SlotLost, match="another holder"):
        holder.reserve(PROVIDER, 0.0, 0.0, slot=renewed)


def test_a_holder_whose_renewals_fail_mid_stream_starts_no_further_request(
    opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    holder, other = opened("a"), opened("b")
    token = holder.acquire_slot(PROVIDER, 1)
    assert token is not None
    sent: list[str] = []

    with SlotKeeper(holder, token, interval=0.01) as keeper:
        holder.wait_turn(PROVIDER, DELAY, 0.0, slot=keeper.token, sleep=clock.advance)
        sent.append("first")
        clock.advance(SETTINGS.slot_ttl_s + 1)
        deadline = time.monotonic() + 10.0
        while not keeper.lost and time.monotonic() < deadline:
            time.sleep(0.01)
        assert keeper.lost
        taken = other.acquire_slot(PROVIDER, 1)
        assert taken is not None and taken.generation == 2
        with pytest.raises(SlotLost):
            holder.wait_turn(
                PROVIDER, DELAY, 0.0, slot=keeper.token, sleep=clock.advance
            )
            sent.append("second")

    assert sent == ["first"]
    other.wait_turn(PROVIDER, DELAY, 0.0, slot=taken, sleep=clock.advance)
    holder.release_slot(token)
    assert other.renew_slot(taken).generation == 2


def test_the_keeper_renews_while_the_slot_is_held(
    opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    holder = opened("a")
    token = holder.acquire_slot(PROVIDER, 1)
    assert token is not None

    with SlotKeeper(holder, token, interval=0.01) as keeper:
        clock.advance(5.0)
        deadline = time.monotonic() + 10.0
        while keeper.token == token and time.monotonic() < deadline:
            time.sleep(0.01)
        assert keeper.token.expires_at >= clock() + SETTINGS.slot_ttl_s - 1
        assert not keeper.lost


@pytest.mark.parametrize("suspended", [1.0, 20.0], ids=["before-expiry", "past"])
def test_a_suspension_before_dispatch_while_another_seeks_the_slot(
    opened: Callable[..., Coordinator], clock: FakeClock, suspended: float
) -> None:
    holder, seeker = opened("holder"), opened("seeker")
    token = holder.acquire_slot(PROVIDER, 1)
    assert token is not None
    seeker_tokens: list[Any] = []
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        if not calls:
            seeker_tokens.append(seeker.acquire_slot(PROVIDER, 1))
            clock.advance(suspended)
            seeker_tokens.append(seeker.acquire_slot(PROVIDER, 1))
        else:
            clock.advance(seconds)
        calls.append(seconds)

    holder.reserve(PROVIDER, DELAY, 0.0, slot=token)  # an earlier request
    if suspended < SETTINGS.slot_ttl_s:
        holder.wait_turn(PROVIDER, DELAY, 0.0, slot=token, sleep=sleep)
        assert seeker_tokens == [None, None]
    else:
        with pytest.raises(SlotLost):
            holder.wait_turn(PROVIDER, DELAY, 0.0, slot=token, sleep=sleep)
        assert seeker_tokens[0] is None
        assert seeker_tokens[1] is not None and seeker_tokens[1].generation == 2


# Breakers


def test_only_the_failure_that_opens_a_breaker_reports_opening(
    opened: Callable[..., Coordinator],
) -> None:
    first, second = opened("a"), opened("b")
    one = first.record_failure(PROVIDER, threshold=3, cooldown_s=60, cause="503")
    two = first.record_failure(PROVIDER, threshold=2, cooldown_s=60, cause="503")
    # A late failure under a higher threshold reaches its own count but
    # finds the breaker already open: it did not open it.
    three = second.record_failure(PROVIDER, threshold=3, cooldown_s=60, cause="503")

    assert (one.opened, two.opened, three.opened) == (False, True, False)
    assert three.status is BreakerStatus.OPEN and three.until == two.until


def test_a_breaker_opened_by_one_holder_blocks_another(
    opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    first, second, third = opened("a"), opened("b"), opened("c")
    assert first.breaker_check(PROVIDER).allows

    state = first.record_failure(PROVIDER, threshold=2, cooldown_s=60, cause="503")
    assert state.status is BreakerStatus.CLOSED and state.failures == 1
    first.record_success(PROVIDER)
    first.record_failure(PROVIDER, threshold=2, cooldown_s=60, cause="503")
    assert first.breaker_check(PROVIDER).status is BreakerStatus.CLOSED
    state = second.record_failure(PROVIDER, threshold=2, cooldown_s=60, cause="503")
    assert state.status is BreakerStatus.OPEN

    blocked = third.breaker_check(PROVIDER)
    assert blocked.status is BreakerStatus.OPEN and not blocked.allows
    assert blocked.until == clock() + 60 and blocked.cause == "503"
    assert third.breaker_check("loc").allows

    clock.advance(60)
    probes = [c.breaker_check(PROVIDER) for c in (first, second, third, first)]
    assert [p.status for p in probes] == [BreakerStatus.HALF_OPEN] * 4
    assert [p.probe for p in probes] == [True, False, False, False]

    failed_probe = first.record_failure(
        PROVIDER, threshold=2, cooldown_s=30, cause="timeout"
    )
    assert failed_probe.opened  # the probe's call finishes its retries
    reopened = second.breaker_check(PROVIDER)
    assert reopened.status is BreakerStatus.OPEN and reopened.cause == "timeout"
    assert reopened.until == clock() + 30

    clock.advance(30)
    assert second.breaker_check(PROVIDER).probe
    clock.advance(SETTINGS.slot_ttl_s)
    assert third.breaker_check(PROVIDER).probe  # the first probe expired
    third.record_success(PROVIDER)
    assert first.breaker_check(PROVIDER).status is BreakerStatus.CLOSED
    with pytest.raises(ValueError):
        first.record_failure(PROVIDER, threshold=0, cooldown_s=1, cause="x")


# Quota


def test_quota_units_are_reserved_released_and_reset(
    opened: Callable[..., Coordinator], clock: FakeClock
) -> None:
    first, second = opened("a"), opened("b")
    start = clock()

    units = [
        c.reserve_quota("annas_archive", daily_limit=3, reset_hours=24)
        for c in (first, second, first)
    ]
    assert [u.unit for u in units if u is not None] == [1, 2, 3]
    assert second.reserve_quota("annas_archive", daily_limit=3, reset_hours=24) is None
    status = second.quota_status("annas_archive")
    assert (status.used, status.daily_limit, status.remaining) == (3, 3, 0)
    assert status.reset_at == start + 24 * 3600

    assert units[2] is not None
    first.release_quota(units[2])
    first.release_quota(units[2])
    assert second.quota_status("annas_archive").used == 2
    assert second.reserve_quota("annas_archive", daily_limit=3, reset_hours=24)

    clock.advance(24 * 3600)
    assert first.quota_status("annas_archive").reset_at is None
    fresh = first.reserve_quota("annas_archive", daily_limit=3, reset_hours=24)
    assert fresh is not None and fresh.unit == 1
    assert units[0] is not None
    first.release_quota(units[0])  # issued by the previous window
    assert first.quota_status("annas_archive").used == 1
    assert first.quota_status("loc").remaining is None
    with pytest.raises(ValueError):
        first.reserve_quota("loc", daily_limit=1, reset_hours=0)


def test_consecutive_refunds_each_return_their_unit(
    opened: Callable[..., Coordinator],
) -> None:
    coordinator = opened("a")
    for _ in range(2):
        token = coordinator.reserve_quota("annas_archive", daily_limit=3, reset_hours=1)
        assert token is not None and token.unit == 1
        coordinator.release_quota(token)
        assert coordinator.quota_status("annas_archive").used == 0


def test_a_failed_refund_can_be_retried(
    opened: Callable[..., Coordinator],
) -> None:
    coordinator = opened(
        "a", Coordination(busy_timeout_s=0.2, pacing_tolerance_ms=TOLERANCE_MS)
    )
    token = coordinator.reserve_quota("annas_archive", daily_limit=3, reset_hours=1)
    assert token is not None
    blocker = sqlite3.connect(coordinator.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(coord.DatabaseBusyError):
            coordinator.release_quota(token)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    coordinator.release_quota(token)
    coordinator.release_quota(token)
    assert coordinator.quota_status("annas_archive").used == 0
    again = coordinator.reserve_quota("annas_archive", daily_limit=3, reset_hours=1)
    assert again is not None and again.token_id != token.token_id


# Multi-process


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.arrivals: list[float] = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def process_request(self, request: Any, client_address: Any) -> None:
        self.arrivals.append(time.perf_counter())
        super().process_request(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self) -> None:
        hold = float(parse_qs(urlsplit(self.path).query).get("hold", ["0"])[0])
        server = self.server
        with server.lock:
            server.in_flight += 1
            server.max_in_flight = max(server.max_in_flight, server.in_flight)
        time.sleep(hold)
        with server.lock:
            server.in_flight -= 1
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def server() -> Iterator[_Server]:
    instance = _Server()
    thread = threading.Thread(
        target=instance.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def _start(stack: contextlib.ExitStack, *args: str) -> subprocess.Popen[str]:
    process = stack.enter_context(child("coord_child.py", *args))
    assert expect(process, "READY") == ["delete"]
    return process


def _finish(processes: list[subprocess.Popen[str]], word: str = "DONE") -> None:
    for process in processes:
        expect(process, word)
    for process in processes:
        assert process.wait(CHILD_TIMEOUT) == 0


@pytest.mark.parametrize(("processes", "count"), [(2, 5), (4, 4)])
def test_the_pacing_interval_holds_across_processes(
    tmp_path: Path, server: _Server, processes: int, count: int
) -> None:
    state, port = str(tmp_path / "state"), str(server.server_address[1])
    with contextlib.ExitStack() as stack:
        children = [
            _start(
                stack,
                "pace",
                state,
                port,
                PROVIDER,
                str(count),
                str(DELAY),
                "0.02",
                str(TOLERANCE_MS),
            )
            for _ in range(processes)
        ]
        for process in children:
            send(process)
        _finish(children)

    assert len(server.arrivals) == processes * count
    gaps = _gaps(server.arrivals)
    assert min(gaps) >= DELAY - TOLERANCE_MS / 1000 - ARRIVAL_SLACK
    span = max(server.arrivals) - min(server.arrivals)
    assert span >= (processes * count - 1) * DELAY - TOLERANCE_MS / 1000 - ARRIVAL_SLACK


@pytest.mark.parametrize(("processes", "limit"), [(2, 1), (4, 2)])
def test_the_slot_limit_holds_across_processes(
    tmp_path: Path, server: _Server, processes: int, limit: int
) -> None:
    state, port = str(tmp_path / "state"), str(server.server_address[1])
    with contextlib.ExitStack() as stack:
        children = [
            _start(stack, "slots", state, port, PROVIDER, "3", str(limit), "0.1")
            for _ in range(processes)
        ]
        for process in children:
            send(process)
        _finish(children)

    assert len(server.arrivals) == processes * 3
    assert server.max_in_flight == limit


def test_a_process_paused_after_reserving_resumes_alongside_another_sender(
    tmp_path: Path, server: _Server
) -> None:
    state, port = str(tmp_path / "state"), str(server.server_address[1])
    with contextlib.ExitStack() as stack:
        paused = _start(
            stack, "pause", state, port, PROVIDER, str(DELAY), str(TOLERANCE_MS)
        )
        sender = _start(
            stack,
            "pace",
            state,
            port,
            PROVIDER,
            "4",
            str(DELAY),
            "0",
            str(TOLERANCE_MS),
        )
        send(paused)
        expect(paused, "PAUSED")
        send(sender)
        time.sleep(2.5 * DELAY)
        send(paused)
        assert expect(paused, "SENT") == ["2"]
        _finish([sender])
        assert paused.wait(CHILD_TIMEOUT) == 0

    assert len(server.arrivals) == 5
    assert min(_gaps(server.arrivals)) >= DELAY - TOLERANCE_MS / 1000


@contextlib.contextmanager
def _servers(
    tmp_path: Path, *names: str, ttl: float = 10.0
) -> Iterator[list[subprocess.Popen[str]]]:
    with contextlib.ExitStack() as stack:
        processes = [
            _start(stack, "serve", str(tmp_path / "state"), name, str(ttl))
            for name in names
        ]
        yield processes
        for process in processes:
            send(process, "exit")
        for process in processes:
            assert process.wait(CHILD_TIMEOUT) == 0


def _ask(process: subprocess.Popen[str], command: str) -> str:
    send(process, command)
    return line(process)


def test_a_breaker_opened_in_one_process_blocks_another(tmp_path: Path) -> None:
    with _servers(tmp_path, "a", "b") as (first, second):
        assert _ask(first, f"fail {PROVIDER} 2 0.5 http-503") == "STATE closed"
        assert _ask(second, f"check {PROVIDER}") == "STATE closed 0"
        assert _ask(first, f"fail {PROVIDER} 2 0.5 http-503") == "STATE open"
        assert _ask(second, f"check {PROVIDER}") == "STATE open 0"
        time.sleep(0.6)
        send(first, f"check {PROVIDER}")
        send(second, f"check {PROVIDER}")
        answers = sorted([line(first), line(second)])
        assert answers == ["STATE half_open 0", "STATE half_open 1"]
        assert _ask(second, f"success {PROVIDER}") == "STATE closed"
        assert _ask(first, f"check {PROVIDER}") == "STATE closed 0"


def test_one_remaining_quota_unit_admits_exactly_one_of_two_processes(
    tmp_path: Path,
) -> None:
    with Coordinator(tmp_path / "state", SETTINGS, Holder.current("t")) as local:
        for _ in range(2):
            assert local.reserve_quota("annas_archive", daily_limit=3, reset_hours=1)
        with _servers(tmp_path, "a", "b") as processes:
            for process in processes:
                send(process, "quota annas_archive 3 1")
            answers = sorted(line(process) for process in processes)
        reset_at = local.quota_status("annas_archive").reset_at

    assert answers[1] == "TOKEN 3"
    assert answers[0] == f"SPENT {reset_at!r}"


def test_a_killed_slot_holders_slot_is_reclaimed_after_expiry(
    tmp_path: Path,
) -> None:
    ttl = 2.0
    with contextlib.ExitStack() as stack:
        holder = _start(stack, "serve", str(tmp_path / "state"), "killed", str(ttl))
        assert _ask(holder, f"acquire {PROVIDER} 1") == "SLOT 0 1"
        holder.kill()
        holder.wait(CHILD_TIMEOUT)

    settings = Coordination(slot_ttl_s=ttl)
    state = tmp_path / "state"
    with Coordinator(state, settings, Holder.current("now")) as now:
        assert now.acquire_slot(PROVIDER, 1) is None
    later = Coordinator(
        state, settings, Holder.current("later"), clock=lambda: time.time() + ttl
    )
    with later:
        token = later.acquire_slot(PROVIDER, 1)
    assert token is not None and (token.slot, token.generation) == (0, 2)
