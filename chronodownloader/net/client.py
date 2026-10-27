"""The run-owned network client: paced, retried, breaker-checked requests.

A run builds one :class:`Network` from its settings and the machine-wide
coordinator and closes it at the end. Every call names its provider key
(``judge`` for the decision endpoint). A call

1. asks the coordinator's breaker; an open breaker raises
   :class:`~chronodownloader.net.errors.BreakerOpen` and nothing is sent;
2. for each attempt, reserves a quota unit when the call is quota-charged,
   waits for its pacing turn (``wait_turn``) and passes the breaker again, so
   every retry reserves anew and stops once another call opens the breaker
   (the call whose own failure opened it finishes its retries); the client
   holds no lock and no transaction while it sleeps;
3. sends the request without automatic redirects and follows up to
   :data:`MAX_REDIRECTS` itself, each hop paced and breaker-checked like an
   attempt (the quota unit covers the whole chain);
4. classifies the result (see :mod:`chronodownloader.net.errors`), records
   every transient, blocked, DNS or certificate failure on the shared
   breaker, and retries transient failures with backoff (a server's
   Retry-After wins), a connect failure at most twice, a name that does not
   resolve only under ``dns_retry``, and a certificate failure once
   unverified under ``retry_insecure_once`` unless a hop carried a
   credential;
5. returns, recording success, or raises
   :class:`~chronodownloader.net.errors.RequestFailed` with its cause.

Hosts of other providers. A client built with a *host_key* resolver (the
provider registry supplies it) re-resolves the provider key for every hop,
the first included: a hop to a registered provider's host takes that
provider's network block, session, pacing and breaker, and the outcome is
recorded on that provider's breaker; a hop to an unregistered host goes
back to the key the call named. The quota and the retry budget stay the
call's.

Quota accounting. Every exception a quota-charged call ends with carries
``quota_charged``: True when a quota unit of the call stayed charged (an
attempt was sent and not answered with one of its ``final_statuses``, an
expected refusal). A call that returns was charged when it asked for quota
(:attr:`DownloadOutcome.quota_charged`).

Header precedence: session defaults, then the provider's ``headers``, then
the call's. Sessions ignore the environment (no ``.netrc`` credentials, no
proxy variables). A provider with ``impersonate`` set sends through
curl_cffi when it is installed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, TypedDict, Unpack
from urllib.parse import urljoin, urlsplit

import requests

from chronodownloader.coord import (
    BreakerStatus,
    QuotaToken,
    SlotKeeper,
    SlotLost,
    SlotToken,
)
from chronodownloader.settings import (
    AnnasArchiveSettings,
    AnnasQuota,
    NetworkSettings,
    Settings,
)

from .atomic import replace_with_retry
from .classify import (
    CHALLENGE_WINDOW,
    CONNECT_FAILURE_MAX_ATTEMPTS,
    Failure,
    backoff_seconds,
    classify_exception,
    is_challenge,
    status_failure,
)
from .credentials import (
    credential_values,
    redact_text,
    redact_url,
    request_carries_credential,
)
from .errors import BreakerOpen, Cause, NetworkError, QuotaSpent, RequestFailed
from .impersonation import (
    CurlBackend,
    ImpersonatingSession,
    build_impersonating_session,
    load_curl_backend,
)
from .payload import (
    HEAD_BYTES,
    content_disposition_filename,
    extension_for_content_type,
    magic_mismatch,
    parse_content_length,
    sniff_extension,
)
from .ports import CoordinatorPort
from .session import build_session

__all__ = ["JUDGE", "DownloadOutcome", "Network", "RequestOptions"]

logger = logging.getLogger(__name__)

JUDGE = "judge"
"""The provider key of the decision endpoint."""

CHUNK_BYTES = 64 * 1024
PART_SUFFIX = ".part"

_SUCCESS_CAUSES = frozenset(
    {Cause.NOT_FOUND, Cause.REJECTED, Cause.INCOMPLETE, Cause.INVALID_PAYLOAD}
)
"""Failures where the provider answered: the breaker counts a success."""

_BREAKER_FAILURES = frozenset({Cause.TRANSIENT, Cause.BLOCKED, Cause.DNS, Cause.SSL})
"""Failures the breaker counts, attempt by attempt."""

MAX_REDIRECTS = 10
"""Redirects followed per attempt; each is a paced, breaker-checked request."""

_ORIGIN_BOUND_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
"""Headers dropped when a redirect leaves the origin."""

_DEFAULT_PORTS = {"http": 80, "https": 443}

SLOT_POLL_S = 0.5
"""The wait between two tries for a download slot of another provider."""

ANSWER_TEXT_CHARS = 200
"""Characters of an expected refusal's body kept in its error text."""

ANSWER_BODY_BYTES = 65_536
"""Bytes of an expected refusal's body read to find its JSON ``error``."""

type HostKey = Callable[[str], str | None]
"""Returns the key of the registered provider serving a URL's host, or None."""


def _origin(url: str) -> tuple[str, str, int | None]:
    """Return the scheme, host and port of *url* (the default port filled in)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        port = None
    return scheme, (parts.hostname or "").lower(), port


def _carries_credential(call: _Call, hop: _Hop) -> bool:
    """Report whether *hop*, as the session would prepare it, carries a secret.

    The prepared request includes the session's cookies and auth, so a
    session cookie, an Authorization header or URL user information count
    alongside the name-based parameter and header rules. Checked only when
    the provider allows the unverified retry. An impersonating session counts
    as carrying one: curl_cffi keeps its own cookie jar, which the prepared
    request does not show.
    """
    if call.policy.ssl_error_policy != "retry_insecure_once":
        return False
    if isinstance(call.session, ImpersonatingSession):
        return True
    request = requests.Request(
        hop.method,
        hop.url,
        params=dict(hop.params or {}),
        headers=dict(hop.headers),
        json=hop.json,
        data=hop.data,
    )
    try:
        prepared = call.session.prepare_request(request)
    except (requests.exceptions.RequestException, ValueError):
        return request_carries_credential(hop.url, hop.params, hop.headers)
    return request_carries_credential(prepared.url or hop.url, None, prepared.headers)


class RequestOptions(TypedDict, total=False):
    """Optional arguments of :meth:`Network.request`.

    ``timeout_s`` overrides the provider's ``timeout_s``; ``slot`` is a held
    download slot the pacing reservation checks; ``quota`` charges each
    attempt to the provider's daily quota. ``final_statuses`` lists HTTP
    statuses that are an expected answer for this call (BNE Digital's 500
    past the last page, a refusal of Anna's Archive's fast-download API):
    such an answer raises :class:`~chronodownloader.net.errors.RequestFailed`
    at once with the body's error text (masked, short) as its detail, is not
    retried, counts as a breaker success and gives back the attempt's quota
    unit, since the provider served nothing.
    """

    params: Mapping[str, Any] | None
    headers: Mapping[str, str] | None
    json: Any
    data: Any
    timeout_s: float | None
    allow_redirects: bool
    slot: SlotToken | None
    quota: bool
    final_statuses: frozenset[int]


class DownloadOptions(TypedDict, total=False):
    """Optional arguments of :meth:`Network.download`, as in :class:`RequestOptions`."""

    params: Mapping[str, Any] | None
    headers: Mapping[str, str] | None
    timeout_s: float | None
    quota: bool
    final_statuses: frozenset[int]


@dataclass(frozen=True)
class DownloadOutcome:
    """A completed download, renamed from its ``.part`` path.

    ``extension`` is the one the Content-Type declares, else the one the
    leading bytes imply, else ''; ``filename`` is the Content-Disposition
    proposal; ``url`` is the final URL with credentials masked; ``insecure``
    marks a body received over the unverified retry; ``quota_charged`` says
    whether a quota unit of the call stayed charged.
    """

    path: Path
    size: int
    content_type: str
    extension: str
    filename: str | None
    url: str
    insecure: bool
    quota_charged: bool = False


@dataclass(frozen=True)
class _Hop:
    """One HTTP request of a redirect chain."""

    method: str
    url: str
    params: Mapping[str, Any] | None
    headers: Mapping[str, str]
    json: Any = None
    data: Any = None

    def send_kwargs(self, call: _Call, verify: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headers": dict(self.headers) or None,
            "timeout": call.timeout_s,
            "verify": verify,
            "stream": call.stream,
            "allow_redirects": False,
        }
        if self.params:
            kwargs["params"] = dict(self.params)
        if self.json is not None:
            kwargs["json"] = self.json
        if self.data is not None:
            kwargs["data"] = self.data
        return kwargs

    def redirected(self, response: requests.Response) -> _Hop:
        """Return the hop a redirect response points to.

        303, and 301 or 302 after a POST, continue as GET without a body, as
        browsers and requests do; a hop to another origin drops the
        Authorization, Proxy-Authorization and Cookie headers. Query
        parameters are never re-added: the new URL is used as given.
        """
        status = response.status_code
        target = urljoin(response.url or self.url, response.headers["Location"])
        method = self.method
        if (status == 303 and method != "HEAD") or (
            status in (301, 302) and method == "POST"
        ):
            method = "GET"
        keeps_body = status in (307, 308)
        headers = dict(self.headers)
        if _origin(target) != _origin(self.url):
            headers = {
                name: value
                for name, value in headers.items()
                if name.lower() not in _ORIGIN_BOUND_HEADERS
            }
        return _Hop(
            method,
            target,
            None,
            MappingProxyType(headers),
            self.json if keeps_body else None,
            self.data if keeps_body else None,
        )


@dataclass(frozen=True)
class _Call:
    """One logical request: what every attempt sends first.

    ``provider`` is the key the current hop is paced under; ``origin`` the
    key the call named.
    """

    provider: str
    origin: str
    method: str
    url: str
    policy: NetworkSettings
    session: requests.Session
    params: Mapping[str, Any] | None
    headers: Mapping[str, str]
    json: Any
    data: Any
    timeout_s: float
    follow_redirects: bool
    stream: bool
    slot: SlotToken | None
    quota: AnnasQuota | None
    final_statuses: frozenset[int] = frozenset()

    @property
    def log_url(self) -> str:
        return redact_url(self.url)

    def first_hop(self) -> _Hop:
        return _Hop(
            self.method, self.url, self.params, self.headers, self.json, self.data
        )


@dataclass
class _Admission:
    """One call's breaker and quota state.

    Holds the probes the call was granted and the breakers its own failure
    opened (each by provider key, with the time the breaker reports), and
    whether a quota unit stayed charged.
    """

    probes: dict[str, float | None] = field(default_factory=dict)
    tripped: dict[str, float | None] = field(default_factory=dict)
    charged: bool = False


@dataclass(frozen=True)
class _Done[T]:
    value: T


@dataclass(frozen=True)
class _Outcome[T]:
    """An attempt's result.

    ``credentialed`` covers every hop it sent; ``call`` is the last hop's
    call, whose provider the result belongs to.
    """

    result: _Done[T] | Failure
    credentialed: bool
    call: _Call


class _Rejected(Exception):
    """A response a handler refuses; carries the failure it stands for."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.detail)
        self.failure = failure


type _Handler[T] = Callable[[requests.Response, bool], T]
"""Turns a successful response into the call's value, or raises
:class:`_Rejected`; the flag marks the unverified retry."""


class Network:
    """The network client of one run; thread-safe, no mutable state.

    *session_factory* builds the run's plain session; *curl_loader* imports
    curl_cffi when a provider sets ``impersonate``; *sleep* waits out
    backoffs, pacing and slot polls; *clock* reads the wall clock for
    HTTP-date Retry-After values; *host_key* maps a URL onto the key of the
    registered provider serving its host (None: every hop keeps the key the
    call names).
    """

    def __init__(
        self,
        settings: Settings,
        coordinator: CoordinatorPort,
        *,
        session_factory: Callable[[], requests.Session] = build_session,
        curl_loader: Callable[[], CurlBackend | None] = load_curl_backend,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        host_key: HostKey | None = None,
    ) -> None:
        policies = {key: block.network for key, block in settings.providers.items()}
        policies[JUDGE] = settings.judge.network.network_settings()
        self._policies: Mapping[str, NetworkSettings] = MappingProxyType(policies)
        slot_limits = {key: settings.slots(key) for key in settings.providers}
        slot_limits[JUDGE] = settings.judge.network.slots
        self._slot_limits: Mapping[str, int] = MappingProxyType(slot_limits)
        self._host_key = host_key
        self._quotas: Mapping[str, AnnasQuota] = MappingProxyType(
            {
                key: block.quota
                for key, block in settings.providers.items()
                if isinstance(block, AnnasArchiveSettings) and block.quota.enabled
            }
        )
        self._coordinator = coordinator
        self._sleep = sleep
        self._clock = clock
        self._session = session_factory()
        targets = {
            key: policy.impersonate
            for key, policy in policies.items()
            if policy.impersonate
        }
        sessions: dict[str, requests.Session] = {}
        if targets:
            backend = curl_loader()
            for key, target in targets.items():
                built = build_impersonating_session(backend, key, target)
                if built is not None:
                    sessions[key] = built
        self._impersonated: Mapping[str, requests.Session] = MappingProxyType(sessions)
        for session in (self._session, *sessions.values()):
            session.trust_env = False

    def __enter__(self) -> Network:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the run's sessions."""
        for session in self._impersonated.values():
            session.close()
        self._session.close()

    def policy(self, provider: str) -> NetworkSettings:
        """Return the network block of *provider*.

        Raises:
            ValueError: *provider* is neither a provider key nor ``judge``.
        """
        try:
            return self._policies[provider]
        except KeyError:
            raise ValueError(f"unknown provider {provider!r}") from None

    def session_for(self, provider: str) -> requests.Session:
        """Return the session that serves *provider*."""
        self.policy(provider)
        return self._impersonated.get(provider, self._session)

    @contextlib.contextmanager
    def hold_slot(self, provider: str) -> Iterator[SlotToken]:
        """Hold one of *provider*'s download slots while the block runs.

        For a transfer that another provider's download makes from this
        provider's hosts, and for a call to the decision endpoint (``judge``,
        limited to ``judge.network.slots``). The slot is polled for every
        :data:`SLOT_POLL_S` seconds (sleeping outside any transaction) until
        granted, renewed by a :class:`~chronodownloader.coord.SlotKeeper` and
        released at the end.

        Raises:
            ValueError: *provider* is neither a provider key nor ``judge``.
        """
        limit = self._slot_limits.get(provider)
        if limit is None:
            raise ValueError(f"unknown provider {provider!r}")
        token = self._coordinator.acquire_slot(provider, limit)
        while token is None:
            self._sleep(SLOT_POLL_S)
            token = self._coordinator.acquire_slot(provider, limit)
        keeper = SlotKeeper(self._coordinator, token)
        try:
            with keeper:
                yield keeper.token
        finally:
            self._coordinator.release_slot(keeper.token)

    def _resolve(self, url: str, origin: str) -> str:
        """Return the key a hop to *url* is paced under (see the module doc)."""
        if self._host_key is None:
            return origin
        key = self._host_key(url)
        return key if key is not None and key in self._policies else origin

    def _for_hop(self, call: _Call, url: str) -> _Call:
        """Return *call* as a hop to *url*.

        The hop takes the key, network block and session of *url*'s serving
        provider.
        """
        key = self._resolve(url, call.origin)
        if key == call.provider:
            return call
        return replace(
            call, provider=key, policy=self.policy(key), session=self.session_for(key)
        )

    def request(
        self, provider: str, method: str, url: str, **options: Unpack[RequestOptions]
    ) -> requests.Response:
        """Send one request and return its response, body read.

        Raises:
            BreakerOpen: The provider's breaker is open; nothing was sent.
            QuotaSpent: The call is quota-charged and the quota is spent.
            RequestFailed: The request failed; ``cause`` says why.
            ValueError: *provider* is unknown, or quota is asked of a
                provider without one.
        """
        call = self._call(provider, method, url, options, stream=False)

        def read(response: requests.Response, insecure: bool) -> requests.Response:
            if is_challenge(response.headers, response.content[:CHALLENGE_WINDOW]):
                raise _Rejected(
                    Failure(
                        Cause.BLOCKED,
                        status=response.status_code,
                        detail="captcha or bot challenge",
                    )
                )
            return response

        return self._exchange(call, read)

    def download(
        self,
        provider: str,
        url: str,
        dest_part_path: Path,
        *,
        slot: SlotToken,
        **options: Unpack[DownloadOptions],
    ) -> DownloadOutcome:
        """Stream *url* to *dest_part_path* and rename it without ``.part``.

        The body is written in 64 KiB chunks; a body shorter than its declared
        Content-Length (unless content-encoded), an empty body, a challenge
        page, or leading bytes that contradict a PDF or EPUB type are
        discarded. The ``.part`` file never survives a failure or an
        interrupt; the rename retries a transient Windows lock. *slot* is the
        held download slot every pacing reservation checks.

        Raises:
            BreakerOpen: The provider's breaker is open; nothing was sent.
            QuotaSpent: The call is quota-charged and the quota is spent.
            RequestFailed: The download failed; ``cause`` says why.
            OSError: The file could not be written or renamed.
            ValueError: *dest_part_path* does not end in ``.part``.
        """
        part = Path(dest_part_path)
        if part.suffix != PART_SUFFIX or part.name == PART_SUFFIX:
            raise ValueError(f"download target must end in {PART_SUFFIX}: {part}")
        final = part.with_name(part.name[: -len(PART_SUFFIX)])
        call = self._call(provider, "GET", url, {**options, "slot": slot}, stream=True)

        part.parent.mkdir(parents=True, exist_ok=True)

        def stream(response: requests.Response, insecure: bool) -> DownloadOutcome:
            return _stream_body(response, part, final, insecure)

        outcome = self._exchange(call, stream)
        return replace(outcome, quota_charged=call.quota is not None)

    def _call(
        self,
        provider: str,
        method: str,
        url: str,
        options: Mapping[str, Any],
        *,
        stream: bool,
    ) -> _Call:
        """Return the call; *options* holds :class:`RequestOptions` keys.

        The first hop is paced under the key :meth:`_resolve` gives for
        *url*; the quota is *provider*'s.
        """
        self.policy(provider)
        quota: AnnasQuota | None = None
        if options.get("quota", False):
            quota = self._quotas.get(provider)
            if quota is None:
                raise ValueError(f"{provider} has no enabled quota")
        key = self._resolve(url, provider)
        policy = self.policy(key)
        headers = {str(k): str(v) for k, v in policy.headers.items()}
        headers.update(
            {str(k): str(v) for k, v in (options.get("headers") or {}).items()}
        )
        timeout_s = options.get("timeout_s")
        return _Call(
            provider=key,
            origin=provider,
            method=method.upper(),
            url=url,
            policy=policy,
            session=self.session_for(key),
            params=options.get("params"),
            headers=MappingProxyType(headers),
            json=options.get("json"),
            data=options.get("data"),
            timeout_s=policy.timeout_s if timeout_s is None else float(timeout_s),
            follow_redirects=bool(options.get("allow_redirects", True)),
            stream=stream,
            slot=options.get("slot"),
            quota=quota,
            final_statuses=frozenset(options.get("final_statuses", ())),
        )

    def _exchange[T](self, call: _Call, handle: _Handler[T]) -> T:
        """Run the attempts of *call* and return what *handle* makes of it.

        Every exception the call ends with carries ``quota_charged``.
        """
        admission = _Admission()
        try:
            return self._attempts(call, handle, admission)
        except (NetworkError, SlotLost) as exc:
            exc.quota_charged = admission.charged
            raise

    def _attempts[T](
        self, call: _Call, handle: _Handler[T], admission: _Admission
    ) -> T:
        """Run the attempts of *call* (see :meth:`_exchange`)."""
        policy = call.policy
        self._admit(call, admission)
        verify = policy.verify_ssl
        insecure = False
        attempt = 0
        while True:
            attempt += 1
            token = self._reserve_quota(call)
            try:
                self._turn(call, admission)
            except BaseException:  # nothing was sent: release, then re-raise
                self._release_quota(token)
                raise
            outcome = self._attempt(call, verify, insecure, handle, admission)
            result = outcome.result
            final = (
                not isinstance(result, _Done)
                and result.status is not None
                and result.status in call.final_statuses
            )
            if token is not None:
                if final or not (isinstance(result, _Done) or result.sent):
                    self._release_quota(token)
                else:
                    admission.charged = True
            if isinstance(result, _Done):
                self._record_success(outcome.call)
                return result.value
            if final:
                raise self._final_answer(outcome.call, result, attempt)
            if result.cause in _BREAKER_FAILURES:
                self._record_failure(outcome.call, result.cause, admission)
            if self._insecure_retry(
                call, result, attempt, verify, outcome.credentialed
            ):
                verify, insecure = False, True
                continue
            wait = self._retry_wait(call, result, attempt)
            if wait is None:
                raise self._give_up(outcome.call, result, attempt)
            logger.warning(
                "%s for %s (%s); retrying in %.1f s (attempt %d/%d)",
                result.cause.value,
                call.log_url,
                result.status if result.status is not None else result.detail,
                wait,
                attempt,
                policy.max_attempts,
            )
            if wait > 0:
                self._sleep(wait)

    def _attempt[T](
        self,
        call: _Call,
        verify: bool,
        insecure: bool,
        handle: _Handler[T],
        admission: _Admission,
    ) -> _Outcome[T]:
        """Send one attempt, following up to :data:`MAX_REDIRECTS` redirects.

        The first hop's turn is taken by the caller; every later hop is
        re-resolved to its serving provider (:meth:`_for_hop`), waits for
        that provider's turn and passes its breaker.
        """
        hop = call.first_hop()
        credentialed = False
        current = call
        hop_verify = verify
        for number in range(MAX_REDIRECTS + 1):
            if number:
                current = self._for_hop(call, hop.url)
                hop_verify = (
                    verify
                    if current.provider == call.provider
                    else current.policy.verify_ssl and not insecure
                )
                self._turn(current, admission)
            credentialed = credentialed or _carries_credential(current, hop)
            try:
                response = current.session.request(
                    hop.method, hop.url, **hop.send_kwargs(current, hop_verify)
                )
            except requests.exceptions.RequestException as exc:
                return _Outcome(classify_exception(exc), credentialed, current)
            if call.follow_redirects and response.is_redirect:
                with response:
                    following = hop.redirected(response)
                logger.debug(
                    "%s redirects to %s",
                    redact_url(hop.url),
                    redact_url(following.url),
                )
                hop = following
                continue
            return _Outcome(
                self._answer(current, response, insecure, handle),
                credentialed,
                current,
            )
        failure = Failure(Cause.REJECTED, detail=f"more than {MAX_REDIRECTS} redirects")
        return _Outcome(failure, credentialed, current)

    def _answer[T](
        self,
        call: _Call,
        response: requests.Response,
        insecure: bool,
        handle: _Handler[T],
    ) -> _Done[T] | Failure:
        """Classify a final response and hand a successful one to *handle*."""
        with response:
            failure = status_failure(
                response.status_code,
                response.headers,
                max_backoff_s=call.policy.max_backoff_s,
                now=self._clock(),
            )
            if failure is not None:
                head = b"" if call.stream else response.content[:CHALLENGE_WINDOW]
                if failure.cause is not Cause.BLOCKED and is_challenge(
                    response.headers, head
                ):
                    failure = replace(failure, cause=Cause.BLOCKED)
                if failure.status in call.final_statuses and not call.stream:
                    failure = replace(failure, detail=_answer_text(response))
                return failure
            try:
                return _Done(handle(response, insecure))
            except requests.exceptions.RequestException as exc:
                return classify_exception(exc, after_response=True)
            except _Rejected as rejected:
                return rejected.failure

    def _turn(self, call: _Call, admission: _Admission) -> None:
        """Wait for the provider's pacing turn, then pass the breaker."""
        policy = call.policy
        self._coordinator.wait_turn(
            call.provider,
            policy.delay_ms / 1000.0,
            policy.jitter_ms / 1000.0,
            slot=call.slot,
            sleep=self._sleep,
        )
        self._admit(call, admission)

    def _admit(self, call: _Call, admission: _Admission) -> None:
        """Raise :class:`BreakerOpen` unless the breaker admits this call now.

        A call granted the half-open probe keeps it while the coordinator
        still reports that same probe window; any outcome that reopened or
        reset the breaker decides instead. A call whose own failure opened
        the breaker (:meth:`_record_failure`) finishes its retry budget while
        the breaker stays open until that same time; a breaker another call
        opened, or reopened since, stops it.
        """
        if not call.policy.circuit_breaker_enabled:
            return
        state = self._coordinator.breaker_check(call.provider)
        if state.probe:
            admission.probes[call.provider] = state.until
            return
        if state.allows:
            return
        probe_until = admission.probes.get(call.provider)
        if (
            probe_until is not None
            and state.status is BreakerStatus.HALF_OPEN
            and state.until == probe_until
        ):
            return
        if (
            state.status is BreakerStatus.OPEN
            and call.provider in admission.tripped
            and state.until == admission.tripped[call.provider]
        ):
            return
        raise BreakerOpen(call.provider, state.until, state.cause)

    def _record_success(self, call: _Call) -> None:
        if call.policy.circuit_breaker_enabled:
            self._coordinator.record_success(call.provider)

    def _record_failure(self, call: _Call, cause: Cause, admission: _Admission) -> None:
        """Count a failed attempt on the shared breaker.

        The failure that brings the count to the threshold opens the
        breaker, and a failure while half-open (the probe's) reopens it;
        that call notes it in *admission* and may finish its retries, while
        every other call is refused at once.
        """
        if not call.policy.circuit_breaker_enabled:
            return
        state = self._coordinator.record_failure(
            call.provider,
            threshold=call.policy.circuit_breaker_threshold,
            cooldown_s=call.policy.circuit_breaker_cooldown_s,
            cause=cause.value,
        )
        # Only the failure the coordinator reports as the opening one, in
        # the same transaction, earns the exception.
        if state.opened:
            admission.tripped[call.provider] = state.until

    def _reserve_quota(self, call: _Call) -> QuotaToken | None:
        if call.quota is None:
            return None
        token = self._coordinator.reserve_quota(
            call.provider,
            daily_limit=call.quota.daily_limit,
            reset_hours=call.quota.reset_hours,
        )
        if token is None:
            raise QuotaSpent(
                call.provider, self._coordinator.quota_status(call.provider)
            )
        return token

    def _release_quota(self, token: QuotaToken | None) -> None:
        if token is not None:
            self._coordinator.release_quota(token)

    def _insecure_retry(
        self,
        call: _Call,
        failure: Failure,
        attempt: int,
        verify: bool,
        credentialed: bool,
    ) -> bool:
        """Report whether a certificate failure earns the unverified retry.

        *credentialed* says whether any hop of the failed attempt, as
        prepared for sending, carried a credential.
        """
        policy = call.policy
        if failure.cause is not Cause.SSL or not verify:
            return False
        if policy.ssl_error_policy != "retry_insecure_once":
            return False
        if attempt >= policy.max_attempts:
            return False
        if credentialed:
            logger.warning(
                "certificate check failed for %s; no unverified retry because "
                "the request carries a credential",
                call.log_url,
            )
            return False
        logger.warning(
            "certificate check failed for %s; retrying once unverified (policy)",
            call.log_url,
        )
        return True

    def _retry_wait(self, call: _Call, failure: Failure, attempt: int) -> float | None:
        """Return the wait before the next attempt, or None to give up."""
        policy = call.policy
        if failure.cause is Cause.DNS:
            retry = policy.dns_retry and attempt < policy.max_attempts
        elif failure.cause is Cause.TRANSIENT:
            cap = policy.max_attempts
            if failure.connect:
                cap = min(cap, CONNECT_FAILURE_MAX_ATTEMPTS)
            retry = attempt < cap
        else:
            retry = False
        if not retry:
            return None
        if failure.retry_after_s is not None:
            return failure.retry_after_s
        return backoff_seconds(
            attempt,
            base_s=policy.base_backoff_s,
            multiplier=policy.backoff_multiplier,
            max_s=policy.max_backoff_s,
        )

    def _final_answer(
        self, call: _Call, failure: Failure, attempt: int
    ) -> RequestFailed:
        """Return the exception of an expected answer (``final_statuses``).

        The provider answered, so the breaker counts a success.
        """
        self._record_success(call)
        logger.info(
            "%s: HTTP %s for %s ends the call",
            call.provider,
            failure.status,
            call.log_url,
        )
        return RequestFailed(
            failure.cause,
            call.log_url,
            status=failure.status,
            attempts=attempt,
            detail=failure.detail,
        )

    def _give_up(self, call: _Call, failure: Failure, attempt: int) -> RequestFailed:
        """Return the final failure's exception; an answer counts as success.

        Breaker failures were recorded attempt by attempt.
        """
        if failure.cause in _SUCCESS_CAUSES:
            self._record_success(call)
        error = RequestFailed(
            failure.cause,
            call.log_url,
            status=failure.status,
            attempts=attempt,
            detail=failure.detail,
        )
        logger.warning("%s: %s", call.provider, error)
        return error


def _stream_body(
    response: requests.Response, part: Path, final: Path, insecure: bool
) -> DownloadOutcome:
    """Write the body to *part*, check it and rename it to *final*.

    Raises:
        _Rejected: The body is unusable; *part* is removed.
    """
    headers = response.headers
    buffer = bytearray()
    size = 0
    try:
        with part.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                if not chunk:
                    continue
                handle.write(chunk)
                size += len(chunk)
                if len(buffer) < HEAD_BYTES:
                    buffer.extend(chunk[: HEAD_BYTES - len(buffer)])
    except BaseException:  # no .part file survives any exit; re-raised
        _remove(part)
        raise
    head = bytes(buffer)
    failure = _payload_failure(response, head, size, final)
    if failure is not None:
        _remove(part)
        raise _Rejected(failure)
    replace_with_retry(final, part)
    content_type = headers.get("Content-Type", "")
    return DownloadOutcome(
        path=final,
        size=size,
        content_type=content_type,
        extension=extension_for_content_type(content_type) or sniff_extension(head),
        filename=content_disposition_filename(headers.get("Content-Disposition")),
        url=redact_url(response.url),
        insecure=insecure,
    )


def _payload_failure(
    response: requests.Response, head: bytes, size: int, final: Path
) -> Failure | None:
    """Return why a fully streamed body is unusable, or None."""
    headers = response.headers
    status = response.status_code
    if is_challenge(headers, head):
        return Failure(Cause.BLOCKED, status=status, detail="captcha or bot challenge")
    if size == 0:
        return Failure(Cause.INCOMPLETE, status=status, detail="no bytes received")
    declared = parse_content_length(headers.get("Content-Length"))
    encoding = (headers.get("Content-Encoding") or "").strip().lower()
    # iter_content yields decoded bytes; Content-Length counts the encoded
    # ones, so only an identity-encoded body can be checked against it.
    if declared is not None and encoding in ("", "identity") and size != declared:
        return Failure(
            Cause.INCOMPLETE,
            status=status,
            detail=f"wrote {size} of {declared} declared bytes",
        )
    declared_type = extension_for_content_type(headers.get("Content-Type", ""))
    for extension in dict.fromkeys((declared_type, final.suffix.lower())):
        reason = magic_mismatch(extension, head)
        if reason:
            return Failure(Cause.INVALID_PAYLOAD, status=status, detail=reason)
    return None


def _remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _answer_text(response: requests.Response) -> str:
    """Return what an expected refusal says, masked and short.

    That is the ``error`` field of a JSON body (read up to
    :data:`ANSWER_BODY_BYTES`, so a long preamble before the field does not
    hide it), else the body's first characters. The request's own credential
    values are masked before anything is cut, so no prefix of an echoed key
    survives.
    """
    text = response.content[:ANSWER_BODY_BYTES].decode("utf-8", "replace")
    request = response.request
    if request is not None:
        for secret in credential_values(str(request.url or ""), request.headers):
            text = text.replace(secret, "***")
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, Mapping) and isinstance(document.get("error"), str):
        text = document["error"]
    text = " ".join(text.split())[:ANSWER_TEXT_CHARS]
    return redact_text(text)
