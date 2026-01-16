"""Tests for api/core/network.py - Network utilities."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests


class TestCircuitState:
    """Tests for CircuitState enum."""

    def test_circuit_states_exist(self):
        """CircuitState has expected values."""
        from api.core.network import CircuitState
        
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"


class TestCircuitBreaker:
    """Tests for CircuitBreaker class."""

    def test_initial_state_is_closed(self):
        """CircuitBreaker starts in CLOSED state."""
        from api.core.network import CircuitBreaker, CircuitState
        
        cb = CircuitBreaker()
        
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_record_success_resets_failure_count(self):
        """record_success resets failure count to zero."""
        from api.core.network import CircuitBreaker
        
        cb = CircuitBreaker()
        cb.failure_count = 2
        
        cb.record_success()
        
        assert cb.failure_count == 0

    def test_record_success_closes_half_open_circuit(self):
        """record_success closes circuit if in HALF_OPEN state."""
        from api.core.network import CircuitBreaker, CircuitState
        
        cb = CircuitBreaker()
        cb.state = CircuitState.HALF_OPEN
        
        cb.record_success()
        
        assert cb.state == CircuitState.CLOSED

    def test_record_failure_increments_count(self):
        """record_failure increments failure count."""
        from api.core.network import CircuitBreaker
        
        cb = CircuitBreaker()
        
        cb.record_failure("test")
        
        assert cb.failure_count == 1

    def test_record_failure_opens_circuit_at_threshold(self):
        """record_failure opens circuit when threshold reached."""
        from api.core.network import CircuitBreaker, CircuitState
        
        cb = CircuitBreaker(failure_threshold=3)
        
        cb.record_failure("test")
        cb.record_failure("test")
        assert cb.state == CircuitState.CLOSED
        
        cb.record_failure("test")
        assert cb.state == CircuitState.OPEN

    def test_record_failure_reopens_half_open_circuit(self):
        """record_failure reopens circuit if in HALF_OPEN state."""
        from api.core.network import CircuitBreaker, CircuitState
        
        cb = CircuitBreaker()
        cb.state = CircuitState.HALF_OPEN
        
        cb.record_failure("test")
        
        assert cb.state == CircuitState.OPEN

    def test_allow_request_true_when_closed(self):
        """allow_request returns True when circuit is CLOSED."""
        from api.core.network import CircuitBreaker, CircuitState
        
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        
        assert cb.allow_request() is True

    def test_allow_request_false_when_open(self):
        """allow_request returns False when circuit is OPEN and cooldown not elapsed."""
        from api.core.network import CircuitBreaker, CircuitState
        
        cb = CircuitBreaker(cooldown_seconds=300)
        cb.state = CircuitState.OPEN
        cb.opened_at = time.monotonic()  # Just opened
        
        assert cb.allow_request() is False

    def test_allow_request_transitions_to_half_open(self):
        """allow_request transitions to HALF_OPEN after cooldown."""
        from api.core.network import CircuitBreaker, CircuitState
        
        cb = CircuitBreaker(cooldown_seconds=0.01)  # Very short cooldown
        cb.state = CircuitState.OPEN
        cb.opened_at = time.monotonic() - 1  # Opened 1 second ago
        
        result = cb.allow_request()
        
        assert result is True
        assert cb.state == CircuitState.HALF_OPEN

    def test_allow_request_true_when_half_open(self):
        """allow_request returns True when circuit is HALF_OPEN."""
        from api.core.network import CircuitBreaker, CircuitState
        
        cb = CircuitBreaker()
        cb.state = CircuitState.HALF_OPEN
        
        assert cb.allow_request() is True

    def test_time_until_retry_zero_when_closed(self):
        """time_until_retry returns 0 when circuit is CLOSED."""
        from api.core.network import CircuitBreaker
        
        cb = CircuitBreaker()
        
        assert cb.time_until_retry() == 0.0

    def test_time_until_retry_returns_remaining_time(self):
        """time_until_retry returns remaining cooldown time when OPEN."""
        from api.core.network import CircuitBreaker, CircuitState
        
        cb = CircuitBreaker(cooldown_seconds=300)
        cb.state = CircuitState.OPEN
        cb.opened_at = time.monotonic()
        
        remaining = cb.time_until_retry()
        
        assert 299 < remaining <= 300


class TestRateLimiter:
    """Tests for RateLimiter class."""

    def test_init_with_zero_interval(self):
        """RateLimiter with zero interval doesn't sleep."""
        from api.core.network import RateLimiter
        
        rl = RateLimiter(min_interval_s=0.0, jitter_s=0.0)
        
        start = time.monotonic()
        rl.wait()
        elapsed = time.monotonic() - start
        
        assert elapsed < 0.01  # Should be essentially instant

    def test_wait_enforces_minimum_interval(self):
        """RateLimiter enforces minimum interval between calls."""
        from api.core.network import RateLimiter
        
        rl = RateLimiter(min_interval_s=0.05, jitter_s=0.0)
        
        rl.wait()  # First call sets timestamp
        start = time.monotonic()
        rl.wait()  # Second call should wait
        elapsed = time.monotonic() - start
        
        assert elapsed >= 0.04  # Should have waited ~0.05s

    def test_wait_adds_jitter(self):
        """RateLimiter adds random jitter to wait time."""
        from api.core.network import RateLimiter
        
        rl = RateLimiter(min_interval_s=0.01, jitter_s=0.05)
        
        rl.wait()
        # Can't easily test randomness, just ensure no errors
        rl.wait()


class TestCircuitBreakerFunctions:
    """Tests for module-level circuit breaker functions."""

    @pytest.fixture(autouse=True)
    def reset_circuit_breakers(self):
        """Reset circuit breakers between tests."""
        from api.core import network
        network._CIRCUIT_BREAKERS.clear()
        yield
        network._CIRCUIT_BREAKERS.clear()

    def test_get_circuit_breaker_returns_none_for_none(self):
        """get_circuit_breaker returns None for None provider."""
        from api.core.network import get_circuit_breaker
        
        assert get_circuit_breaker(None) is None

    def test_get_circuit_breaker_creates_new(self):
        """get_circuit_breaker creates new breaker for unknown provider."""
        from api.core.network import get_circuit_breaker
        
        with patch("api.core.network.get_network_config") as mock_config:
            mock_config.return_value = {"circuit_breaker_enabled": True}
            
            cb = get_circuit_breaker("test_provider")
            
            assert cb is not None

    def test_get_circuit_breaker_returns_same_instance(self):
        """get_circuit_breaker returns same instance for same provider."""
        from api.core.network import get_circuit_breaker
        
        with patch("api.core.network.get_network_config") as mock_config:
            mock_config.return_value = {"circuit_breaker_enabled": True}
            
            cb1 = get_circuit_breaker("test")
            cb2 = get_circuit_breaker("test")
            
            assert cb1 is cb2

    def test_get_circuit_breaker_returns_none_if_disabled(self):
        """get_circuit_breaker returns None if circuit breaker disabled."""
        from api.core.network import get_circuit_breaker
        
        with patch("api.core.network.get_network_config") as mock_config:
            mock_config.return_value = {"circuit_breaker_enabled": False}
            
            cb = get_circuit_breaker("test")
            
            assert cb is None

    def test_is_provider_available_true_when_no_breaker(self):
        """is_provider_available returns True when no circuit breaker."""
        from api.core.network import is_provider_available
        
        with patch("api.core.network.get_circuit_breaker", return_value=None):
            assert is_provider_available("test") is True

    def test_is_provider_available_checks_breaker(self):
        """is_provider_available checks circuit breaker state."""
        from api.core.network import is_provider_available, CircuitBreaker, CircuitState
        
        cb = CircuitBreaker()
        cb.state = CircuitState.OPEN
        cb.opened_at = time.monotonic()
        cb.cooldown_seconds = 300
        
        with patch("api.core.network.get_circuit_breaker", return_value=cb):
            assert is_provider_available("test") is False

    def test_get_provider_cooldown_returns_zero_when_available(self):
        """get_provider_cooldown returns 0 when provider available."""
        from api.core.network import get_provider_cooldown
        
        with patch("api.core.network.get_circuit_breaker", return_value=None):
            assert get_provider_cooldown("test") == 0.0

    def test_get_provider_cooldown_returns_remaining_time(self):
        """get_provider_cooldown returns remaining cooldown time."""
        from api.core.network import get_provider_cooldown, CircuitBreaker, CircuitState
        
        cb = CircuitBreaker(cooldown_seconds=300)
        cb.state = CircuitState.OPEN
        cb.opened_at = time.monotonic()
        
        with patch("api.core.network.get_circuit_breaker", return_value=cb):
            remaining = get_provider_cooldown("test")
            assert 299 < remaining <= 300


class TestRateLimiterFunctions:
    """Tests for module-level rate limiter functions."""

    @pytest.fixture(autouse=True)
    def reset_rate_limiters(self):
        """Reset rate limiters between tests."""
        from api.core import network
        network._RATE_LIMITERS.clear()
        yield
        network._RATE_LIMITERS.clear()

    def test_get_provider_for_url_identifies_providers(self):
        """get_provider_for_url identifies known providers."""
        from api.core.network import get_provider_for_url
        
        assert get_provider_for_url("https://archive.org/details/test") == "internet_archive"
        assert get_provider_for_url("https://gallica.bnf.fr/ark:/12148/test") == "gallica"
        assert get_provider_for_url("https://api.digitale-sammlungen.de/v1") == "mdz"

    def test_get_provider_for_url_returns_none_for_unknown(self):
        """get_provider_for_url returns None for unknown URLs."""
        from api.core.network import get_provider_for_url
        
        assert get_provider_for_url("https://unknown-site.com/page") is None

    def test_get_provider_for_url_handles_invalid_urls(self):
        """get_provider_for_url handles invalid URLs gracefully."""
        from api.core.network import get_provider_for_url
        
        assert get_provider_for_url("not a valid url") is None

    def test_get_rate_limiter_returns_none_for_none(self):
        """get_rate_limiter returns None for None provider."""
        from api.core.network import get_rate_limiter
        
        assert get_rate_limiter(None) is None

    def test_get_rate_limiter_creates_limiter(self):
        """get_rate_limiter creates rate limiter for provider."""
        from api.core.network import get_rate_limiter
        
        with patch("api.core.network.get_network_config") as mock_config:
            mock_config.return_value = {"delay_ms": 100, "jitter_ms": 50}
            
            rl = get_rate_limiter("test")
            
            assert rl is not None
            assert rl.min_interval_s == 0.1


class TestSessionManagement:
    """Tests for session management."""

    @pytest.fixture(autouse=True)
    def reset_session(self):
        """Reset global session between tests."""
        from api.core import network
        network._SESSION = None
        yield
        network._SESSION = None

    def test_build_session_returns_session(self):
        """build_session returns configured Session."""
        from api.core.network import build_session
        
        session = build_session()
        
        assert isinstance(session, requests.Session)
        assert "User-Agent" in session.headers

    def test_get_session_creates_session_once(self):
        """get_session creates session on first call."""
        from api.core.network import get_session
        
        session1 = get_session()
        session2 = get_session()
        
        assert session1 is session2


class TestMakeRequest:
    """Tests for make_request function."""

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        """Reset global state between tests."""
        from api.core import network
        network._SESSION = None
        network._CIRCUIT_BREAKERS.clear()
        network._RATE_LIMITERS.clear()
        yield
        network._SESSION = None
        network._CIRCUIT_BREAKERS.clear()
        network._RATE_LIMITERS.clear()

    def test_make_request_returns_none_when_circuit_open(self):
        """make_request returns None when circuit breaker is open."""
        from api.core.network import make_request, CircuitBreaker, CircuitState
        
        cb = CircuitBreaker(cooldown_seconds=300)
        cb.state = CircuitState.OPEN
        cb.opened_at = time.monotonic()
        
        with patch("api.core.network.get_circuit_breaker", return_value=cb):
            with patch("api.core.network.get_network_config", return_value={}):
                result = make_request("https://archive.org/test")
                
                assert result is None

    def test_make_request_returns_json_for_json_response(self):
        """make_request returns dict for JSON responses."""
        from api.core.network import make_request
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.json.return_value = {"key": "value"}
        
        with patch("api.core.network.get_session") as mock_session:
            mock_session.return_value.get.return_value = mock_response
            with patch("api.core.network.get_network_config", return_value={}):
                with patch("api.core.network.get_circuit_breaker", return_value=None):
                    with patch("api.core.network.get_rate_limiter", return_value=None):
                        result = make_request("https://example.com/api")
                        
                        assert result == {"key": "value"}

    def test_make_request_returns_text_for_html(self):
        """make_request returns str for text/html responses."""
        from api.core.network import make_request
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.text = "<html></html>"
        
        with patch("api.core.network.get_session") as mock_session:
            mock_session.return_value.get.return_value = mock_response
            with patch("api.core.network.get_network_config", return_value={}):
                with patch("api.core.network.get_circuit_breaker", return_value=None):
                    with patch("api.core.network.get_rate_limiter", return_value=None):
                        result = make_request("https://example.com/page")
                        
                        assert result == "<html></html>"

    def test_make_request_returns_bytes_for_binary(self):
        """make_request returns bytes for binary responses."""
        from api.core.network import make_request
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/pdf"}
        mock_response.content = b"PDF content"
        
        with patch("api.core.network.get_session") as mock_session:
            mock_session.return_value.get.return_value = mock_response
            with patch("api.core.network.get_network_config", return_value={}):
                with patch("api.core.network.get_circuit_breaker", return_value=None):
                    with patch("api.core.network.get_rate_limiter", return_value=None):
                        result = make_request("https://example.com/file.pdf")
                        
                        assert result == b"PDF content"

    def test_make_request_returns_none_for_404(self):
        """make_request returns None for 404 responses."""
        from api.core.network import make_request
        
        mock_response = MagicMock()
        mock_response.status_code = 404
        
        with patch("api.core.network.get_session") as mock_session:
            mock_session.return_value.get.return_value = mock_response
            with patch("api.core.network.get_network_config", return_value={}):
                with patch("api.core.network.get_circuit_breaker", return_value=None):
                    with patch("api.core.network.get_rate_limiter", return_value=None):
                        result = make_request("https://example.com/notfound")
                        
                        assert result is None

    def test_make_request_records_success_to_circuit_breaker(self):
        """make_request records success to circuit breaker."""
        from api.core.network import make_request, CircuitBreaker
        
        cb = CircuitBreaker()
        cb.failure_count = 2  # Some previous failures
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.json.return_value = {}
        
        with patch("api.core.network.get_session") as mock_session:
            mock_session.return_value.get.return_value = mock_response
            with patch("api.core.network.get_network_config", return_value={}):
                with patch("api.core.network.get_circuit_breaker", return_value=cb):
                    with patch("api.core.network.get_rate_limiter", return_value=None):
                        make_request("https://example.com/api")
                        
                        assert cb.failure_count == 0

    def test_make_request_handles_timeout(self):
        """make_request handles timeout errors."""
        from api.core.network import make_request
        
        with patch("api.core.network.get_session") as mock_session:
            mock_session.return_value.get.side_effect = requests.exceptions.Timeout()
            with patch("api.core.network.get_network_config", return_value={"max_attempts": 1}):
                with patch("api.core.network.get_circuit_breaker", return_value=None):
                    with patch("api.core.network.get_rate_limiter", return_value=None):
                        result = make_request("https://example.com/slow")
                        
                        assert result is None

    def test_make_request_handles_json_decode_error(self):
        """make_request handles JSON decode errors."""
        from api.core.network import make_request
        import json
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.json.side_effect = json.JSONDecodeError("err", "", 0)
        
        with patch("api.core.network.get_session") as mock_session:
            mock_session.return_value.get.return_value = mock_response
            with patch("api.core.network.get_network_config", return_value={}):
                with patch("api.core.network.get_circuit_breaker", return_value=None):
                    with patch("api.core.network.get_rate_limiter", return_value=None):
                        result = make_request("https://example.com/bad-json")
                        
                        assert result is None


class TestProviderHostMap:
    """Tests for PROVIDER_HOST_MAP configuration."""

    def test_provider_host_map_has_known_providers(self):
        """PROVIDER_HOST_MAP contains expected providers."""
        from api.core.network import PROVIDER_HOST_MAP
        
        assert "internet_archive" in PROVIDER_HOST_MAP
        assert "gallica" in PROVIDER_HOST_MAP
        assert "mdz" in PROVIDER_HOST_MAP
        assert "europeana" in PROVIDER_HOST_MAP

    def test_provider_host_map_hosts_are_tuples(self):
        """PROVIDER_HOST_MAP values are tuples of hostnames."""
        from api.core.network import PROVIDER_HOST_MAP
        
        for provider, hosts in PROVIDER_HOST_MAP.items():
            assert isinstance(hosts, tuple)
            assert len(hosts) > 0
            for host in hosts:
                assert isinstance(host, str)
