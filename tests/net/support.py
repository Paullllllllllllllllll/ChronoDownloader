"""Test doubles of the network tests.

A scripted HTTP server on 127.0.0.1 records arrivals and answers each path
with its scripted replies; :class:`FakeCoordinator` keeps pacing
reservations, breaker counts and quota units in memory; scripted sessions
raise transport errors or return hand-built responses; :func:`settings_with`
builds settings with a fast network block for one provider.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import requests

from chronodownloader.coord import (
    BreakerState,
    BreakerStatus,
    QuotaStatus,
    QuotaToken,
    SlotToken,
)
from chronodownloader.settings import (
    PROVIDER_DEFAULTS,
    AnnasArchiveSettings,
    AnnasQuota,
    Coordination,
    NetworkSettings,
    Settings,
)

NOW = 1_800_000_000.0
"""The wall clock the tests hand to the network client."""

FAST = NetworkSettings(
    delay_ms=0,
    jitter_ms=0,
    max_attempts=3,
    base_backoff_s=0.5,
    backoff_multiplier=2.0,
    max_backoff_s=30.0,
    timeout_s=5.0,
    circuit_breaker_threshold=100,
)
"""A network block without pacing; backoffs are recorded, not slept. The
breaker threshold is high so that retry budgets play out; breaker tests set
their own."""

SLOT = SlotToken("mdz", 1, 1, NOW + 120.0)


def settings_with(
    provider: str = "mdz", *, quota: AnnasQuota | None = None, **network: Any
) -> Settings:
    """Return settings whose *provider* block is :data:`FAST` with *network*."""
    providers = dict(PROVIDER_DEFAULTS)
    block = replace(providers[provider], network=replace(FAST, **network))
    if quota is not None:
        assert isinstance(block, AnnasArchiveSettings)
        block = replace(block, quota=quota)
    providers[provider] = block
    return Settings(providers=MappingProxyType(providers))


@dataclass(frozen=True)
class Reply:
    """A scripted answer; ``send_bytes`` truncates the body and drops the line."""

    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    send_bytes: int | None = None


@dataclass(frozen=True)
class Arrival:
    """A request the server received."""

    method: str
    path: str
    at: float
    headers: Mapping[str, str]


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._serve()

    def _serve(self) -> None:
        owner = self.server.owner
        reply = owner.arrive(self.command, self.path, dict(self.headers))
        self.send_response(reply.status)
        for name, value in reply.headers.items():
            self.send_header(name, value)
        if not any(name.lower() == "content-length" for name in reply.headers):
            self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        body = (
            reply.body if reply.send_bytes is None else reply.body[: reply.send_bytes]
        )
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output quiet."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, owner: ScriptedServer) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.owner = owner


class ScriptedServer:
    """A local HTTP server answering each path with its scripted replies.

    A path's replies are used in order and the last one repeats; an
    unscripted path answers 404.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scripts: dict[str, list[Reply]] = {}
        self.arrivals: list[Arrival] = []
        self._server = _Server(self)
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.01,), daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def url(self, path: str, *, scheme: str = "http") -> str:
        return f"{scheme}://127.0.0.1:{self.port}{path}"

    def script(self, path: str, *replies: Reply) -> str:
        """Script *path* and return its URL."""
        with self._lock:
            self._scripts[path] = list(replies)
        return self.url(path)

    def hits(self, path: str) -> int:
        with self._lock:
            return sum(1 for arrival in self.arrivals if arrival.path == path)

    def arrive(self, method: str, target: str, headers: dict[str, str]) -> Reply:
        path = urlsplit(target).path
        with self._lock:
            self.arrivals.append(Arrival(method, target, time.monotonic(), headers))
            replies = self._scripts.get(path)
            if not replies:
                return Reply(404, b"not found")
            return replies.pop(0) if len(replies) > 1 else replies[0]


@dataclass(frozen=True)
class Reservation:
    """One ``wait_turn`` call."""

    provider: str
    delay_s: float
    jitter_s: float
    slot: SlotToken | None


class FakeCoordinator:
    """In-memory pacing, breaker, quota and slots with the Coordinator's
    methods; slots are always granted and recorded in ``slots_taken``."""

    def __init__(self, clock: Callable[[], float] = lambda: NOW) -> None:
        self.clock = clock
        self.settings = Coordination()
        self.slots_taken: list[str] = []
        self.slots_released: list[str] = []
        self.reservations: list[Reservation] = []
        self.successes: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.breakers: dict[str, BreakerState] = {}
        self.counts: dict[str, int] = {}
        self.quota_used: dict[str, int] = {}
        self.released: list[QuotaToken] = []
        self.on_wait: Callable[[str], None] | None = None

    def wait_turn(
        self,
        provider: str,
        delay_s: float,
        jitter_s: float,
        *,
        slot: SlotToken | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.reservations.append(Reservation(provider, delay_s, jitter_s, slot))
        if self.on_wait is not None:
            self.on_wait(provider)

    def breaker_check(self, provider: str) -> BreakerState:
        return self.breakers.get(provider, BreakerState(BreakerStatus.CLOSED))

    def record_success(self, provider: str) -> None:
        self.successes.append(provider)
        self.counts[provider] = 0
        self.breakers.pop(provider, None)

    def record_failure(
        self, provider: str, *, threshold: int, cooldown_s: float, cause: str
    ) -> BreakerState:
        self.failures.append((provider, cause))
        count = self.counts.get(provider, 0) + 1
        self.counts[provider] = count
        if count >= threshold and provider not in self.breakers:
            state = BreakerState(
                BreakerStatus.OPEN, count, self.clock() + cooldown_s, cause
            )
            self.breakers[provider] = state
            return replace(state, opened=True)
        return self.breaker_check(provider)

    def reserve_quota(
        self, provider: str, *, daily_limit: int, reset_hours: float
    ) -> QuotaToken | None:
        used = self.quota_used.get(provider, 0)
        if used >= daily_limit:
            return None
        self.quota_used[provider] = used + 1
        return QuotaToken(provider, 0.0, used + 1)

    def release_quota(self, token: QuotaToken) -> None:
        self.released.append(token)
        self.quota_used[token.provider] -= 1

    def quota_status(self, provider: str) -> QuotaStatus:
        return QuotaStatus(
            provider, self.quota_used.get(provider, 0), None, self.clock() + 3600.0
        )

    def acquire_slot(self, provider: str, limit: int) -> SlotToken | None:
        self.slots_taken.append(provider)
        return SlotToken(provider, 0, len(self.slots_taken), self.clock() + 120.0)

    def renew_slot(self, token: SlotToken) -> SlotToken:
        return token

    def release_slot(self, token: SlotToken) -> None:
        self.slots_released.append(token.provider)


class RecordingSession(requests.Session):
    """A real session that records the keyword arguments of every request."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return super().request(method, url, **kwargs)


type Outcome = BaseException | Callable[[], requests.Response]


class ScriptedSession(requests.Session):
    """A session whose requests raise or answer from a script, in order.

    The last outcome repeats.
    """

    def __init__(self, *outcomes: Outcome) -> None:
        super().__init__()
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        outcome = (
            self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        )
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome()


class _Raw:
    """A urllib3-shaped body: yields *chunks*, then raises *error* if given."""

    def __init__(self, chunks: list[bytes], error: BaseException | None) -> None:
        self._chunks = chunks
        self._error = error

    def stream(
        self, amt: int | None = None, decode_content: bool = True
    ) -> Iterator[bytes]:
        yield from self._chunks
        if self._error is not None:
            raise self._error

    def close(self) -> None:
        """Nothing to release."""


def response(
    status: int = 200,
    chunks: list[bytes] | None = None,
    headers: Mapping[str, str] | None = None,
    error: BaseException | None = None,
    url: str = "https://example.org/book.pdf",
) -> Callable[[], requests.Response]:
    """Return a factory of hand-built responses for :class:`ScriptedSession`."""

    def build() -> requests.Response:
        built = requests.Response()
        built.status_code = status
        built.headers = requests.structures.CaseInsensitiveDict(dict(headers or {}))
        built.url = url
        built.raw = _Raw(list(chunks or []), error)
        return built

    return build
