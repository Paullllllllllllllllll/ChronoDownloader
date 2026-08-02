"""Tests for the optional browser-impersonation seam in api/core/network.py.

Everything here runs offline and without ``curl_cffi`` installed: the module
is faked, because the point of the seam is that the rest of the tool cannot
tell which HTTP client served a request. Three things are checked -- which
session a provider is handed, how curl_cffi's parallel exception hierarchy is
translated into the ``requests`` one the retry logic keys off, and that the
plain-requests path is untouched when no provider opts in.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator, Iterator
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.utils import get_encoding_from_headers

from api.core import download as dl_mod
from api.core import network
from api.core.network import (
    CONNECT_FAILURE_MAX_ATTEMPTS,
    DEFAULT_IMPERSONATE_TARGET,
    ImpersonatingSession,
    build_session,
    connect_aware_attempt_cap,
    get_session,
    is_connect_failure,
    make_request,
    resolve_impersonate_target,
    translate_curl_error,
)

# ============================================================================
# curl_cffi stand-ins
# ============================================================================


class CurlErrors:
    """Stand-ins for curl_cffi's exception hierarchy.

    The translation matches on class names walked over the MRO, so these
    reproduce curl_cffi 0.15's names and inheritance exactly; nothing else
    about them matters. They are nested so the module scope does not shadow
    the builtin ``ConnectionError``.
    """

    class CurlError(Exception):
        def __init__(self, msg: str, code: int = 0) -> None:
            super().__init__(msg)
            self.code = code

    class RequestException(CurlError):
        pass

    class ConnectionError(RequestException):
        pass

    class DNSError(ConnectionError):
        pass

    class Timeout(RequestException):
        pass

    class ConnectTimeout(ConnectionError, Timeout):
        pass

    class ReadTimeout(Timeout):
        pass

    class SSLError(ConnectionError):
        pass

    class CertificateVerifyError(SSLError):
        pass

    class ProxyError(RequestException):
        pass

    class TooManyRedirects(RequestException):
        pass

    class HTTPError(RequestException):
        pass

    class IncompleteRead(HTTPError):
        pass

    class ContentDecodingError(RequestException):
        pass

    class InvalidURL(RequestException):
        pass

    class ImpersonateError(RequestException):
        pass


class FakeCurlResponse:
    """Minimal stand-in for a curl_cffi response object."""

    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
        url: str = "https://example.org/resource",
        reason: str = "OK",
        chunks: list[bytes] | None = None,
        stream_error: BaseException | None = None,
        charset_encoding: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        self.content = content
        self.url = url
        self.reason = reason
        # curl_cffi answers ``encoding`` with a string unconditionally (its
        # default_encoding when the response declares none), and reports the
        # declared charset -- or None -- through ``charset_encoding``.
        self.encoding = "utf-8"
        self.charset_encoding = charset_encoding
        self.closed = False
        self.chunk_sizes: list[int | None] = []
        self._chunks = chunks
        self._stream_error = stream_error

    def iter_content(self, chunk_size: int | None = None) -> Iterator[bytes]:
        self.chunk_sizes.append(chunk_size)
        yield from self._chunks if self._chunks is not None else [self.content]
        if self._stream_error is not None:
            raise self._stream_error

    def close(self) -> None:
        self.closed = True


class FakeCurlSession:
    """Minimal stand-in for ``curl_cffi.requests.Session``."""

    def __init__(
        self,
        response: FakeCurlResponse | None = None,
        error: BaseException | None = None,
        responses: list[FakeCurlResponse] | None = None,
    ) -> None:
        self.impersonate: str | None = None
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False
        self.response = response
        self.responses = responses
        self.error = error

    def request(self, method: str, url: str, **kwargs: Any) -> FakeCurlResponse:
        self.calls.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        return self.response or FakeCurlResponse()

    def close(self) -> None:
        self.closed = True


# curl_cffi publishes the profiles it can impersonate as a typing Literal;
# a handful of the real names is enough to exercise the pre-flight check.
FakeBrowserTypeLiteral = Literal["chrome", "chrome124", "safari17_0", "firefox135"]


def fake_curl_module(session: FakeCurlSession) -> SimpleNamespace:
    """Return a stand-in for the ``curl_cffi.requests`` module."""
    constructed: list[str] = []

    def make_session(impersonate: str) -> FakeCurlSession:
        constructed.append(impersonate)
        session.impersonate = impersonate
        return session

    return SimpleNamespace(
        Session=make_session,
        constructed=constructed,
        BrowserTypeLiteral=FakeBrowserTypeLiteral,
    )


def impersonating(session: FakeCurlSession, target: str = "chrome") -> requests.Session:
    """Build an ImpersonatingSession over a fake curl session."""
    return ImpersonatingSession(fake_curl_module(session), target)


@pytest.fixture(autouse=True)
def reset_network_state() -> Generator[None, None, None]:
    """Clear cached sessions, breakers, limiters, and warnings around a test."""
    network._SESSION = None
    network._IMPERSONATED_SESSIONS.clear()
    network._IMPERSONATE_TYPE_WARNED.clear()
    network._CIRCUIT_BREAKERS.clear()
    network._RATE_LIMITERS.clear()
    yield
    network._SESSION = None
    network._IMPERSONATED_SESSIONS.clear()
    network._IMPERSONATE_TYPE_WARNED.clear()
    network._CIRCUIT_BREAKERS.clear()
    network._RATE_LIMITERS.clear()


# ============================================================================
# Configuration
# ============================================================================


class TestResolveImpersonateTarget:
    """Reading ``provider_settings.<provider>.network.impersonate``."""

    @staticmethod
    def _target(net: dict[str, Any], provider: str | None = "bne") -> str | None:
        with patch("api.core.network.get_network_config", return_value=net):
            return resolve_impersonate_target(provider)

    def test_absent_key_disables_impersonation(self) -> None:
        assert self._target({"delay_ms": 500}) is None

    def test_true_selects_the_default_browser(self) -> None:
        assert self._target({"impersonate": True}) == DEFAULT_IMPERSONATE_TARGET

    def test_string_selects_a_named_browser(self) -> None:
        assert self._target({"impersonate": "chrome124"}) == "chrome124"

    def test_string_is_stripped(self) -> None:
        assert self._target({"impersonate": " safari17_0 "}) == "safari17_0"

    def test_false_disables_impersonation(self) -> None:
        assert self._target({"impersonate": False}) is None

    def test_blank_string_disables_impersonation(self) -> None:
        assert self._target({"impersonate": "   "}) is None

    def test_no_provider_never_impersonates(self) -> None:
        assert self._target({"impersonate": "chrome"}, provider=None) is None

    @pytest.mark.parametrize("raw", [1, {}, {"name": "chrome"}, ["chrome"], 2.5])
    def test_unusable_type_disables_impersonation(self, raw: Any) -> None:
        assert self._target({"impersonate": raw}) is None

    def test_truthy_non_string_warns_once_per_provider(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A silent disable is a config trap; the resolver runs per request.

        The warning therefore has to fire, and has to be deduped per provider
        or it would be logged on every single request.
        """
        with caplog.at_level(logging.WARNING, logger="api.core.network"):
            assert self._target({"impersonate": 1}) is None
            assert self._target({"impersonate": 1}) is None
            assert self._target({"impersonate": 1}, provider="polona") is None

        warnings = [
            r for r in caplog.records if "network.impersonate" in r.getMessage()
        ]
        assert len(warnings) == 2
        assert "bne" in warnings[0].getMessage()
        assert "polona" in warnings[1].getMessage()

    def test_falsy_and_valid_values_are_never_warned_about(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="api.core.network"):
            self._target({"impersonate": False})
            self._target({"impersonate": 0})
            self._target({"impersonate": None})
            self._target({"impersonate": "   "})
            self._target({"impersonate": "chrome"})
            self._target({"delay_ms": 500})

        assert not [
            r for r in caplog.records if "network.impersonate" in r.getMessage()
        ]


# ============================================================================
# Session selection
# ============================================================================


class TestSessionSelection:
    """Which client a provider is handed: configured, missing, or off."""

    def test_configured_and_importable_yields_curl_session(self) -> None:
        curl_session = FakeCurlSession()
        module = fake_curl_module(curl_session)

        with (
            patch(
                "api.core.network.get_network_config",
                return_value={"impersonate": "chrome"},
            ),
            patch("api.core.network._load_curl_requests", return_value=module),
        ):
            session = get_session("bne")

        assert isinstance(session, ImpersonatingSession)
        assert session.impersonate == "chrome"
        assert module.constructed == ["chrome"]
        assert curl_session.impersonate == "chrome"

    def test_configured_session_sends_no_library_user_agent(self) -> None:
        """curl_cffi supplies the browser headers that match its fingerprint."""
        session = impersonating(FakeCurlSession())
        assert "User-Agent" not in session.headers

    def test_configured_but_missing_falls_back_with_one_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch(
                "api.core.network.get_network_config",
                return_value={"impersonate": "chrome"},
            ),
            patch("api.core.network._load_curl_requests", return_value=None),
            caplog.at_level(logging.WARNING, logger="api.core.network"),
        ):
            first = get_session("bne")
            second = get_session("bne")

        assert isinstance(first, requests.Session)
        assert not isinstance(first, ImpersonatingSession)
        assert first is second
        warnings = [r for r in caplog.records if "curl_cffi" in r.getMessage()]
        assert len(warnings) == 1
        assert "uv sync --extra impersonate" in warnings[0].getMessage()

    def test_unknown_target_falls_back_before_any_request(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """curl_cffi resolves the profile per request, not in ``Session()``.

        A typo therefore constructs a healthy-looking session and then fails
        every request with an ImpersonateError, which reaches the retry loop as
        a bare RequestException and burns the whole backoff budget on each URL.
        The profile is checked up front so it stays a one-time fallback.
        """
        curl_session = FakeCurlSession()
        module = fake_curl_module(curl_session)

        with (
            patch(
                "api.core.network.get_network_config",
                return_value={"impersonate": "netscape"},
            ),
            patch("api.core.network._load_curl_requests", return_value=module),
            caplog.at_level(logging.WARNING, logger="api.core.network"),
        ):
            session = get_session("bne")

        assert not isinstance(session, ImpersonatingSession)
        assert module.constructed == []
        assert any("netscape" in r.getMessage() for r in caplog.records)

    def test_known_target_passes_the_preflight_check(self) -> None:
        module = fake_curl_module(FakeCurlSession())
        with (
            patch(
                "api.core.network.get_network_config",
                return_value={"impersonate": "safari17_0"},
            ),
            patch("api.core.network._load_curl_requests", return_value=module),
        ):
            session = get_session("bne")

        assert isinstance(session, ImpersonatingSession)
        assert module.constructed == ["safari17_0"]

    def test_target_is_not_checked_when_curl_lists_no_profiles(self) -> None:
        """A future curl_cffi without the literal must not break impersonation."""
        curl_session = FakeCurlSession()
        module = SimpleNamespace(Session=lambda impersonate: curl_session)

        with (
            patch(
                "api.core.network.get_network_config",
                return_value={"impersonate": "chrome999"},
            ),
            patch("api.core.network._load_curl_requests", return_value=module),
        ):
            session = get_session("bne")

        assert isinstance(session, ImpersonatingSession)

    def test_failing_constructor_still_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def exploding_session(impersonate: str) -> FakeCurlSession:
            raise CurlErrors.ImpersonateError(f"cannot start {impersonate}")

        module = SimpleNamespace(
            Session=exploding_session, BrowserTypeLiteral=FakeBrowserTypeLiteral
        )
        with (
            patch(
                "api.core.network.get_network_config",
                return_value={"impersonate": "chrome"},
            ),
            patch("api.core.network._load_curl_requests", return_value=module),
            caplog.at_level(logging.WARNING, logger="api.core.network"),
        ):
            session = get_session("bne")

        assert not isinstance(session, ImpersonatingSession)
        assert any("cannot start chrome" in r.getMessage() for r in caplog.records)

    def test_not_configured_uses_the_shared_plain_session(self) -> None:
        with patch("api.core.network.get_network_config", return_value={}):
            shared = get_session()
            per_provider = get_session("gallica")

        assert per_provider is shared
        assert not isinstance(shared, ImpersonatingSession)
        assert network._IMPERSONATED_SESSIONS == {}

    def test_configured_session_is_cached_per_provider(self) -> None:
        module = fake_curl_module(FakeCurlSession())
        with (
            patch(
                "api.core.network.get_network_config",
                return_value={"impersonate": "chrome"},
            ),
            patch("api.core.network._load_curl_requests", return_value=module),
        ):
            first = get_session("bne")
            second = get_session("bne")

        assert first is second
        assert module.constructed == ["chrome"]

    def test_changed_target_rebuilds_the_cached_session(self) -> None:
        """A reloaded config naming another profile must not serve the old one.

        The cache used to be keyed by provider alone, so editing the profile
        string kept every later request on the session built for the previous
        browser fingerprint.
        """
        module = fake_curl_module(FakeCurlSession())
        net: dict[str, Any] = {"impersonate": "chrome"}

        with (
            patch("api.core.network.get_network_config", return_value=net),
            patch("api.core.network._load_curl_requests", return_value=module),
        ):
            first = get_session("bne")
            net["impersonate"] = "safari17_0"
            second = get_session("bne")
            third = get_session("bne")

        assert first is not second
        assert second is third
        assert module.constructed == ["chrome", "safari17_0"]
        assert isinstance(second, ImpersonatingSession)
        assert second.impersonate == "safari17_0"

    def test_cached_fallback_is_tied_to_its_target(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The plain-session fallback is cached per target, not per provider."""
        net: dict[str, Any] = {"impersonate": "chrome"}
        with (
            patch("api.core.network.get_network_config", return_value=net),
            patch("api.core.network._load_curl_requests", return_value=None),
            caplog.at_level(logging.WARNING, logger="api.core.network"),
        ):
            first = get_session("bne")
            net["impersonate"] = "safari17_0"
            second = get_session("bne")

        assert first is not second
        warnings = [r for r in caplog.records if "curl_cffi" in r.getMessage()]
        assert len(warnings) == 2

    def test_impersonating_provider_does_not_touch_the_shared_session(self) -> None:
        module = fake_curl_module(FakeCurlSession())

        def net_for(provider: str | None) -> dict[str, Any]:
            return {"impersonate": "chrome"} if provider == "bne" else {}

        with (
            patch("api.core.network.get_network_config", side_effect=net_for),
            patch("api.core.network._load_curl_requests", return_value=module),
        ):
            bne = get_session("bne")
            gallica = get_session("gallica")

        assert isinstance(bne, ImpersonatingSession)
        assert not isinstance(gallica, ImpersonatingSession)
        assert gallica is network._SESSION


# ============================================================================
# Exception translation
# ============================================================================


class TestTranslateCurlError:
    """curl_cffi errors have to arrive as their requests equivalents."""

    @pytest.mark.parametrize(
        ("curl_class", "expected"),
        [
            (CurlErrors.RequestException, requests.exceptions.RequestException),
            (CurlErrors.ConnectionError, requests.exceptions.ConnectionError),
            (CurlErrors.DNSError, requests.exceptions.ConnectionError),
            (CurlErrors.Timeout, requests.exceptions.Timeout),
            (CurlErrors.ReadTimeout, requests.exceptions.ReadTimeout),
            (CurlErrors.ConnectTimeout, requests.exceptions.ConnectTimeout),
            (CurlErrors.SSLError, requests.exceptions.SSLError),
            (CurlErrors.CertificateVerifyError, requests.exceptions.SSLError),
            (CurlErrors.ProxyError, requests.exceptions.ProxyError),
            (CurlErrors.TooManyRedirects, requests.exceptions.TooManyRedirects),
            (CurlErrors.HTTPError, requests.exceptions.HTTPError),
            (CurlErrors.IncompleteRead, requests.exceptions.ChunkedEncodingError),
            (
                CurlErrors.ContentDecodingError,
                requests.exceptions.ContentDecodingError,
            ),
            (CurlErrors.InvalidURL, requests.exceptions.InvalidURL),
            (CurlErrors.ImpersonateError, requests.exceptions.RequestException),
        ],
    )
    def test_class_mapping(
        self, curl_class: type[Exception], expected: type[Exception]
    ) -> None:
        mapped = translate_curl_error(curl_class("boom"))
        assert type(mapped) is expected

    def test_every_mapped_error_is_caught_by_the_retry_loops(self) -> None:
        """Whatever curl raises must satisfy ``except RequestException``."""
        for curl_class in (
            CurlErrors.RequestException,
            CurlErrors.ConnectionError,
            CurlErrors.DNSError,
            CurlErrors.Timeout,
            CurlErrors.SSLError,
            CurlErrors.IncompleteRead,
            CurlErrors.TooManyRedirects,
        ):
            mapped = translate_curl_error(curl_class("boom"))
            assert isinstance(mapped, requests.exceptions.RequestException)

    def test_non_curl_exception_is_left_alone(self) -> None:
        assert translate_curl_error(ValueError("not a transport error")) is None

    def test_requests_exception_is_left_alone(self) -> None:
        exc = requests.exceptions.ConnectionError("already translated")
        assert translate_curl_error(exc) is None

    def test_dns_failure_message_carries_the_resolution_markers(self) -> None:
        """``make_request`` recognizes a DNS failure from the message text."""
        mapped = translate_curl_error(
            CurlErrors.DNSError("Could not resolve host: datos.bne.es", code=6)
        )
        assert mapped is not None
        message = str(mapped).lower()
        assert "nameresolutionerror" in message
        assert "failed to resolve" in message
        assert "could not resolve host: datos.bne.es" in message
        # Parity with requests: a DNS failure is not a connect-level failure,
        # so it keeps the full attempt budget rather than the short one.
        assert is_connect_failure(mapped) is False

    def test_refused_connection_gets_the_short_attempt_budget(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.ConnectionError(
                "Failed to connect to bnedigital.bne.es port 443", code=7
            )
        )
        assert mapped is not None
        assert is_connect_failure(mapped) is True
        assert connect_aware_attempt_cap(mapped, 25) == CONNECT_FAILURE_MAX_ATTEMPTS

    def test_read_error_keeps_the_full_attempt_budget(self) -> None:
        """A connection that came up and then broke is not a connect failure."""
        mapped = translate_curl_error(
            CurlErrors.ConnectionError("Recv failure: Connection was reset", code=56)
        )
        assert mapped is not None
        assert is_connect_failure(mapped) is False
        assert connect_aware_attempt_cap(mapped, 25) == 25

    def test_connect_phase_timeout_becomes_a_connect_timeout(self) -> None:
        """curl files both timeout phases under code 28; requests separates them."""
        mapped = translate_curl_error(
            CurlErrors.Timeout(
                "Failed to connect to datos.bne.es port 443 after 10000 ms: "
                "Timeout was reached",
                code=28,
            )
        )
        assert type(mapped) is requests.exceptions.ConnectTimeout
        assert is_connect_failure(mapped) is True

    def test_read_phase_timeout_stays_a_plain_timeout(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.Timeout(
                "Operation timed out after 30000 ms with 0 bytes received", code=28
            )
        )
        assert type(mapped) is requests.exceptions.Timeout
        assert is_connect_failure(mapped) is False
        assert connect_aware_attempt_cap(mapped, 25) == 25


class TestTranslateBareCurlRequestException:
    """The streaming path erases the class, so the result code has to carry it.

    curl_cffi's stream implementation re-raises every transport failure as a
    bare ``RequestException(str(e), e.code, rsp)``. The name walk can then only
    reach the catch-all entry, which cost a connect timeout its short budget and
    a certificate failure its ``ssl_error_policy`` handling entirely.
    """

    @pytest.mark.parametrize(
        "code", [35, 53, 54, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91]
    )
    def test_ssl_result_codes_become_ssl_errors(self, code: int) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("SSL peer certificate problem", code=code)
        )
        assert type(mapped) is requests.exceptions.SSLError

    def test_connect_phase_timeout_code_becomes_a_connect_timeout(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException(
                "Failed to connect to bnedigital.bne.es port 443 after 10000 ms: "
                "Timeout was reached",
                code=28,
            )
        )
        assert type(mapped) is requests.exceptions.ConnectTimeout
        assert connect_aware_attempt_cap(mapped, 25) == CONNECT_FAILURE_MAX_ATTEMPTS

    def test_read_phase_timeout_code_becomes_a_plain_timeout(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("Operation timed out after 30000 ms", code=28)
        )
        assert type(mapped) is requests.exceptions.Timeout
        assert connect_aware_attempt_cap(mapped, 25) == 25

    def test_partial_file_code_becomes_a_chunked_encoding_error(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("transfer closed with bytes remaining", code=18)
        )
        assert type(mapped) is requests.exceptions.ChunkedEncodingError

    def test_resolution_code_becomes_a_connection_error_with_markers(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("Could not resolve host: datos.bne.es", code=6)
        )
        assert type(mapped) is requests.exceptions.ConnectionError
        assert "nameresolutionerror" in str(mapped).lower()

    def test_connect_refused_code_gets_the_short_budget(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("Failed to connect: refused", code=7)
        )
        assert type(mapped) is requests.exceptions.ConnectionError
        assert is_connect_failure(mapped) is True

    def test_unmapped_code_stays_a_generic_request_exception(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("something curl-specific", code=23)
        )
        assert type(mapped) is requests.exceptions.RequestException

    def test_codeless_error_stays_a_generic_request_exception(self) -> None:
        mapped = translate_curl_error(CurlErrors.RequestException("no code at all"))
        assert type(mapped) is requests.exceptions.RequestException

    def test_a_named_class_is_never_overridden_by_its_code(self) -> None:
        """curl naming the failure itself outranks the code lookup."""
        mapped = translate_curl_error(CurlErrors.HTTPError("bad status", code=60))
        assert type(mapped) is requests.exceptions.HTTPError


# ============================================================================
# Translated errors inside make_request
# ============================================================================


def _run_make_request(
    error: BaseException | None = None,
    response: FakeCurlResponse | None = None,
    net: dict[str, Any] | None = None,
) -> tuple[MagicMock, FakeCurlSession, Any]:
    """Drive make_request over an impersonating session; return breaker and calls."""
    config: dict[str, Any] = {
        "impersonate": "chrome",
        "max_attempts": 3,
        "base_backoff_s": 0.0,
        "backoff_multiplier": 1.0,
        "max_backoff_s": 0.0,
    }
    config.update(net or {})

    curl_session = FakeCurlSession(response=response, error=error)
    session = impersonating(curl_session)
    cb = MagicMock()
    cb.allow_request.return_value = True

    with (
        patch("api.core.network.get_session", return_value=session),
        patch("api.core.network.get_network_config", return_value=config),
        patch("api.core.network.get_circuit_breaker", return_value=cb),
        patch("api.core.network.time.sleep"),
    ):
        result = make_request("https://datos.bne.es/sparql")

    return cb, curl_session, result


class TestTranslatedErrorsReachTheRightBranch:
    """Each mapped class has to land in the branch its requests twin would."""

    def test_dns_failure_is_not_retried_and_feeds_the_breaker(self) -> None:
        cb, curl_session, result = _run_make_request(
            CurlErrors.DNSError("Could not resolve host: datos.bne.es", code=6)
        )
        assert result is None
        assert len(curl_session.calls) == 1
        cb.record_failure.assert_called_once()

    def test_refused_connection_stops_after_the_short_budget(self) -> None:
        cb, curl_session, result = _run_make_request(
            CurlErrors.ConnectionError("Failed to connect: refused", code=7),
            net={"max_attempts": 25},
        )
        assert result is None
        assert len(curl_session.calls) == CONNECT_FAILURE_MAX_ATTEMPTS
        cb.record_failure.assert_called_once()

    def test_read_error_uses_the_whole_budget(self) -> None:
        cb, curl_session, result = _run_make_request(
            CurlErrors.ConnectionError("Recv failure: Connection was reset", code=56)
        )
        assert result is None
        assert len(curl_session.calls) == 3
        cb.record_failure.assert_called_once()

    def test_read_timeout_uses_the_whole_budget(self) -> None:
        cb, curl_session, result = _run_make_request(
            CurlErrors.Timeout("Operation timed out after 30000 ms", code=28)
        )
        assert result is None
        assert len(curl_session.calls) == 3
        cb.record_failure.assert_called_once()

    def test_connect_timeout_stops_after_the_short_budget(self) -> None:
        cb, curl_session, result = _run_make_request(
            CurlErrors.Timeout(
                "Failed to connect to datos.bne.es port 443 after 10000 ms", code=28
            ),
            net={"max_attempts": 25},
        )
        assert result is None
        assert len(curl_session.calls) == CONNECT_FAILURE_MAX_ATTEMPTS
        cb.record_failure.assert_called_once()

    def test_certificate_error_aborts_and_feeds_the_breaker(self) -> None:
        """A handshake that never completes is a provider outage, not a hiccup.

        The branch used to return without recording, so a provider failing every
        request on its certificate never tripped its breaker.
        """
        cb, curl_session, result = _run_make_request(
            CurlErrors.CertificateVerifyError("certificate verify failed", code=60)
        )
        assert result is None
        assert len(curl_session.calls) == 1
        cb.record_failure.assert_called_once()
        cb.record_success.assert_not_called()

    def test_ssl_policy_retries_insecurely_once(self) -> None:
        """The SSL branch's verify=False retry must work through curl too."""
        cb, curl_session, result = _run_make_request(
            CurlErrors.SSLError("certificate verify failed", code=60),
            net={"ssl_error_policy": "retry_insecure_once"},
        )
        assert result is None
        assert len(curl_session.calls) == 2
        assert curl_session.calls[0][2]["verify"] is True
        assert curl_session.calls[1][2]["verify"] is False
        cb.record_failure.assert_called_once()

    def test_stream_wrapped_ssl_error_still_reaches_the_ssl_policy(self) -> None:
        """The bare RequestException curl's stream path raises must not retry.

        Without the result-code fallback this landed in the generic branch and
        was retried through the full budget with ``verify`` untouched, so
        ``ssl_error_policy`` never applied to a streamed download.
        """
        cb, curl_session, result = _run_make_request(
            CurlErrors.RequestException("SSL peer certificate problem", code=60),
            net={"ssl_error_policy": "retry_insecure_once"},
        )
        assert result is None
        assert len(curl_session.calls) == 2
        assert curl_session.calls[1][2]["verify"] is False
        cb.record_failure.assert_called_once()

    def test_stream_wrapped_connect_timeout_gets_the_short_budget(self) -> None:
        cb, curl_session, result = _run_make_request(
            CurlErrors.RequestException(
                "Failed to connect to datos.bne.es port 443 after 10000 ms", code=28
            ),
            net={"max_attempts": 25},
        )
        assert result is None
        assert len(curl_session.calls) == CONNECT_FAILURE_MAX_ATTEMPTS
        cb.record_failure.assert_called_once()

    def test_unknown_error_is_retried_like_any_transport_error(self) -> None:
        cb, curl_session, result = _run_make_request(
            CurlErrors.RequestException("something curl-specific went wrong", code=23)
        )
        assert result is None
        assert len(curl_session.calls) == 3
        cb.record_failure.assert_called_once()


class TestAdaptedResponses:
    """A curl response has to behave like the requests one it replaces."""

    def test_json_response_is_parsed(self) -> None:
        response = FakeCurlResponse(
            headers={"Content-Type": "application/sparql-results+json"},
            content=b'{"results": {"bindings": [{"id": {"value": "x"}}]}}',
        )
        cb, _, result = _run_make_request(response=response)
        assert isinstance(result, dict)
        assert result["results"]["bindings"][0]["id"]["value"] == "x"
        cb.record_success.assert_called_once()

    def test_text_response_is_decoded(self) -> None:
        response = FakeCurlResponse(
            headers={"Content-Type": "text/plain; charset=utf-8"},
            content="Nuevo arte de cocina española".encode(),
        )
        _, _, result = _run_make_request(response=response)
        assert result == "Nuevo arte de cocina española"

    def test_declared_charset_is_honored(self) -> None:
        raw = FakeCurlResponse(
            headers={"Content-Type": "text/plain; charset=iso-8859-1"},
            content="café".encode("iso-8859-1"),
            charset_encoding="iso-8859-1",
        )
        resp = network.adapt_curl_response(raw, "https://datos.bne.es/x", False)
        assert resp.encoding == "iso-8859-1"
        assert resp.text == "café"

    def test_undeclared_charset_falls_back_to_requests_header_rule(self) -> None:
        """``raw.encoding`` always answers, so reading it masked requests' rule.

        curl_cffi fills ``encoding`` from its own ``default_encoding`` when the
        response declares no charset, which made the header fallback below dead
        code and silently declared every such body UTF-8.
        """
        raw = FakeCurlResponse(
            headers={"Content-Type": "text/html"}, content=b"<html></html>"
        )
        assert raw.encoding == "utf-8"
        assert raw.charset_encoding is None

        resp = network.adapt_curl_response(raw, "https://datos.bne.es/x", False)
        assert resp.encoding == get_encoding_from_headers(resp.headers)
        assert resp.encoding == "ISO-8859-1"

    def test_binary_response_is_returned_as_bytes(self) -> None:
        response = FakeCurlResponse(
            headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.4\n"
        )
        _, _, result = _run_make_request(response=response)
        assert result == b"%PDF-1.4\n"

    def test_blocked_status_feeds_the_breaker(self) -> None:
        """403 is the failure BNE actually produces without impersonation."""
        response = FakeCurlResponse(status_code=403, headers={}, content=b"denied")
        cb, curl_session, result = _run_make_request(response=response)
        assert result is None
        assert len(curl_session.calls) == 1
        cb.record_failure.assert_called_once()

    def test_missing_resource_records_success(self) -> None:
        response = FakeCurlResponse(status_code=404, headers={}, content=b"")
        cb, _, result = _run_make_request(response=response)
        assert result is None
        cb.record_success.assert_called_once()

    def test_server_errors_are_retried(self) -> None:
        response = FakeCurlResponse(status_code=503, headers={}, content=b"")
        cb, curl_session, result = _run_make_request(response=response)
        assert result is None
        assert len(curl_session.calls) == 3
        cb.record_failure.assert_called_once()

    def test_request_arguments_reach_curl(self) -> None:
        session = impersonating(FakeCurlSession(response=FakeCurlResponse()))
        session.get(
            "https://datos.bne.es/sparql",
            params={"query": "SELECT *"},
            headers={"Accept": "application/json"},
            timeout=30.0,
            verify=True,
        )
        curl_session = session._curl_session  # type: ignore[attr-defined]
        method, url, kwargs = curl_session.calls[0]
        assert method == "GET"
        assert url == "https://datos.bne.es/sparql"
        assert kwargs["params"] == {"query": "SELECT *"}
        assert kwargs["headers"] == {"Accept": "application/json"}
        assert kwargs["timeout"] == 30.0
        assert kwargs["verify"] is True
        assert kwargs["stream"] is False

    def test_supported_arguments_may_be_none(self) -> None:
        """An unset optional argument is omitted rather than forwarded as None."""
        session = impersonating(FakeCurlSession(response=FakeCurlResponse()))
        session.get(
            "https://datos.bne.es/sparql", params=None, headers=None, timeout=None
        )
        curl_session = session._curl_session  # type: ignore[attr-defined]
        _, _, kwargs = curl_session.calls[0]
        assert set(kwargs) == {"stream", "allow_redirects"}
        assert kwargs["stream"] is False

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"json": {"query": "SELECT *"}},
            {"data": b"payload"},
            {"cookies": {"session": "x"}},
            {"auth": ("user", "pass")},
            {"proxies": {"https": "http://127.0.0.1:8080"}},
            {"cert": "client.pem"},
        ],
    )
    def test_unforwarded_arguments_are_refused(self, kwargs: dict[str, Any]) -> None:
        """Silently dropping a body would send a bodiless request instead.

        Everything outside the forwarded set has to fail loudly, so a future
        call that carries one is caught here rather than on the provider's side.
        """
        curl_session = FakeCurlSession(response=FakeCurlResponse())
        session = impersonating(curl_session)

        with pytest.raises(TypeError, match=next(iter(kwargs))):
            session.request("POST", "https://datos.bne.es/sparql", **kwargs)

        assert curl_session.calls == []

    def test_positional_arguments_are_still_refused(self) -> None:
        curl_session = FakeCurlSession(response=FakeCurlResponse())
        session = impersonating(curl_session)
        with pytest.raises(TypeError, match="keyword arguments only"):
            session.request("POST", "https://datos.bne.es/sparql", b"body")
        assert curl_session.calls == []

    @pytest.mark.parametrize("status", [0, None, "", "oops"])
    def test_unusable_status_is_a_transport_error(self, status: Any) -> None:
        """Status 0 would pass raise_for_status() and count as a success."""
        raw = SimpleNamespace(
            status_code=status, headers={}, content=b"", url="", reason=""
        )
        with pytest.raises(requests.exceptions.ConnectionError):
            network.adapt_curl_response(raw, "https://datos.bne.es/sparql", False)

    def test_response_without_status_never_records_a_success(self) -> None:
        """It has to reach the retry/breaker machinery as a failure instead."""
        response = FakeCurlResponse(status_code=0, headers={}, content=b"")
        cb, curl_session, result = _run_make_request(response=response)

        assert result is None
        cb.record_success.assert_not_called()
        assert len(curl_session.calls) == 3
        cb.record_failure.assert_called_once()

    def test_close_closes_the_curl_session(self) -> None:
        curl_session = FakeCurlSession()
        session = impersonating(curl_session)
        session.close()
        assert curl_session.closed is True


# ============================================================================
# Streaming downloads
# ============================================================================


def _objects(folder: str) -> list[str]:
    path = os.path.join(folder, "objects")
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


class TestStreamingThroughCurl:
    """download_file streams through requests.Response.iter_content."""

    @staticmethod
    def _download(
        curl_session: FakeCurlSession, folder: str
    ) -> tuple[str | None, FakeCurlSession]:
        session = impersonating(curl_session)
        dl_mod._BUDGET._exhausted = False
        with (
            patch.object(dl_mod, "get_session", return_value=session),
            patch("api.core.download.time.sleep"),
        ):
            result = dl_mod.download_file(
                "https://bnedigital.bne.es/bd/es/pdf?id=x&page=1-25", folder, "bne_x"
            )
        return result, curl_session

    def test_streamed_pdf_is_written(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        payload = b"%PDF-1.4\n" + b"x" * 4096
        response = FakeCurlResponse(
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": str(len(payload)),
            },
            chunks=[payload[:2048], payload[2048:]],
        )
        folder = str(tmp_path / "work")

        result, curl_session = self._download(
            FakeCurlSession(response=response), folder
        )

        assert result is not None
        assert _objects(folder) == ["bne_x_bne.pdf"]
        with open(result, "rb") as fh:
            assert fh.read() == payload
        assert curl_session.calls[0][2]["stream"] is True

    def test_midstream_error_is_translated_and_retried(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """A curl error raised inside iter_content must reach the retry loop.

        Untranslated it would escape ``except requests.exceptions.RequestException``
        in the streaming handler, abort the run, and leave the .part file behind.
        """
        payload = b"%PDF-1.4\n" + b"y" * 1024
        broken = FakeCurlResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[payload[:64]],
            stream_error=CurlErrors.IncompleteRead("transfer closed", code=18),
        )
        good = FakeCurlResponse(
            headers={"Content-Type": "application/pdf"}, chunks=[payload]
        )
        folder = str(tmp_path / "work")

        result, curl_session = self._download(
            FakeCurlSession(responses=[broken, good]), folder
        )

        assert result is not None
        assert len(curl_session.calls) == 2
        # The failed attempt returned its sequence number, so no gap appears.
        assert _objects(folder) == ["bne_x_bne.pdf"]

    def test_stream_passes_no_chunk_size_to_curl(self) -> None:
        """curl_cffi ignores chunk_size and warns once per streamed download.

        Its iter_content yields whatever the transfer delivers, so forwarding
        the caller's hint bought nothing and emitted a CurlCffiWarning on every
        file. The requests-side chunking of the buffered (non-stream) branch is
        unaffected.
        """
        raw = FakeCurlResponse(chunks=[b"ab", b"cd"])
        body = network._CurlResponseBody(raw, stream=True)

        assert list(body.stream(65536)) == [b"ab", b"cd"]
        assert raw.chunk_sizes == [None]

    def test_buffered_body_still_honors_the_requested_chunk_size(self) -> None:
        raw = FakeCurlResponse(content=b"abcdef")
        body = network._CurlResponseBody(raw, stream=False)
        assert list(body.stream(2)) == [b"ab", b"cd", b"ef"]

    def test_stream_error_that_is_not_a_curl_error_propagates(self) -> None:
        raw = FakeCurlResponse(chunks=[b"abc"], stream_error=ValueError("bug"))
        body = network._CurlResponseBody(raw, stream=True)
        with pytest.raises(ValueError):
            list(body.stream(64))


# ============================================================================
# Plain-requests regression
# ============================================================================


class TestPlainClientUnchanged:
    """With impersonation off, the session is built exactly as before."""

    def test_default_session_is_a_plain_requests_session(self) -> None:
        with patch("api.core.network.get_network_config", return_value={}):
            session = build_session()

        assert type(session) is requests.Session
        assert "Mozilla/5.0" in session.headers["User-Agent"]
        assert session.headers["Accept"] == "*/*"
        assert session.headers["Accept-Language"] == "en-US,en;q=0.9"

    def test_default_session_keeps_its_retry_policy(self) -> None:
        with patch("api.core.network.get_network_config", return_value={}):
            session = build_session()

        adapter = session.get_adapter("https://example.org")
        assert isinstance(adapter, HTTPAdapter)
        retries = adapter.max_retries
        assert retries.total == 2
        assert retries.connect == 0
        assert retries.read == 2
        assert retries.status == 0
        assert not retries.status_forcelist

    def test_build_session_without_provider_reads_no_config(self) -> None:
        """The default path must not depend on provider configuration at all."""
        with patch("api.core.network.get_network_config") as net_cfg:
            build_session()
        net_cfg.assert_not_called()

    def test_curl_cffi_is_never_imported_when_no_provider_opts_in(self) -> None:
        with (
            patch("api.core.network.get_network_config", return_value={}),
            patch("api.core.network._load_curl_requests") as loader,
        ):
            get_session("bne")
            get_session()
        loader.assert_not_called()
