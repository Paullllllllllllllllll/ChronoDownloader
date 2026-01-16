"""Tests for main/quota_manager.py - Centralized quota management."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


class TestProviderQuota:
    """Tests for ProviderQuota dataclass."""

    def test_to_dict_serialization(self):
        """to_dict returns serializable dictionary."""
        from main.quota_manager import ProviderQuota
        
        quota = ProviderQuota(
            provider_key="test_provider",
            daily_limit=100,
            reset_hours=24,
            downloads_used=50
        )
        
        result = quota.to_dict()
        
        assert result["provider_key"] == "test_provider"
        assert result["daily_limit"] == 100
        assert result["downloads_used"] == 50

    def test_from_dict_deserialization(self):
        """from_dict creates ProviderQuota from dict."""
        from main.quota_manager import ProviderQuota
        
        data = {
            "provider_key": "test",
            "daily_limit": 50,
            "reset_hours": 12,
            "downloads_used": 10,
            "period_start": "2024-01-01T00:00:00+00:00"
        }
        
        quota = ProviderQuota.from_dict(data)
        
        assert quota.provider_key == "test"
        assert quota.daily_limit == 50
        assert quota.downloads_used == 10

    def test_from_dict_with_missing_fields(self):
        """from_dict handles missing fields with defaults."""
        from main.quota_manager import ProviderQuota
        
        quota = ProviderQuota.from_dict({})
        
        assert quota.provider_key == "unknown"
        assert quota.daily_limit == 10
        assert quota.downloads_used == 0

    def test_get_reset_time_calculates_correctly(self):
        """get_reset_time returns period_start + reset_hours."""
        from main.quota_manager import ProviderQuota
        
        start_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        quota = ProviderQuota(
            provider_key="test",
            reset_hours=6,
            period_start=start_time.isoformat()
        )
        
        reset_time = quota.get_reset_time()
        
        expected = datetime(2024, 1, 1, 18, 0, 0, tzinfo=timezone.utc)
        assert reset_time == expected

    def test_get_reset_time_returns_none_without_period_start(self):
        """get_reset_time returns None if period_start not set."""
        from main.quota_manager import ProviderQuota
        
        quota = ProviderQuota(provider_key="test")
        assert quota.get_reset_time() is None

    def test_seconds_until_reset_future(self):
        """seconds_until_reset returns positive value for future reset."""
        from main.quota_manager import ProviderQuota
        
        future_start = datetime.now(timezone.utc) + timedelta(hours=1)
        quota = ProviderQuota(
            provider_key="test",
            reset_hours=12,
            period_start=future_start.isoformat()
        )
        
        seconds = quota.seconds_until_reset()
        assert seconds > 0

    def test_seconds_until_reset_past(self):
        """seconds_until_reset returns 0 for past reset time."""
        from main.quota_manager import ProviderQuota
        
        past_start = datetime.now(timezone.utc) - timedelta(days=2)
        quota = ProviderQuota(
            provider_key="test",
            reset_hours=24,
            period_start=past_start.isoformat()
        )
        
        seconds = quota.seconds_until_reset()
        assert seconds == 0

    def test_is_exhausted_when_limit_reached(self):
        """is_exhausted returns True when downloads >= limit."""
        from main.quota_manager import ProviderQuota
        
        # Future reset time
        future_start = datetime.now(timezone.utc)
        quota = ProviderQuota(
            provider_key="test",
            daily_limit=10,
            downloads_used=10,
            reset_hours=24,
            period_start=future_start.isoformat()
        )
        
        assert quota.is_exhausted() is True

    def test_is_exhausted_false_when_available(self):
        """is_exhausted returns False when quota available."""
        from main.quota_manager import ProviderQuota
        
        quota = ProviderQuota(
            provider_key="test",
            daily_limit=10,
            downloads_used=5
        )
        
        assert quota.is_exhausted() is False

    def test_is_period_expired(self):
        """is_period_expired returns True for expired period."""
        from main.quota_manager import ProviderQuota
        
        old_start = datetime.now(timezone.utc) - timedelta(days=2)
        quota = ProviderQuota(
            provider_key="test",
            reset_hours=24,
            period_start=old_start.isoformat()
        )
        
        assert quota.is_period_expired() is True


class TestQuotaManagerSingleton:
    """Tests for QuotaManager singleton pattern."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, temp_dir, mock_config):
        """Reset singletons before and after each test."""
        from main.quota_manager import QuotaManager
        from main.state_manager import StateManager
        
        QuotaManager._instance = None
        StateManager._instance = None
        yield
        QuotaManager._instance = None
        StateManager._instance = None

    def test_singleton_returns_same_instance(self, temp_dir):
        """Multiple instantiations return same instance."""
        from main.quota_manager import QuotaManager
        
        manager1 = QuotaManager()
        manager2 = QuotaManager()
        
        assert manager1 is manager2

    def test_singleton_thread_safety(self, temp_dir):
        """Singleton creation is thread-safe."""
        from main.quota_manager import QuotaManager
        
        instances = []
        
        def create_instance():
            instances.append(QuotaManager())
        
        threads = [threading.Thread(target=create_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert all(inst is instances[0] for inst in instances)


class TestQuotaManagerOperations:
    """Tests for QuotaManager quota operations."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, temp_dir, mock_config):
        """Reset singletons before and after each test."""
        from main.quota_manager import QuotaManager
        from main.state_manager import StateManager
        
        QuotaManager._instance = None
        StateManager._instance = None
        yield
        QuotaManager._instance = None
        StateManager._instance = None

    @pytest.fixture
    def manager(self):
        """Create fresh QuotaManager."""
        from main.quota_manager import QuotaManager
        return QuotaManager()

    def test_can_download_true_when_available(self, manager):
        """can_download returns True when quota available."""
        # Set up a quota with available downloads
        from main.quota_manager import ProviderQuota
        
        quota = ProviderQuota(
            provider_key="test_provider",
            daily_limit=100,
            downloads_used=5,
            period_start=datetime.now(timezone.utc).isoformat()
        )
        manager._quotas["test_provider"] = quota
        
        can, wait = manager.can_download("test_provider")
        assert can is True
        assert wait is None

    def test_can_download_false_when_exhausted(self, manager):
        """can_download returns False with wait time when exhausted."""
        # Set up exhausted quota
        from main.quota_manager import ProviderQuota
        
        future_start = datetime.now(timezone.utc)
        quota = ProviderQuota(
            provider_key="test_provider",
            daily_limit=5,
            downloads_used=5,
            reset_hours=24,
            period_start=future_start.isoformat()
        )
        manager._quotas["test_provider"] = quota
        
        can, wait = manager.can_download("test_provider")
        
        assert can is False
        assert wait is not None
        assert wait > 0

    def test_record_download_increments_count(self, manager):
        """record_download increments downloads_used."""
        from main.quota_manager import ProviderQuota
        
        # Set up a quota directly
        quota = ProviderQuota(
            provider_key="test_provider",
            daily_limit=100,
            downloads_used=0,
            period_start=datetime.now(timezone.utc).isoformat()
        )
        manager._quotas["test_provider"] = quota
        
        remaining = manager.record_download("test_provider")
        
        status = manager.get_quota_status("test_provider")
        assert status["downloads_used"] == 1

    def test_record_download_marks_exhausted(self, manager):
        """record_download marks quota exhausted when limit reached."""
        from main.quota_manager import ProviderQuota
        
        quota = ProviderQuota(
            provider_key="test",
            daily_limit=1,
            downloads_used=0,
            period_start=datetime.now(timezone.utc).isoformat()
        )
        manager._quotas["test"] = quota
        
        remaining = manager.record_download("test")
        
        assert remaining == 0
        assert manager._quotas["test"].exhausted_at is not None

    def test_get_quota_status_returns_complete_info(self, manager):
        """get_quota_status returns comprehensive status dict."""
        with patch("main.quota_manager.get_provider_setting", return_value={}):
            manager.can_download("test")  # Initialize
            
            status = manager.get_quota_status("test")
            
            assert "provider_key" in status
            assert "daily_limit" in status
            assert "downloads_used" in status
            assert "remaining" in status
            assert "is_exhausted" in status
            assert "seconds_until_reset" in status

    def test_reset_provider_clears_quota(self, manager):
        """reset_provider resets quota for a provider."""
        from main.quota_manager import ProviderQuota
        
        quota = ProviderQuota(
            provider_key="test",
            daily_limit=10,
            downloads_used=10,
            exhausted_at=datetime.now(timezone.utc).isoformat()
        )
        manager._quotas["test"] = quota
        
        manager.reset_provider("test")
        
        assert manager._quotas["test"].downloads_used == 0
        assert manager._quotas["test"].exhausted_at is None

    def test_reset_all_resets_all_providers(self, manager):
        """reset_all resets all provider quotas."""
        from main.quota_manager import ProviderQuota
        
        for i in range(3):
            manager._quotas[f"provider_{i}"] = ProviderQuota(
                provider_key=f"provider_{i}",
                downloads_used=i + 1
            )
        
        manager.reset_all()
        
        for i in range(3):
            assert manager._quotas[f"provider_{i}"].downloads_used == 0

    def test_get_exhausted_providers_lists_exhausted(self, manager):
        """get_exhausted_providers returns list of exhausted providers."""
        from main.quota_manager import ProviderQuota
        
        future = datetime.now(timezone.utc)
        manager._quotas["exhausted"] = ProviderQuota(
            provider_key="exhausted",
            daily_limit=1,
            downloads_used=5,
            reset_hours=24,
            period_start=future.isoformat()
        )
        manager._quotas["available"] = ProviderQuota(
            provider_key="available",
            daily_limit=10,
            downloads_used=1
        )
        
        exhausted = manager.get_exhausted_providers()
        
        assert "exhausted" in exhausted
        assert "available" not in exhausted

    def test_get_next_reset_returns_earliest(self, manager):
        """get_next_reset returns earliest reset time."""
        from main.quota_manager import ProviderQuota
        
        now = datetime.now(timezone.utc)
        
        # Provider with later reset
        manager._quotas["later"] = ProviderQuota(
            provider_key="later",
            daily_limit=1,
            downloads_used=5,
            reset_hours=48,
            period_start=now.isoformat()
        )
        
        # Provider with earlier reset
        manager._quotas["earlier"] = ProviderQuota(
            provider_key="earlier",
            daily_limit=1,
            downloads_used=5,
            reset_hours=12,
            period_start=now.isoformat()
        )
        
        result = manager.get_next_reset()
        
        assert result is not None
        assert result[0] == "earlier"


class TestQuotaManagerHasQuota:
    """Tests for has_quota and quota detection."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config):
        """Reset singletons before and after each test."""
        from main.quota_manager import QuotaManager
        from main.state_manager import StateManager
        
        QuotaManager._instance = None
        StateManager._instance = None
        yield
        QuotaManager._instance = None
        StateManager._instance = None

    def test_has_quota_true_with_new_config(self):
        """has_quota returns True for quota.enabled = true."""
        from main.quota_manager import QuotaManager
        
        manager = QuotaManager()
        
        with patch("main.quota_manager.get_provider_setting") as mock:
            mock.return_value = {"enabled": True, "daily_limit": 100}
            assert manager.has_quota("test_provider") is True

    def test_has_quota_false_without_config(self):
        """has_quota returns False for providers without quota config."""
        from main.quota_manager import QuotaManager
        
        manager = QuotaManager()
        
        with patch("main.quota_manager.get_provider_setting", return_value=None):
            assert manager.has_quota("test_provider") is False

    def test_get_quota_limited_providers(self):
        """get_quota_limited_providers returns providers with quotas."""
        from main.quota_manager import QuotaManager
        
        manager = QuotaManager()
        
        mock_config = {
            "provider_settings": {
                "provider_with_quota": {"quota": {"enabled": True}},
                "provider_without": {}
            }
        }
        
        with patch("main.quota_manager.get_config", return_value=mock_config):
            with patch.object(manager, "has_quota", side_effect=lambda k: k == "provider_with_quota"):
                providers = manager.get_quota_limited_providers()
                
                assert "provider_with_quota" in providers


class TestGetQuotaManager:
    """Tests for get_quota_manager helper."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config):
        """Reset singletons."""
        from main.quota_manager import QuotaManager
        from main.state_manager import StateManager
        
        QuotaManager._instance = None
        StateManager._instance = None
        yield
        QuotaManager._instance = None
        StateManager._instance = None

    def test_get_quota_manager_returns_singleton(self):
        """get_quota_manager returns singleton instance."""
        from main.quota_manager import get_quota_manager
        
        manager1 = get_quota_manager()
        manager2 = get_quota_manager()
        
        assert manager1 is manager2


class TestQuotaManagerPeriodReset:
    """Tests for automatic period reset behavior."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config):
        """Reset singletons."""
        from main.quota_manager import QuotaManager
        from main.state_manager import StateManager
        
        QuotaManager._instance = None
        StateManager._instance = None
        yield
        QuotaManager._instance = None
        StateManager._instance = None

    def test_check_and_reset_period_resets_expired(self):
        """_check_and_reset_period resets expired quota period."""
        from main.quota_manager import QuotaManager, ProviderQuota
        
        manager = QuotaManager()
        
        # Create quota with expired period
        old_start = datetime.now(timezone.utc) - timedelta(days=2)
        quota = ProviderQuota(
            provider_key="test",
            daily_limit=10,
            downloads_used=10,
            reset_hours=24,
            period_start=old_start.isoformat(),
            exhausted_at=old_start.isoformat()
        )
        
        was_reset = manager._check_and_reset_period(quota)
        
        assert was_reset is True
        assert quota.downloads_used == 0
        assert quota.exhausted_at is None

    def test_check_and_reset_period_keeps_valid(self):
        """_check_and_reset_period keeps valid period."""
        from main.quota_manager import QuotaManager, ProviderQuota
        
        manager = QuotaManager()
        
        # Create quota with recent period
        recent_start = datetime.now(timezone.utc) - timedelta(hours=1)
        quota = ProviderQuota(
            provider_key="test",
            daily_limit=10,
            downloads_used=5,
            reset_hours=24,
            period_start=recent_start.isoformat()
        )
        
        was_reset = manager._check_and_reset_period(quota)
        
        assert was_reset is False
        assert quota.downloads_used == 5

    def test_can_download_auto_resets_expired_quota(self):
        """can_download automatically resets expired quota."""
        from main.quota_manager import QuotaManager, ProviderQuota
        
        manager = QuotaManager()
        
        # Create exhausted but expired quota
        old_start = datetime.now(timezone.utc) - timedelta(days=2)
        manager._quotas["test"] = ProviderQuota(
            provider_key="test",
            daily_limit=10,
            downloads_used=10,
            reset_hours=24,
            period_start=old_start.isoformat()
        )
        
        can, wait = manager.can_download("test")
        
        assert can is True
        assert wait is None
