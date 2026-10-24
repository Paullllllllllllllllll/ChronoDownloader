"""Child process and shared clock for the coordination tests.

Usage, from the repository root; STATE is the state folder, PORT the local
test server's port. Every mode first prints ``READY <journal_mode>`` with
the journal mode of its coordination connection.

    python tests/core/coord_child.py pace STATE PORT PROVIDER COUNT DELAY JITTER TOL_MS
        Wait for a line on stdin, then send COUNT paced requests and print
        DONE.

    python tests/core/coord_child.py slots STATE PORT PROVIDER COUNT LIMIT HOLD
        Wait for a line, then COUNT times: take one of LIMIT slots, send one
        request that the server holds HOLD seconds, release the slot. Print
        DONE.

    python tests/core/coord_child.py pause STATE PORT PROVIDER DELAY TOL_MS
        Wait for a line, reserve, print PAUSED and wait for another line
        (the pause point after reserving), then send once and print
        ``SENT <reservations>``.

    python tests/core/coord_child.py serve STATE RUN_ID TTL
        Answer one command per stdin line until ``exit``:
        ``acquire P LIMIT`` prints ``SLOT <slot> <generation>`` or NONE;
        ``fail P THRESHOLD COOLDOWN CAUSE`` and ``success P`` print
        ``STATE <status>``; ``check P`` prints ``STATE <status> <probe>``;
        ``quota P LIMIT HOURS`` prints ``TOKEN <unit>`` or
        ``SPENT <reset_at>``.

The module also defines the injectable clock the tests import.
"""

from __future__ import annotations

import http.client
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chronodownloader.coord import Coordinator, Holder, SlotToken  # noqa: E402
from chronodownloader.settings import Coordination  # noqa: E402


class FakeClock:
    """An injectable clock in epoch seconds that tests advance by hand."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _say(text: str) -> None:
    print(text, flush=True)  # noqa: T201 - the parent reads stdout


class CountingCoordinator(Coordinator):
    """A coordinator that counts its pacing reservations."""

    reservations = 0

    def reserve(
        self,
        provider: str,
        delay_s: float,
        jitter_s: float,
        *,
        slot: SlotToken | None = None,
    ) -> float:
        self.reservations += 1
        return super().reserve(provider, delay_s, jitter_s, slot=slot)


def _coordinator(
    state: str, run_id: str, settings: Coordination
) -> CountingCoordinator:
    coordinator = CountingCoordinator(Path(state), settings, Holder.current(run_id))
    (mode,) = coordinator._connection().execute("PRAGMA journal_mode").fetchone()
    _say(f"READY {mode}")
    return coordinator


def _get(port: int, path: str) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path)
        connection.getresponse().read()
    finally:
        connection.close()


def pace(argv: list[str]) -> None:
    state, port, provider, count, delay, jitter, tolerance = argv
    run_id = f"pace-{os.getpid()}"
    settings = Coordination(pacing_tolerance_ms=int(tolerance))
    with _coordinator(state, run_id, settings) as coord:
        sys.stdin.readline()
        for _ in range(int(count)):
            coord.wait_turn(provider, float(delay), float(jitter))
            _get(int(port), f"/{run_id}")
    _say("DONE")


def slots(argv: list[str]) -> None:
    state, port, provider, count, limit, hold = argv
    run_id = f"slots-{os.getpid()}"
    with _coordinator(state, run_id, Coordination()) as coord:
        sys.stdin.readline()
        for _ in range(int(count)):
            token = coord.acquire_slot(provider, int(limit))
            while token is None:
                time.sleep(0.005)
                token = coord.acquire_slot(provider, int(limit))
            coord.wait_turn(provider, 0.0, 0.0, slot=token)
            _get(int(port), f"/{run_id}?hold={hold}")
            coord.release_slot(token)
    _say("DONE")


def pause(argv: list[str]) -> None:
    state, port, provider, delay, tolerance = argv
    run_id = f"pause-{os.getpid()}"
    settings = Coordination(pacing_tolerance_ms=int(tolerance))
    with _coordinator(state, run_id, settings) as coord:
        paused = False

        def sleep(seconds: float) -> None:
            nonlocal paused
            if not paused:
                paused = True
                _say("PAUSED")
                sys.stdin.readline()
                return
            time.sleep(seconds)

        sys.stdin.readline()
        coord.wait_turn(provider, float(delay), 0.0, sleep=sleep)
        _get(int(port), f"/{run_id}")
        _say(f"SENT {coord.reservations}")


def _command(coord: Coordinator, words: list[str]) -> str:
    verb, provider, *rest = words
    if verb == "acquire":
        token = coord.acquire_slot(provider, int(rest[0]))
        return "NONE" if token is None else f"SLOT {token.slot} {token.generation}"
    if verb == "fail":
        threshold, cooldown, cause = rest
        state = coord.record_failure(
            provider, threshold=int(threshold), cooldown_s=float(cooldown), cause=cause
        )
        return f"STATE {state.status}"
    if verb == "success":
        coord.record_success(provider)
        return f"STATE {coord.breaker_check(provider).status}"
    if verb == "check":
        state = coord.breaker_check(provider)
        return f"STATE {state.status} {int(state.probe)}"
    if verb == "quota":
        unit = coord.reserve_quota(
            provider, daily_limit=int(rest[0]), reset_hours=float(rest[1])
        )
        if unit is None:
            return f"SPENT {coord.quota_status(provider).reset_at!r}"
        return f"TOKEN {unit.unit}"
    raise SystemExit(f"unknown command {verb!r}")


def serve(argv: list[str]) -> None:
    state, run_id, ttl = argv
    with _coordinator(state, run_id, Coordination(slot_ttl_s=float(ttl))) as coord:
        for raw in sys.stdin:
            words = raw.split()
            if not words or words[0] == "exit":
                break
            _say(_command(coord, words))


def main(argv: list[str]) -> None:
    modes = {"pace": pace, "slots": slots, "pause": pause, "serve": serve}
    mode = argv[0]
    if mode not in modes:
        raise SystemExit(f"unknown mode {mode!r}")
    modes[mode](argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
