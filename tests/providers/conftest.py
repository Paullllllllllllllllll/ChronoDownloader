"""Fixtures of the provider tests: a local server, the real coordination
database in a temporary state folder with a held slot, and a network client
over it."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from chronodownloader.coord import Coordinator, Holder, SlotToken
from chronodownloader.net import Network
from chronodownloader.settings import Coordination
from tests.net.conftest import server
from tests.net.support import settings_with

__all__ = ["coord", "network", "server", "slot"]

PROVIDER = "mdz"


@pytest.fixture
def coord(tmp_path: Path) -> Iterator[Coordinator]:
    """The real coordination database of a temporary state folder."""
    with Coordinator(
        tmp_path / "state", Coordination(), Holder.current("run-test")
    ) as coordinator:
        yield coordinator


@pytest.fixture
def slot(coord: Coordinator) -> SlotToken:
    """A held download slot of the test provider."""
    token = coord.acquire_slot(PROVIDER, 1)
    assert token is not None
    return token


@pytest.fixture
def network(coord: Coordinator) -> Iterator[Network]:
    """A network client without pacing over the real coordinator."""
    with Network(settings_with(PROVIDER), coord, sleep=lambda _seconds: None) as client:
        yield client
