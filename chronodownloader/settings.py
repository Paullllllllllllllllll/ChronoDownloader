"""Typed settings: machine values and optional run defaults.

:func:`load` reads ``--settings FILE`` when given, else ``config/settings.yaml``
in the repository (gitignored), else the packaged
``resources/settings.example.yaml``; without any file the code values apply.
Every key is optional and falls back to the code value; unknown keys and
values of the wrong type are errors (:class:`SettingsError`, exit 2 at the
CLI). Relative paths in the file resolve against the current directory,
except ``state_dir``, which must be absolute (``~`` is expanded) so that every
process on the machine shares one coordination database.

The ``defaults`` block sets run options by option name. It is validated
against the option table the caller passes (the ``download`` table); a flag
overrides it, and it overrides the code default.

A provider block holds the network policy, download slots, page limit and
extras of one of the 17 provider keys. A provider or key missing in the file
keeps the value of the provider's built-in profile in
:data:`PROVIDER_DEFAULTS`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from chronodownloader.options.options import OptionTable
from chronodownloader.options.settings_loader import SettingsError, load_settings
from chronodownloader.options.settings_schema import (
    Reader,
    ValueKind,
    log_file,
    read_logs_dir,
    read_state_dir,
    state_directory,
)

__all__ = [
    "DEFAULT_API_KEYS",
    "DEFAULT_LOGS_DIR",
    "DEFAULT_SETTINGS_PATH",
    "EXAMPLE_SETTINGS_PATH",
    "PROVIDER_DEFAULTS",
    "PROVIDER_KEYS",
    "AnnasArchiveSettings",
    "AnnasQuota",
    "Coordination",
    "DownloadLimits",
    "GoogleBooksSettings",
    "Judge",
    "JudgeNetwork",
    "JudgeThresholds",
    "Naming",
    "NetworkSettings",
    "PerWorkLimits",
    "ProviderSettings",
    "Settings",
    "SettingsError",
    "TotalLimits",
    "load",
    "provider_key",
]

_PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SETTINGS_PATH = _PACKAGE_DIR.parent / "config" / "settings.yaml"
EXAMPLE_SETTINGS_PATH = _PACKAGE_DIR / "resources" / "settings.example.yaml"
DEFAULT_LOGS_DIR = _PACKAGE_DIR.parent / "logs"
STATE_DIR_NAME = ".chronodownloader"
"""The state folder in the home folder when ``state_dir`` is unset."""

PROVIDER_KEYS: tuple[str, ...] = (
    "internet_archive",
    "bnf_gallica",
    "loc",
    "europeana",
    "dpla",
    "ddb",
    "british_library",
    "mdz",
    "polona",
    "bne",
    "google_books",
    "hathitrust",
    "wellcome",
    "annas_archive",
    "slub",
    "e_rara",
    "sbb_digital",
)
"""The provider keys, as in v1.30.3."""

_GALLICA_HINT = "the Gallica key is bnf_gallica"

DEFAULT_API_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "europeana": "EUROPEANA_API_KEY",
        "dpla": "DPLA_API_KEY",
        "ddb": "DDB_API_KEY",
        "google_books": "GOOGLE_BOOKS_API_KEY",
        "hathitrust": "HATHI_API_KEY",
        "annas_archive": "ANNAS_ARCHIVE_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
)
"""The environment variable holding each service's key."""

SSL_ERROR_POLICIES = ("fail", "retry_insecure_once")
GOOGLE_BOOKS_FORMATS = ("pdf", "epub")
ON_EXCEED = ("stop", "skip")


def provider_key(value: str) -> str:
    """Convert a provider key; ``gallica`` gets a hint to ``bnf_gallica``.

    Raises:
        ValueError: The key names no provider.
    """
    key = value.strip()
    if key in PROVIDER_KEYS:
        return key
    hint = f"; {_GALLICA_HINT}" if key == "gallica" else ""
    raise ValueError(f"unknown provider {key!r}{hint}")


def _no_headers() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True)
class NetworkSettings:
    """Pacing, retries, TLS and circuit breaker of one provider's requests.

    ``delay_ms`` is the minimum pause between requests, ``jitter_ms`` a
    random extra pause up to that value. ``max_attempts`` counts every try,
    the first included; the wait after a failed try starts at
    ``base_backoff_s``, grows by ``backoff_multiplier`` and is capped at
    ``max_backoff_s``. ``impersonate`` names a curl_cffi browser profile, or
    None. The breaker opens after ``circuit_breaker_threshold`` consecutive
    failures and probes again after ``circuit_breaker_cooldown_s``.
    """

    delay_ms: int = 0
    jitter_ms: int = 0
    max_attempts: int = 5
    base_backoff_s: float = 1.5
    backoff_multiplier: float = 1.5
    max_backoff_s: float = 60.0
    timeout_s: float = 30.0
    verify_ssl: bool = True
    ssl_error_policy: str = "fail"
    dns_retry: bool = False
    impersonate: str | None = None
    headers: Mapping[str, str] = field(default_factory=_no_headers)
    circuit_breaker_enabled: bool = True
    circuit_breaker_threshold: int = 3
    circuit_breaker_cooldown_s: float = 300.0


_PACING_KEYS: tuple[tuple[str, ValueKind], ...] = (
    ("delay_ms", "integer"),
    ("jitter_ms", "integer"),
    ("max_attempts", "positive"),
    ("base_backoff_s", "number"),
    ("backoff_multiplier", "number"),
    ("max_backoff_s", "number"),
    ("timeout_s", "number"),
)

_NETWORK_FLAT_KEYS: tuple[tuple[str, ValueKind], ...] = (
    *_PACING_KEYS,
    ("verify_ssl", "flag"),
    ("dns_retry", "flag"),
    ("circuit_breaker_enabled", "flag"),
    ("circuit_breaker_threshold", "positive"),
    ("circuit_breaker_cooldown_s", "number"),
)

_NETWORK_KEYS = frozenset(key for key, _ in _NETWORK_FLAT_KEYS) | {
    "ssl_error_policy",
    "impersonate",
    "headers",
}


@dataclass(frozen=True)
class ProviderSettings:
    """One provider's page limit, download slots and network policy.

    ``max_pages`` 0 sets no page limit; ``slots`` None takes
    ``coordination.default_slots``.
    """

    max_pages: int = 0
    slots: int | None = None
    network: NetworkSettings = field(default_factory=NetworkSettings)


@dataclass(frozen=True)
class GoogleBooksSettings(ProviderSettings):
    """Google Books: the provider block plus its download extras.

    ``max_files`` bounds the direct pdf or epub files per volume;
    ``max_pages`` bounds the page-by-page image fallback.
    """

    free_only: bool = True
    prefer: str = "pdf"
    allow_drm: bool = False
    max_files: int = 2


@dataclass(frozen=True)
class AnnasQuota:
    """The daily quota of Anna's Archive fast downloads."""

    enabled: bool = True
    daily_limit: int = 875
    reset_hours: float = 24.0


@dataclass(frozen=True)
class AnnasArchiveSettings(ProviderSettings):
    """Anna's Archive: the provider block plus mirror URL and quota.

    ``base_url`` is the mirror; Anna's Archive rotates its domains, and an
    empty value makes the provider unavailable.
    """

    base_url: str = "https://annas-archive.gl"
    quota: AnnasQuota = field(default_factory=AnnasQuota)


_PROVIDER_COMMON_KEYS = frozenset({"max_pages", "slots", "network"})
_GOOGLE_BOOKS_KEYS = frozenset({"free_only", "prefer", "allow_drm", "max_files"})
_ANNAS_KEYS = frozenset({"base_url", "quota"})


def _headers(**headers: str) -> Mapping[str, str]:
    return MappingProxyType(headers)


PROVIDER_DEFAULTS: Mapping[str, ProviderSettings] = MappingProxyType(
    {
        "internet_archive": ProviderSettings(
            slots=2,
            network=NetworkSettings(delay_ms=300, jitter_ms=150, max_attempts=8),
        ),
        "bnf_gallica": ProviderSettings(
            slots=1,
            network=NetworkSettings(
                delay_ms=8000,
                jitter_ms=2000,
                max_attempts=25,
                base_backoff_s=3.0,
                backoff_multiplier=1.8,
                max_backoff_s=120.0,
                impersonate="chrome",
            ),
        ),
        "loc": ProviderSettings(
            network=NetworkSettings(
                delay_ms=300,
                jitter_ms=150,
                headers=_headers(Accept="application/json"),
            )
        ),
        "europeana": ProviderSettings(
            network=NetworkSettings(delay_ms=400, jitter_ms=200, max_attempts=8)
        ),
        "dpla": ProviderSettings(
            network=NetworkSettings(
                delay_ms=400,
                jitter_ms=200,
                max_attempts=8,
                headers=_headers(Accept="application/json"),
            )
        ),
        "ddb": ProviderSettings(network=NetworkSettings(delay_ms=400, jitter_ms=200)),
        "british_library": ProviderSettings(),
        "mdz": ProviderSettings(
            slots=1,
            network=NetworkSettings(delay_ms=300, jitter_ms=150, max_attempts=25),
        ),
        "polona": ProviderSettings(
            network=NetworkSettings(delay_ms=600, jitter_ms=200, max_attempts=8)
        ),
        "bne": ProviderSettings(
            network=NetworkSettings(
                delay_ms=4000,
                jitter_ms=1000,
                base_backoff_s=2.0,
                backoff_multiplier=1.8,
                max_backoff_s=120.0,
                timeout_s=60.0,
                impersonate="chrome",
            )
        ),
        "google_books": GoogleBooksSettings(
            max_pages=50,
            slots=1,
            network=NetworkSettings(
                delay_ms=2000,
                jitter_ms=500,
                max_attempts=3,
                base_backoff_s=2.0,
                backoff_multiplier=2.0,
                max_backoff_s=30.0,
                circuit_breaker_threshold=2,
                circuit_breaker_cooldown_s=600.0,
                headers=_headers(Referer="https://books.google.com/"),
            ),
        ),
        "hathitrust": ProviderSettings(),
        "wellcome": ProviderSettings(
            slots=1,
            network=NetworkSettings(delay_ms=300, jitter_ms=150, max_attempts=8),
        ),
        "annas_archive": AnnasArchiveSettings(
            slots=1,
            network=NetworkSettings(
                delay_ms=800,
                jitter_ms=300,
                max_attempts=5,
                base_backoff_s=2.0,
                backoff_multiplier=2.0,
                max_backoff_s=45.0,
                timeout_s=45.0,
            ),
        ),
        "slub": ProviderSettings(
            slots=1,
            network=NetworkSettings(delay_ms=600, jitter_ms=200, max_attempts=8),
        ),
        "e_rara": ProviderSettings(
            slots=1,
            network=NetworkSettings(delay_ms=600, jitter_ms=200, max_attempts=8),
        ),
        "sbb_digital": ProviderSettings(
            slots=1,
            network=NetworkSettings(
                delay_ms=700,
                jitter_ms=250,
                max_attempts=8,
                timeout_s=40.0,
                headers=_headers(Accept="application/xml"),
            ),
        ),
    }
)
"""The built-in profile of each provider: the code values of its block.

Phase 3 of the v1.0.0 rewrite moves each profile into its provider class.
"""


TEXT_SCORE_MAX = 123.5
"""The highest text score total (title 100, creator bonus 20, boosts 3.5)."""


@dataclass(frozen=True)
class JudgeThresholds:
    """Decision thresholds per matching mode.

    ``same_work_*`` are probabilities (judge, hybrid): at or above accept,
    at or below reject, review between. ``text_*`` apply to the text score
    total of :mod:`chronodownloader.matching.fuzzy` (fuzzy accept and
    reject, hybrid prefilter); None never fires. The text defaults come from
    the jev_eval fixture: pairs of different works (a translation among them)
    reach every text score a same-work pair reaches, so fuzzy accepts nothing
    on its own and its matches go to review; the lowest same-work pair scores
    35.5, and 30 leaves a margin below it.
    """

    same_work_accept: float = 0.9
    same_work_reject: float = 0.1
    text_accept: float | None = None
    text_reject: float | None = 30.0
    text_prefilter: float | None = 30.0


@dataclass(frozen=True)
class JudgeNetwork:
    """Pacing, retries and timeout of the judge endpoint, and its slots."""

    delay_ms: int = 0
    jitter_ms: int = 0
    max_attempts: int = 3
    base_backoff_s: float = 1.5
    backoff_multiplier: float = 1.5
    max_backoff_s: float = 60.0
    timeout_s: float = 300.0
    slots: int = 4

    def network_settings(self) -> NetworkSettings:
        """Return the endpoint's policy as the network block of provider ``judge``.

        Pacing, retries and timeout come from this block; the rest keeps the
        code values: certificates verified with ``ssl_error_policy`` fail, no
        DNS retry, no impersonation, no extra headers, and the circuit breaker
        enabled with its default threshold and cooldown.
        """
        return NetworkSettings(
            delay_ms=self.delay_ms,
            jitter_ms=self.jitter_ms,
            max_attempts=self.max_attempts,
            base_backoff_s=self.base_backoff_s,
            backoff_multiplier=self.backoff_multiplier,
            max_backoff_s=self.max_backoff_s,
            timeout_s=self.timeout_s,
        )


@dataclass(frozen=True)
class Judge:
    """The decision model that judges candidates and pages.

    ``model`` gives the text judgments, ``page_model`` the page check;
    ``retry_after_minutes`` is the deferral after a persistent outage.
    """

    endpoint: str = "https://openrouter.ai/api/alpha/decisions"
    model: str = "typesafe/jev-1.14"
    page_model: str = "openai/gpt-6-luna-decisions"
    retry_after_minutes: float = 60.0
    thresholds: JudgeThresholds = field(default_factory=JudgeThresholds)
    network: JudgeNetwork = field(default_factory=JudgeNetwork)


@dataclass(frozen=True)
class Coordination:
    """Machine-wide coordination of providers and the judge across agents."""

    default_slots: int = 2
    pacing_tolerance_ms: int = 250
    slot_ttl_s: float = 120.0
    slot_renew_s: float = 30.0
    busy_timeout_s: float = 30.0


_COORDINATION_KEYS: tuple[tuple[str, ValueKind], ...] = (
    ("default_slots", "positive"),
    ("pacing_tolerance_ms", "integer"),
    ("slot_ttl_s", "number"),
    ("slot_renew_s", "number"),
    ("busy_timeout_s", "number"),
)


@dataclass(frozen=True)
class TotalLimits:
    """Size limits of a run in GB; 0 sets no limit."""

    images_gb: float = 100.0
    pdfs_gb: float = 50.0
    metadata_gb: float = 1.0


@dataclass(frozen=True)
class PerWorkLimits:
    """Size limits of one work in GB (metadata in MB); 0 sets no limit."""

    images_gb: float = 5.0
    pdfs_gb: float = 3.0
    metadata_mb: float = 250.0


@dataclass(frozen=True)
class DownloadLimits:
    """Run and per-work size limits; ``on_exceed`` is ``stop`` or ``skip``."""

    total: TotalLimits = field(default_factory=TotalLimits)
    per_work: PerWorkLimits = field(default_factory=PerWorkLimits)
    on_exceed: str = "stop"


@dataclass(frozen=True)
class Naming:
    """Work-folder names of new runs."""

    include_creator_in_work_dir: bool = False
    include_year_in_work_dir: bool = False
    title_slug_max_len: int = 80


_NAMING_KEYS: tuple[tuple[str, ValueKind], ...] = (
    ("include_creator_in_work_dir", "flag"),
    ("include_year_in_work_dir", "flag"),
    ("title_slug_max_len", "positive"),
)


def _default_providers() -> Mapping[str, ProviderSettings]:
    return PROVIDER_DEFAULTS


def _no_defaults() -> Mapping[str, Any]:
    return MappingProxyType({})


@dataclass(frozen=True)
class Settings:
    """Machine values and the optional run defaults.

    ``providers`` holds a block for each of the 17 provider keys;
    ``defaults`` holds the converted run defaults by option name.
    """

    api_keys: Mapping[str, str] = field(default_factory=lambda: DEFAULT_API_KEYS)
    judge: Judge = field(default_factory=Judge)
    coordination: Coordination = field(default_factory=Coordination)
    providers: Mapping[str, ProviderSettings] = field(
        default_factory=_default_providers
    )
    download_limits: DownloadLimits = field(default_factory=DownloadLimits)
    naming: Naming = field(default_factory=Naming)
    state_dir: Path | None = None
    logs_dir: Path | None = None
    defaults: Mapping[str, Any] = field(default_factory=_no_defaults)
    source: Path | None = None

    def state_directory(self) -> Path:
        """Return the folder of the tool's state files: ``state_dir``, else
        ``~/.chronodownloader``.

        Raises:
            SettingsError: ``state_dir`` is relative after ``~`` expansion.
        """
        directory = state_directory(self.state_dir, STATE_DIR_NAME)
        if not directory.is_absolute():
            raise SettingsError(f"state_dir: {_STATE_DIR_ABSOLUTE}, got {directory}")
        return directory

    def log_file(self) -> Path:
        """Return the log file: ``logs_dir`` names a ``.log`` file or its folder.

        Without ``logs_dir`` the file is ``application.log`` in the
        repository's ``logs`` folder.
        """
        return log_file(self.logs_dir, DEFAULT_LOGS_DIR)

    def api_key_env(self, service: str) -> str:
        """Return the environment variable that holds the key of a service.

        Raises:
            SettingsError: The service has no key variable.
        """
        try:
            return self.api_keys[service]
        except KeyError:
            raise SettingsError(f"no API key variable for {service!r}") from None

    def slots(self, provider: str) -> int:
        """Return a provider's download slots, else ``coordination.default_slots``.

        Raises:
            SettingsError: The key names no provider.
        """
        try:
            slots = self.providers[provider].slots
        except KeyError:
            raise SettingsError(f"unknown provider {provider!r}") from None
        return self.coordination.default_slots if slots is None else slots

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        defaults: Mapping[str, Any] | None = None,
        source: Path | None = None,
        base: Path | None = None,
    ) -> Settings:
        """Build settings from parsed YAML; missing keys keep code values.

        *data* holds every top-level key except ``defaults``, whose converted
        values come in *defaults*. Relative paths resolve against *base*, by
        default the current directory.

        Raises:
            SettingsError: An unknown key or a value of the wrong type.
        """
        where = str(source) if source is not None else "settings"
        reader = Reader(where, base)
        reader.keys(data, _TOP_KEYS, "")
        return cls(
            api_keys=_api_keys(reader, data.get("api_keys")),
            judge=_judge(reader, data.get("judge")),
            coordination=_coordination(reader, data.get("coordination")),
            providers=_providers(reader, data.get("providers")),
            download_limits=_download_limits(reader, data.get("download_limits")),
            naming=_naming(reader, data.get("naming")),
            state_dir=_state_dir(reader, data),
            logs_dir=read_logs_dir(reader, data),
            defaults=MappingProxyType(dict(defaults or {})),
            source=source,
        )


_TOP_KEYS = frozenset(
    {
        "api_keys",
        "judge",
        "coordination",
        "providers",
        "download_limits",
        "naming",
        "state_dir",
        "logs_dir",
    }
)

_STATE_DIR_ABSOLUTE = "must be an absolute path (~ is expanded)"


def _state_dir(reader: Reader, data: Mapping[str, Any]) -> Path | None:
    """Return ``state_dir`` with ``~`` expanded, or None for '' (the default).

    A relative path is an error: the coordination database must be one file
    per machine whatever the current directory.
    """
    value = read_state_dir(reader, data)
    if value is None:
        return None
    expanded = value.expanduser()
    if not expanded.is_absolute():
        raise reader.fail("state_dir", _STATE_DIR_ABSOLUTE)
    return expanded


def _api_keys(reader: Reader, raw: Any) -> Mapping[str, str]:
    section = reader.section(raw, "api_keys", frozenset(DEFAULT_API_KEYS))
    return MappingProxyType(
        {
            service: reader.text(section, service, default, "api_keys.") or default
            for service, default in DEFAULT_API_KEYS.items()
        }
    )


def _nonempty(reader: Reader, raw: Mapping[str, Any], key: str, default: str) -> str:
    value = reader.text(raw, key, default, "judge.")
    if not value:
        raise reader.fail("judge." + key, "must not be empty")
    return value


def _bounded(
    reader: Reader, raw: Mapping[str, Any], key: str, default: float, top: float
) -> float:
    """Return a threshold from 0 to *top*; a missing or null key keeps *default*."""
    path = "judge.thresholds."
    value = reader.number(raw, key, default, path)
    if value > top:
        raise reader.fail(path + key, f"must be at most {top:g}")
    return value


def _score(
    reader: Reader, raw: Mapping[str, Any], key: str, default: float | None
) -> float | None:
    """Return a text score total from 0 to :data:`TEXT_SCORE_MAX`; a missing
    key keeps *default*, an explicit null switches the threshold off."""
    if key not in raw:
        return default
    if raw[key] is None:
        return None
    return _bounded(reader, raw, key, 0.0, TEXT_SCORE_MAX)


def _judge(reader: Reader, raw: Any) -> Judge:
    keys = frozenset(
        {
            "endpoint",
            "model",
            "page_model",
            "retry_after_minutes",
            "thresholds",
            "network",
        }
    )
    section = reader.section(raw, "judge", keys)
    base = Judge()
    return Judge(
        endpoint=_nonempty(reader, section, "endpoint", base.endpoint),
        model=_nonempty(reader, section, "model", base.model),
        page_model=_nonempty(reader, section, "page_model", base.page_model),
        retry_after_minutes=reader.number(
            section, "retry_after_minutes", base.retry_after_minutes, "judge."
        ),
        thresholds=_thresholds(reader, section.get("thresholds")),
        network=_judge_network(reader, section.get("network")),
    )


def _thresholds(reader: Reader, raw: Any) -> JudgeThresholds:
    keys = frozenset(
        {
            "same_work_accept",
            "same_work_reject",
            "text_accept",
            "text_reject",
            "text_prefilter",
        }
    )
    section = reader.section(raw, "judge.thresholds", keys)
    base = JudgeThresholds()
    return JudgeThresholds(
        same_work_accept=_bounded(
            reader, section, "same_work_accept", base.same_work_accept, 1.0
        ),
        same_work_reject=_bounded(
            reader, section, "same_work_reject", base.same_work_reject, 1.0
        ),
        text_accept=_score(reader, section, "text_accept", base.text_accept),
        text_reject=_score(reader, section, "text_reject", base.text_reject),
        text_prefilter=_score(reader, section, "text_prefilter", base.text_prefilter),
    )


def _judge_network(reader: Reader, raw: Any) -> JudgeNetwork:
    keys = (*_PACING_KEYS, ("slots", "positive"))
    allowed = frozenset(key for key, _ in keys)
    section = reader.section(raw, "judge.network", allowed)
    base = JudgeNetwork()
    return JudgeNetwork(**reader.flat(section, keys, base, "judge.network."))


def _coordination(reader: Reader, raw: Any) -> Coordination:
    allowed = frozenset(key for key, _ in _COORDINATION_KEYS)
    section = reader.section(raw, "coordination", allowed)
    base = Coordination()
    return Coordination(
        **reader.flat(section, _COORDINATION_KEYS, base, "coordination.")
    )


def _providers(reader: Reader, raw: Any) -> Mapping[str, ProviderSettings]:
    section = reader.section(raw, "providers")
    if "gallica" in section:
        raise reader.fail("providers", f"unknown key gallica; {_GALLICA_HINT}")
    reader.keys(section, frozenset(PROVIDER_KEYS), "providers")
    return MappingProxyType(
        {
            key: _provider(reader, key, section.get(key), PROVIDER_DEFAULTS[key])
            for key in PROVIDER_KEYS
        }
    )


def _provider(
    reader: Reader, key: str, raw: Any, base: ProviderSettings
) -> ProviderSettings:
    path = f"providers.{key}"
    allowed = _PROVIDER_COMMON_KEYS
    if isinstance(base, GoogleBooksSettings):
        allowed = allowed | _GOOGLE_BOOKS_KEYS
    elif isinstance(base, AnnasArchiveSettings):
        allowed = allowed | _ANNAS_KEYS
    section = reader.section(raw, path, allowed)
    prefix = path + "."
    slots = base.slots
    if section.get("slots") is not None:
        slots = reader.positive(section, "slots", 1, prefix)
    value = replace(
        base,
        max_pages=reader.integer(section, "max_pages", base.max_pages, prefix),
        slots=slots,
        network=_network(reader, section.get("network"), base.network, path),
    )
    if isinstance(value, GoogleBooksSettings):
        return replace(
            value,
            free_only=reader.flag(section, "free_only", value.free_only, prefix),
            prefer=reader.choice(
                section, "prefer", value.prefer, prefix, GOOGLE_BOOKS_FORMATS
            ),
            allow_drm=reader.flag(section, "allow_drm", value.allow_drm, prefix),
            max_files=reader.positive(section, "max_files", value.max_files, prefix),
        )
    if isinstance(value, AnnasArchiveSettings):
        return replace(
            value,
            base_url=reader.text(section, "base_url", value.base_url, prefix),
            quota=_quota(reader, section.get("quota"), value.quota, prefix),
        )
    return value


def _network(
    reader: Reader, raw: Any, base: NetworkSettings, provider: str
) -> NetworkSettings:
    path = provider + ".network"
    section = reader.section(raw, path, _NETWORK_KEYS)
    prefix = path + "."
    impersonate = base.impersonate
    if section.get("impersonate") is not None:
        impersonate = reader.text(section, "impersonate", "", prefix) or None
    return replace(
        base,
        **reader.flat(section, _NETWORK_FLAT_KEYS, base, prefix),
        ssl_error_policy=reader.choice(
            section,
            "ssl_error_policy",
            base.ssl_error_policy,
            prefix,
            SSL_ERROR_POLICIES,
        ),
        impersonate=impersonate,
        headers=_header_map(reader, section, base.headers, prefix),
    )


def _header_map(
    reader: Reader, raw: Mapping[str, Any], default: Mapping[str, str], path: str
) -> Mapping[str, str]:
    """Return the ``headers`` mapping; a given mapping replaces *default*."""
    value = raw.get("headers")
    if value is None:
        return default
    if not isinstance(value, Mapping) or not all(
        isinstance(name, str) and isinstance(text, str) for name, text in value.items()
    ):
        raise reader.fail(path + "headers", "must map header names to strings")
    return MappingProxyType(dict(value))


def _quota(reader: Reader, raw: Any, base: AnnasQuota, provider: str) -> AnnasQuota:
    path = provider + "quota"
    keys: tuple[tuple[str, ValueKind], ...] = (
        ("enabled", "flag"),
        ("daily_limit", "integer"),
        ("reset_hours", "number"),
    )
    section = reader.section(raw, path, frozenset(key for key, _ in keys))
    return replace(base, **reader.flat(section, keys, base, path + "."))


def _download_limits(reader: Reader, raw: Any) -> DownloadLimits:
    section = reader.section(
        raw, "download_limits", frozenset({"total", "per_work", "on_exceed"})
    )
    base = DownloadLimits()
    total_keys: tuple[tuple[str, ValueKind], ...] = (
        ("images_gb", "number"),
        ("pdfs_gb", "number"),
        ("metadata_gb", "number"),
    )
    work_keys: tuple[tuple[str, ValueKind], ...] = (
        ("images_gb", "number"),
        ("pdfs_gb", "number"),
        ("metadata_mb", "number"),
    )
    total = reader.section(
        section.get("total"),
        "download_limits.total",
        frozenset(key for key, _ in total_keys),
    )
    per_work = reader.section(
        section.get("per_work"),
        "download_limits.per_work",
        frozenset(key for key, _ in work_keys),
    )
    return DownloadLimits(
        total=TotalLimits(
            **reader.flat(total, total_keys, base.total, "download_limits.total.")
        ),
        per_work=PerWorkLimits(
            **reader.flat(
                per_work, work_keys, base.per_work, "download_limits.per_work."
            )
        ),
        on_exceed=reader.choice(
            section, "on_exceed", base.on_exceed, "download_limits.", ON_EXCEED
        ),
    )


def _naming(reader: Reader, raw: Any) -> Naming:
    allowed = frozenset(key for key, _ in _NAMING_KEYS)
    section = reader.section(raw, "naming", allowed)
    return Naming(**reader.flat(section, _NAMING_KEYS, Naming(), "naming."))


def load(path: Path | None, options: OptionTable) -> Settings:
    """Load the settings; an explicit *path* (``--settings``) wins.

    Without *path* the repository's ``config/settings.yaml`` is read, else
    the packaged example; when neither exists the code values apply. The
    ``defaults`` block is validated against *options*, the ``download``
    option table.

    Raises:
        SettingsError: The file is missing or invalid, or its ``defaults``
            block names an option outside *options* or a value it rejects.
    """
    if path is None and not (
        DEFAULT_SETTINGS_PATH.is_file() or EXAMPLE_SETTINGS_PATH.is_file()
    ):
        return Settings()
    loaded = load_settings(
        DEFAULT_SETTINGS_PATH,
        example=EXAMPLE_SETTINGS_PATH,
        explicit=path,
        options=options,
    )
    return Settings.from_mapping(
        loaded.data, defaults=loaded.defaults, source=loaded.path
    )
