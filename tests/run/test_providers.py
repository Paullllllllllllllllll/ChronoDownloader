"""End to end: ``providers`` lists capabilities, keys and coordination state
without writing."""

from __future__ import annotations

from pathlib import Path

import pytest

from chronodownloader import run as core
from chronodownloader.coord import DATABASE_NAME, Coordinator, Holder
from chronodownloader.settings import PROVIDER_KEYS, Settings
from chronodownloader.spec import resolve_providers
from tests.run.support import run_command


def listing(settings: Settings) -> dict[str, core.ProviderState]:
    result = run_command(resolve_providers, settings, {})
    assert isinstance(result.report, core.ProvidersReport)
    assert result.counts.total == len(PROVIDER_KEYS)
    return {state.key: state for state in result.report.providers}


def test_every_provider_is_listed_with_its_capabilities(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(settings.api_keys["europeana"], raising=False)

    states = listing(settings)

    assert list(states) == list(PROVIDER_KEYS)
    assert states["hathitrust"].downloads is False
    assert states["wellcome"].image_max_px == 1024
    assert states["mdz"].iiif
    europeana = states["europeana"]
    assert (europeana.api_key, europeana.key_set) == (
        settings.api_keys["europeana"],
        False,
    )
    assert all(state.breaker == "closed" for state in states.values())


def test_an_open_breaker_is_shown_and_the_database_is_not_written(
    tmp_path: Path, settings: Settings
) -> None:
    state_dir = settings.state_directory()
    with Coordinator(state_dir, settings.coordination, Holder.current("t")) as coord:
        coord.record_failure("mdz", threshold=1, cooldown_s=600.0, cause="blocked")
    database = state_dir / DATABASE_NAME
    before = database.read_bytes()

    states = listing(settings)

    mdz = states["mdz"]
    assert (mdz.breaker, mdz.failures, mdz.breaker_cause) == ("open", 1, "blocked")
    assert mdz.breaker_until is not None
    assert states["bnf_gallica"].breaker == "closed"
    assert database.read_bytes() == before


def test_without_a_coordination_database_nothing_is_created(
    tmp_path: Path, settings: Settings
) -> None:
    listing(settings)

    assert not (settings.state_directory() / DATABASE_NAME).exists()
