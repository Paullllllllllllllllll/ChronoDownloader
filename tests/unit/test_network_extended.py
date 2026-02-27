"""Extended tests for api.core.network module — circuit breaker and request handling."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from api.core.network import (
    CircuitBreaker,
    CircuitState,
    _CIRCUIT_BREAKERS,
    build_session,
    get_circuit_breaker,
    get_provider_cooldown,
    get_provider_for_url,
    is_provider_available,
    make_json_request,
)


# ============================================================================
# CircuitBreaker
# ============================================================================

class TestCircuitBreaker:
    """Tests for the CircuitBreaker class."""

    def test_initial_state_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_record_success_resets(self):
        cb = CircuitBreaker()
        cb.failure_count = 2
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_record_failure_increments(self):
        cb = CircuitBreaker(failure_threshold=5)
        cb.record_failure()
        assert cb.failure_count == 1
        assert cb.state == CircuitState.CLOSED

    def test_opens_circuit_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure("test")
        cb.record_failure("test")
        cb.record_failure("test")
        assert cb.state == CircuitState.OPEN

    def test_allow_request_when_closed(self):
        cb = CircuitBreaker()
        assert cb.allow_request() is True

    def test_deny_request_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        cb.record_failure("test")
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
        cb.record_failure("test")
        assert cb.state == CircuitState.OPEN
        # Cooldown is 0s, so should immediately transition
        assert cb.allow_request() is True
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes(self):
        cb = CircuitBreaker()
        cb.state = CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        cb.state = CircuitState.HALF_OPEN
        cb.record_failure("test")
        assert cb.state == CircuitState.OPEN

    def test_time_until_retry_closed(self):
        cb = CircuitBreaker()
        assert cb.time_until_retry() == 0.0

    def test_time_until_retry_open(self):
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

    def setup_method(self):
        _CIRCUIT_BREAKERS.clear()

    def teardown_method(self):
        _CIRCUIT_BREAKERS.clear()

    def test_returns_none_for_none_provider(self):
        assert get_circuit_breaker(None) is None

    @patch("api.core.network.get_network_config", return_value={
        "circuit_breaker_enabled": True,
        "circuit_breaker_threshold": 5,
        "circuit_breaker_cooldown_s": 120,
    })
    def test_creates_circuit_breaker(self, mock_cfg):
        cb = get_circuit_breaker("ia")
        assert cb is not None
        assert cb.failure_threshold == 5
        assert cb.cooldown_seconds == 120

    @patch("api.core.network.get_network_config", return_value={
        "circuit_breaker_enabled": True,
    })
    def test_returns_same_instance(self, mock_cfg):
        cb1 = get_circuit_breaker("ia")
        cb2 = get_circuit_breaker("ia")
        assert cb1 is cb2

    @patch("api.core.network.get_network_config", return_value={
        "circuit_breaker_enabled": False,
    })
    def test_returns_none_when_disabled(self, mock_cfg):
        assert get_circuit_breaker("ia") is None


# ============================================================================
# is_provider_available
# ============================================================================

class TestIsProviderAvailable:
    """Tests for provider availability check."""

    def setup_method(self):
        _CIRCUIT_BREAKERS.clear()

    def teardown_method(self):
        _CIRCUIT_BREAKERS.clear()

    def test_none_provider_always_available(self):
        assert is_provider_available(None) is True

    @patch("api.core.network.get_network_config", return_value={
        "circuit_breaker_enabled": True,
        "circuit_breaker_threshold": 1,
        "circuit_breaker_cooldown_s": 300,
    })
    def test_unavailable_when_circuit_open(self, mock_cfg):
        cb = get_circuit_breaker("ia")
        cb.record_failure("ia")
        assert is_provider_available("ia") is False


# ============================================================================
# get_provider_cooldown
# ============================================================================

class TestGetProviderCooldown:
    """Tests for provider cooldown time."""

    def setup_method(self):
        _CIRCUIT_BREAKERS.clear()

    def teardown_method(self):
        _CIRCUIT_BREAKERS.clear()

    def test_zero_for_none_provider(self):
        assert get_provider_cooldown(None) == 0.0

    @patch("api.core.network.get_network_config", return_value={
        "circuit_breaker_enabled": True,
        "circuit_breaker_threshold": 1,
        "circuit_breaker_cooldown_s": 300,
    })
    def test_positive_when_open(self, mock_cfg):
        cb = get_circuit_breaker("ia")
        cb.record_failure("ia")
        assert get_provider_cooldown("ia") > 0


# ============================================================================
# get_provider_for_url
# ============================================================================

class TestGetProviderForUrlExtended:
    """Extended tests for URL-to-provider mapping."""

    def test_gallica(self):
        assert get_provider_for_url("https://gallica.bnf.fr/ark:/12148/bpt6k123") == "gallica"

    def test_mdz(self):
        assert get_provider_for_url("https://api.digitale-sammlungen.de/item/bsb123") == "mdz"

    def test_loc(self):
        assert get_provider_for_url("https://www.loc.gov/item/123") == "loc"

    def test_unknown_url(self):
        assert get_provider_for_url("https://completely-unknown.org/page") is None

    def test_with_port(self):
        assert get_provider_for_url("https://gallica.bnf.fr:443/ark:/123") == "gallica"

    def test_invalid_url(self):
        assert get_provider_for_url("not-a-url") is None

    def test_internet_archive(self):
        assert get_provider_for_url("https://archive.org/details/test") == "internet_archive"

    def test_annas_archive(self):
        assert get_provider_for_url("https://annas-archive.li/download") == "annas_archive"


# ============================================================================
# build_session
# ============================================================================

class TestBuildSession:
    """Tests for HTTP session construction."""

    def test_returns_session(self):
        session = build_session()
        assert session is not None
        assert "User-Agent" in session.headers


# ============================================================================
# make_json_request
# ============================================================================

class TestMakeJsonRequest:
    """Tests for JSON-specific request wrapper."""

    @patch("api.core.network.make_request")
    def test_returns_dict(self, mock_req):
        mock_req.return_value = {"key": "value"}
        result = make_json_request("https://example.org/api")
        assert result == {"key": "value"}

    @patch("api.core.network.make_request")
    def test_returns_none_for_non_dict(self, mock_req):
        mock_req.return_value = "text response"
        result = make_json_request("https://example.org/api")
        assert result is None

    @patch("api.core.network.make_request")
    def test_returns_none_on_error(self, mock_req):
        mock_req.return_value = None
        result = make_json_request("https://example.org/api")
        assert result is None
