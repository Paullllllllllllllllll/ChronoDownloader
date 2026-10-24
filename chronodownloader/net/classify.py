"""Classification of HTTP statuses and transport errors, and retry timing.

The connect-failure rules, the DNS and certificate message markers and the
invalid-URL errors are copied from ChronoDownloader v1.30.3
``api/core/network.py`` (lines 182-246, 723-728, 1426-1469) on 19.10.2026;
the two Retry-After parsers of v1.30.3 (``network.py`` 1304-1326,
``download.py`` 1048-1066) are merged into :func:`retry_after_seconds`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC
from email.utils import parsedate_to_datetime

import requests

from .credentials import redact_text
from .errors import Cause

__all__ = [
    "CONNECT_FAILURE_MAX_ATTEMPTS",
    "Failure",
    "backoff_seconds",
    "classify_exception",
    "connect_aware_attempt_cap",
    "is_challenge",
    "is_connect_failure",
    "retry_after_seconds",
    "status_failure",
]

CONNECT_FAILURE_MAX_ATTEMPTS = 2
"""Attempts for a connection that never came up (refused, unreachable,
connect timeout): a host that drops SYNs costs the full timeout on every
attempt, so one retry covers a blip and the breaker takes over from there."""

_CONNECT_FAILURE_MARKERS = (
    "newconnectionerror",
    "connecttimeouterror",
    "failed to establish a new connection",
    "connection refused",
    "actively refused",  # Windows WinError 10061
    "network is unreachable",
    "no route to host",
)

_DNS_MARKERS = (
    "nameresolutionerror",
    "failed to resolve",
    "getaddrinfo failed",
    "temporary failure in name resolution",
)

_CERTIFICATE_MARKERS = (
    "certificate verify failed",
    "sslcertverificationerror",
    "ssl: certificate_verify_failed",
)

INVALID_URL_ERRORS = (
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
    requests.exceptions.InvalidURL,
    requests.exceptions.URLRequired,
)
"""Errors raised before any socket opens: no retry makes such a URL work,
and the provider is not at fault."""

BLOCKED_STATUSES = frozenset({401, 403, 451})
NOT_FOUND_STATUSES = frozenset({404, 410})
TRANSIENT_STATUSES = frozenset({429})
"""Besides every 5xx status."""
RETRY_AFTER_STATUSES = frozenset({429, 503})

_CHALLENGE_TITLES = ("captcha", "just a moment", "attention required")
_TITLE = re.compile(rb"<title[^>]*>(.*?)</title", re.IGNORECASE | re.DOTALL)
CHALLENGE_WINDOW = 4096
"""Leading bytes of an HTML body searched for a challenge page's title."""


@dataclass(frozen=True)
class Failure:
    """One failed attempt.

    ``sent`` is False when the request provably never reached the server
    (see :func:`classify_exception`); ``connect`` marks a connection that
    never came up; ``retry_after_s`` is the server's Retry-After value,
    already clamped; ``detail`` is safe to log.
    """

    cause: Cause
    status: int | None = None
    sent: bool = True
    connect: bool = False
    retry_after_s: float | None = None
    detail: str = ""


def is_connect_failure(exc: BaseException) -> bool:
    """Report whether *exc* means the connection was never established."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _CONNECT_FAILURE_MARKERS)


def connect_aware_attempt_cap(exc: BaseException, max_attempts: int) -> int:
    """Return *max_attempts*, or the shorter budget for a connect failure."""
    if is_connect_failure(exc):
        return min(max_attempts, CONNECT_FAILURE_MAX_ATTEMPTS)
    return max_attempts


def classify_exception(
    exc: requests.exceptions.RequestException, *, after_response: bool = False
) -> Failure:
    """Return the failure a transport error stands for.

    Only failures that provably precede transmission count as unsent: an
    unusable URL, a name that does not resolve, a connection that never came
    up, and a certificate rejected during the handshake. An error raised
    *after_response* started (while reading the body) always counts as sent.
    The detail has credential values and URL user information masked.
    """
    detail = redact_text(str(exc))
    message = detail.lower()
    if isinstance(exc, INVALID_URL_ERRORS):
        failure = Failure(Cause.INVALID_URL, sent=False, detail=detail)
    elif isinstance(exc, requests.exceptions.Timeout):
        connect = is_connect_failure(exc)
        failure = Failure(
            Cause.TRANSIENT, sent=not connect, connect=connect, detail=detail
        )
    elif any(marker in message for marker in _DNS_MARKERS):
        failure = Failure(Cause.DNS, sent=False, detail=detail)
    elif isinstance(exc, requests.exceptions.SSLError) or any(
        marker in message for marker in _CERTIFICATE_MARKERS
    ):
        handshake = any(marker in message for marker in _CERTIFICATE_MARKERS)
        failure = Failure(Cause.SSL, sent=not handshake, detail=detail)
    else:
        connect = is_connect_failure(exc)
        failure = Failure(
            Cause.TRANSIENT, sent=not connect, connect=connect, detail=detail
        )
    if after_response:
        return replace(failure, sent=True, connect=False)
    return failure


def status_failure(
    status: int, headers: Mapping[str, str], *, max_backoff_s: float, now: float
) -> Failure | None:
    """Return the failure an HTTP status stands for, or None for success.

    429 and 503 carry their Retry-After value, clamped to
    ``[0, max_backoff_s]``.
    """
    if status < 400:
        return None
    retry_after = None
    if status in RETRY_AFTER_STATUSES:
        retry_after = retry_after_seconds(
            headers.get("Retry-After"), max_s=max_backoff_s, now=now
        )
    if status in BLOCKED_STATUSES:
        cause = Cause.BLOCKED
    elif status in NOT_FOUND_STATUSES:
        cause = Cause.NOT_FOUND
    elif status in TRANSIENT_STATUSES or status >= 500:
        cause = Cause.TRANSIENT
    else:
        cause = Cause.REJECTED
    return Failure(cause, status=status, retry_after_s=retry_after)


def is_challenge(headers: Mapping[str, str], head: bytes) -> bool:
    """Report whether a response is a captcha or bot challenge.

    A Cloudflare ``cf-mitigated: challenge`` header decides; otherwise an
    HTML body whose ``<title>`` within its first bytes names a captcha or a
    Cloudflare interstitial ("Just a moment", "Attention Required").
    """
    if headers.get("cf-mitigated", "").strip().lower() == "challenge":
        return True
    if "html" not in headers.get("Content-Type", "").lower():
        return False
    match = _TITLE.search(head[:CHALLENGE_WINDOW])
    if match is None:
        return False
    title = match.group(1).decode("utf-8", "replace").lower()
    return any(marker in title for marker in _CHALLENGE_TITLES)


def retry_after_seconds(value: str | None, *, max_s: float, now: float) -> float | None:
    """Return the wait a Retry-After header asks for, clamped to [0, *max_s*].

    The header holds seconds or an HTTP date, which is compared with *now*
    (seconds since the epoch). A missing or unreadable value returns None,
    and the caller falls back to its own backoff.
    """
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        seconds = moment.timestamp() - now
    if not math.isfinite(seconds):
        return None
    return max(0.0, min(seconds, max_s))


def backoff_seconds(
    attempt: int, *, base_s: float, multiplier: float, max_s: float
) -> float:
    """Return the wait after failed attempt *attempt* (1-based)."""
    try:
        wait = base_s * multiplier ** (attempt - 1)
    except OverflowError:
        return max_s
    return max(0.0, min(wait, max_s))
