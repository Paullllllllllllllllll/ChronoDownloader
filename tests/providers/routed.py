"""A routed session for adapter tests that keep the libraries' real URLs.

:class:`RoutedSession` answers each request from a route table (the exact
URL first, else the longest matching prefix; anything else is a 404) and
records every call. :func:`routed_network` builds the real
:class:`~chronodownloader.net.Network` over it with the in-memory
coordinator, so pacing, breaker counts and causes are those of the real
client. Fixture files under ``tests/fixtures/providers/<key>/`` load with
:func:`fixture_json` and :func:`fixture_bytes`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests

from chronodownloader.coord import SlotToken
from chronodownloader.net import Network
from tests.iiif.support import JPEG, PDF
from tests.net.support import NOW, FakeCoordinator, response, settings_with

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "providers"

type Route = tuple[int, bytes, Mapping[str, str]]


def fixture_bytes(provider: str, name: str) -> bytes:
    return (FIXTURES / provider / name).read_bytes()


def fixture_json(provider: str, name: str) -> Any:
    return json.loads(fixture_bytes(provider, name).decode("utf-8"))


def json_route(document: Any, status: int = 200) -> Route:
    body = json.dumps(document).encode("utf-8")
    return status, body, {"Content-Type": "application/json"}


def fixture_route(provider: str, name: str) -> Route:
    return 200, fixture_bytes(provider, name), {"Content-Type": "application/json"}


def jpeg_route() -> Route:
    return 200, JPEG, {"Content-Type": "image/jpeg"}


def pdf_route() -> Route:
    return 200, PDF, {"Content-Type": "application/pdf"}


def status_route(status: int) -> Route:
    return status, b"refused", {"Content-Type": "text/plain"}


class RoutedSession(requests.Session):
    """Answers requests from *routes*; ``calls`` records every request."""

    def __init__(self, routes: Mapping[str, Route] | None = None) -> None:
        super().__init__()
        self.routes: dict[str, Route] = dict(routes or {})
        self.calls: list[dict[str, Any]] = []

    def _route(self, url: str) -> Route:
        if url in self.routes:
            return self.routes[url]
        prefixes = [prefix for prefix in self.routes if url.startswith(prefix)]
        if not prefixes:
            return 404, b"not found", {"Content-Type": "text/plain"}
        return self.routes[max(prefixes, key=len)]

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        status, body, headers = self._route(url)
        return response(status, [body], headers, url=url)()

    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]

    def params_of(self, index: int = 0) -> dict[str, Any]:
        """Return the query parameters passed with call *index*."""
        return dict(self.calls[index].get("params") or {})


def routed_network(
    provider: str,
    session: RoutedSession,
    coordinator: FakeCoordinator | None = None,
    **network: Any,
) -> Network:
    """Return the real network client over *session* for *provider*."""
    return Network(
        settings_with(provider, **network),
        coordinator or FakeCoordinator(),
        session_factory=lambda: session,
        sleep=lambda _seconds: None,
        clock=lambda: NOW,
    )


def slot_for(provider: str) -> SlotToken:
    return SlotToken(provider, 1, 1, NOW + 120.0)


def staged_names(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )
