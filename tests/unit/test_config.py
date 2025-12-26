"""Unit tests for api.core.config module."""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

import api.core.config as config_module
from api.core.config import (
    get_config,
    get_download_config,
    get_download_limits,
    get_max_pages,
    get_network_config,
    get_provider_setting,
    get_resume_mode,
    include_metadata,
    overwrite_existing,
    prefer_pdf_over_images,
)


class TestGetConfig:
    """Tests for get_config function."""
    
    def test_returns_dict(self, config_file: str):
        """Test that get_config returns a dictionary."""
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_file}):
            config_module._CONFIG_CACHE = None
            result = get_config(force_reload=True)
            assert isinstance(result, dict)
    
    def test_loads_from_env_path(self, config_file: str):
        """Test loading config from CHRONO_CONFIG_PATH environment variable."""
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_file}):
            config_module._CONFIG_CACHE = None
            result = get_config(force_reload=True)
            assert "providers" in result
    
    def test_returns_empty_dict_for_missing_file(self, temp_dir: str):
        """Test that missing file returns empty dict."""
        missing_path = os.path.join(temp_dir, "nonexistent.json")
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": missing_path}):
            config_module._CONFIG_CACHE = None
            result = get_config(force_reload=True)
            assert result == {}
    
    def test_caches_result(self, config_file: str):
        """Test that config is cached."""
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_file}):
            config_module._CONFIG_CACHE = None
            result1 = get_config(force_reload=True)
            result2 = get_config()
            assert result1 is result2
    
    def test_force_reload(self, config_file: str, sample_config):
        """Test that force_reload refreshes the cache."""
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_file}):
            config_module._CONFIG_CACHE = {"old": "data"}
            result = get_config(force_reload=True)
            assert "old" not in result
            assert "providers" in result
    
    def test_handles_invalid_json(self, temp_dir: str):
        """Test handling of invalid JSON file."""
        invalid_path = os.path.join(temp_dir, "invalid.json")
        with open(invalid_path, "w") as f:
            f.write("not valid json {{{")
        
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": invalid_path}):
            config_module._CONFIG_CACHE = None
            result = get_config(force_reload=True)
            assert result == {}


class TestGetProviderSetting:
    """Tests for get_provider_setting function."""
    
    def test_retrieves_existing_setting(self, mock_config):
        """Test retrieving an existing provider setting."""
        result = get_provider_setting("gallica", "max_pages")
        assert result == 500
    
    def test_returns_default_for_missing_setting(self, mock_config):
        """Test that default is returned for missing setting."""
        result = get_provider_setting("gallica", "nonexistent", default=42)
        assert result == 42
    
    def test_returns_default_for_missing_provider(self, mock_config):
        """Test that default is returned for missing provider."""
        result = get_provider_setting("unknown_provider", "max_pages", default=100)
        assert result == 100
    
    def test_alias_mapping(self, mock_config):
        """Test that bnf_gallica maps to gallica."""
        result = get_provider_setting("bnf_gallica", "max_pages")
        assert result == 500


class TestGetDownloadConfig:
    """Tests for get_download_config function."""
    
    def test_returns_download_section(self, mock_config):
        """Test that download section is returned."""
        result = get_download_config()
        assert isinstance(result, dict)
        assert result.get("prefer_pdf_over_images") is True
    
    def test_applies_defaults(self):
        """Test that defaults are applied for missing keys."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_download_config()
            assert "prefer_pdf_over_images" in result
            assert "overwrite_existing" in result
            assert "include_metadata" in result
    
    def test_default_values(self):
        """Test default values."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_download_config()
            assert result["prefer_pdf_over_images"] is True
            assert result["overwrite_existing"] is False
            assert result["include_metadata"] is True


class TestPreferPdfOverImages:
    """Tests for prefer_pdf_over_images function."""
    
    def test_returns_true_by_default(self):
        """Test that True is returned by default."""
        with patch("api.core.config.get_config", return_value={}):
            assert prefer_pdf_over_images() is True
    
    def test_returns_configured_value(self, mock_config):
        """Test that configured value is returned."""
        assert prefer_pdf_over_images() is True


class TestOverwriteExisting:
    """Tests for overwrite_existing function."""
    
    def test_returns_false_by_default(self):
        """Test that False is returned by default."""
        with patch("api.core.config.get_config", return_value={}):
            assert overwrite_existing() is False


class TestIncludeMetadata:
    """Tests for include_metadata function."""
    
    def test_returns_true_by_default(self):
        """Test that True is returned by default."""
        with patch("api.core.config.get_config", return_value={}):
            assert include_metadata() is True


class TestGetNetworkConfig:
    """Tests for get_network_config function."""
    
    def test_returns_dict(self, mock_config):
        """Test that a dictionary is returned."""
        result = get_network_config("internet_archive")
        assert isinstance(result, dict)
    
    def test_applies_defaults(self):
        """Test that default values are applied."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_network_config("internet_archive")
            assert "delay_ms" in result
            assert "max_attempts" in result
            assert "base_backoff_s" in result
            assert "verify_ssl" in result
    
    def test_default_values(self):
        """Test specific default values."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_network_config(None)
            assert result["delay_ms"] == 0
            assert result["max_attempts"] == 5
            assert result["base_backoff_s"] == 1.5
            assert result["verify_ssl"] is True
    
    def test_provider_specific_settings(self, mock_config):
        """Test that provider-specific settings are used."""
        result = get_network_config("gallica")
        assert result["max_attempts"] == 3
    
    def test_none_provider(self):
        """Test with None provider."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_network_config(None)
            assert isinstance(result, dict)


class TestGetDownloadLimits:
    """Tests for get_download_limits function."""
    
    def test_returns_limits_section(self, mock_config):
        """Test that download_limits section is returned."""
        result = get_download_limits()
        assert isinstance(result, dict)
        assert "total" in result
        assert "per_work" in result
    
    def test_returns_empty_dict_when_missing(self):
        """Test that empty dict is returned when section missing."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_download_limits()
            assert result == {}


class TestGetMaxPages:
    """Tests for get_max_pages function."""
    
    def test_returns_configured_value(self, mock_config):
        """Test that configured max_pages is returned."""
        result = get_max_pages("gallica")
        assert result == 500
    
    def test_returns_none_for_missing(self, mock_config):
        """Test that None is returned for missing config."""
        result = get_max_pages("unknown_provider")
        assert result is None


class TestGetResumeMode:
    """Tests for get_resume_mode function."""
    
    def test_returns_default(self):
        """Test that default resume mode is returned."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_resume_mode()
            assert result == "skip_completed"
    
    def test_returns_configured_value(self, mock_config):
        """Test that configured value is returned."""
        result = get_resume_mode()
        assert result == "skip_completed"
