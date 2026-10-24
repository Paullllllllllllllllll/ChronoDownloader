"""Tests for the curl_cffi seam in net/impersonation.py and its use by Network.

curl_cffi is faked: the point of the seam is that nothing downstream can tell
which client served a request. Re-expresses v1.30.3
``tests/unit/test_impersonation.py`` (``TestSessionSelection``,
``TestTranslateCurlError``, ``TestTranslateBareCurlRequestException``,
``TestTranslatedErrorsReachTheRightBranch``) against the new API.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import requests

from chronodownloader.net import Cause, Network, RequestFailed
from chronodownloader.net.classify import (
    CONNECT_FAILURE_MAX_ATTEMPTS,
    connect_aware_attempt_cap,
    is_connect_failure,
)
from chronodownloader.net.impersonation import (
    CurlBackend,
    ImpersonatingSession,
    build_impersonating_session,
    load_curl_backend,
    translate_curl_error,
)
from chronodownloader.settings import PROVIDER_DEFAULTS, Settings

from .conftest import NetworkFactory
from .support import SLOT, FakeCoordinator, settings_with


class CurlErrors:
    """Stand-ins with curl_cffi 0.15's exception names and inheritance."""

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
    """Minimal stand-in for a curl_cffi response."""

    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
        chunks: list[bytes] | None = None,
        stream_error: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.url = "https://datos.bne.es/resource"
        self.reason = "OK"
        self.charset_encoding: str | None = None
        self._chunks = chunks
        self._stream_error = stream_error

    def iter_content(self) -> Iterator[bytes]:
        yield from self._chunks if self._chunks is not None else [self.content]
        if self._stream_error is not None:
            raise self._stream_error

    def close(self) -> None:
        """Nothing to release."""


class FakeCurlSession:
    """Minimal stand-in for ``curl_cffi.requests.Session``."""

    def __init__(
        self,
        response: FakeCurlResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.response = response
        self.error = error
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> FakeCurlResponse:
        self.calls.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response or FakeCurlResponse()

    def close(self) -> None:
        self.closed = True


BROWSERS = frozenset({"chrome", "chrome124", "safari17_0", "firefox135"})


def backend(
    session: FakeCurlSession, constructed: list[str] | None = None
) -> CurlBackend:
    def make(target: str) -> FakeCurlSession:
        if constructed is not None:
            constructed.append(target)
        return session

    return CurlBackend(make, CurlErrors.CurlError, BROWSERS)


class TestSessionSelection:
    def test_a_configured_profile_yields_a_curl_session(self) -> None:
        constructed: list[str] = []
        session = build_impersonating_session(
            backend(FakeCurlSession(), constructed), "bne", "chrome"
        )
        assert isinstance(session, ImpersonatingSession)
        assert session.impersonate == "chrome"
        assert constructed == ["chrome"]
        assert "User-Agent" not in session.headers

    def test_missing_curl_falls_back_with_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="chronodownloader.net"):
            assert build_impersonating_session(None, "bne", "chrome") is None
        assert any("uv sync" in r.getMessage() for r in caplog.records)

    def test_an_unknown_profile_falls_back_before_any_request(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        constructed: list[str] = []
        with caplog.at_level(logging.WARNING, logger="chronodownloader.net"):
            session = build_impersonating_session(
                backend(FakeCurlSession(), constructed), "bne", "netscape"
            )
        assert session is None
        assert constructed == []
        assert any("netscape" in r.getMessage() for r in caplog.records)

    def test_profiles_are_not_checked_when_curl_lists_none(self) -> None:
        unlisted = CurlBackend(lambda target: FakeCurlSession(), CurlErrors.CurlError)
        assert isinstance(
            build_impersonating_session(unlisted, "bne", "chrome999"),
            ImpersonatingSession,
        )

    def test_a_failing_constructor_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def explode(target: str) -> FakeCurlSession:
            raise CurlErrors.ImpersonateError(f"cannot start {target}")

        broken = CurlBackend(explode, CurlErrors.CurlError, BROWSERS)
        with caplog.at_level(logging.WARNING, logger="chronodownloader.net"):
            assert build_impersonating_session(broken, "bne", "chrome") is None
        assert any("cannot start chrome" in r.getMessage() for r in caplog.records)

    def test_only_the_configured_providers_use_curl(
        self, make_network: NetworkFactory
    ) -> None:
        client = make_network(
            settings_with(), curl_loader=lambda: backend(FakeCurlSession())
        )
        assert isinstance(client.session_for("bne"), ImpersonatingSession)
        assert isinstance(client.session_for("bnf_gallica"), ImpersonatingSession)
        assert not isinstance(client.session_for("mdz"), ImpersonatingSession)
        assert client.session_for("mdz") is client.session_for("loc")

    def test_curl_is_not_loaded_without_a_configured_profile(
        self, make_network: NetworkFactory
    ) -> None:
        def loader() -> CurlBackend | None:
            raise AssertionError("curl_cffi loaded")

        providers = {
            key: replace(block, network=replace(block.network, impersonate=None))
            for key, block in PROVIDER_DEFAULTS.items()
        }
        make_network(
            Settings(providers=MappingProxyType(providers)), curl_loader=loader
        )

    @pytest.mark.real_curl
    def test_the_real_backend_loads_when_installed(self) -> None:
        pytest.importorskip("curl_cffi")
        loaded = load_curl_backend()
        assert loaded is not None
        assert "chrome" in loaded.browsers


class TestTranslateCurlError:
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
            (CurlErrors.ContentDecodingError, requests.exceptions.ContentDecodingError),
            (CurlErrors.InvalidURL, requests.exceptions.InvalidURL),
            (CurlErrors.ImpersonateError, requests.exceptions.RequestException),
        ],
    )
    def test_class_mapping(
        self, curl_class: type[Exception], expected: type[Exception]
    ) -> None:
        assert type(translate_curl_error(curl_class("boom"))) is expected

    def test_non_curl_and_requests_exceptions_are_left_alone(self) -> None:
        assert translate_curl_error(ValueError("not a transport error")) is None
        assert translate_curl_error(requests.exceptions.ConnectionError("x")) is None

    def test_dns_failure_message_carries_the_resolution_markers(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.DNSError("Could not resolve host: datos.bne.es", code=6)
        )
        assert mapped is not None
        message = str(mapped).lower()
        assert "nameresolutionerror" in message
        assert "could not resolve host: datos.bne.es" in message
        assert is_connect_failure(mapped) is False

    def test_refused_connection_gets_the_short_attempt_budget(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.ConnectionError(
                "Failed to connect to bnedigital.bne.es port 443", code=7
            )
        )
        assert mapped is not None
        assert connect_aware_attempt_cap(mapped, 25) == CONNECT_FAILURE_MAX_ATTEMPTS

    def test_read_error_keeps_the_full_attempt_budget(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.ConnectionError("Recv failure: Connection was reset", code=56)
        )
        assert mapped is not None
        assert connect_aware_attempt_cap(mapped, 25) == 25

    def test_connect_phase_timeout_becomes_a_connect_timeout(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.Timeout(
                "Failed to connect to datos.bne.es port 443 after 10000 ms: "
                "Timeout was reached",
                code=28,
            )
        )
        assert type(mapped) is requests.exceptions.ConnectTimeout

    def test_read_phase_timeout_stays_a_plain_timeout(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.Timeout("Operation timed out after 30000 ms", code=28)
        )
        assert type(mapped) is requests.exceptions.Timeout


class TestTranslateBareCurlRequestException:
    """curl_cffi's streaming path erases the class; the result code carries it."""

    @pytest.mark.parametrize(
        "code", [35, 53, 54, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91]
    )
    def test_ssl_result_codes_become_ssl_errors(self, code: int) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("SSL peer certificate problem", code=code)
        )
        assert type(mapped) is requests.exceptions.SSLError

    def test_connect_phase_timeout_code(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException(
                "Failed to connect to x port 443 after 10000 ms", code=28
            )
        )
        assert type(mapped) is requests.exceptions.ConnectTimeout

    def test_partial_file_code(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("transfer closed with bytes remaining", code=18)
        )
        assert type(mapped) is requests.exceptions.ChunkedEncodingError

    def test_resolution_code(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("Could not resolve host: datos.bne.es", code=6)
        )
        assert type(mapped) is requests.exceptions.ConnectionError
        assert "nameresolutionerror" in str(mapped).lower()

    def test_connect_refused_code(self) -> None:
        mapped = translate_curl_error(
            CurlErrors.RequestException("Failed to connect: refused", code=7)
        )
        assert mapped is not None and is_connect_failure(mapped)

    def test_unmapped_and_missing_codes_stay_generic(self) -> None:
        for exc in (
            CurlErrors.RequestException("something curl-specific", code=23),
            CurlErrors.RequestException("no code at all"),
        ):
            assert (
                type(translate_curl_error(exc)) is requests.exceptions.RequestException
            )

    def test_a_named_class_is_never_overridden_by_its_code(self) -> None:
        mapped = translate_curl_error(CurlErrors.HTTPError("bad status", code=60))
        assert type(mapped) is requests.exceptions.HTTPError


def _run(
    make_network: NetworkFactory,
    error: BaseException | None = None,
    response: FakeCurlResponse | None = None,
    **network: Any,
) -> tuple[FakeCurlSession, Network]:
    """Return a client whose provider bne sends through a fake curl session."""
    network.setdefault("max_attempts", 3)
    curl_session = FakeCurlSession(response=response, error=error)
    client = make_network(
        settings_with("bne", impersonate="chrome", **network),
        curl_loader=lambda: backend(curl_session),
    )
    return curl_session, client


def _fail(client: Network) -> RequestFailed:
    with pytest.raises(RequestFailed) as excinfo:
        client.request("bne", "GET", "https://datos.bne.es/sparql")
    return excinfo.value


class TestTranslatedErrorsReachTheRightBranch:
    """Each mapped class lands where its requests twin would."""

    def test_dns_failure_is_not_retried_and_feeds_the_breaker(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        curl, client = _run(
            make_network,
            CurlErrors.DNSError("Could not resolve host: datos.bne.es", code=6),
        )
        assert _fail(client).cause is Cause.DNS
        assert len(curl.calls) == 1
        assert coordinator.failures == [("bne", "dns")]

    def test_refused_connection_stops_after_the_short_budget(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        curl, client = _run(
            make_network,
            CurlErrors.ConnectionError("Failed to connect: refused", code=7),
            max_attempts=25,
        )
        assert _fail(client).cause is Cause.TRANSIENT
        assert len(curl.calls) == CONNECT_FAILURE_MAX_ATTEMPTS
        assert coordinator.failures == [("bne", "transient")] * 2

    def test_read_error_uses_the_whole_budget(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        curl, client = _run(
            make_network,
            CurlErrors.ConnectionError("Recv failure: Connection was reset", code=56),
        )
        _fail(client)
        assert len(curl.calls) == 3
        assert coordinator.failures == [("bne", "transient")] * 3

    def test_read_timeout_uses_the_whole_budget(
        self, make_network: NetworkFactory
    ) -> None:
        curl, client = _run(
            make_network,
            CurlErrors.Timeout("Operation timed out after 30000 ms", code=28),
        )
        _fail(client)
        assert len(curl.calls) == 3

    def test_connect_timeout_stops_after_the_short_budget(
        self, make_network: NetworkFactory
    ) -> None:
        curl, client = _run(
            make_network,
            CurlErrors.Timeout(
                "Failed to connect to datos.bne.es port 443 after 10000 ms", code=28
            ),
            max_attempts=25,
        )
        _fail(client)
        assert len(curl.calls) == CONNECT_FAILURE_MAX_ATTEMPTS

    def test_certificate_error_aborts_and_feeds_the_breaker(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        curl, client = _run(
            make_network,
            CurlErrors.CertificateVerifyError("certificate verify failed", code=60),
        )
        assert _fail(client).cause is Cause.SSL
        assert len(curl.calls) == 1
        assert coordinator.failures == [("bne", "ssl")]
        assert coordinator.successes == []

    def test_impersonated_sessions_never_retry_unverified(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        # curl_cffi keeps its own cookie jar, which the credential guard cannot
        # see, so the unverified retry is refused even under the policy.
        curl, client = _run(
            make_network,
            CurlErrors.SSLError("certificate verify failed", code=60),
            ssl_error_policy="retry_insecure_once",
        )
        assert _fail(client).cause is Cause.SSL
        assert [call[2]["verify"] for call in curl.calls] == [True]
        assert coordinator.failures == [("bne", "ssl")]

    def test_stream_wrapped_ssl_error_is_classified_as_ssl(
        self, make_network: NetworkFactory
    ) -> None:
        curl, client = _run(
            make_network,
            CurlErrors.RequestException("SSL peer certificate problem", code=60),
            ssl_error_policy="retry_insecure_once",
        )
        assert _fail(client).cause is Cause.SSL
        assert len(curl.calls) == 1

    def test_unknown_error_is_retried_like_any_transport_error(
        self, coordinator: FakeCoordinator, make_network: NetworkFactory
    ) -> None:
        curl, client = _run(
            make_network,
            CurlErrors.RequestException("something curl-specific went wrong", code=23),
        )
        _fail(client)
        assert len(curl.calls) == 3
        assert coordinator.failures == [("bne", "transient")] * 3


class TestAdaptedResponses:
    def test_a_json_response_is_parsed(self, make_network: NetworkFactory) -> None:
        _curl, client = _run(
            make_network,
            response=FakeCurlResponse(
                headers={"Content-Type": "application/json"}, content=b'{"ok": true}'
            ),
        )
        assert client.request("bne", "GET", "https://datos.bne.es/x").json() == {
            "ok": True
        }

    def test_a_streamed_download_through_curl(
        self, tmp_path: Path, make_network: NetworkFactory
    ) -> None:
        body = b"%PDF-1.4\n" + b"y" * 1000
        curl, client = _run(
            make_network,
            response=FakeCurlResponse(
                headers={"Content-Type": "application/pdf"},
                chunks=[body[:10], body[10:]],
            ),
        )
        outcome = client.download(
            "bne", "https://bdh.bne.es/b.pdf", tmp_path / "b.pdf.part", slot=SLOT
        )
        assert outcome.path.read_bytes() == body
        assert curl.calls[0][2]["stream"] is True

    def test_a_midstream_curl_error_is_retried_and_cleaned_up(
        self, tmp_path: Path, make_network: NetworkFactory
    ) -> None:
        dropping = FakeCurlResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[b"%PDF-1.4 partial"],
            stream_error=CurlErrors.RequestException("transfer closed", code=18),
        )
        curl, client = _run(make_network, response=dropping, max_attempts=2)
        with pytest.raises(RequestFailed) as excinfo:
            client.download(
                "bne", "https://bdh.bne.es/b.pdf", tmp_path / "b.pdf.part", slot=SLOT
            )
        assert excinfo.value.cause is Cause.TRANSIENT
        assert len(curl.calls) == 2
        assert list(tmp_path.iterdir()) == []

    def test_unsupported_keywords_are_refused(self) -> None:
        session = ImpersonatingSession(backend(FakeCurlSession()), "chrome")
        with pytest.raises(TypeError, match="json"):
            session.request("POST", "https://x.example", json={})
