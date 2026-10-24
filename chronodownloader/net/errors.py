"""Failure causes and the exceptions :class:`~chronodownloader.net.Network` raises.

A request either returns or raises one of these; it never returns None. The
cause tells the caller why a request failed and decides how the shared
circuit breaker counts it:

- ``blocked``: the provider refuses the client (401, 403, 451, a captcha or
  bot challenge); counts as a breaker failure, not retried.
- ``transient``: timeouts, connections that fail or break, 429 and 5xx;
  retried with backoff, a breaker failure once the attempts are spent.
- ``dns``: the host name does not resolve; retried only under ``dns_retry``,
  then a breaker failure.
- ``ssl``: the TLS handshake fails; one unverified retry under
  ``retry_insecure_once`` when the request carries no credential, then a
  breaker failure.
- ``not_found``: 404 or 410; the provider answered, so a breaker success.
- ``rejected``: any other 4xx (400, 422, ...); a breaker success.
- ``invalid_url``: the URL cannot be sent at all; the breaker is untouched.
- ``incomplete``: a download delivered no bytes or fewer than its declared
  Content-Length; not retried.
- ``invalid_payload``: a download's leading bytes contradict its type (HTML
  where a PDF was expected); not retried.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from chronodownloader.coord import QuotaStatus

__all__ = ["BreakerOpen", "Cause", "NetworkError", "QuotaSpent", "RequestFailed"]


class Cause(StrEnum):
    """Why a request failed."""

    BLOCKED = "blocked"
    TRANSIENT = "transient"
    DNS = "dns"
    SSL = "ssl"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    INVALID_URL = "invalid_url"
    INCOMPLETE = "incomplete"
    INVALID_PAYLOAD = "invalid_payload"


class NetworkError(Exception):
    """Base of the exceptions a request raises instead of returning.

    ``quota_charged`` is True when a quota unit of the failed call stayed
    charged (an attempt was sent), even if the exception itself means that
    nothing was sent by the attempt that raised it.
    """

    quota_charged: bool = False


class RequestFailed(NetworkError):
    """A request failed for *cause* after its last attempt.

    ``status`` is the last HTTP status, or None when no response arrived;
    ``url`` has credential values masked; ``attempts`` counts the tries sent
    or started.
    """

    def __init__(
        self,
        cause: Cause,
        url: str,
        *,
        status: int | None = None,
        attempts: int = 1,
        detail: str = "",
    ) -> None:
        self.cause = cause
        self.url = url
        self.status = status
        self.attempts = attempts
        self.detail = detail
        shown = f"HTTP {status}" if status is not None else "no response"
        text = f"{cause.value} ({shown}) after {attempts} attempt(s): {url}"
        super().__init__(f"{text}: {detail}" if detail else text)


class BreakerOpen(NetworkError):
    """The provider's circuit breaker is open; nothing was sent.

    ``until`` is the wall-clock time (seconds since the epoch) when the
    breaker admits a probe again, or None when unknown; ``cause`` is the
    failure cause that opened it, when the coordinator recorded one. The
    message names the provider whose breaker is open (a hop to another
    provider's host meets that provider's breaker) and the reopening time
    in UTC.
    """

    def __init__(self, provider: str, until: float | None, cause: str | None) -> None:
        self.provider = provider
        self.until = until
        self.cause = cause
        reason = f" ({cause})" if cause else ""
        when = ""
        if until is not None:
            stamp = datetime.fromtimestamp(until, tz=UTC)
            when = f" until {stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        super().__init__(f"circuit breaker open for {provider}{reason}{when}")


class QuotaSpent(NetworkError):
    """The provider's daily quota is spent; nothing was sent.

    ``status`` holds the units used and the reset time.
    """

    def __init__(self, provider: str, status: QuotaStatus) -> None:
        self.provider = provider
        self.status = status
        super().__init__(f"daily quota spent for {provider}")
