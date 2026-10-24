"""Fixtures of the network tests: a local server, a coordinator stand-in and a
client factory that closes what it builds."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from chronodownloader.net import Network
from chronodownloader.settings import Settings

from .support import NOW, FakeCoordinator, ScriptedServer


@pytest.fixture
def server() -> Iterator[ScriptedServer]:
    """A scripted HTTP server on 127.0.0.1."""
    running = ScriptedServer()
    running.start()
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
def coordinator() -> FakeCoordinator:
    return FakeCoordinator()


@pytest.fixture
def sleeps() -> list[float]:
    """The backoff waits the client asked for, in order."""
    return []


type NetworkFactory = Callable[..., Network]


@pytest.fixture
def make_network(
    coordinator: FakeCoordinator, sleeps: list[float]
) -> Iterator[NetworkFactory]:
    """Build clients over the fake coordinator; recorded sleeps, fixed clock."""
    built: list[Network] = []

    def make(settings: Settings, **kwargs: Any) -> Network:
        kwargs.setdefault("sleep", sleeps.append)
        kwargs.setdefault("clock", lambda: NOW)
        client = Network(settings, kwargs.pop("coord", coordinator), **kwargs)
        built.append(client)
        return client

    yield make
    for client in built:
        client.close()
