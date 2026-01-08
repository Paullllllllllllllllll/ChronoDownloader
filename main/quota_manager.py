"""Centralized quota management for quota-limited providers.

This module provides unified quota tracking across providers that have
daily/hourly download limits (e.g., Anna's Archive fast downloads).

IMPORTANT: This system is ONLY for providers with daily/hourly quotas.
Providers with unlimited downloads (MDZ, Gallica, etc.) use network rate
limiting instead, which is handled separately via the network config.

Quota vs Rate Limiting:
- Quota: Daily/hourly hard limit (e.g., 875 downloads/day for Anna's Archive)
- Rate Limiting: Request delays and backoff (applies to all providers)

Features:
- Persistent quota state via unified StateManager
- Thread-safe operations
- Provider-agnostic design for any quota-limited provider
- Configurable limits and reset periods
- Auto-detection of quota-enabled providers from config
"""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from api.core.config import get_config, get_provider_setting

logger = logging.getLogger(__name__)


@dataclass
class ProviderQuota:
    """Quota state for a single provider.
    
    Attributes:
        provider_key: Provider identifier
        daily_limit: Maximum downloads per reset period
        reset_hours: Hours until quota resets
        downloads_used: Downloads consumed in current period
        period_start: When the current quota period started (ISO format)
        exhausted_at: When quota was exhausted (ISO format), or None
    """
    provider_key: str
    daily_limit: int = 10
    reset_hours: int = 24
    downloads_used: int = 0
    period_start: Optional[str] = None
    exhausted_at: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderQuota":
        """Create from dictionary."""
        return cls(
            provider_key=data.get("provider_key", "unknown"),
            daily_limit=int(data.get("daily_limit", 10)),
            reset_hours=int(data.get("reset_hours", 24)),
            downloads_used=int(data.get("downloads_used", 0)),
            period_start=data.get("period_start"),
            exhausted_at=data.get("exhausted_at"),
        )
    
    def get_reset_time(self) -> Optional[datetime]:
        """Get the datetime when quota period will reset.
        
        Uses period_start + reset_hours for consistent rolling window behavior.
        
        Returns:
            Reset datetime (UTC) or None if period_start not set
        """
        if not self.period_start:
            return None
        try:
            start = datetime.fromisoformat(self.period_start)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            return start + timedelta(hours=self.reset_hours)
        except Exception:
            return None
    
    def seconds_until_reset(self) -> float:
        """Get seconds until quota resets.
        
        Returns:
            Seconds until reset, or 0 if quota is available
        """
        reset_time = self.get_reset_time()
        if not reset_time:
            return 0.0
        now = datetime.now(timezone.utc)
        delta = (reset_time - now).total_seconds()
        return max(0.0, delta)
    
    def is_exhausted(self) -> bool:
        """Check if quota is currently exhausted.
        
        Returns:
            True if quota exhausted and reset time not yet reached
        """
        return self.downloads_used >= self.daily_limit and self.seconds_until_reset() > 0
    
    def is_period_expired(self) -> bool:
        """Check if the quota period has expired and should reset.
        
        Returns:
            True if period has expired
        """
        return self.seconds_until_reset() <= 0


class QuotaManager:
    """Centralized manager for provider quotas.
    
    Thread-safe singleton that tracks quota usage across all providers
    and persists state to disk.
    """
    
    _instance: Optional["QuotaManager"] = None
    _lock = threading.Lock()
    
    def __new__(cls, state_file: Optional[str] = None) -> "QuotaManager":
        """Singleton pattern - only one QuotaManager instance."""
        with cls._lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instance = instance
            return cls._instance
    
    def __init__(self, state_file: Optional[str] = None):
        """Initialize the quota manager.
        
        Args:
            state_file: Ignored (kept for backward compatibility)
        """
        if getattr(self, "_initialized", False):
            return
        
        self._quotas: Dict[str, ProviderQuota] = {}
        self._data_lock = threading.RLock()
        self._state_manager = None  # Lazy init to avoid circular imports
        
        # Load existing state
        self._load_state()
        self._initialized = True
        logger.debug("QuotaManager initialized")
    
    def _get_state_manager(self):
        """Get the StateManager instance (lazy init)."""
        if self._state_manager is None:
            from main.state_manager import get_state_manager
            self._state_manager = get_state_manager()
        return self._state_manager
    
    def _load_state(self) -> None:
        """Load quota state from StateManager."""
        try:
            state_manager = self._get_state_manager()
            quotas_data = state_manager.get_quotas()
            
            for provider_key, quota_data in quotas_data.items():
                self._quotas[provider_key] = ProviderQuota.from_dict(quota_data)
            
            if self._quotas:
                logger.info(
                    "Loaded quota state for %d provider(s)",
                    len(self._quotas)
                )
        except Exception as e:
            logger.warning("Failed to load quota state: %s", e)
    
    def _save_state(self) -> None:
        """Save quota state to StateManager."""
        try:
            state_manager = self._get_state_manager()
            quotas_data = {k: v.to_dict() for k, v in self._quotas.items()}
            state_manager.update_quotas(quotas_data)
        except Exception as e:
            logger.warning("Failed to save quota state: %s", e)
    
    def _get_or_create_quota(self, provider_key: str) -> ProviderQuota:
        """Get or create quota tracking for a provider.
        
        Only creates quota tracking for providers with quota.enabled = true in config.
        For unlimited providers, this will still create a quota object but it won't
        be enforced (use has_quota() to check first).
        
        Args:
            provider_key: Provider identifier
            
        Returns:
            ProviderQuota instance
        """
        if provider_key not in self._quotas:
            # Check for new quota config structure first
            quota_config = get_provider_setting(provider_key, "quota", {})
            
            if isinstance(quota_config, dict) and quota_config.get("enabled"):
                # New config structure with quota.enabled = true
                daily_limit = quota_config.get("daily_limit", 875)
                reset_hours = quota_config.get("reset_hours", 24)
            else:
                # Fallback to legacy config for backward compatibility
                daily_limit = get_provider_setting(
                    provider_key, "daily_download_limit",
                    get_provider_setting(provider_key, "daily_fast_download_limit", 10)
                )
                reset_hours = get_provider_setting(
                    provider_key, "quota_reset_hours",
                    get_provider_setting(provider_key, "quota_reset_wait_hours", 24)
                )
            
            self._quotas[provider_key] = ProviderQuota(
                provider_key=provider_key,
                daily_limit=int(daily_limit),
                reset_hours=int(reset_hours),
                period_start=datetime.now(timezone.utc).isoformat(),
            )
            self._save_state()
            logger.debug(
                "Initialized quota tracking for %s: %d downloads per %d hours",
                provider_key, int(daily_limit), int(reset_hours)
            )
        
        return self._quotas[provider_key]
    
    def _check_and_reset_period(self, quota: ProviderQuota) -> bool:
        """Check if quota period should reset and apply if needed.
        
        Args:
            quota: ProviderQuota to check
            
        Returns:
            True if period was reset
        """
        if not quota.period_start:
            quota.period_start = datetime.now(timezone.utc).isoformat()
            return True
        
        try:
            period_start = datetime.fromisoformat(quota.period_start)
            if period_start.tzinfo is None:
                period_start = period_start.replace(tzinfo=timezone.utc)
            
            now = datetime.now(timezone.utc)
            hours_elapsed = (now - period_start).total_seconds() / 3600
            
            if hours_elapsed >= quota.reset_hours:
                # Reset the period
                old_used = quota.downloads_used
                quota.downloads_used = 0
                quota.period_start = now.isoformat()
                quota.exhausted_at = None
                self._save_state()
                
                # Enhanced notification logging
                logger.info(
                    "QUOTA RESET: %s - %d downloads now available (was %d/%d used, %.1f hours elapsed)",
                    quota.provider_key, quota.daily_limit, old_used, quota.daily_limit, hours_elapsed
                )
                return True
        except Exception as e:
            logger.debug("Error checking quota period: %s", e)
        
        return False
    
    def can_download(self, provider_key: str) -> Tuple[bool, Optional[float]]:
        """Check if a download is allowed for a provider.
        
        Args:
            provider_key: Provider identifier
            
        Returns:
            Tuple of (can_download, seconds_until_reset_if_not)
            - can_download: True if quota allows
            - seconds_until_reset: Seconds to wait, or None if can download
        """
        with self._data_lock:
            quota = self._get_or_create_quota(provider_key)
            
            # Check for period reset
            self._check_and_reset_period(quota)
            
            if quota.downloads_used < quota.daily_limit:
                return True, None
            
            # Quota exhausted - calculate wait time
            wait_seconds = quota.seconds_until_reset()
            if wait_seconds <= 0:
                # Reset time passed, reset quota
                quota.downloads_used = 0
                quota.exhausted_at = None
                quota.period_start = datetime.now(timezone.utc).isoformat()
                self._save_state()
                return True, None
            
            return False, wait_seconds
    
    def record_download(self, provider_key: str) -> int:
        """Record that a download was used.
        
        Args:
            provider_key: Provider identifier
            
        Returns:
            Remaining downloads for this period
        """
        with self._data_lock:
            quota = self._get_or_create_quota(provider_key)
            quota.downloads_used += 1
            
            remaining = quota.daily_limit - quota.downloads_used
            
            if remaining <= 0 and not quota.exhausted_at:
                quota.exhausted_at = datetime.now(timezone.utc).isoformat()
                logger.info(
                    "%s: Quota exhausted (%d/%d). Resets in %.1f hours.",
                    provider_key, quota.downloads_used, quota.daily_limit,
                    quota.reset_hours
                )
            else:
                logger.debug(
                    "%s: Download recorded. %d/%d remaining.",
                    provider_key, remaining, quota.daily_limit
                )
            
            self._save_state()
            return max(0, remaining)
    
    def get_quota_status(self, provider_key: str) -> Dict[str, Any]:
        """Get current quota status for a provider.
        
        Args:
            provider_key: Provider identifier
            
        Returns:
            Dictionary with quota status info
        """
        with self._data_lock:
            quota = self._get_or_create_quota(provider_key)
            self._check_and_reset_period(quota)
            
            return {
                "provider_key": provider_key,
                "daily_limit": quota.daily_limit,
                "downloads_used": quota.downloads_used,
                "remaining": max(0, quota.daily_limit - quota.downloads_used),
                "is_exhausted": quota.is_exhausted(),
                "reset_time": quota.get_reset_time().isoformat() if quota.get_reset_time() else None,
                "seconds_until_reset": quota.seconds_until_reset(),
            }
    
    def get_next_reset(self) -> Optional[Tuple[str, datetime]]:
        """Get the next quota reset time across all providers.
        
        Returns:
            Tuple of (provider_key, reset_datetime) or None if no quotas exhausted
        """
        with self._data_lock:
            earliest: Optional[Tuple[str, datetime]] = None
            
            for provider_key, quota in self._quotas.items():
                if quota.is_exhausted():
                    reset_time = quota.get_reset_time()
                    if reset_time:
                        if earliest is None or reset_time < earliest[1]:
                            earliest = (provider_key, reset_time)
            
            return earliest
    
    def get_exhausted_providers(self) -> List[str]:
        """Get list of providers with exhausted quotas.
        
        Returns:
            List of provider keys
        """
        with self._data_lock:
            return [k for k, v in self._quotas.items() if v.is_exhausted()]
    
    def reset_provider(self, provider_key: str) -> None:
        """Manually reset quota for a provider.
        
        Args:
            provider_key: Provider identifier
        """
        with self._data_lock:
            if provider_key in self._quotas:
                quota = self._quotas[provider_key]
                quota.downloads_used = 0
                quota.exhausted_at = None
                quota.period_start = datetime.now(timezone.utc).isoformat()
                self._save_state()
                logger.info("Quota manually reset for %s", provider_key)
    
    def reset_all(self) -> None:
        """Reset quotas for all providers."""
        with self._data_lock:
            for quota in self._quotas.values():
                quota.downloads_used = 0
                quota.exhausted_at = None
                quota.period_start = datetime.now(timezone.utc).isoformat()
            self._save_state()
            logger.info("All quotas reset")
    
    def has_quota(self, provider_key: str) -> bool:
        """Check if a provider has quota limits enabled.
        
        Args:
            provider_key: Provider identifier
            
        Returns:
            True if provider has quotas, False if unlimited
        """
        # Check new config structure
        quota_config = get_provider_setting(provider_key, "quota", {})
        if isinstance(quota_config, dict):
            return quota_config.get("enabled", False)
        
        # For legacy configs, assume quota exists if daily_limit setting exists
        daily_limit = get_provider_setting(
            provider_key, "daily_download_limit",
            get_provider_setting(provider_key, "daily_fast_download_limit", None)
        )
        return daily_limit is not None
    
    def get_quota_limited_providers(self) -> List[str]:
        """Get list of all providers with quota limits enabled.
        
        Returns:
            List of provider keys that have quotas
        """
        cfg = get_config()
        provider_settings = cfg.get("provider_settings", {})
        quota_providers = []
        
        for provider_key in provider_settings.keys():
            if self.has_quota(provider_key):
                quota_providers.append(provider_key)
        
        return quota_providers


def get_quota_manager() -> QuotaManager:
    """Get the singleton QuotaManager instance.
    
    Returns:
        QuotaManager instance
    """
    return QuotaManager()


__all__ = [
    "QuotaManager",
    "ProviderQuota",
    "get_quota_manager",
]
