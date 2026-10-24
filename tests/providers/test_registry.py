"""The provider registry: the 17 keys, lazy construction, the hosts' keys."""

from __future__ import annotations

import sys
import threading

import pytest

from chronodownloader.providers.base import Provider
from chronodownloader.providers.registry import (
    PROVIDER_MODULES,
    ProviderMissing,
    Registry,
    host_keys,
    serving_key,
)
from chronodownloader.settings import PROVIDER_KEYS, Settings

from .fakes import FakeIIIFProvider


def test_the_registry_knows_the_seventeen_settings_keys() -> None:
    registry = Registry(Settings())
    assert registry.keys == PROVIDER_KEYS
    assert len(registry.keys) == 17
    assert "bnf_gallica" in registry.keys
    assert set(PROVIDER_MODULES) == set(PROVIDER_KEYS)
    assert PROVIDER_MODULES["mdz"] == "chronodownloader.providers.mdz"


def test_a_provider_is_built_on_first_use_and_kept() -> None:
    settings = Settings()
    calls: list[Settings] = []

    def build(received: Settings) -> Provider:
        calls.append(received)
        return FakeIIIFProvider({})

    registry = Registry(settings, factories={"mdz": build})
    assert calls == []
    first = registry.get("mdz")
    assert registry.get(" mdz ") is first
    assert calls == [settings]


def test_concurrent_first_uses_build_once() -> None:
    calls: list[int] = []

    def build(_settings: Settings) -> Provider:
        calls.append(1)
        return FakeIIIFProvider({})

    registry = Registry(Settings(), factories={"mdz": build})
    built: list[Provider] = []
    threads = [
        threading.Thread(target=lambda: built.append(registry.get("mdz")))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
    assert len({id(provider) for provider in built}) == 1


def test_gallica_gets_the_hint_to_its_key() -> None:
    with pytest.raises(ValueError, match="bnf_gallica"):
        Registry(Settings()).get("gallica")


def test_a_key_without_an_adapter_module_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None entry makes the import fail as if the module did not exist.
    monkeypatch.setitem(sys.modules, "chronodownloader.providers.loc", None)
    with pytest.raises(ProviderMissing) as excinfo:
        Registry(Settings()).get("loc")
    assert excinfo.value.module == "chronodownloader.providers.loc"


def test_factories_for_unknown_keys_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown provider keys"):
        Registry(Settings(), factories={"gallica": lambda _s: FakeIIIFProvider({})})


def test_every_provider_class_declares_its_hosts() -> None:
    table = host_keys()
    assert set(table.values()) == set(PROVIDER_KEYS) - {"annas_archive"}
    assert table["gallica.bnf.fr"] == "bnf_gallica"


@pytest.mark.parametrize(
    ("url", "key"),
    [
        (
            "https://gallica.bnf.fr/iiif/ark:/12148/x/f1/full/full/0/native.jpg",
            "bnf_gallica",
        ),
        ("http://GALLICA.bnf.fr./view3if/ga/x", "bnf_gallica"),
        ("https://ia800300.us.archive.org/1/items/x/x.pdf", "internet_archive"),
        (
            "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb1/manifest",
            "mdz",
        ),
        ("https://iiif.europeana.eu/presentation/1/x/manifest", "europeana"),
        ("https://digi.ub.uni-heidelberg.de/diglit/iiif/x/manifest.json", "ddb"),
        ("https://data.bnf.fr/x", "ddb"),
        ("not a url", "ddb"),
    ],
)
def test_serving_key_maps_a_host_onto_its_provider(url: str, key: str) -> None:
    assert serving_key(url, "ddb") == key


def test_a_factory_must_build_its_own_key() -> None:
    registry = Registry(
        Settings(), factories={"loc": lambda _s: FakeIIIFProvider({}, key="mdz")}
    )
    with pytest.raises(ValueError, match="built provider 'mdz'"):
        registry.get("loc")
