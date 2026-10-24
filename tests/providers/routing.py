"""Serve an adapter's real hosts from the local scripted server.

The adapters keep their real URLs. :func:`routed_network` builds a real
:class:`~chronodownloader.net.Network` whose session sends a request for any
host other than 127.0.0.1 to the :class:`~tests.net.support.ScriptedServer`
under the path ``/<host><path>`` (:func:`local_path`), so a test scripts
``https://polona.pl/iiif/3/x/full/max/0/default.jpg`` as
``/polona.pl/iiif/3/x/full/max/0/default.jpg``. The coordinator is the
in-memory stand-in and every provider's network block is the fast one, so
nothing waits. Recorded responses live under ``tests/fixtures/providers/``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

from chronodownloader.coord import SlotToken
from chronodownloader.net import Network
from chronodownloader.settings import PROVIDER_DEFAULTS, Settings
from tests.net.support import FAST, NOW, FakeCoordinator, Reply, ScriptedServer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"


def local_path(url: str) -> str:
    """Return the server path that stands for *url* (query string dropped)."""
    parts = urlsplit(url)
    return f"/{parts.hostname}{parts.path}"


class RoutingSession(requests.Session):
    """A session that sends every non-local URL to the scripted server."""

    def __init__(self, server: ScriptedServer) -> None:
        super().__init__()
        self.trust_env = False
        self._server = server

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        parts = urlsplit(url)
        if parts.hostname != "127.0.0.1":
            query = f"?{parts.query}" if parts.query else ""
            url = self._server.url(f"{local_path(url)}{query}")
        return super().request(method, url, **kwargs)


def fast_settings() -> Settings:
    """Return settings whose every provider has the fast network block."""
    providers = {
        key: replace(block, network=FAST) for key, block in PROVIDER_DEFAULTS.items()
    }
    return Settings(providers=MappingProxyType(providers))


@contextmanager
def routed_network(server: ScriptedServer) -> Iterator[Network]:
    """Yield a network client whose requests the scripted server answers."""
    with Network(
        fast_settings(),
        FakeCoordinator(),
        session_factory=lambda: RoutingSession(server),
        sleep=lambda _seconds: None,
        clock=lambda: NOW,
    ) as client:
        yield client


def slot_of(provider: str) -> SlotToken:
    """Return a download slot of *provider* (the stand-in checks none)."""
    return SlotToken(provider, 1, 1, NOW + 120.0)


def fixture_bytes(provider: str, name: str) -> bytes:
    """Return the recorded response *name* of *provider*."""
    return (FIXTURES / provider / name).read_bytes()


def fixture_json(provider: str, name: str) -> Any:
    """Return the recorded JSON response *name* of *provider*, parsed."""
    return json.loads(fixture_bytes(provider, name).decode("utf-8"))


def body_reply(body: bytes, content_type: str, status: int = 200) -> Reply:
    """Return a reply carrying *body* as *content_type*."""
    return Reply(status, body, {"Content-Type": content_type})


def query_of(server: ScriptedServer, path: str) -> dict[str, list[str]]:
    """Return the query parameters of the last request to *path*."""
    for arrival in reversed(server.arrivals):
        if urlsplit(arrival.path).path == path:
            return parse_qs(urlsplit(arrival.path).query, keep_blank_values=True)
    raise AssertionError(f"no request to {path}")


def requested_paths(server: ScriptedServer) -> list[str]:
    """Return the paths of every request, in arrival order."""
    return [urlsplit(arrival.path).path for arrival in server.arrivals]
