"""Extended tests for api.core.network module — circuit breaker and request handling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from api.core.network import (
    _CIRCUIT_BREAKERS,
    CircuitBreaker,
    CircuitState,
    build_session,
    get_circuit_breaker,
    get_provider_for_url,
    make_json_request,
    make_request,
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
    """A non-retryable status (400/401/403/404/410/422) records success.

    Pre-fix, aborting on a non-retryable status returned None without ever
    touching the breaker, so a half-open probe spent on a permanently-dead
    URL left the breaker stuck HALF_OPEN and throttled a working provider to
    one request per cooldown indefinitely.
    """

    def setup_method(self) -> None:
        _CIRCUIT_BREAKERS.clear()

    def teardown_method(self) -> None:
        _CIRCUIT_BREAKERS.clear()

    def test_404_records_success_on_breaker(self) -> None:
        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        resp = MagicMock()
        resp.status_code = 404
        resp.headers = {}

        session = MagicMock()
        session.get.return_value = resp

        with (
            patch("api.core.network.get_session", return_value=session),
            patch("api.core.network.get_circuit_breaker", return_value=mock_cb),
            patch("api.core.network.get_network_config", return_value={}),
        ):
            result = make_request("https://example.org/missing")

        assert result is None
        mock_cb.record_success.assert_called_once()


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
