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
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def get_config(force_reload: bool = False) -> Dict[str, Any]:
    """Load project configuration JSON.

    Looks for the path in CHRONO_CONFIG_PATH env var; falls back to 'config.json' in CWD.
    Caches the result unless force_reload is True.
    
    Returns:
        Configuration dictionary (empty dict if file not found or invalid)
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE
    
    path = os.environ.get("CHRONO_CONFIG_PATH", "config.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _CONFIG_CACHE = json.load(f) or {}
        else:
            _CONFIG_CACHE = {}
    except Exception as e:
        logger.error("Failed to load config from %s: %s", path, e)
        _CONFIG_CACHE = {}
    
    return _CONFIG_CACHE


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


def get_download_config() -> Dict[str, Any]:
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
    dl.setdefault("rendering_mime_whitelist", ["application/pdf", "application/epub+zip"])
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


def get_network_config(provider_key: Optional[str]) -> Dict[str, Any]:
    """Return network policy for a provider, with sensible defaults.
    
    Args:
        provider_key: Provider identifier (may be None for generic defaults)
        
    Returns:
        Network configuration dictionary with all fields populated
    """
    cfg = get_config()
    prov_cfg = cfg.get("provider_settings", {}).get(provider_key or "", {}) if provider_key else {}
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
    
    # Ensure headers is a dict if provided
    if not isinstance(net.get("headers", {}), dict):
        net["headers"] = {}
    
    return net


def get_download_limits() -> Dict[str, Any]:
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
    
    Returns:
        Resume mode string
    """
    return str(get_download_config().get("resume_mode", "skip_completed"))
