"""Extended tests for api.core.network module — circuit breaker and request handling."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import requests

from api.core.network import (
    _CIRCUIT_BREAKERS,
    CONNECT_FAILURE_MAX_ATTEMPTS,
    CircuitBreaker,
    CircuitState,
    build_session,
    connect_aware_attempt_cap,
    get_circuit_breaker,
    get_provider_for_url,
    is_connect_failure,
    make_json_request,
    make_request,
    request_carries_credential,
)

# ============================================================================
# CircuitBreaker
# ============================================================================


class TestCircuitBreaker:
    """Tests for the CircuitBreaker class."""

    def test_initial_state_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_record_success_resets(self) -> None:
        cb = CircuitBreaker()
        cb.failure_count = 2
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_record_failure_increments(self) -> None:
        cb = CircuitBreaker(failure_threshold=5)
        cb.record_failure()
        assert cb.failure_count == 1
        assert cb.state == CircuitState.CLOSED

    def test_opens_circuit_at_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure("test")
        cb.record_failure("test")
        cb.record_failure("test")
        assert cb.state == CircuitState.OPEN

    def test_allow_request_when_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.allow_request() is True

    def test_deny_request_when_open(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        cb.record_failure("test")
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_half_open_after_cooldown(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
        cb.record_failure("test")
        assert cb.state == CircuitState.OPEN
        # Cooldown is 0s, so should immediately transition
        assert cb.allow_request() is True
        assert cb.state == CircuitState.HALF_OPEN  # type: ignore[comparison-overlap]

    def test_half_open_success_closes(self) -> None:
        cb = CircuitBreaker()
        cb.state = CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        cb.state = CircuitState.HALF_OPEN
        cb.record_failure("test")
        assert cb.state == CircuitState.OPEN

    def test_time_until_retry_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.time_until_retry() == 0.0

    def test_time_until_retry_open(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=300)
        cb.record_failure("test")
        remaining = cb.time_until_retry()
        assert remaining > 0
        assert remaining <= 300


# ============================================================================
# get_circuit_breaker
# ============================================================================


class TestGetCircuitBreaker:
    """Tests for circuit breaker factory."""

    def setup_method(self) -> None:
        _CIRCUIT_BREAKERS.clear()

    def teardown_method(self) -> None:
        _CIRCUIT_BREAKERS.clear()

    def test_returns_none_for_none_provider(self) -> None:
        assert get_circuit_breaker(None) is None

    @patch(
        "api.core.network.get_network_config",
        return_value={
            "circuit_breaker_enabled": True,
            "circuit_breaker_threshold": 5,
            "circuit_breaker_cooldown_s": 120,
        },
    )
    def test_creates_circuit_breaker(self, mock_cfg: MagicMock) -> None:
        cb = get_circuit_breaker("ia")
        assert cb is not None
        assert cb.failure_threshold == 5
        assert cb.cooldown_seconds == 120

    @patch(
        "api.core.network.get_network_config",
        return_value={
            "circuit_breaker_enabled": True,
        },
    )
    def test_returns_same_instance(self, mock_cfg: MagicMock) -> None:
        cb1 = get_circuit_breaker("ia")
        cb2 = get_circuit_breaker("ia")
        assert cb1 is cb2

    @patch(
        "api.core.network.get_network_config",
        return_value={
            "circuit_breaker_enabled": False,
        },
    )
    def test_returns_none_when_disabled(self, mock_cfg: MagicMock) -> None:
        assert get_circuit_breaker("ia") is None


# ============================================================================
# get_provider_for_url
# ============================================================================


class TestGetProviderForUrlExtended:
    """Extended tests for URL-to-provider mapping."""

    def test_gallica(self) -> None:
        assert (
            get_provider_for_url("https://gallica.bnf.fr/ark:/12148/bpt6k123")
            == "gallica"
        )

    def test_mdz(self) -> None:
        assert (
            get_provider_for_url("https://api.digitale-sammlungen.de/item/bsb123")
            == "mdz"
        )

    def test_loc(self) -> None:
        assert get_provider_for_url("https://www.loc.gov/item/123") == "loc"

    def test_unknown_url(self) -> None:
        assert get_provider_for_url("https://completely-unknown.org/page") is None

    def test_with_port(self) -> None:
        assert get_provider_for_url("https://gallica.bnf.fr:443/ark:/123") == "gallica"

    def test_invalid_url(self) -> None:
        assert get_provider_for_url("not-a-url") is None

    def test_internet_archive(self) -> None:
        assert (
            get_provider_for_url("https://archive.org/details/test")
            == "internet_archive"
        )

    def test_annas_archive(self) -> None:
        assert (
            get_provider_for_url("https://annas-archive.li/download") == "annas_archive"
        )


# ============================================================================
# make_request — non-retryable status + circuit breaker recovery
# ============================================================================


class TestMakeRequestNonRetryableRecordsSuccess:
    """Non-retryable statuses split by what they say about the provider.

    A resource error (400/404/410/422) records success: aborting without
    touching the breaker left a half-open probe spent on a permanently-dead
    URL stuck HALF_OPEN, throttling a working provider to one request per
    cooldown indefinitely. A blanket rejection of the client (401/403)
    records failure instead, or a provider that rejects every request never
    trips its own breaker and is re-dialled at full cost for the whole run.
    """

    def setup_method(self) -> None:
        _CIRCUIT_BREAKERS.clear()

    def teardown_method(self) -> None:
        _CIRCUIT_BREAKERS.clear()

    @staticmethod
    def _request_with_status(status: int) -> MagicMock:
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        resp = MagicMock()
        resp.status_code = status
        resp.headers = {}

        session = MagicMock()
        session.get.return_value = resp

        with (
            patch("api.core.network.get_session", return_value=session),
            patch("api.core.network.get_circuit_breaker", return_value=mock_cb),
            patch("api.core.network.get_network_config", return_value={}),
        ):
            assert make_request("https://example.org/probe") is None
        return mock_cb

    def test_404_records_success_on_breaker(self) -> None:
        mock_cb = self._request_with_status(404)
        mock_cb.record_success.assert_called_once()
        mock_cb.record_failure.assert_not_called()

    def test_resource_errors_record_success(self) -> None:
        for status in (400, 410, 422):
            mock_cb = self._request_with_status(status)
            assert mock_cb.record_success.call_count == 1, status
            mock_cb.record_failure.assert_not_called()

    def test_client_blocked_statuses_record_failure(self) -> None:
        for status in (401, 403):
            mock_cb = self._request_with_status(status)
            assert mock_cb.record_failure.call_count == 1, status
            mock_cb.record_success.assert_not_called()

    def test_repeated_403_trips_the_breaker(self) -> None:
        """A provider that 403s everything must eventually be skipped."""
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=300.0)

        resp = MagicMock()
        resp.status_code = 403
        resp.headers = {}
        session = MagicMock()
        session.get.return_value = resp

        with (
            patch("api.core.network.get_session", return_value=session),
            patch("api.core.network.get_circuit_breaker", return_value=cb),
            patch("api.core.network.get_network_config", return_value={}),
        ):
            for _ in range(3):
                make_request("https://example.org/blocked")
            assert cb.state == CircuitState.OPEN
            calls_before = session.get.call_count
            make_request("https://example.org/blocked")

        # Fourth call is short-circuited: no further HTTP request is made.
        assert session.get.call_count == calls_before

    def test_dns_failure_records_failure_on_breaker(self) -> None:
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='gone.example'): Max retries exceeded "
            "(Caused by NameResolutionError('Failed to resolve gone.example'))"
        )

        with (
            patch("api.core.network.get_session", return_value=session),
            patch("api.core.network.get_circuit_breaker", return_value=mock_cb),
            patch("api.core.network.get_network_config", return_value={}),
        ):
            assert make_request("https://gone.example/api") is None

        assert session.get.call_count == 1  # not retried
        mock_cb.record_failure.assert_called_once()


# ============================================================================
# Connect-level failures
# ============================================================================


class TestConnectFailureFastFail:
    """A host that never accepts a connection must not burn the full budget.

    sru.gbv.de silently drops SYNs: every attempt costs the whole timeout, so
    one search spent minutes exhausting max_attempts before failing anyway.
    Errors raised after a connection was established keep the full budget,
    since those are the genuinely transient ones.
    """

    def test_connect_timeout_is_connect_level(self) -> None:
        assert is_connect_failure(requests.exceptions.ConnectTimeout("timed out"))

    def test_refused_and_unreachable_are_connect_level(self) -> None:
        for msg in (
            "Failed to establish a new connection: [Errno 111] Connection refused",
            "NewConnectionError: [WinError 10061] actively refused it",
            "[Errno 101] Network is unreachable",
            "ConnectTimeoutError(Connection to sru.gbv.de timed out.)",
        ):
            assert is_connect_failure(requests.exceptions.ConnectionError(msg)), msg

    def test_midstream_errors_keep_the_full_budget(self) -> None:
        for exc in (
            requests.exceptions.ConnectionError(
                "Connection aborted, RemoteDisconnected"
            ),
            requests.exceptions.ConnectionError("Connection reset by peer"),
            requests.exceptions.ReadTimeout("Read timed out. (read timeout=30)"),
            requests.exceptions.ChunkedEncodingError("incomplete chunked read"),
        ):
            assert not is_connect_failure(exc), exc
            assert connect_aware_attempt_cap(exc, 25) == 25

    def test_cap_shortens_only_connect_failures(self) -> None:
        dead = requests.exceptions.ConnectTimeout("timed out")
        assert connect_aware_attempt_cap(dead, 8) == CONNECT_FAILURE_MAX_ATTEMPTS
        # Never lengthens a budget that is already shorter than the cap.
        assert connect_aware_attempt_cap(dead, 1) == 1

    def test_make_request_stops_early_on_connect_timeout(self) -> None:
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectTimeout(
            "Connection to sru.gbv.de timed out. (connect timeout=40)"
        )

        with (
            patch("api.core.network.get_session", return_value=session),
            patch("api.core.network.get_circuit_breaker", return_value=mock_cb),
            patch(
                "api.core.network.get_network_config",
                return_value={"max_attempts": 8, "base_backoff_s": 0.0},
            ),
            patch("api.core.network.time.sleep"),
        ):
            assert make_request("https://sru.gbv.de/gvk") is None

        assert session.get.call_count == CONNECT_FAILURE_MAX_ATTEMPTS
        mock_cb.record_failure.assert_called_once()

    def test_make_request_keeps_full_budget_for_read_timeout(self) -> None:
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ReadTimeout("Read timed out.")

        with (
            patch("api.core.network.get_session", return_value=session),
            patch("api.core.network.get_circuit_breaker", return_value=None),
            patch(
                "api.core.network.get_network_config",
                return_value={"max_attempts": 4, "base_backoff_s": 0.0},
            ),
            patch("api.core.network.time.sleep"),
        ):
            assert make_request("https://example.org/slow") is None

        assert session.get.call_count == 4


# ============================================================================
# build_session
# ============================================================================


class TestBuildSession:
    """Tests for HTTP session construction."""

    def test_returns_session(self) -> None:
        session = build_session()
        assert session is not None
        assert "User-Agent" in session.headers


# ============================================================================
# make_json_request
# ============================================================================


class TestMakeJsonRequest:
    """Tests for JSON-specific request wrapper."""

    @patch("api.core.network.make_request")
    def test_returns_dict(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"key": "value"}
        result = make_json_request("https://example.org/api")
        assert result == {"key": "value"}

    @patch("api.core.network.make_request")
    def test_returns_none_for_non_dict(self, mock_req: MagicMock) -> None:
        mock_req.return_value = "text response"
        result = make_json_request("https://example.org/api")
        assert result is None

    @patch("api.core.network.make_request")
    def test_returns_none_on_error(self, mock_req: MagicMock) -> None:
        mock_req.return_value = None
        result = make_json_request("https://example.org/api")
        assert result is None


class TestHalfOpenRetryReport:
    """A denied HALF_OPEN caller must not be told to retry in zero seconds."""

    def test_half_open_reports_the_remaining_probe_window(self) -> None:
        from api.core.network import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0)
        cb.record_failure("p")
        cb.opened_at -= 61.0  # cooldown elapsed: next call takes the probe
        assert cb.allow_request() is True  # probe admitted
        assert cb.allow_request() is False  # concurrent worker denied
        assert cb.time_until_retry() > 0.0


class TestSslFailuresFeedTheBreaker:
    """A provider whose certificate never verifies has to trip its breaker.

    Both terminal SSL returns used to leave the breaker untouched, so a host
    failing 100% of its requests on the handshake stayed nominally healthy and
    every later work re-attempted it.
    """

    @staticmethod
    def _run(net: dict[str, object]) -> MagicMock:
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        session = MagicMock()
        session.get.side_effect = requests.exceptions.SSLError(
            "certificate verify failed: unable to get local issuer certificate"
        )
        config: dict[str, object] = {"max_attempts": 3, "base_backoff_s": 0.0}
        config.update(net)

        with (
            patch("api.core.network.get_session", return_value=session),
            patch("api.core.network.get_circuit_breaker", return_value=mock_cb),
            patch("api.core.network.get_network_config", return_value=config),
            patch("api.core.network.time.sleep"),
        ):
            assert make_request("https://bad-cert.example/api") is None

        return mock_cb

    def test_policy_disallowed_records_failure(self) -> None:
        self._run({}).record_failure.assert_called_once()

    def test_exhausted_insecure_retry_records_failure(self) -> None:
        cb = self._run({"ssl_error_policy": "retry_insecure_once"})
        cb.record_failure.assert_called_once()


class TestCredentialDetection:
    """Name-based detection guarding the insecure-retry downgrade."""

    def test_query_string_key_is_a_credential(self) -> None:
        assert request_carries_credential("https://x.example/api?md5=abc&key=s3cr3t")

    def test_separate_params_are_inspected(self) -> None:
        assert request_carries_credential(
            "https://x.example/api", {"md5": "abc", "api_key": "s3cr3t"}
        )

    def test_vendor_variants_are_caught(self) -> None:
        for name in ("access_token", "clientSecret", "sig", "url-signature", "pwd"):
            assert request_carries_credential("https://x.example/a", {name: "v"}), name

    def test_authorization_and_api_key_headers_are_credentials(self) -> None:
        assert request_carries_credential(
            "https://x.example/a", None, {"Authorization": "Bearer t"}
        )
        assert request_carries_credential(
            "https://x.example/a", None, {"X-Api-Key": "t"}
        )

    def test_plain_request_carries_no_credential(self) -> None:
        assert not request_carries_credential(
            "https://x.example/api?q=cookbook&page=2",
            {"format": "json"},
            {"Accept": "application/json", "User-Agent": "chrono"},
        )

    def test_empty_values_do_not_count(self) -> None:
        assert not request_carries_credential(
            "https://x.example/api", {"key": ""}, {"Authorization": ""}
        )


class TestInsecureRetryIsSuppressedForCredentialedRequests:
    """``retry_insecure_once`` must never replay a secret unverified.

    The policy trades verification for reach on providers with periodically
    broken certificate chains. That trade is only acceptable for public
    payloads: an API key in the query string would otherwise be resent over a
    connection whose peer is unauthenticated.
    """

    @staticmethod
    def _run(**kwargs: Any) -> MagicMock:
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        session = MagicMock()
        session.get.side_effect = requests.exceptions.SSLError(
            "certificate verify failed: unable to get local issuer certificate"
        )

        with (
            patch("api.core.network.get_session", return_value=session),
            patch("api.core.network.get_circuit_breaker", return_value=mock_cb),
            patch(
                "api.core.network.get_network_config",
                return_value={
                    "max_attempts": 3,
                    "base_backoff_s": 0.0,
                    "ssl_error_policy": "retry_insecure_once",
                },
            ),
            patch("api.core.network.time.sleep"),
        ):
            assert make_request("https://bad-cert.example/api", **kwargs) is None

        return session

    def test_credentialed_request_is_not_retried_insecurely(self) -> None:
        session = self._run(params={"md5": "abc", "key": "s3cr3t"})
        assert session.get.call_count == 1
        assert session.get.call_args.kwargs["verify"] is True

    def test_credentialed_header_is_not_retried_insecurely(self) -> None:
        session = self._run(headers={"Authorization": "Bearer t"})
        assert session.get.call_count == 1

    def test_uncredentialed_request_still_retries_once(self) -> None:
        session = self._run(params={"q": "cookbook"})
        assert session.get.call_count == 2
        assert session.get.call_args.kwargs["verify"] is False


class TestInvalidUrlIsNotRetried:
    """A malformed URL is a metadata defect, not a provider outage.

    requests raises these before any socket is opened, so retrying burns the
    whole backoff budget for nothing and the breaker entry it used to record
    marked a perfectly healthy provider as failing.
    """

    @staticmethod
    def _run(exc: Exception) -> tuple[MagicMock, MagicMock]:
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        session = MagicMock()
        session.get.side_effect = exc

        with (
            patch("api.core.network.get_session", return_value=session),
            patch("api.core.network.get_circuit_breaker", return_value=mock_cb),
            patch(
                "api.core.network.get_network_config",
                return_value={"max_attempts": 5, "base_backoff_s": 0.0},
            ),
            patch("api.core.network.time.sleep"),
        ):
            assert make_request("not-a-url") is None

        return session, mock_cb

    def test_missing_schema_fails_immediately(self) -> None:
        session, cb = self._run(requests.exceptions.MissingSchema("no schema"))
        assert session.get.call_count == 1
        cb.record_failure.assert_not_called()

    def test_invalid_schema_fails_immediately(self) -> None:
        session, cb = self._run(
            requests.exceptions.InvalidSchema("no adapter for x://")
        )
        assert session.get.call_count == 1
        cb.record_failure.assert_not_called()

    def test_invalid_url_fails_immediately(self) -> None:
        session, cb = self._run(requests.exceptions.InvalidURL("invalid label"))
        assert session.get.call_count == 1
        cb.record_failure.assert_not_called()

    def test_url_required_fails_immediately(self) -> None:
        session, cb = self._run(requests.exceptions.URLRequired("no url"))
        assert session.get.call_count == 1
        cb.record_failure.assert_not_called()

    def test_other_request_errors_are_still_retried(self) -> None:
        session, cb = self._run(requests.exceptions.ConnectionError("reset by peer"))
        assert session.get.call_count == 5
        cb.record_failure.assert_called_once()


class TestDdbManifestHostsArePaced:
    """DDB manifests live at the holding library, not at DDB's own hosts."""

    def test_aggregated_manifest_hosts_map_back_to_ddb(self) -> None:
        from api.core.network import get_provider_for_url

        urls = (
            "https://digi.ub.uni-heidelberg.de/diglit/iiif/xyz/manifest.json",
            "https://manifests.sub.uni-goettingen.de/iiif/presentation/x/manifest",
        )
        for url in urls:
            assert get_provider_for_url(url) == "ddb", url
