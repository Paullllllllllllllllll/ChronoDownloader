"""Browser impersonation through curl_cffi.

Some providers reject the requests client on its TLS and HTTP/2 fingerprint
(Gallica and BNE answer it with a Cloudflare 403). ``curl_cffi`` reproduces a
browser's fingerprint; it is imported only when a provider names a profile in
``network.impersonate``, and a broken installation falls back to the plain
client with a warning.

The swap is invisible downstream: :class:`ImpersonatingSession` is a
``requests.Session`` whose responses are genuine ``requests.Response``
objects reading from the curl body, and every curl_cffi error, which lives in
a parallel hierarchy, is translated into its requests equivalent.

Copied from ChronoDownloader v1.30.3 ``api/core/network.py`` (lines 633-1114)
on 19.10.2026. Changed: the curl module is passed in as a
:class:`CurlBackend`, so the seam catches curl_cffi's own base error instead
of ``Exception``; no module-level session cache or warning registry; the
``impersonate`` setting is a profile name or None.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, get_args

import requests
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers

__all__ = [
    "CurlBackend",
    "ImpersonatingSession",
    "adapt_curl_response",
    "build_impersonating_session",
    "load_curl_backend",
    "translate_curl_error",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurlBackend:
    """The parts of curl_cffi the seam uses.

    ``make_session`` builds a ``curl_cffi.requests.Session`` impersonating a
    profile; ``error_type`` is curl_cffi's base error; ``browsers`` holds the
    profile names it ships, empty when this build lists none.
    """

    make_session: Callable[[str], Any]
    error_type: type[Exception]
    browsers: frozenset[str] = frozenset()


def load_curl_backend() -> CurlBackend | None:
    """Import curl_cffi, or return None when it is not installed."""
    try:
        import curl_cffi
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None
    module: Any = curl_requests
    try:
        browsers = frozenset(str(name) for name in get_args(module.BrowserTypeLiteral))
    except AttributeError:
        browsers = frozenset()

    def make_session(target: str) -> Any:
        return module.Session(impersonate=target)

    error_type: type[Exception] = curl_cffi.CurlError
    return CurlBackend(make_session, error_type, browsers)


# curl_cffi exception class name -> requests equivalent. Matching runs over the
# raised class's MRO, so the most specific entry wins (curl_cffi's
# ConnectTimeout derives from both its ConnectionError and its Timeout).
_CURL_ERROR_CLASS_MAP: Mapping[str, type[requests.exceptions.RequestException]] = {
    "ConnectTimeout": requests.exceptions.ConnectTimeout,
    "ReadTimeout": requests.exceptions.ReadTimeout,
    "Timeout": requests.exceptions.Timeout,
    "CertificateVerifyError": requests.exceptions.SSLError,
    "SSLError": requests.exceptions.SSLError,
    "ProxyError": requests.exceptions.ProxyError,
    "TooManyRedirects": requests.exceptions.TooManyRedirects,
    "ContentDecodingError": requests.exceptions.ContentDecodingError,
    # A truncated body is a ChunkedEncodingError in requests; curl_cffi files
    # IncompleteRead under its HTTPError, so it precedes the HTTPError entry.
    "IncompleteRead": requests.exceptions.ChunkedEncodingError,
    "ChunkedEncodingError": requests.exceptions.ChunkedEncodingError,
    "InvalidURL": requests.exceptions.InvalidURL,
    "InvalidSchema": requests.exceptions.InvalidSchema,
    "MissingSchema": requests.exceptions.MissingSchema,
    "InvalidHeader": requests.exceptions.InvalidHeader,
    "HTTPError": requests.exceptions.HTTPError,
    "DNSError": requests.exceptions.ConnectionError,
    "ConnectionError": requests.exceptions.ConnectionError,
    "RequestException": requests.exceptions.RequestException,
}

_CURL_COULDNT_RESOLVE_HOST = 6
_CURL_COULDNT_CONNECT = 7
_CURL_PARTIAL_FILE = 18
_CURL_OPERATION_TIMEDOUT = 28

# libcurl result code -> requests equivalent, consulted only when the class
# walk reached the catch-all entry: curl_cffi's streaming path wraps every
# transport failure as a bare RequestException, and the code survives that.
_CURL_ERROR_CODE_MAP: Mapping[int, type[requests.exceptions.RequestException]] = {
    _CURL_COULDNT_RESOLVE_HOST: requests.exceptions.ConnectionError,
    _CURL_COULDNT_CONNECT: requests.exceptions.ConnectionError,
    _CURL_PARTIAL_FILE: requests.exceptions.ChunkedEncodingError,
    _CURL_OPERATION_TIMEDOUT: requests.exceptions.Timeout,
    35: requests.exceptions.SSLError,  # SSL_CONNECT_ERROR
    53: requests.exceptions.SSLError,  # SSL_ENGINE_NOTFOUND
    54: requests.exceptions.SSLError,  # SSL_ENGINE_SETFAILED
    58: requests.exceptions.SSLError,  # SSL_CERTPROBLEM
    59: requests.exceptions.SSLError,  # SSL_CIPHER
    60: requests.exceptions.SSLError,  # PEER_FAILED_VERIFICATION
    64: requests.exceptions.SSLError,  # USE_SSL_FAILED
    66: requests.exceptions.SSLError,  # SSL_ENGINE_INITFAILED
    77: requests.exceptions.SSLError,  # SSL_CACERT_BADFILE
    80: requests.exceptions.SSLError,  # SSL_SHUTDOWN_FAILED
    82: requests.exceptions.SSLError,  # SSL_CRL_BADFILE
    83: requests.exceptions.SSLError,  # SSL_ISSUER_ERROR
    90: requests.exceptions.SSLError,  # SSL_PINNEDPUBKEYNOTMATCH
    91: requests.exceptions.SSLError,  # SSL_INVALIDCERTSTATUS
}

# curl reports connect-phase and read-phase timeouts under code 28; only the
# connect-phase one earns the short attempt budget.
_CURL_CONNECT_PHASE_MARKERS = (
    "failed to connect",
    "couldn't connect",
    "connection timed out",
    "connection timeout",
)


def _curl_error_code(exc: BaseException) -> int:
    """Return the libcurl result code a curl_cffi error carries (0 if none)."""
    try:
        return int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_curl_message(exc: BaseException, code: int) -> str:
    """Restate a curl error so the DNS and connect-failure markers match.

    The classification reads name-resolution and connect failures from
    urllib3's message text; curl phrases both differently.
    """
    text = str(exc)
    if code == _CURL_COULDNT_RESOLVE_HOST or type(exc).__name__ == "DNSError":
        return f"NameResolutionError: Failed to resolve host [curl {code}]: {text}"
    if code == _CURL_COULDNT_CONNECT:
        return (
            "NewConnectionError: Failed to establish a new connection "
            f"[curl {code}]: {text}"
        )
    return text


def translate_curl_error(
    exc: BaseException,
) -> requests.exceptions.RequestException | None:
    """Return the requests equivalent of a curl_cffi transport error.

    Returns None when *exc* is not a curl_cffi error (a requests exception
    already, or unrelated); the caller re-raises it untouched.
    """
    if isinstance(exc, requests.exceptions.RequestException):
        return None
    target: type[requests.exceptions.RequestException] | None = None
    for klass in type(exc).__mro__:
        target = _CURL_ERROR_CLASS_MAP.get(klass.__name__)
        if target is not None:
            break
    if target is None:
        return None
    code = _curl_error_code(exc)
    if target is requests.exceptions.RequestException and code:
        target = _CURL_ERROR_CODE_MAP.get(code, target)
    if (
        target is requests.exceptions.Timeout
        and code == _CURL_OPERATION_TIMEDOUT
        and any(marker in str(exc).lower() for marker in _CURL_CONNECT_PHASE_MARKERS)
    ):
        target = requests.exceptions.ConnectTimeout
    return target(_normalize_curl_message(exc, code))


def _translated(exc: Exception) -> Exception:
    """Return the requests equivalent of *exc*, or *exc* itself."""
    mapped = translate_curl_error(exc)
    return exc if mapped is None else mapped


class _CurlResponseBody:
    """urllib3-shaped ``raw`` object feeding a requests.Response from curl.

    ``requests.Response`` reads its body through ``raw.stream()``, the one
    place a mid-stream curl error surfaces; it is translated here.
    """

    def __init__(self, raw: Any, stream: bool, error_type: type[Exception]) -> None:
        self._raw = raw
        self._stream = stream
        self._error_type = error_type

    def stream(
        self, amt: int | None = None, decode_content: bool = True
    ) -> Iterator[bytes]:
        """Yield the body in *amt*-sized chunks (curl decodes transparently)."""
        try:
            if self._stream:
                # curl_cffi's iter_content takes no chunk size and warns when
                # one is passed; amt is only a hint.
                yield from self._raw.iter_content()
                return
            content = self._raw.content or b""
            if not amt or amt <= 0:
                if content:
                    yield content
                return
            for start in range(0, len(content), amt):
                yield content[start : start + amt]
        except self._error_type as exc:
            raise _translated(exc) from exc

    def close(self) -> None:
        """Release the underlying curl response."""
        try:
            self._raw.close()
        except self._error_type as exc:
            logger.debug("closing a curl response failed: %s", exc)


def adapt_curl_response(
    raw: Any, url: str, stream: bool, error_type: type[Exception]
) -> requests.Response:
    """Wrap a curl_cffi response in a real ``requests.Response``.

    Raises:
        requests.exceptions.ConnectionError: *raw* carries no usable HTTP
            status; a fabricated status 0 would pass as a success.
    """
    raw_status = getattr(raw, "status_code", None)
    try:
        status = int(raw_status or 0)
    except (TypeError, ValueError) as exc:
        raise requests.exceptions.ConnectionError(
            f"curl_cffi response for {url} carries an unusable status code "
            f"{raw_status!r}"
        ) from exc
    if status <= 0:
        raise requests.exceptions.ConnectionError(
            f"curl_cffi response for {url} carries no HTTP status code ({raw_status!r})"
        )
    response = requests.Response()
    response.status_code = status
    headers: Any = getattr(raw, "headers", None) or {}
    items = headers.items() if hasattr(headers, "items") else []
    response.headers = CaseInsensitiveDict({str(k): str(v) for k, v in items})
    response.url = str(getattr(raw, "url", "") or url)
    reason = getattr(raw, "reason", "")
    response.reason = str(reason) if reason else ""
    # curl_cffi's ``encoding`` always answers with a string; ``charset_encoding``
    # stays None unless the Content-Type declared a charset, as in requests.
    charset = getattr(raw, "charset_encoding", None)
    response.encoding = (
        charset
        if isinstance(charset, str) and charset
        else get_encoding_from_headers(response.headers)
    )
    response.raw = _CurlResponseBody(raw, stream, error_type)
    return response


_FORWARDED_REQUEST_KWARGS = frozenset(
    {"stream", "params", "headers", "timeout", "verify", "allow_redirects"}
)
"""The only request arguments forwarded to curl_cffi; anything else would be
dropped silently, so it is refused."""


class ImpersonatingSession(requests.Session):
    """A ``requests.Session`` that sends its requests through curl_cffi."""

    def __init__(self, backend: CurlBackend, target: str) -> None:
        super().__init__()
        self.trust_env = False
        self.impersonate = target
        self._error_type = backend.error_type
        self._curl_session = backend.make_session(target)
        # curl_cffi sends the complete header set of the browser it
        # impersonates; a User-Agent of ours would contradict the fingerprint.
        self.headers.clear()

    def request(  # type: ignore[override]
        self, method: str | bytes, url: str | bytes, **kwargs: Any
    ) -> requests.Response:
        """Send one request through curl_cffi, in requests' vocabulary.

        Raises:
            TypeError: A keyword argument curl_cffi would not receive.
        """
        unsupported = sorted(set(kwargs) - _FORWARDED_REQUEST_KWARGS)
        if unsupported:
            raise TypeError(
                "ImpersonatingSession.request does not forward "
                f"{', '.join(unsupported)} to curl_cffi; supported keywords are "
                f"{', '.join(sorted(_FORWARDED_REQUEST_KWARGS))}"
            )
        url_text = url.decode() if isinstance(url, bytes) else str(url)
        method_text = method.decode() if isinstance(method, bytes) else str(method)
        stream = bool(kwargs.get("stream") or False)
        call: dict[str, Any] = {"stream": stream}
        for key in ("params", "headers", "timeout", "verify", "allow_redirects"):
            value = kwargs.get(key)
            if value is not None:
                call[key] = value
        try:
            raw = self._curl_session.request(method_text, url_text, **call)
        except self._error_type as exc:
            raise _translated(exc) from exc
        return adapt_curl_response(raw, url_text, stream, self._error_type)

    def close(self) -> None:
        """Close the curl session alongside the requests one."""
        try:
            self._curl_session.close()
        except self._error_type as exc:
            logger.debug("closing a curl session failed: %s", exc)
        super().close()


def build_impersonating_session(
    backend: CurlBackend | None, provider: str, target: str
) -> requests.Session | None:
    """Return a curl_cffi session impersonating *target*, or None.

    None, with a warning, when curl_cffi is missing, names no such profile, or
    fails to start; the caller then uses the plain session.
    """
    if backend is None:
        logger.warning(
            "%s is set to impersonate %r but curl_cffi is not installed; using "
            "the standard HTTP client, which this provider may reject. Install "
            "it with: uv sync",
            provider,
            target,
        )
        return None
    if backend.browsers and target not in backend.browsers:
        logger.warning(
            "curl_cffi knows no browser profile %r (network.impersonate of %s); "
            "using the standard HTTP client, which this provider may reject",
            target,
            provider,
        )
        return None
    try:
        session = ImpersonatingSession(backend, target)
    except backend.error_type as exc:
        logger.warning(
            "could not start a curl_cffi session impersonating %r for %s: %s; "
            "using the standard HTTP client",
            target,
            provider,
            exc,
        )
        return None
    logger.info("using curl_cffi impersonation (%s) for %s", target, provider)
    return session
