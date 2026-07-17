"""Configuration management for ChronoDownloader.

Handles loading and caching of the JSON configuration file with environment
variable support (CHRONO_CONFIG_PATH) and provider-specific settings.

The configuration system provides:
- Centralized config loading with caching
- Provider-specific settings (network, limits, preferences)
- Download preferences (PDF vs images, metadata, overwrite)
- Budget limits (per-work and global)
- Selection strategy and matching thresholds
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_CACHE: dict[str, Any] | None = None
_API_KEYS_CACHE: dict[str, Any] | None = None

# Code default for the per-provider search timeout (seconds). Consumed by
# api.core.config.get_search_timeout and mirrored in config.example.json.
DEFAULT_SEARCH_TIMEOUT_SECONDS = 60.0


def get_config(force_reload: bool = False) -> dict[str, Any]:
    """Load project configuration JSON.

    Looks for the path in CHRONO_CONFIG_PATH env var; falls back to
    ``config.json`` in the current working directory.  When the resolved
    path is absent, tries ``config.example.json`` in the same directory
    and logs an INFO message directing the user to copy and customize it.
    Raises ``FileNotFoundError`` when neither file is present.
    Caches the result unless force_reload is True.

    Returns:
        Configuration dictionary
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE

    config_path = os.environ.get("CHRONO_CONFIG_PATH", "config.json")
    config_dir = os.path.dirname(os.path.abspath(config_path))
    example_path = os.path.join(config_dir, "config.example.json")

    try:
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                _CONFIG_CACHE = json.load(f) or {}
        elif os.path.exists(example_path):
            logger.info(
                "config.json not found; using bundled defaults from "
                "config.example.json. Copy it to config.json and edit "
                "it to set your own values."
            )
            with open(example_path, encoding="utf-8") as f:
                _CONFIG_CACHE = json.load(f) or {}
        else:
            raise FileNotFoundError(
                "No configuration file found. Expected config.json at "
                f"{config_path!r}. Copy config.example.json to config.json "
                "and edit it to set your paths and preferences."
            )
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, ValueError) as e:
        # A present-but-unparseable config is a user error: fail loudly rather
        # than silently caching an empty dict and running on bare defaults
        # (wrong output paths, no budget limits) for the rest of the process.
        raise ValueError(
            f"Configuration file {config_path!r} contains invalid JSON: {e}"
        ) from e

    return _CONFIG_CACHE


def get_api_keys_config(force_reload: bool = False) -> dict[str, Any]:
    """Load the optional API-key environment-variable mapping.

    Looks for an ``api_keys.json`` file in the same directory as the
    resolved configuration path (``CHRONO_CONFIG_PATH`` or ``config.json``).
    When ``api_keys.json`` is absent, tries ``api_keys.example.json`` in the
    same directory and logs an INFO message.  When neither file exists,
    returns an empty dict (the file is fully optional).
    Caches the result unless force_reload is True.

    Returns:
        Mapping of provider key to env var name (empty dict if absent/invalid)
    """
    global _API_KEYS_CACHE
    if _API_KEYS_CACHE is not None and not force_reload:
        return _API_KEYS_CACHE

    config_path = os.environ.get("CHRONO_CONFIG_PATH", "config.json")
    config_dir = os.path.dirname(os.path.abspath(config_path))
    path = os.path.join(config_dir, "api_keys.json")
    example_path = os.path.join(config_dir, "api_keys.example.json")

    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                _API_KEYS_CACHE = json.load(f) or {}
        elif os.path.exists(example_path):
            logger.info(
                "api_keys.json not found; using bundled defaults from "
                "api_keys.example.json. Copy it to api_keys.json and "
                "edit it to remap provider API-key environment variables."
            )
            with open(example_path, encoding="utf-8") as f:
                _API_KEYS_CACHE = json.load(f) or {}
        else:
            _API_KEYS_CACHE = {}
    except Exception as e:
        logger.error("Failed to load API-key mapping from %s: %s", path, e)
        _API_KEYS_CACHE = {}

    return _API_KEYS_CACHE


def get_api_key_envvar(provider_key: str, default: str) -> str:
    """Resolve the environment-variable name holding a provider's API key.

    Returns the name mapped in ``api_keys.json`` for ``provider_key`` when
    present and non-empty; otherwise returns ``default`` (the provider's
    built-in environment variable name). Behavior is therefore identical to the
    historical default whenever the mapping file or its entry is absent.

    Args:
        provider_key: Provider identifier (e.g., 'europeana', 'dpla')
        default: Default environment variable name to fall back to

    Returns:
        Environment variable name to read the API key from
    """
    name = get_api_keys_config().get(provider_key)
    if isinstance(name, str) and name.strip():
        return name
    return default


def get_provider_setting(provider_key: str, setting: str, default: Any = None) -> Any:
    """Retrieve a provider-specific setting from the configuration.

    Args:
        provider_key: Provider identifier (e.g., 'internet_archive', 'bnf_gallica')
        setting: Setting name to retrieve
        default: Default value if not found

    Returns:
        The setting value or default
    """
    cfg = get_config()
    ps = cfg.get("provider_settings", {})

    # Map known aliases to config keys
    aliases = {
        "bnf_gallica": "gallica",
    }

    key = provider_key
    if key not in ps:
        key = aliases.get(provider_key, provider_key)

    return ps.get(key, {}).get(setting, default)


def get_download_config() -> dict[str, Any]:
    """Get download-related configuration section.

    Returns:
        Download configuration dictionary with defaults
    """
    cfg = get_config()
    dl = dict(cfg.get("download", {}) or {})

    # Apply defaults
    dl.setdefault("prefer_pdf_over_images", True)
    dl.setdefault("download_manifest_renderings", True)
    dl.setdefault("max_renderings_per_manifest", 1)
    dl.setdefault(
        "rendering_mime_whitelist", ["application/pdf", "application/epub+zip"]
    )
    dl.setdefault("overwrite_existing", False)
    dl.setdefault("include_metadata", True)

    return dl


def prefer_pdf_over_images() -> bool:
    """Check if PDF downloads should be preferred over page images."""
    return bool(get_download_config().get("prefer_pdf_over_images", True))


def overwrite_existing() -> bool:
    """Check if existing files should be overwritten."""
    return bool(get_download_config().get("overwrite_existing", False))


def include_metadata() -> bool:
    """Check if metadata files should be saved."""
    return bool(get_download_config().get("include_metadata", True))


def get_network_config(provider_key: str | None) -> dict[str, Any]:
    """Return network policy for a provider, with sensible defaults.

    Args:
        provider_key: Provider identifier (may be None for generic defaults)

    Returns:
        Network configuration dictionary with all fields populated
    """
    cfg = get_config()
    prov_cfg = (
        cfg.get("provider_settings", {}).get(provider_key or "", {})
        if provider_key
        else {}
    )
    net = dict(prov_cfg.get("network", {}) or {})

    # Back-compat: lift legacy delay_ms into network if not provided
    if "delay_ms" not in net and "delay_ms" in prov_cfg:
        net["delay_ms"] = prov_cfg.get("delay_ms")

    # Defaults
    net.setdefault("delay_ms", 0)
    net.setdefault("jitter_ms", 0)
    net.setdefault("max_attempts", 5)
    net.setdefault("base_backoff_s", 1.5)
    net.setdefault("backoff_multiplier", 1.5)
    net.setdefault("max_backoff_s", 60.0)  # Cap exponential backoff at 60 seconds
    net.setdefault("verify_ssl", True)
    net.setdefault("ssl_error_policy", "fail")
    net.setdefault("dns_retry", False)

    # Circuit breaker defaults
    net.setdefault("circuit_breaker_enabled", True)
    net.setdefault(
        "circuit_breaker_threshold", 3
    )  # Consecutive failures before disabling
    net.setdefault("circuit_breaker_cooldown_s", 300.0)  # 5 minutes cooldown

    # Ensure headers is a dict if provided
    if not isinstance(net.get("headers", {}), dict):
        net["headers"] = {}

    return net


def get_download_limits() -> dict[str, Any]:
    """Get download limits configuration section.

    Returns:
        Download limits dictionary
    """
    cfg = get_config()
    return dict(cfg.get("download_limits", {}) or {})


def get_max_pages(provider_key: str) -> int | None:
    """Get max pages limit for a provider.

    Args:
        provider_key: Provider identifier (e.g., 'internet_archive', 'gallica', 'loc')

    Returns:
        Max pages limit (0 or None means unlimited)
    """
    val = get_provider_setting(provider_key, "max_pages", None)
    if isinstance(val, int):
        return val
    return None


def get_resume_mode() -> str:
    """Get the resume mode for processing works.

    Resume modes:
    - "skip_completed": Skip works with status="completed" in work.json (default)
    - "reprocess_all": Reprocess all works regardless of status
    - "skip_if_has_objects": Skip works that have files in objects/ directory
    - "resume_from_csv": Resume purely from the source CSV's retrievable column;
      handled by CSV-level pending filtering (see main.cli.overrides), not by
      per-work.json checks, so ``check_work_status`` treats it as a no-op.

    Returns:
        Resume mode string
    """
    return str(get_download_config().get("resume_mode", "skip_completed"))


def get_min_title_score(
    provider_key: str | None = None, default: float = 50.0
) -> float:
    """Get minimum title score threshold, with optional per-provider override.

    Checks provider_settings.<provider_key>.min_title_score first,
    then falls back to selection.min_title_score, then to default.

    Args:
        provider_key: Provider identifier (e.g., 'annas_archive', 'mdz')
        default: Default value if not configured anywhere

    Returns:
        Minimum title score threshold (0-100)
    """
    cfg = get_config()

    # Check per-provider setting first
    if provider_key:
        provider_score = get_provider_setting(provider_key, "min_title_score", None)
        if provider_score is not None:
            try:
                return float(provider_score)
            except (TypeError, ValueError):
                pass

    # Fall back to global selection.min_title_score
    sel = cfg.get("selection", {})
    global_score = sel.get("min_title_score")
    if global_score is not None:
        try:
            return float(global_score)
        except (TypeError, ValueError):
            pass

    return default


def _coerce_search_timeout(value: Any) -> float | None:
    """Normalize a raw search-timeout config value to seconds or None.

    Returns None (timeout disabled) when the value is null, zero, negative,
    or non-numeric; otherwise returns the positive float.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds


def get_search_timeout(
    provider_key: str | None = None,
    default: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
) -> float | None:
    """Resolve the per-provider search timeout in seconds.

    Resolution order: ``provider_settings.<provider_key>.search_timeout_seconds``,
    then ``selection.search_timeout_seconds``, then ``default``. A value of 0,
    null, or non-numeric disables the timeout (returns None = unbounded).

    Args:
        provider_key: Provider identifier for a per-provider override.
        default: Fallback when no config value is present.

    Returns:
        Positive timeout in seconds, or None when the timeout is disabled.
    """
    cfg = get_config()

    # Per-provider override wins over the global selection value.
    if provider_key:
        sentinel: Any = object()
        pv = get_provider_setting(provider_key, "search_timeout_seconds", sentinel)
        if pv is not sentinel:
            return _coerce_search_timeout(pv)

    sel = cfg.get("selection", {})
    if isinstance(sel, dict) and "search_timeout_seconds" in sel:
        return _coerce_search_timeout(sel.get("search_timeout_seconds"))

    return _coerce_search_timeout(default)
