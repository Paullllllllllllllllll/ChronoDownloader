"""Network clients sharing the real coordination database.

Two clients, each with its own Coordinator on one state folder (as two runs
on one machine would be), send to a local server: their arrivals keep the
provider's pacing interval, a breaker opened through one blocks the other,
and the judge's slots follow ``judge.network.slots``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from chronodownloader.coord import Coordinator, Holder
from chronodownloader.net import JUDGE, BreakerOpen, Network, RequestFailed
from chronodownloader.settings import Coordination, Judge, JudgeNetwork, Settings

from .support import Reply, ScriptedServer, settings_with

DELAY_MS = 300
TOLERANCE_MS = 100
MARGIN_S = 0.03
"""Allowance for scheduling between the client's wake-up and the arrival."""


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[tuple[Coordinator, Coordinator]]:
    settings = Coordination(pacing_tolerance_ms=TOLERANCE_MS)
    first = Coordinator(tmp_path, settings, Holder.current("run-a"))
    second = Coordinator(tmp_path, settings, Holder.current("run-b"))
    try:
        yield first, second
    finally:
        first.close()
        second.close()


def _clients(
    settings: Settings, pair: tuple[Coordinator, Coordinator]
) -> tuple[Network, Network]:
    return Network(settings, pair[0]), Network(settings, pair[1])


def test_two_clients_keep_the_pacing_interval(
    server: ScriptedServer, pair: tuple[Coordinator, Coordinator]
) -> None:
    url = server.script("/paced", Reply(200, b"ok"))
    clients = _clients(settings_with(delay_ms=DELAY_MS), pair)
    errors: list[BaseException] = []

    def send(client: Network) -> None:
        try:
            for _ in range(3):
                client.request("mdz", "GET", url)
        except RequestFailed as exc:
            errors.append(exc)

    threads = [threading.Thread(target=send, args=(client,)) for client in clients]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    finally:
        for client in clients:
            client.close()
    assert errors == []
    times = sorted(arrival.at for arrival in server.arrivals)
    assert len(times) == 6
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    floor = (DELAY_MS - TOLERANCE_MS) / 1000.0 - MARGIN_S
    assert min(gaps) >= floor, gaps


def _gaps(server: ScriptedServer) -> list[float]:
    times = sorted(arrival.at for arrival in server.arrivals)
    return [later - earlier for earlier, later in zip(times, times[1:], strict=False)]


def test_each_hop_of_a_redirect_chain_is_paced(
    server: ScriptedServer, pair: tuple[Coordinator, Coordinator]
) -> None:
    server.script("/a", Reply(302, headers={"Location": "/b"}))
    server.script("/b", Reply(302, headers={"Location": "/c"}))
    server.script("/c", Reply(200, b"ok"))
    with Network(settings_with(delay_ms=DELAY_MS), pair[0]) as client:
        assert client.request("mdz", "GET", server.url("/a")).content == b"ok"
    assert [arrival.path for arrival in server.arrivals] == ["/a", "/b", "/c"]
    floor = (DELAY_MS - TOLERANCE_MS) / 1000.0 - MARGIN_S
    assert min(_gaps(server)) >= floor


def test_a_breaker_opened_by_another_run_during_backoff_stops_the_retry(
    server: ScriptedServer, pair: tuple[Coordinator, Coordinator]
) -> None:
    flaky = server.script("/flaky", Reply(503), Reply(200))
    blocked = server.script("/blocked", Reply(403))
    settings = settings_with(circuit_breaker_threshold=2, max_attempts=3)
    other = Network(settings, pair[1])
    backoffs: list[float] = []

    def other_run_fails_during_backoff(seconds: float) -> None:
        if seconds > 0 and not backoffs:
            backoffs.append(seconds)
            with pytest.raises(RequestFailed):
                other.request("mdz", "GET", blocked)

    retrying = Network(settings, pair[0], sleep=other_run_fails_during_backoff)
    try:
        with pytest.raises(BreakerOpen) as excinfo:
            retrying.request("mdz", "GET", flaky)
    finally:
        other.close()
        retrying.close()
    assert backoffs == [0.5]
    assert server.hits("/flaky") == 1
    assert excinfo.value.cause == "blocked"


def test_the_probe_holder_keeps_its_probe_across_a_redirect_chain(
    server: ScriptedServer, tmp_path: Path
) -> None:
    now = [1_000.0]
    with Coordinator(
        tmp_path / "probe",
        Coordination(),
        Holder.current("run-p"),
        clock=lambda: now[0],
    ) as coordinator:
        coordinator.record_failure("mdz", threshold=1, cooldown_s=10.0, cause="blocked")
        now[0] += 11.0
        server.script("/a", Reply(302, headers={"Location": "/b"}))
        server.script("/b", Reply(200, b"ok"))
        with Network(settings_with(), coordinator, clock=lambda: now[0]) as client:
            assert client.request("mdz", "GET", server.url("/a")).content == b"ok"
            with Network(settings_with(), coordinator) as late:
                assert late.request("mdz", "GET", server.url("/b")).content == b"ok"
        assert coordinator.breaker_check("mdz").allows


def test_a_failed_probe_finishes_its_retries_while_others_stay_refused(
    server: ScriptedServer, tmp_path: Path
) -> None:
    """Live round R1: one 503 on the half-open probe reopened the breaker
    and the probe's own retry never ran (Google Books)."""
    now = [1_000.0]

    def clock() -> float:
        return now[0]

    flaky = server.script("/flaky", Reply(503), Reply(200, b"ok"))
    other_url = server.script("/other", Reply(200, b"ok"))
    settings = settings_with(circuit_breaker_threshold=2, max_attempts=3)
    refused: list[BreakerOpen] = []
    with (
        Coordinator(
            tmp_path, Coordination(), Holder.current("run-p"), clock=clock
        ) as probing_coordinator,
        Coordinator(
            tmp_path, Coordination(), Holder.current("run-o"), clock=clock
        ) as other_coordinator,
        Network(settings, other_coordinator, clock=clock) as other,
    ):
        for _ in range(2):
            probing_coordinator.record_failure(
                "mdz", threshold=2, cooldown_s=10.0, cause="transient"
            )
        now[0] += 11.0

        def other_call_during_backoff(seconds: float) -> None:
            if seconds > 0 and not refused:
                with pytest.raises(BreakerOpen) as caught:
                    other.request("mdz", "GET", other_url)
                refused.append(caught.value)

        with Network(
            settings, probing_coordinator, clock=clock, sleep=other_call_during_backoff
        ) as probing:
            assert probing.request("mdz", "GET", flaky).content == b"ok"
        assert probing_coordinator.breaker_check("mdz").allows
    assert len(refused) == 1 and refused[0].cause == "transient"
    assert server.hits("/flaky") == 2
    assert server.hits("/other") == 0


class _Polled(Exception):
    """Raised by a seeker's sleep: the slot was refused and polled for."""


def _refuse_wait(seconds: float) -> None:
    raise _Polled


def test_judge_slots_follow_the_judge_network_block(
    pair: tuple[Coordinator, Coordinator],
) -> None:
    settings = Settings(judge=Judge(network=JudgeNetwork(slots=1)))
    assert settings.coordination.default_slots > 1
    with (
        Network(settings, pair[0]) as holder,
        Network(settings, pair[1], sleep=_refuse_wait) as seeker,
    ):
        with holder.hold_slot(JUDGE) as token:
            assert token.provider == JUDGE
            with pytest.raises(_Polled), seeker.hold_slot(JUDGE):
                pass
        with seeker.hold_slot(JUDGE) as token:
            assert token.provider == JUDGE


def test_a_breaker_opened_by_one_client_blocks_the_other(
    server: ScriptedServer, pair: tuple[Coordinator, Coordinator]
) -> None:
    url = server.script("/blocked", Reply(403))
    first, second = _clients(settings_with(circuit_breaker_threshold=2), pair)
    try:
        for _ in range(2):
            with pytest.raises(RequestFailed):
                first.request("mdz", "GET", url)
        with pytest.raises(BreakerOpen) as excinfo:
            second.request("mdz", "GET", url)
    finally:
        first.close()
        second.close()
    assert excinfo.value.cause == "blocked"
    assert excinfo.value.until is not None
    assert server.hits("/blocked") == 2
