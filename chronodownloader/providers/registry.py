"""The provider registry: the 17 keys, lazy construction, the hosts' keys.

Each key names a module ``chronodownloader.providers.<key>`` that defines
``build(settings) -> Provider`` and one provider class whose class
attribute ``hosts`` (a ``frozenset[str]``) names the hosts the library
serves. :class:`Registry` imports and builds an adapter on its first request
and returns the same instance afterwards; a key whose module is not there
yet raises :class:`ProviderMissing`. Tests pass their own factories.

:func:`serving_key` maps a URL onto the key of the provider whose declared
hosts include the URL's host, so that a download an aggregator makes from a
registered library is paced and breaker-checked under that library's key;
:func:`host_key` is the same lookup as the resolver the network client
applies to every redirect hop.
"""

from __future__ import annotations

import functools
import importlib
import threading
from collections.abc import Callable, Mapping
from types import MappingProxyType, ModuleType
from urllib.parse import urlsplit

from chronodownloader.settings import PROVIDER_KEYS, Settings, provider_key

from .base import Provider, ProviderError

__all__ = [
    "PROVIDER_MODULES",
    "ProviderFactory",
    "ProviderMissing",
    "Registry",
    "host_key",
    "host_keys",
    "serving_key",
]

type ProviderFactory = Callable[[Settings], Provider]
"""Builds an adapter from the run's settings."""

PROVIDER_MODULES: Mapping[str, str] = MappingProxyType(
    {key: f"{__package__}.{key}" for key in PROVIDER_KEYS}
)
"""The module of each provider key, in the settings order."""


class ProviderMissing(ProviderError):
    """No adapter exists for the key yet."""

    def __init__(self, provider: str, module: str) -> None:
        super().__init__(provider, f"no adapter module {module}")
        self.module = module


def _module(key: str) -> ModuleType:
    """Return *key*'s adapter module, imported.

    Raises:
        ProviderMissing: The module does not exist.
    """
    module_name = PROVIDER_MODULES[key]
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise ProviderMissing(key, module_name) from exc


def _module_factory(key: str) -> ProviderFactory:
    """Return the ``build`` function of *key*'s module.

    Raises:
        ProviderMissing: The module does not exist.
        TypeError: The module defines no callable ``build``.
    """
    module = _module(key)
    build = getattr(module, "build", None)
    if not callable(build):
        raise TypeError(f"{module.__name__} defines no build(settings) function")
    factory: ProviderFactory = build
    return factory


def _declared_hosts(module: ModuleType) -> frozenset[str]:
    """Return the ``hosts`` of the one class *module* defines with them.

    Raises:
        TypeError: No class or more than one declares ``hosts``.
    """
    declared = [
        value.__dict__["hosts"]
        for value in vars(module).values()
        if isinstance(value, type)
        and value.__module__ == module.__name__
        and "hosts" in value.__dict__
    ]
    if len(declared) != 1 or not isinstance(declared[0], frozenset):
        raise TypeError(f"{module.__name__} needs one class with frozenset hosts")
    return frozenset(str(host).lower() for host in declared[0])


@functools.cache
def host_keys() -> Mapping[str, str]:
    """Return every declared host with the key of the provider serving it.

    Raises:
        TypeError: A module declares no ``hosts``.
        ValueError: Two providers declare the same host.
    """
    table: dict[str, str] = {}
    for key in PROVIDER_KEYS:
        try:
            module = _module(key)
        except ProviderMissing:
            continue
        for host in _declared_hosts(module):
            owner = table.setdefault(host, key)
            if owner != key:
                raise ValueError(f"host {host} is declared by {owner} and {key}")
    return MappingProxyType(table)


def host_key(url: str) -> str | None:
    """Return the key of the provider that serves *url*'s host, or None.

    A declared host covers its subdomains (``archive.org`` covers
    ``ia800300.us.archive.org``); the longest declared match wins. This is
    the resolver a run passes to :class:`~chronodownloader.net.Network`.
    """
    try:
        host = (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return None
    table = host_keys()
    labels = host.split(".")
    for start in range(len(labels)):
        key = table.get(".".join(labels[start:]))
        if key is not None:
            return key
    return None


def serving_key(url: str, caller: str) -> str:
    """Return the key of the provider that serves *url*'s host, else *caller*
    (:func:`host_key`)."""
    return host_key(url) or caller


class Registry:
    """The adapters of one run, built on first use; thread-safe.

    *factories* replaces the module lookup for the keys it names.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        factories: Mapping[str, ProviderFactory] | None = None,
    ) -> None:
        self._settings = settings
        self._factories = dict(factories or {})
        unknown = set(self._factories) - set(PROVIDER_KEYS)
        if unknown:
            raise ValueError(f"unknown provider keys: {sorted(unknown)}")
        self._built: dict[str, Provider] = {}
        self._lock = threading.Lock()

    @property
    def keys(self) -> tuple[str, ...]:
        """Return the 17 provider keys in the settings order."""
        return PROVIDER_KEYS

    def get(self, key: str) -> Provider:
        """Return the adapter of *key*, building it on the first call.

        Raises:
            ValueError: *key* names no provider (``gallica`` gets a hint).
            ProviderMissing: No adapter exists for *key* yet.
        """
        key = provider_key(key)
        with self._lock:
            built = self._built.get(key)
            if built is None:
                factory = self._factories.get(key) or _module_factory(key)
                built = factory(self._settings)
                if built.key != key:
                    raise ValueError(f"the {key} factory built provider {built.key!r}")
                self._built[key] = built
            return built
