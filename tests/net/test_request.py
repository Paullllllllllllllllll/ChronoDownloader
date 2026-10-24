"""Tests for Network.request against a local server and scripted sessions.

Re-expresses v1.30.3 ``tests/unit/test_network_extended.py``
(``TestMakeRequestNonRetryableRecordsSuccess``, ``TestConnectFailureFastFail``,
``TestSslFailuresFeedTheBreaker``,
``TestInsecureRetryIsSuppressedForCredentialedRequests``) against the new
API: failures raise ``RequestFailed`` with a cause instead of returning None.
"""

from __future__ import annotations

import logging
import socket
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from types import MappingProxyType

import pytest
import requests

from chronodownloader.coord import BreakerState, BreakerStatus
from chronodownloader.net import (
    JUDGE,
    BreakerOpen,
    Cause,
    QuotaSpent,
    RequestFailed,
)
from chronodownloader.net.classify import CONNECT_FAILURE_MAX_ATTEMPTS
from chronodownloader.net.session import USER_AGENT
from chronodownloader.settings import AnnasQuota, Judge, JudgeNetwork, Settings

from .conftest import NetworkFactory
from .support import (
    NOW,
    SLOT,
    FakeCoordinator,
    RecordingSession,
    Reply,
    ScriptedServer,
    ScriptedSession,
    response,
    settings_with,
)


class TestSuccess:
    def test_returns_the_response_and_records_success(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script(
            "/api", Reply(200, b'{"a": 1}', {"Content-Type": "application/json"})
        )
        result = make_network(settings_with()).request("mdz", "GET", url)
        assert result.json() == {"a": 1}
        assert coordinator.successes == ["mdz"]
        assert coordinator.failures == []

    def test_headers_merge_session_then_provider_then_call(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        url = server.script("/h", Reply(200, b"ok"))
        settings = settings_with(
            headers=MappingProxyType({"Accept": "application/xml", "X-Provider": "p"})
        )
        make_network(settings).request(
            "mdz", "GET", url, headers={"X-Provider": "call"}
        )
        sent = server.arrivals[0].headers
        assert sent["User-Agent"] == USER_AGENT
        assert sent["Accept"] == "application/xml"
        assert sent["X-Provider"] == "call"

    def test_post_with_json_to_the_judge(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        url = server.script("/decide", Reply(200, b"{}"))
        settings = Settings(judge=Judge(network=JudgeNetwork(timeout_s=5.0)))
        make_network(settings).request(JUDGE, "post", url, json={"q": "same_work"})
        assert server.arrivals[0].method == "POST"

    def test_an_unknown_provider_is_refused(self, make_network: NetworkFactory) -> None:
        with pytest.raises(ValueError, match="unknown provider 'gallica'"):
            make_network(settings_with()).request("gallica", "GET", "http://x")


class TestCauses:
    @pytest.mark.parametrize(
        ("status", "cause"),
        [
            (404, Cause.NOT_FOUND),
            (410, Cause.NOT_FOUND),
            (400, Cause.REJECTED),
            (422, Cause.REJECTED),
        ],
    )
    def test_resource_errors_record_success(
        self,
        status: int,
        cause: Cause,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/r", Reply(status))
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with()).request("mdz", "GET", url)
        assert (excinfo.value.cause, excinfo.value.status) == (cause, status)
        assert server.hits("/r") == 1
        assert coordinator.successes == ["mdz"]
        assert coordinator.failures == []

    @pytest.mark.parametrize("status", [401, 403, 451])
    def test_blocked_statuses_record_failure(
        self,
        status: int,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/b", Reply(status))
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with()).request("mdz", "GET", url)
        assert (excinfo.value.cause, excinfo.value.status) == (Cause.BLOCKED, status)
        assert server.hits("/b") == 1
        assert coordinator.failures == [("mdz", "blocked")]
        assert coordinator.successes == []

    def test_a_final_status_ends_the_call_as_an_answer(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/end", Reply(500), Reply(200, b"ok"))
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with(max_attempts=5)).request(
                "mdz", "GET", url, final_statuses=frozenset({500})
            )
        assert excinfo.value.status == 500
        assert excinfo.value.attempts == 1
        assert server.hits("/end") == 1
        assert coordinator.failures == []
        assert coordinator.successes == ["mdz"]

    def test_an_echoed_key_is_masked_before_the_answer_is_cut(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        secret = "K" * 40
        body = ("x" * 180 + f" bad key {secret} " + "y" * 50).encode()
        url = server.script("/refuse", Reply(403, body))
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with()).request(
                "mdz",
                "GET",
                url,
                params={"key": secret},
                final_statuses=frozenset({403}),
            )
        assert "KKKK" not in str(excinfo.value)

    def test_a_final_status_also_ends_a_download(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
        tmp_path: Path,
    ) -> None:
        url = server.script("/pdf", Reply(500), Reply(200, b"%PDF-1.4"))
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with(max_attempts=5)).download(
                "mdz",
                url,
                tmp_path / "x.pdf.part",
                slot=SLOT,
                final_statuses=frozenset({500}),
            )
        assert excinfo.value.status == 500
        assert server.hits("/pdf") == 1
        assert coordinator.failures == []
        assert not (tmp_path / "x.pdf.part").exists()

    def test_a_captcha_page_is_blocked(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        page = b"<html><head><title>Captcha check</title></head><body></body></html>"
        url = server.script("/c", Reply(200, page, {"Content-Type": "text/html"}))
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with()).request("mdz", "GET", url)
        assert excinfo.value.cause is Cause.BLOCKED
        assert coordinator.failures == [("mdz", "blocked")]

    def test_a_challenged_429_is_blocked_not_retried(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        url = server.script("/cf", Reply(429, b"", {"cf-mitigated": "challenge"}))
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with()).request("mdz", "GET", url)
        assert excinfo.value.cause is Cause.BLOCKED
        assert server.hits("/cf") == 1

    def test_a_5xx_storm_is_transient_after_every_attempt(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        sleeps: list[float],
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/s", Reply(503))
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with(max_attempts=3)).request("mdz", "GET", url)
        assert (excinfo.value.cause, excinfo.value.status) == (Cause.TRANSIENT, 503)
        assert excinfo.value.attempts == 3
        assert server.hits("/s") == 3
        assert sleeps == [0.5, 1.0]
        assert coordinator.failures == [("mdz", "transient")] * 3

    def test_repeated_403_opens_the_breaker_and_stops_sending(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/blocked", Reply(403))
        client = make_network(settings_with(circuit_breaker_threshold=3))
        for _ in range(3):
            with pytest.raises(RequestFailed):
                client.request("mdz", "GET", url)
        with pytest.raises(BreakerOpen) as excinfo:
            client.request("mdz", "GET", url)
        assert excinfo.value.cause == "blocked"
        assert excinfo.value.until == NOW + 300.0
        assert server.hits("/blocked") == 3


class TestBreakerAndPacing:
    def test_an_open_breaker_blocks_sending(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/x", Reply(200))
        coordinator.breakers["mdz"] = BreakerState(
            BreakerStatus.OPEN, 3, NOW + 60.0, "transient"
        )
        with pytest.raises(BreakerOpen) as excinfo:
            make_network(settings_with()).request("mdz", "GET", url)
        assert (excinfo.value.until, excinfo.value.cause) == (NOW + 60.0, "transient")
        assert server.arrivals == []
        assert coordinator.reservations == []

    def test_a_half_open_breaker_without_the_probe_blocks(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/x", Reply(200))
        coordinator.breakers["mdz"] = BreakerState(BreakerStatus.HALF_OPEN, 3, NOW + 5)
        with pytest.raises(BreakerOpen):
            make_network(settings_with()).request("mdz", "GET", url)
        assert server.arrivals == []

    def test_the_probe_holder_sends(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/x", Reply(200))
        coordinator.breakers["mdz"] = BreakerState(
            BreakerStatus.HALF_OPEN, 3, NOW + 5, probe=True
        )
        make_network(settings_with()).request("mdz", "GET", url)
        assert server.hits("/x") == 1
        assert "mdz" not in coordinator.breakers

    def test_a_disabled_breaker_is_never_consulted(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/x", Reply(403))
        coordinator.breakers["mdz"] = BreakerState(BreakerStatus.OPEN, 3, NOW + 60)
        with pytest.raises(RequestFailed):
            make_network(settings_with(circuit_breaker_enabled=False)).request(
                "mdz", "GET", url
            )
        assert coordinator.failures == []

    def test_every_attempt_reserves_its_own_turn(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/flaky", Reply(503), Reply(502), Reply(200, b"ok"))
        settings = settings_with(delay_ms=800, jitter_ms=200, max_attempts=5)
        make_network(settings).request("mdz", "GET", url, slot=SLOT)
        assert server.hits("/flaky") == 3
        assert [
            (r.provider, r.delay_s, r.jitter_s, r.slot)
            for r in coordinator.reservations
        ] == [("mdz", 0.8, 0.2, SLOT)] * 3

    def test_each_turn_comes_before_its_request(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/order", Reply(503), Reply(200))
        seen: list[int] = []
        coordinator.on_wait = lambda provider: seen.append(server.hits("/order"))
        make_network(settings_with()).request("mdz", "GET", url)
        assert seen == [0, 1]


class TestBreakerPerAttempt:
    """Admission is checked before every attempt and every attempt is counted."""

    def test_a_breaker_opened_during_the_backoff_stops_the_next_attempt(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/flaky", Reply(503), Reply(200))

        def other_run_opens_it(_seconds: float) -> None:
            coordinator.breakers["mdz"] = BreakerState(
                BreakerStatus.OPEN, 9, NOW + 60.0, "blocked"
            )

        client = make_network(settings_with(), sleep=other_run_opens_it)
        with pytest.raises(BreakerOpen) as excinfo:
            client.request("mdz", "GET", url)
        assert excinfo.value.cause == "blocked"
        assert server.hits("/flaky") == 1

    def test_its_own_retries_reaching_the_threshold_finish_the_request(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        """Live round D5: the breaker a request trips itself does not cut its
        retries short (SLUB's three 500s of eight attempts); the next request
        is refused."""
        url = server.script("/down", Reply(503), Reply(503), Reply(503), Reply(200))
        client = make_network(
            settings_with(max_attempts=5, circuit_breaker_threshold=2)
        )
        assert client.request("mdz", "GET", url).status_code == 200
        assert server.hits("/down") == 4
        assert coordinator.failures == [("mdz", "transient")] * 3
        assert "mdz" not in coordinator.breakers

    def test_a_request_that_tripped_the_breaker_and_failed_leaves_it_open(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/down", Reply(503))
        client = make_network(
            settings_with(max_attempts=4, circuit_breaker_threshold=2)
        )
        with pytest.raises(RequestFailed):
            client.request("mdz", "GET", url)
        assert server.hits("/down") == 4
        with pytest.raises(BreakerOpen) as excinfo:
            client.request("mdz", "GET", url)
        assert excinfo.value.cause == "transient"
        assert server.hits("/down") == 4

    def test_a_breaker_another_request_tripped_stops_the_retry(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        """Two concurrent requests, one 503 each: the second failure trips the
        breaker; the request that counted the first is refused."""
        url = server.script("/down", Reply(503), Reply(200))
        settings = settings_with(max_attempts=5, circuit_breaker_threshold=2)

        def other_request_fails(_seconds: float) -> None:
            coordinator.record_failure(
                "mdz", threshold=2, cooldown_s=300.0, cause="transient"
            )

        client = make_network(settings, sleep=other_request_fails)
        with pytest.raises(BreakerOpen):
            client.request("mdz", "GET", url)
        assert server.hits("/down") == 1

    def test_a_refused_admission_refunds_the_unsent_unit(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/fast", Reply(503), Reply(200))

        def other_run_opens_it(_seconds: float) -> None:
            coordinator.breakers["annas_archive"] = BreakerState(
                BreakerStatus.OPEN, 9, NOW + 60.0, "blocked"
            )

        settings = settings_with("annas_archive", quota=AnnasQuota(daily_limit=5))
        client = make_network(settings, sleep=other_run_opens_it)
        with pytest.raises(BreakerOpen):
            client.request("annas_archive", "GET", url, quota=True)
        assert server.hits("/fast") == 1
        assert coordinator.quota_used["annas_archive"] == 1
        assert len(coordinator.released) == 1


class TestRedirects:
    """Redirects are followed hop by hop, each hop paced and breaker-checked."""

    def test_each_hop_takes_its_own_turn(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        server.script("/a", Reply(302, headers={"Location": "/b"}))
        server.script("/b", Reply(301, headers={"Location": server.url("/c")}))
        url = server.script("/c", Reply(200, b"done"))
        seen: list[int] = []
        coordinator.on_wait = lambda provider: seen.append(len(server.arrivals))
        result = make_network(settings_with(delay_ms=500)).request(
            "mdz", "GET", server.url("/a"), slot=SLOT
        )
        assert result.content == b"done"
        assert result.url == url
        assert [arrival.path for arrival in server.arrivals] == ["/a", "/b", "/c"]
        assert seen == [0, 1, 2]
        assert [(r.delay_s, r.slot) for r in coordinator.reservations] == [
            (0.5, SLOT)
        ] * 3

    def test_a_breaker_opened_mid_chain_stops_the_next_hop(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        server.script("/a", Reply(302, headers={"Location": "/b"}))
        server.script("/b", Reply(200))

        def open_after_first(provider: str) -> None:
            if server.arrivals:
                coordinator.breakers["mdz"] = BreakerState(BreakerStatus.OPEN, 9, NOW)

        coordinator.on_wait = open_after_first
        with pytest.raises(BreakerOpen):
            make_network(settings_with()).request("mdz", "GET", server.url("/a"))
        assert [arrival.path for arrival in server.arrivals] == ["/a"]

    def test_a_quota_unit_covers_the_whole_chain(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        server.script("/fast", Reply(302, headers={"Location": "/file"}))
        server.script("/file", Reply(200, b"x"))
        settings = settings_with("annas_archive", quota=AnnasQuota(daily_limit=5))
        make_network(settings).request(
            "annas_archive", "GET", server.url("/fast"), quota=True
        )
        assert coordinator.quota_used["annas_archive"] == 1

    def test_a_cross_origin_hop_drops_credentials(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        other = ScriptedServer()
        other.start()
        try:
            target = other.script("/file", Reply(200, b"x"))
            server.script("/a", Reply(302, headers={"Location": target}))
            make_network(settings_with()).request(
                "mdz",
                "GET",
                server.url("/a"),
                params={"key": "s3cr3t"},
                headers={"Authorization": "Bearer t", "Cookie": "sid=1", "X-Tag": "t"},
            )
            first, second = server.arrivals[0], other.arrivals[0]
        finally:
            other.stop()
        assert "key=s3cr3t" in first.path
        assert first.headers["Authorization"] == "Bearer t"
        assert second.path == "/file"
        assert "Authorization" not in second.headers
        assert "Cookie" not in second.headers
        assert second.headers["X-Tag"] == "t"

    def test_a_same_origin_hop_keeps_the_headers(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        server.script("/a", Reply(307, headers={"Location": "/b"}))
        server.script("/b", Reply(200))
        make_network(settings_with()).request(
            "mdz", "GET", server.url("/a"), headers={"Authorization": "Bearer t"}
        )
        assert server.arrivals[1].headers["Authorization"] == "Bearer t"

    def test_303_after_a_post_continues_as_get(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        server.script("/submit", Reply(303, headers={"Location": "/result"}))
        server.script("/result", Reply(200))
        make_network(settings_with()).request(
            "mdz", "POST", server.url("/submit"), json={"a": 1}
        )
        assert [a.method for a in server.arrivals] == ["POST", "GET"]

    def test_a_redirect_loop_is_cut_off(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        url = server.script("/loop", Reply(302, headers={"Location": "/loop"}))
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with()).request("mdz", "GET", url)
        assert excinfo.value.cause is Cause.REJECTED
        assert server.hits("/loop") == 11

    def test_redirects_can_be_left_to_the_caller(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        url = server.script("/a", Reply(302, headers={"Location": "/b"}))
        result = make_network(settings_with()).request(
            "mdz", "GET", url, allow_redirects=False
        )
        assert result.status_code == 302
        assert server.hits("/b") == 0


class TestRetryAfter:
    def test_seconds_replace_the_backoff(
        self,
        server: ScriptedServer,
        sleeps: list[float],
        make_network: NetworkFactory,
    ) -> None:
        url = server.script(
            "/slow", Reply(429, headers={"Retry-After": "7"}), Reply(200)
        )
        make_network(settings_with()).request("mdz", "GET", url)
        assert sleeps == [7.0]

    def test_an_http_date_is_honored(
        self,
        server: ScriptedServer,
        sleeps: list[float],
        make_network: NetworkFactory,
    ) -> None:
        date = format_datetime(datetime.fromtimestamp(NOW + 12, UTC), usegmt=True)
        url = server.script(
            "/slow", Reply(503, headers={"Retry-After": date}), Reply(200)
        )
        make_network(settings_with()).request("mdz", "GET", url)
        assert sleeps == [12.0]

    def test_a_long_wait_is_capped_at_max_backoff(
        self,
        server: ScriptedServer,
        sleeps: list[float],
        make_network: NetworkFactory,
    ) -> None:
        url = server.script(
            "/slow", Reply(429, headers={"Retry-After": "3600"}), Reply(200)
        )
        make_network(settings_with(max_backoff_s=20.0)).request("mdz", "GET", url)
        assert sleeps == [20.0]

    def test_garbage_falls_back_to_the_backoff(
        self,
        server: ScriptedServer,
        sleeps: list[float],
        make_network: NetworkFactory,
    ) -> None:
        url = server.script(
            "/slow", Reply(429, headers={"Retry-After": "later"}), Reply(200)
        )
        make_network(settings_with()).request("mdz", "GET", url)
        assert sleeps == [0.5]


class TestTransportFailures:
    def test_a_refused_connection_gets_the_short_budget(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with(max_attempts=8)).request(
                "mdz", "GET", f"http://127.0.0.1:{closed_port}/x"
            )
        assert excinfo.value.cause is Cause.TRANSIENT
        assert excinfo.value.status is None
        assert excinfo.value.attempts == CONNECT_FAILURE_MAX_ATTEMPTS
        assert coordinator.failures == [("mdz", "transient")] * 2

    def test_a_connect_timeout_gets_the_short_budget(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        session = ScriptedSession(
            requests.exceptions.ConnectTimeout("Connection to sru.gbv.de timed out.")
        )
        with pytest.raises(RequestFailed) as excinfo:
            make_network(
                settings_with(max_attempts=8), session_factory=lambda: session
            ).request("mdz", "GET", "https://sru.gbv.de/gvk")
        assert excinfo.value.cause is Cause.TRANSIENT
        assert len(session.calls) == CONNECT_FAILURE_MAX_ATTEMPTS
        assert coordinator.failures == [("mdz", "transient")] * 2

    def test_a_read_timeout_keeps_the_full_budget(
        self, make_network: NetworkFactory
    ) -> None:
        session = ScriptedSession(requests.exceptions.ReadTimeout("Read timed out."))
        with pytest.raises(RequestFailed):
            make_network(
                settings_with(max_attempts=4), session_factory=lambda: session
            ).request("mdz", "GET", "https://example.org/slow")
        assert len(session.calls) == 4

    def test_a_read_timeout_then_success(self, make_network: NetworkFactory) -> None:
        session = ScriptedSession(
            requests.exceptions.ReadTimeout("Read timed out."), response(200, [b"ok"])
        )
        result = make_network(settings_with(), session_factory=lambda: session).request(
            "mdz", "GET", "https://example.org/x"
        )
        assert result.content == b"ok"
        assert len(session.calls) == 2

    def test_dns_failure_is_not_retried_and_feeds_the_breaker(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        session = ScriptedSession(
            requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='gone.example'): Max retries exceeded "
                "(Caused by NameResolutionError('Failed to resolve gone.example'))"
            )
        )
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with(), session_factory=lambda: session).request(
                "mdz", "GET", "https://gone.example/api"
            )
        assert excinfo.value.cause is Cause.DNS
        assert len(session.calls) == 1
        assert coordinator.failures == [("mdz", "dns")]

    def test_dns_retry_uses_the_budget(self, make_network: NetworkFactory) -> None:
        session = ScriptedSession(
            requests.exceptions.ConnectionError("getaddrinfo failed")
        )
        with pytest.raises(RequestFailed):
            make_network(
                settings_with(dns_retry=True, max_attempts=3),
                session_factory=lambda: session,
            ).request("mdz", "GET", "https://gone.example/api")
        assert len(session.calls) == 3

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.MissingSchema("no schema"),
            requests.exceptions.InvalidURL("invalid label"),
        ],
    )
    def test_an_invalid_url_is_not_retried_or_charged(
        self,
        exc: requests.exceptions.RequestException,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        session = ScriptedSession(exc)
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with(), session_factory=lambda: session).request(
                "mdz", "GET", "not a url"
            )
        assert excinfo.value.cause is Cause.INVALID_URL
        assert len(session.calls) == 1
        assert coordinator.failures == []
        assert coordinator.successes == []

    def test_the_exception_masks_credentials(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        url = server.script("/k", Reply(403)) + "?wskey=SECRET"
        with pytest.raises(RequestFailed) as excinfo:
            make_network(settings_with()).request("mdz", "GET", url)
        assert "SECRET" not in str(excinfo.value)
        assert "SECRET" not in excinfo.value.url

    def test_a_connection_error_never_logs_the_key(
        self, make_network: NetworkFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = ScriptedSession(
            requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='api.europeana.eu', port=443): Max retries "
                "exceeded with url: /record/v2/search.json?wskey=SECRET&query=x "
                "(Caused by ProtocolError('Connection aborted.'))"
            )
        )
        client = make_network(settings_with(), session_factory=lambda: session)
        with (
            caplog.at_level(logging.DEBUG, logger="chronodownloader"),
            pytest.raises(RequestFailed) as excinfo,
        ):
            client.request(
                "mdz",
                "GET",
                "https://api.europeana.eu/record/v2/search.json?wskey=SECRET",
            )
        assert len(session.calls) == 3
        assert caplog.records
        assert "SECRET" not in caplog.text
        assert "SECRET" not in str(excinfo.value)
        assert "SECRET" not in excinfo.value.detail


class TestSslPolicy:
    """A handshake that fails is a provider outage; one unverified retry at most.

    The local server speaks plain HTTP, so an https URL fails the TLS
    handshake with a genuine ``requests.exceptions.SSLError``.
    """

    def _run(
        self,
        server: ScriptedServer,
        make_network: NetworkFactory,
        policy: str,
        **options: object,
    ) -> tuple[RecordingSession, RequestFailed]:
        server.script("/tls", Reply(200))
        session = RecordingSession()
        client = make_network(
            settings_with(ssl_error_policy=policy), session_factory=lambda: session
        )
        with pytest.raises(RequestFailed) as excinfo:
            client.request("mdz", "GET", server.url("/tls", scheme="https"), **options)  # type: ignore[arg-type]
        return session, excinfo.value

    def test_policy_fail_records_failure_once(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        session, error = self._run(server, make_network, "fail")
        assert error.cause is Cause.SSL
        assert [call["verify"] for call in session.calls] == [True]
        assert coordinator.failures == [("mdz", "ssl")]

    def test_retry_insecure_once(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        session, error = self._run(server, make_network, "retry_insecure_once")
        assert error.cause is Cause.SSL
        assert [call["verify"] for call in session.calls] == [True, False]
        assert coordinator.failures == [("mdz", "ssl")] * 2

    def test_a_session_cookie_suppresses_the_retry(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        session = RecordingSession()
        session.cookies.set("sid", "abc", domain="127.0.0.1", path="/")
        client = make_network(
            settings_with(ssl_error_policy="retry_insecure_once"),
            session_factory=lambda: session,
        )
        with pytest.raises(RequestFailed):
            client.request("mdz", "GET", server.url("/tls", scheme="https"))
        assert len(session.calls) == 1

    def test_url_user_information_suppresses_the_retry(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        session = RecordingSession()
        client = make_network(
            settings_with(ssl_error_policy="retry_insecure_once"),
            session_factory=lambda: session,
        )
        url = f"https://reader:pw@127.0.0.1:{server.port}/tls"
        with pytest.raises(RequestFailed) as excinfo:
            client.request("mdz", "GET", url)
        assert len(session.calls) == 1
        assert "pw@" not in str(excinfo.value)

    def test_sessions_ignore_the_environment(
        self, make_network: NetworkFactory
    ) -> None:
        session = requests.Session()
        client = make_network(settings_with(), session_factory=lambda: session)
        assert client.session_for("mdz").trust_env is False

    def test_a_credential_in_the_params_suppresses_the_retry(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        session, _error = self._run(
            server,
            make_network,
            "retry_insecure_once",
            params={"md5": "abc", "key": "s3cr3t"},
        )
        assert [call["verify"] for call in session.calls] == [True]

    def test_a_credential_header_suppresses_the_retry(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        session, _error = self._run(
            server,
            make_network,
            "retry_insecure_once",
            headers={"Authorization": "Bearer t"},
        )
        assert len(session.calls) == 1

    def test_a_public_request_still_retries(
        self, server: ScriptedServer, make_network: NetworkFactory
    ) -> None:
        session, _error = self._run(
            server, make_network, "retry_insecure_once", params={"q": "cookbook"}
        )
        assert len(session.calls) == 2


def _gallica_paths(url: str) -> str | None:
    """A host resolver standing in for the registry: ``/gallica/`` paths of
    the local server belong to Gallica."""
    return "bnf_gallica" if "/gallica/" in url else None


class TestHostKey:
    def test_a_redirect_to_an_open_breaker_host_stops_under_that_key(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/agg", Reply(302, b"", {"Location": "/gallica/p.jpg"}))
        server.script("/gallica/p.jpg", Reply(200, b"x"))
        coordinator.breakers["bnf_gallica"] = BreakerState(
            BreakerStatus.OPEN, 5, NOW + 60.0, "blocked"
        )
        client = make_network(settings_with("europeana"), host_key=_gallica_paths)
        with pytest.raises(BreakerOpen) as excinfo:
            client.request("europeana", "GET", url)
        assert excinfo.value.provider == "bnf_gallica"
        assert server.hits("/gallica/p.jpg") == 0
        assert [r.provider for r in coordinator.reservations] == [
            "europeana",
            "bnf_gallica",
        ]

    def test_the_answer_of_a_redirect_hop_counts_for_its_provider(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/agg", Reply(302, b"", {"Location": "/gallica/p.jpg"}))
        server.script("/gallica/p.jpg", Reply(403))
        client = make_network(settings_with("europeana"), host_key=_gallica_paths)
        with pytest.raises(RequestFailed):
            client.request("europeana", "GET", url)
        assert coordinator.failures == [("bnf_gallica", "blocked")]


class TestQuota:
    def _settings(self, limit: int) -> Settings:
        return settings_with("annas_archive", quota=AnnasQuota(daily_limit=limit))

    def test_each_sent_attempt_spends_a_unit(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/fast", Reply(503), Reply(200))
        make_network(self._settings(5)).request("annas_archive", "GET", url, quota=True)
        assert coordinator.quota_used["annas_archive"] == 2
        assert coordinator.released == []

    def test_a_spent_quota_raises_before_sending(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/fast", Reply(200))
        client = make_network(self._settings(1))
        client.request("annas_archive", "GET", url, quota=True)
        with pytest.raises(QuotaSpent) as excinfo:
            client.request("annas_archive", "GET", url, quota=True)
        assert excinfo.value.status.used == 1
        assert server.hits("/fast") == 1

    def test_a_request_never_sent_gives_its_unit_back(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        session = ScriptedSession(requests.exceptions.ConnectTimeout("timed out"))
        with pytest.raises(RequestFailed):
            make_network(self._settings(5), session_factory=lambda: session).request(
                "annas_archive", "GET", "https://annas.example/fast", quota=True
            )
        assert len(coordinator.released) == CONNECT_FAILURE_MAX_ATTEMPTS
        assert coordinator.quota_used["annas_archive"] == 0

    def test_an_unsent_dns_failure_is_not_charged(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        session = ScriptedSession(
            requests.exceptions.ConnectionError(
                "Failed to resolve 'annas.example' (getaddrinfo failed)"
            )
        )
        with pytest.raises(RequestFailed) as excinfo:
            make_network(self._settings(5), session_factory=lambda: session).request(
                "annas_archive", "GET", "https://annas.example/fast", quota=True
            )
        assert excinfo.value.cause is Cause.DNS
        assert excinfo.value.quota_charged is False
        assert coordinator.quota_used["annas_archive"] == 0

    def test_a_charged_503_then_a_spent_quota_stays_charged(
        self,
        server: ScriptedServer,
        coordinator: FakeCoordinator,
        make_network: NetworkFactory,
    ) -> None:
        url = server.script("/fast", Reply(503))
        with pytest.raises(QuotaSpent) as excinfo:
            make_network(self._settings(1)).request(
                "annas_archive", "GET", url, quota=True
            )
        assert excinfo.value.quota_charged is True
        assert server.hits("/fast") == 1

    def test_a_returned_download_reports_its_charge(
        self,
        server: ScriptedServer,
        make_network: NetworkFactory,
        tmp_path: Path,
    ) -> None:
        url = server.script("/f.pdf", Reply(200, b"%PDF-1.4 x"))
        client = make_network(self._settings(5))
        charged = client.download(
            "annas_archive", url, tmp_path / "a.pdf.part", slot=SLOT, quota=True
        )
        free = client.download("annas_archive", url, tmp_path / "b.pdf.part", slot=SLOT)
        assert (charged.quota_charged, free.quota_charged) == (True, False)

    def test_a_provider_without_quota_is_refused(
        self, make_network: NetworkFactory
    ) -> None:
        with pytest.raises(ValueError, match="no enabled quota"):
            make_network(settings_with()).request("mdz", "GET", "http://x", quota=True)
