"""Network utilities for HTTP requests, rate limiting, and session management.

Provides centralized HTTP session with retries, per-provider rate limiting,
circuit breaker pattern, and robust error handling for API calls and file downloads.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, cast
from urllib.parse import urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry

from .config import get_network_config

logger = logging.getLogger(__name__)

# =============================================================================
# Circuit Breaker Pattern
# =============================================================================


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Provider disabled due to failures
    HALF_OPEN = "half_open"  # Testing if provider recovered


@dataclass
class CircuitBreaker:
    """Thread-safe circuit breaker for a single provider.

    Tracks consecutive failures and temporarily disables providers that are
    consistently failing (e.g., due to rate limiting or 5xx storms). All
    state transitions are guarded by an internal lock so concurrent download
    workers observe a consistent state.
    """

    failure_threshold: int = 3  # Consecutive failures before opening
    cooldown_seconds: float = 300.0  # How long circuit stays open (5 min default)

    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = field(default=0)
    opened_at: float = field(default=0.0)
    probe_started_at: float = field(default=0.0)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def record_success(self) -> None:
        """Record a successful request - resets failure count and closes circuit."""
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                logger.info("Circuit breaker: Provider recovered, closing circuit")
            self.failure_count = 0
            self.state = CircuitState.CLOSED

    def record_failure(self, provider: str = "") -> None:
        """Record a failure (429, 5xx storm, connection failure). May open circuit."""
        with self._lock:
            self.failure_count += 1

            if self.state == CircuitState.HALF_OPEN:
                # Failed during test - reopen circuit
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
                logger.warning(
                    "Circuit breaker: %s still failing, reopening circuit for %.0fs",
                    provider or "Provider",
                    self.cooldown_seconds,
                )
            elif (
                self.failure_count >= self.failure_threshold
                and self.state == CircuitState.CLOSED
            ):
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
                logger.warning(
                    "Circuit breaker: %s hit %d consecutive failures, "
                    "disabling for %.0fs",
                    provider or "Provider",
                    self.failure_count,
                    self.cooldown_seconds,
                )

    def reconfigure(self, failure_threshold: int, cooldown_seconds: float) -> None:
        """Apply refreshed config values under the breaker's own lock.

        Args:
            failure_threshold: Consecutive failures before the circuit opens
            cooldown_seconds: How long the circuit stays open
        """
        with self._lock:
            self.failure_threshold = failure_threshold
            self.cooldown_seconds = cooldown_seconds

    def allow_request(self) -> bool:
        """Check if a request should be allowed.

        Returns:
            True if request can proceed, False if circuit is open
        """
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                # Check if cooldown has elapsed
                elapsed = time.monotonic() - self.opened_at
                if elapsed >= self.cooldown_seconds:
                    self.state = CircuitState.HALF_OPEN
                    self.probe_started_at = time.monotonic()
                    logger.info(
                        "Circuit breaker: Cooldown elapsed (%.0fs), testing provider",
                        elapsed,
                    )
                    return True
                return False

            # HALF_OPEN: admit a single probe request. Concurrent workers that
            # arrive while the probe is in flight are denied, so a provider
            # that just tripped the breaker is not hit by a burst at the
            # recovery moment. If the probe never records an outcome (e.g. the
            # worker died), allow a fresh probe after another cooldown period.
            if time.monotonic() - self.probe_started_at >= self.cooldown_seconds:
                self.probe_started_at = time.monotonic()
                return True
            return False

    def is_available(self) -> bool:
        """Report availability without mutating breaker state.

        Unlike ``allow_request``, this neither performs the OPEN -> HALF_OPEN
        transition nor consumes the single half-open probe slot, so it is safe
        for passive "should I enqueue?" checks.
        """
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                return time.monotonic() - self.opened_at >= self.cooldown_seconds
            return True  # HALF_OPEN: a request may be admitted

    def time_until_retry(self) -> float:
        """Get seconds until circuit will allow requests again.

        Returns:
            Seconds remaining, or 0 if requests are allowed
        """
        with self._lock:
            if self.state == CircuitState.OPEN:
                elapsed = time.monotonic() - self.opened_at
                return max(0.0, self.cooldown_seconds - elapsed)
            if self.state == CircuitState.HALF_OPEN:
                # A denied caller in HALF_OPEN waits out the in-flight probe's
                # window, not zero seconds; callers log this value as the wait.
                elapsed = time.monotonic() - self.probe_started_at
                return max(0.0, self.cooldown_seconds - elapsed)
            return 0.0


# Non-retryable client errors, split by what they say about the provider.
#
# A resource error (400/404/410/422) is an answer about *this* URL: the
# transport and the provider are healthy, so it records a breaker success.
# Without that, a half-open probe spent on a permanently dead URL would leave
# the breaker stuck HALF_OPEN and throttle a working provider to one request
# per cooldown.
#
# A blanket rejection of the client (401/403) is different: the provider is
# refusing us, not the URL. It must feed the breaker, or a provider that
# rejects every request (a bot filter, a missing API key) is retried at full
# cost for the whole run and never trips its own breaker.
RESOURCE_ERROR_STATUSES = frozenset({400, 404, 410, 422})
CLIENT_BLOCKED_STATUSES = frozenset({401, 403})
NON_RETRYABLE_STATUSES = RESOURCE_ERROR_STATUSES | CLIENT_BLOCKED_STATUSES

# Attempts granted to a connection that was never established (DNS failure,
# refused connection, connect timeout). A host that drops SYNs costs the full
# timeout on every attempt, so paying the whole per-provider retry budget for
# it burns minutes per request and buys nothing; one retry covers a transient
# blip, and the breaker takes over from there. Errors raised *after* a
# connection was established (read timeouts, mid-stream resets) keep the full
# budget.
CONNECT_FAILURE_MAX_ATTEMPTS = 2

_CONNECT_FAILURE_MARKERS = (
    "newconnectionerror",
    "connecttimeouterror",
    "failed to establish a new connection",
    "connection refused",
    "actively refused",  # Windows WinError 10061
    "network is unreachable",
    "no route to host",
)


def is_connect_failure(exc: BaseException) -> bool:
    """Report whether an exception means the connection was never established.

    Args:
        exc: Exception raised by the HTTP layer

    Returns:
        True for connect-level death (refused, unreachable, connect timeout),
        False for failures that happened after a connection was up
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _CONNECT_FAILURE_MARKERS)


def connect_aware_attempt_cap(exc: BaseException, max_attempts: int) -> int:
    """Cap the retry budget for connect-level failures.

    Args:
        exc: Exception raised by the HTTP layer
        max_attempts: Configured per-provider attempt budget

    Returns:
        ``max_attempts``, or the shorter connect-failure budget
    """
    if is_connect_failure(exc):
        return min(max_attempts, CONNECT_FAILURE_MAX_ATTEMPTS)
    return max_attempts


def record_client_error(
    cb: CircuitBreaker | None, status_code: int, provider: str = ""
) -> None:
    """Feed a non-retryable client error to the breaker on the correct side.

    Args:
        cb: Circuit breaker for the provider, or None when disabled
        status_code: HTTP status that ended the request
        provider: Provider key, for log messages
    """
    if cb is None:
        return
    if status_code in CLIENT_BLOCKED_STATUSES:
        cb.record_failure(provider or "unknown")
    else:
        cb.record_success()


# Per-provider circuit breakers
_CIRCUIT_BREAKERS: dict[str, CircuitBreaker] = {}
_CIRCUIT_BREAKERS_LOCK = threading.Lock()


def get_circuit_breaker(provider_key: str | None) -> CircuitBreaker | None:
    """Get or create a circuit breaker for a provider.

    Args:
        provider_key: Provider identifier

    Returns:
        CircuitBreaker instance or None if circuit breaker disabled
    """
    if not provider_key:
        return None

    net = get_network_config(provider_key)

    # Check if circuit breaker is enabled for this provider
    if not net.get("circuit_breaker_enabled", True):
        return None

    threshold = int(net.get("circuit_breaker_threshold", 3) or 3)
    cooldown = float(net.get("circuit_breaker_cooldown_s", 300.0) or 300.0)

    with _CIRCUIT_BREAKERS_LOCK:
        cb = _CIRCUIT_BREAKERS.get(provider_key)
        if cb is None:
            cb = CircuitBreaker(failure_threshold=threshold, cooldown_seconds=cooldown)
            _CIRCUIT_BREAKERS[provider_key] = cb
        else:
            # Update settings if changed. Through reconfigure(), so the two
            # fields are not written outside the breaker's own lock while a
            # worker reads them inside it -- the class docstring promises
            # every transition is guarded, and this was the one writer that
            # was not.
            cb.reconfigure(threshold, cooldown)

    return cb


# Global session (lazy-initialized)
_SESSION: requests.Session | None = None

# Per-provider sessions for providers that opt into browser impersonation,
# stored as (impersonate target, session) so a changed profile rebuilds instead
# of serving the session built for the previous one.
_IMPERSONATED_SESSIONS: dict[str, tuple[str, requests.Session]] = {}
_IMPERSONATED_SESSIONS_LOCK = threading.Lock()

# Providers already warned about an unusable network.impersonate value.
_IMPERSONATE_TYPE_WARNED: set[str] = set()
_IMPERSONATE_TYPE_WARNED_LOCK = threading.Lock()

# Map URL hostnames to provider keys for rate limiting and policies
PROVIDER_HOST_MAP: dict[str, tuple[str, ...]] = {
    "gallica": ("gallica.bnf.fr",),
    "british_library": (
        "api.bl.uk",
        "sru.bl.uk",
        "iiif.bl.uk",
        "access.bl.uk",
        "bnb.data.bl.uk",
    ),
    "mdz": (
        "api.digitale-sammlungen.de",
        "www.digitale-sammlungen.de",
        "digitale-sammlungen.de",
    ),
    "europeana": ("api.europeana.eu", "iiif.europeana.eu"),
    "wellcome": ("api.wellcomecollection.org", "iiif.wellcomecollection.org"),
    "loc": ("www.loc.gov", "loc.gov", "tile.loc.gov", "iiif.loc.gov"),
    "ddb": (
        "api.deutsche-digitale-bibliothek.de",
        "iiif.deutsche-digitale-bibliothek.de",
        # DDB aggregates: its manifests live at the holding library, and
        # ddb.IIIF_MANIFEST_PATTERNS builds URLs on these two hosts. Without
        # them, every page image of such a work bypasses DDB's rate limiter
        # and circuit breaker entirely.
        "digi.ub.uni-heidelberg.de",
        "manifests.sub.uni-goettingen.de",
    ),
    "polona": ("polona.pl",),
    "bne": ("datos.bne.es", "bnedigital.bne.es", "bdh.bne.es"),
    "dpla": ("api.dp.la",),
    "internet_archive": ("archive.org", "archivelab.org", "iiif.archivelab.org"),
    "google_books": (
        "www.googleapis.com",
        "books.google.com",
        "books.googleusercontent.com",
        "play.google.com",
    ),
    "hathitrust": ("catalog.hathitrust.org", "babel.hathitrust.org"),
    "annas_archive": (
        "annas-archive.gl",
        "annas-archive.li",
        "annas-archive.pm",
        "annas-archive.in",
        "annas-archive.org",
    ),
    "slub": ("data.slub-dresden.de", "digital.slub-dresden.de", "iiif.slub-dresden.de"),
    "e_rara": ("www.e-rara.ch", "e-rara.ch"),
    "sbb_digital": (
        "sru.gbv.de",
        "digital.staatsbibliothek-berlin.de",
        "content.staatsbibliothek-berlin.de",
        "oai.sbb.berlin",
    ),
}


class RateLimiter:
    """Thread-safe per-provider rate limiter with jitter, using monotonic time.

    A lock serializes concurrent callers so the configured per-provider
    minimum interval is enforced even when multiple download workers hit the
    same provider at once.
    """

    def __init__(self, min_interval_s: float = 0.0, jitter_s: float = 0.0):
        self.min_interval_s = max(0.0, float(min_interval_s or 0.0))
        self.jitter_s = max(0.0, float(jitter_s or 0.0))
        self._last_ts = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Wait until the minimum interval has passed since the last request."""
        if self.min_interval_s <= 0 and self.jitter_s <= 0:
            return

        with self._lock:
            now = time.monotonic()
            # Next ready time is last_ts + base + random jitter
            jitter = random.uniform(0.0, self.jitter_s) if self.jitter_s > 0 else 0.0
            next_ready = self._last_ts + self.min_interval_s + jitter
            sleep_s = next_ready - now

            if sleep_s > 0:
                time.sleep(sleep_s)
                now = time.monotonic()

            self._last_ts = now


# Per-provider rate limiter instances
_RATE_LIMITERS: dict[str, RateLimiter] = {}
_RATE_LIMITERS_LOCK = threading.Lock()

# =============================================================================
# Rate Limiter Functions
# =============================================================================


def get_provider_for_url(url: str) -> str | None:
    """Determine the provider key for a given URL.

    Args:
        url: URL to check

    Returns:
        Provider key or None if not recognized
    """
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None

    # Strip port if present
    if ":" in host:
        host = host.split(":", 1)[0]

    # Match exact domain or subdomain of known host parts
    def _host_matches(h: str, part: str) -> bool:
        return h == part or h.endswith("." + part)

    for provider, host_parts in PROVIDER_HOST_MAP.items():
        for part in host_parts:
            if _host_matches(host, part):
                return provider

    return None


def get_rate_limiter(provider_key: str | None) -> RateLimiter | None:
    """Get or create a rate limiter for a provider.

    Args:
        provider_key: Provider identifier

    Returns:
        RateLimiter instance or None if no rate limiting configured
    """
    if not provider_key:
        return None

    net = get_network_config(provider_key)
    delay_s = float(net.get("delay_ms", 0) or 0) / 1000.0
    jitter_s = float(net.get("jitter_ms", 0) or 0) / 1000.0

    with _RATE_LIMITERS_LOCK:
        rl = _RATE_LIMITERS.get(provider_key)
        if rl is None or rl.min_interval_s != delay_s or rl.jitter_s != jitter_s:
            rl = RateLimiter(delay_s, jitter_s)
            _RATE_LIMITERS[provider_key] = rl

    return rl


# =============================================================================
# Optional browser impersonation (curl_cffi)
# =============================================================================
#
# A few providers reject this tool on its TLS/HTTP2 fingerprint rather than on
# anything it sends: BNE answers every request from the `requests` client with
# a Cloudflare 403 while the same request from a browser-fingerprinted client
# succeeds. `curl_cffi` reproduces such a fingerprint, and ships as an optional
# extra (`uv sync --extra impersonate`) that no provider uses unless its config
# opts in through `provider_settings.<provider>.network.impersonate`.
#
# The swap has to be invisible to everything downstream. Two things make it so:
# the session hands back a genuine `requests.Response` reading from the curl
# body (so status handling, `raise_for_status`, JSON/text decoding, and
# streaming keep requests' own semantics), and every curl_cffi error -- which
# lives in a parallel hierarchy that `except requests.exceptions.RequestException`
# would not catch -- is translated into its requests equivalent at this seam.

# Browser profile used when a provider enables impersonation without naming one.
DEFAULT_IMPERSONATE_TARGET = "chrome"

# curl_cffi exception class name -> requests equivalent. Matching runs over the
# raised class's MRO, so the most specific entry wins for free (curl_cffi's
# ConnectTimeout derives from both its ConnectionError and its Timeout, and its
# CertificateVerifyError from its SSLError). Names rather than the classes
# themselves: curl_cffi must not be imported when the extra is absent.
_CURL_ERROR_CLASS_MAP: dict[str, type[requests.exceptions.RequestException]] = {
    "ConnectTimeout": requests.exceptions.ConnectTimeout,
    "ReadTimeout": requests.exceptions.ReadTimeout,
    "Timeout": requests.exceptions.Timeout,
    "CertificateVerifyError": requests.exceptions.SSLError,
    "SSLError": requests.exceptions.SSLError,
    "ProxyError": requests.exceptions.ProxyError,
    "TooManyRedirects": requests.exceptions.TooManyRedirects,
    "ContentDecodingError": requests.exceptions.ContentDecodingError,
    # A truncated body is a ChunkedEncodingError in requests (urllib3's
    # ProtocolError), not an HTTPError; curl_cffi files IncompleteRead under
    # its HTTPError, so it has to be pulled out before the HTTPError entry.
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

# libcurl result codes whose message has to be rewritten, because the retry
# logic reads transport failure out of urllib3's message text.
_CURL_COULDNT_RESOLVE_HOST = 6
_CURL_COULDNT_CONNECT = 7
_CURL_OPERATION_TIMEDOUT = 28

# curl reports connect-phase and read-phase timeouts under the same result code
# (28) and never raises its own ConnectTimeout, so the phase is read off the
# message. requests separates the two, and only the connect-phase one earns the
# short attempt budget.
_CURL_CONNECT_PHASE_MARKERS = (
    "failed to connect",
    "couldn't connect",
    "connection timed out",
    "connection timeout",
)


def _curl_error_code(exc: BaseException) -> int:
    """Return the libcurl result code carried by a curl_cffi error (0 if none)."""
    try:
        return int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_curl_message(exc: BaseException, code: int) -> str:
    """Restate a curl error so the message-sniffing retry branches still fire.

    ``make_request`` recognizes a name-resolution failure, and
    ``is_connect_failure`` a connection that never came up, from the text
    urllib3 puts in the message. curl phrases both differently ("Could not
    resolve host", "Failed to connect to ... port 443"), so the equivalent
    markers are prefixed here; the original text is kept for the log.
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
    """Return the ``requests`` equivalent of a curl_cffi transport error.

    The retry loops, the circuit breaker, and the connect-failure cap all key
    off the ``requests`` exception hierarchy and off urllib3's message text.
    curl_cffi satisfies neither, so its errors are re-raised as the requests
    exception that carries the same meaning, with the message adjusted where
    the text itself is load-bearing.

    Args:
        exc: Exception raised by curl_cffi

    Returns:
        The equivalent requests exception, or None when ``exc`` is not a
        curl_cffi error (already a requests exception, or unrelated) and the
        caller should re-raise it untouched.
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
    if (
        target is requests.exceptions.Timeout
        and code == _CURL_OPERATION_TIMEDOUT
        and any(m in str(exc).lower() for m in _CURL_CONNECT_PHASE_MARKERS)
    ):
        target = requests.exceptions.ConnectTimeout

    return target(_normalize_curl_message(exc, code))


class _CurlResponseBody:
    """urllib3-shaped ``raw`` object that feeds a requests.Response from curl.

    ``requests.Response`` reads its body through ``raw.stream()``, which is the
    single place a mid-stream curl error can surface; translating here keeps
    the download loop's retry and refund path intact for a connection dropped
    halfway through a large PDF.
    """

    def __init__(self, raw: Any, stream: bool) -> None:
        self._raw = raw
        self._stream = stream

    def stream(
        self, amt: int | None = None, decode_content: bool = True
    ) -> Iterator[bytes]:
        """Yield the body in ``amt``-sized chunks (curl decodes transparently)."""
        try:
            if self._stream:
                yield from self._raw.iter_content(chunk_size=amt)
                return
            content = self._raw.content or b""
            if not amt or amt <= 0:
                if content:
                    yield content
                return
            for start in range(0, len(content), amt):
                yield content[start : start + amt]
        except Exception as exc:
            mapped = translate_curl_error(exc)
            if mapped is None:
                raise
            raise mapped from exc

    def close(self) -> None:
        """Release the underlying curl response."""
        with contextlib.suppress(Exception):
            self._raw.close()


def adapt_curl_response(raw: Any, url: str, stream: bool) -> requests.Response:
    """Wrap a curl_cffi response in a real ``requests.Response``.

    Args:
        raw: curl_cffi response object
        url: Requested URL, used when the response carries none
        stream: Whether the request was made with ``stream=True``

    Returns:
        A requests.Response whose body is read from ``raw``

    Raises:
        requests.exceptions.ConnectionError: When ``raw`` carries no usable
            HTTP status. Fabricating status 0 would sail through
            ``raise_for_status()`` and be recorded as a breaker *success*; a
            transport error instead flows into the retry/breaker machinery.
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

    resp = requests.Response()
    resp.status_code = status

    headers: Any = getattr(raw, "headers", None) or {}
    items = headers.items() if hasattr(headers, "items") else []
    resp.headers = CaseInsensitiveDict({str(k): str(v) for k, v in items})

    resp.url = str(getattr(raw, "url", "") or url)
    reason = getattr(raw, "reason", "")
    resp.reason = str(reason) if reason else ""

    # Fall back to requests' own header parsing when curl declares no charset,
    # so ``resp.text`` decodes a provider's XML/JSON exactly as it did before.
    encoding = getattr(raw, "encoding", None)
    resp.encoding = (
        encoding
        if isinstance(encoding, str)
        else get_encoding_from_headers(resp.headers)
    )

    resp.raw = _CurlResponseBody(raw, stream)
    return resp


# The only request arguments the seam forwards to curl_cffi. Anything else a
# caller passes (data, json, cookies, auth, proxies, cert, ...) would be dropped
# on the floor and the request sent without it, so it is refused outright.
_FORWARDED_REQUEST_KWARGS = frozenset(
    {"stream", "params", "headers", "timeout", "verify", "allow_redirects"}
)


class ImpersonatingSession(requests.Session):
    """A ``requests.Session`` that performs its requests through curl_cffi.

    Subclassing keeps the object a real session for every caller and type
    checker; only ``request`` is overridden, so ``get``/``head`` and the rest
    route through it unchanged.
    """

    impersonate: str

    def __init__(self, curl_requests: Any, impersonate: str) -> None:
        super().__init__()
        self.impersonate = impersonate
        self._curl_session = curl_requests.Session(impersonate=impersonate)
        # curl_cffi sends the complete, self-consistent header set of the
        # browser it impersonates. Layering this library's own User-Agent on
        # top would contradict the TLS fingerprint it just claimed, which is
        # exactly what the bot filter looks for. Per-call headers (a provider's
        # configured ones) are still passed through.
        self.headers.clear()

    def request(
        self, method: Any, url: Any, *args: Any, **kwargs: Any
    ) -> requests.Response:
        """Perform one request through curl_cffi, in requests' vocabulary."""
        if args:
            raise TypeError(
                "ImpersonatingSession.request accepts keyword arguments only"
            )
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
        except Exception as exc:
            mapped = translate_curl_error(exc)
            if mapped is None:
                raise
            raise mapped from exc

        return adapt_curl_response(raw, url_text, stream)

    def close(self) -> None:
        """Close the curl session alongside the requests one."""
        with contextlib.suppress(Exception):
            self._curl_session.close()
        super().close()


def _load_curl_requests() -> Any | None:
    """Import ``curl_cffi.requests``, or return None when the extra is absent."""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None
    return curl_requests


def _warn_unusable_impersonate_value(provider_key: str, raw: Any) -> None:
    """Warn once per provider about an ``impersonate`` value of the wrong type.

    ``get_session`` resolves the target on every request, so a per-call warning
    would flood the log; deduped per provider like the missing-extra warning.
    """
    with _IMPERSONATE_TYPE_WARNED_LOCK:
        if provider_key in _IMPERSONATE_TYPE_WARNED:
            return
        _IMPERSONATE_TYPE_WARNED.add(provider_key)
    logger.warning(
        "Ignoring network.impersonate=%r for %s: expected a browser name "
        '(e.g. "chrome") or true; impersonation stays off.',
        raw,
        provider_key,
    )


def resolve_impersonate_target(provider_key: str | None) -> str | None:
    """Return the browser profile a provider is configured to impersonate.

    Reads ``provider_settings.<provider>.network.impersonate``: a browser name
    ("chrome", "chrome124", "safari17_0", ...), or ``true`` for
    ``DEFAULT_IMPERSONATE_TARGET``. Absent, ``false``, or empty means the
    standard HTTP client, which is the default for every provider.

    Args:
        provider_key: Provider identifier

    Returns:
        Browser profile name, or None when impersonation is off
    """
    if not provider_key:
        return None
    raw = get_network_config(provider_key).get("impersonate")
    if raw is True:
        return DEFAULT_IMPERSONATE_TARGET
    if isinstance(raw, str):
        return raw.strip() or None
    # A truthy value of any other type (1, {}, [...]) is a config mistake that
    # would otherwise disable impersonation in silence.
    if raw:
        _warn_unusable_impersonate_value(provider_key, raw)
    return None


def _build_impersonating_session(
    provider_key: str, target: str
) -> requests.Session | None:
    """Build a curl_cffi-backed session, or None when it cannot be had."""
    curl_requests = _load_curl_requests()
    if curl_requests is None:
        logger.warning(
            "Provider %s is configured for browser impersonation "
            "(network.impersonate=%r) but curl_cffi is not installed; falling "
            "back to the standard HTTP client, which this provider may reject. "
            "Install it with: uv sync --extra impersonate",
            provider_key,
            target,
        )
        return None

    try:
        session = ImpersonatingSession(curl_requests, target)
    except Exception as e:
        logger.warning(
            "Could not start a curl_cffi session impersonating %r for %s: %s; "
            "falling back to the standard HTTP client.",
            target,
            provider_key,
            e,
        )
        return None

    logger.info(
        "Using curl_cffi browser impersonation (%s) for %s.", target, provider_key
    )
    return session


def build_session(provider_key: str | None = None) -> requests.Session:
    """Build a configured requests session with retries and default headers.

    Args:
        provider_key: Provider the session will serve. When that provider
            enables ``network.impersonate`` and curl_cffi is installed, the
            returned session routes through curl_cffi instead; in every other
            case (the default) the plain session below is returned unchanged.

    Returns:
        Configured Session instance
    """
    target = resolve_impersonate_target(provider_key)
    if target and provider_key:
        impersonating = _build_impersonating_session(provider_key, target)
        if impersonating is not None:
            return impersonating

    session = requests.Session()

    # Avoid urllib3 retries on connection errors (DNS/SSL) and on HTTP status
    # codes: 429/5xx handling lives in the app-level retry loops (make_request
    # / download_file), so internal status retries would multiply the attempt
    # count (max_attempts x urllib3 retries) and double-sleep. Keep only a
    # small budget for read timeouts, which the socket layer sees first.
    retry = Retry(
        total=2,
        connect=0,  # no retries on connection errors (e.g., DNS/SSL)
        read=2,
        status=0,  # no internal retries on HTTP status codes
        backoff_factor=0.8,
        status_forcelist=(),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Default headers
    session.headers.update(
        {
            # Use a modern browser-like UA to avoid occasional 403s from some providers
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            # Encourage English-language responses and better cache hits across
            # providers
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    # Silence warnings when a provider's policy mandates an insecure retry once
    with contextlib.suppress(Exception):
        urllib3.disable_warnings(InsecureRequestWarning)

    return session


def get_session(provider_key: str | None = None) -> requests.Session:
    """Get the HTTP session serving a provider (lazy initialization).

    Providers that enable ``network.impersonate`` get their own curl_cffi-backed
    session, cached per provider; every other provider shares the single global
    session, exactly as before.

    Args:
        provider_key: Provider the session will serve (None for the shared one)

    Returns:
        Configured Session instance
    """
    global _SESSION

    target = resolve_impersonate_target(provider_key) if provider_key else None
    if provider_key and target:
        with _IMPERSONATED_SESSIONS_LOCK:
            cached = _IMPERSONATED_SESSIONS.get(provider_key)
            if cached is not None and cached[0] == target:
                return cached[1]
            # A build that falls back to the plain client is cached too, so the
            # "curl_cffi is missing" warning is logged once per provider rather
            # than once per request -- but tied to the target it was built for,
            # so an in-process config reload that names another browser profile
            # is not served the stale session. A superseded session is left to
            # be collected; a concurrent worker may still be reading from it.
            session = build_session(provider_key)
            _IMPERSONATED_SESSIONS[provider_key] = (target, session)
            return session

    if _SESSION is None:
        _SESSION = build_session()
    return _SESSION


def make_request(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[Any, Any] | str | bytes | None:
    """HTTP GET with centralized per-provider pacing, backoff, and circuit breaker.

    Args:
        url: URL to request
        params: Query parameters
        headers: Additional headers
        timeout: Request timeout in seconds (overridden by provider config)

    Returns:
        - dict for JSON responses
        - str for text/xml/html
        - bytes for other/binary content
        - None on error (including circuit breaker open)
    """
    provider = get_provider_for_url(url)
    session = get_session(provider)
    net = get_network_config(provider)

    # Check circuit breaker before making any requests
    cb = get_circuit_breaker(provider)
    if cb and not cb.allow_request():
        remaining = cb.time_until_retry()
        logger.warning(
            "Circuit breaker OPEN for %s; skipping request (retry in %.0fs): %s",
            provider or "unknown",
            remaining,
            url,
        )
        return None

    max_attempts = int(net.get("max_attempts", 5) or 5)
    base_backoff = float(net.get("base_backoff_s", 1.5) or 1.5)
    backoff_mult = float(net.get("backoff_multiplier", 1.5) or 1.5)
    max_backoff = float(net.get("max_backoff_s", 60.0) or 60.0)
    net_timeout = net.get("timeout_s")
    effective_timeout = (
        float(net_timeout) if net_timeout is not None else float(timeout)
    )

    rl = get_rate_limiter(provider)
    verify_default = bool(net.get("verify_ssl", True))
    ssl_policy = str(net.get("ssl_error_policy", "fail") or "fail").lower()
    provider_headers = dict(net.get("headers", {}) or {})

    # Track transient provider failures during this request (for circuit
    # breaker): 429 storms and 5xx storms both count when retries exhaust.
    hit_rate_limit = False
    hit_server_error = False

    # Merge headers: session defaults < provider headers < per-call headers
    req_headers = {}
    if provider_headers:
        req_headers.update(
            {str(k): str(v) for k, v in provider_headers.items() if v is not None}
        )
    if headers:
        req_headers.update(headers)

    insecure_retry_used = False
    verify = verify_default

    for attempt in range(1, max_attempts + 1):
        try:
            if rl:
                rl.wait()

            resp = session.get(
                url,
                params=params,
                headers=req_headers or None,
                timeout=effective_timeout,
                verify=verify,
            )

            # Explicit 429 handling with Retry-After
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                sleep_s = None

                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except ValueError:
                        try:
                            retry_dt = parsedate_to_datetime(retry_after)
                            sleep_s = max(
                                0.0,
                                (
                                    retry_dt - datetime.now(retry_dt.tzinfo)
                                ).total_seconds(),
                            )
                        except Exception:
                            sleep_s = None

                if sleep_s is None:
                    sleep_s = min(
                        base_backoff * (backoff_mult ** (attempt - 1)), max_backoff
                    )
                else:
                    # Clamp to [0, max_backoff]: a malformed negative numeric
                    # Retry-After must not reach time.sleep (ValueError).
                    sleep_s = max(0.0, min(sleep_s, max_backoff))

                logger.warning(
                    "429 Too Many Requests for %s; sleeping %.1fs (attempt %d/%d)",
                    url,
                    sleep_s,
                    attempt,
                    max_attempts,
                )
                hit_rate_limit = True
                # No point sleeping out the backoff on the final attempt;
                # the loop is about to give up anyway.
                if attempt < max_attempts:
                    time.sleep(sleep_s)
                continue

            # Retry transient 5xx
            if resp.status_code in (500, 502, 503, 504):
                sleep_s = min(
                    base_backoff * (backoff_mult ** (attempt - 1)), max_backoff
                )
                logger.warning(
                    "%s for %s; sleeping %.1fs (attempt %d/%d)",
                    resp.status_code,
                    url,
                    sleep_s,
                    attempt,
                    max_attempts,
                )
                hit_server_error = True
                # No point sleeping out the backoff on the final attempt;
                # the loop is about to give up anyway.
                if attempt < max_attempts:
                    time.sleep(sleep_s)
                continue

            # Non-retryable client errors
            if resp.status_code in NON_RETRYABLE_STATUSES:
                record_client_error(cb, resp.status_code, provider or "unknown")
                logger.warning(
                    "Non-retryable HTTP %s for %s; not retrying", resp.status_code, url
                )
                return None

            resp.raise_for_status()

            # Success! Record it for circuit breaker
            if cb:
                cb.record_success()

            # Parse response based on content type
            content_type = resp.headers.get("Content-Type", "").lower()
            if "json" in content_type:
                try:
                    return cast(dict[Any, Any], resp.json())
                except json.JSONDecodeError as e:
                    logger.error("JSON decode error for %s: %s", url, e)
                    return None

            if any(t in content_type for t in ("text/", "xml", "html")):
                return resp.text

            return resp.content

        except requests.exceptions.Timeout as e:
            # ConnectTimeout subclasses Timeout, so a host that silently drops
            # SYNs lands here; it gets the short connect budget, not the full
            # one (which would cost timeout_s per attempt for nothing).
            attempt_cap = connect_aware_attempt_cap(e, max_attempts)
            if attempt < attempt_cap:
                sleep_s = min(
                    base_backoff * (backoff_mult ** (attempt - 1)), max_backoff
                )
                logger.warning(
                    "Timeout for %s; sleeping %.1fs (attempt %d/%d)",
                    url,
                    sleep_s,
                    attempt,
                    attempt_cap,
                )
                time.sleep(sleep_s)
                continue
            logger.error("Request timed out: %s", url)
            if cb:
                cb.record_failure(provider or "unknown")
            return None

        except requests.exceptions.RequestException as e:
            msg = str(e).lower()

            # Handle DNS/Name resolution errors
            if any(
                term in msg
                for term in [
                    "nameresolutionerror",
                    "failed to resolve",
                    "getaddrinfo failed",
                    "temporary failure in name resolution",
                ]
            ):
                dns_retry = bool(net.get("dns_retry", False))
                if dns_retry and attempt < max_attempts:
                    sleep_s = min(
                        base_backoff * (backoff_mult ** (attempt - 1)), max_backoff
                    )
                    logger.warning(
                        "Name resolution error for %s: %s; dns_retry=true, sleeping "
                        "%.1fs (attempt %d/%d)",
                        url,
                        e,
                        sleep_s,
                        attempt,
                        max_attempts,
                    )
                    time.sleep(sleep_s)
                    continue
                logger.warning("Name resolution error for %s: %s; not retrying", url, e)
                # A host that does not resolve is a provider-level outage, not
                # a bad URL: feed the breaker so the rest of the run stops
                # re-dialling a dead hostname once per work.
                if cb:
                    cb.record_failure(provider or "unknown")
                return None

            # Handle SSL certificate verification errors
            if isinstance(e, requests.exceptions.SSLError) or any(
                term in msg
                for term in [
                    "certificate verify failed",
                    "sslcertverificationerror",
                    "ssl: certificate_verify_failed",
                ]
            ):
                if not verify and insecure_retry_used:
                    logger.warning(
                        "SSL error (insecure retry already used) for %s: %s; "
                        "not retrying",
                        url,
                        e,
                    )
                    return None

                if ssl_policy == "retry_insecure_once" and verify:
                    logger.warning(
                        "SSL verify failed for %s; retrying once with verify=False "
                        "due to policy.",
                        url,
                    )
                    verify = False
                    insecure_retry_used = True
                    continue

                logger.warning(
                    "SSL certificate verification error for %s: %s; not retrying",
                    url,
                    e,
                )
                return None

            # Generic retry for other errors. A connection that never came up
            # (refused, unreachable) gets the short budget; errors raised after
            # the connection was established keep the full one.
            attempt_cap = connect_aware_attempt_cap(e, max_attempts)
            if attempt < attempt_cap:
                sleep_s = min(
                    base_backoff * (backoff_mult ** (attempt - 1)), max_backoff
                )
                logger.warning(
                    "Request error for %s: %s; sleeping %.1fs (attempt %d/%d)",
                    url,
                    e,
                    sleep_s,
                    attempt,
                    attempt_cap,
                )
                time.sleep(sleep_s)
                continue

            logger.error("Request failed for %s: %s", url, e)
            if cb:
                cb.record_failure(provider or "unknown")
            return None

    # Record failure for circuit breaker when retries were exhausted by rate
    # limiting or by a 5xx storm (both indicate a struggling provider).
    if cb and (hit_rate_limit or hit_server_error):
        cb.record_failure(provider or "unknown")

    logger.error("Giving up after %d attempts for %s", max_attempts, url)
    return None


def make_json_request(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any] | None:
    """HTTP GET expecting a JSON response, with type-safe return.

    This is a convenience wrapper around make_request() that returns only
    dict or None, making it suitable for API calls where JSON is expected.

    Args:
        url: URL to request
        params: Query parameters
        headers: Additional headers
        timeout: Request timeout in seconds

    Returns:
        Parsed JSON dict or None on error/non-JSON response
    """
    result = make_request(url, params=params, headers=headers, timeout=timeout)
    if isinstance(result, dict):
        return result
    return None
