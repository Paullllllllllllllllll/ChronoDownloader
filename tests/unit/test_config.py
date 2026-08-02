"""Unit tests for api.core.config module."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import pytest

import api.core.config as config_module
from api.core.config import (
    DEFAULT_MIN_TITLE_SCORE,
    get_config,
    get_download_config,
    get_download_limits,
    get_max_pages,
    get_min_title_score,
    get_network_config,
    get_provider_setting,
    get_resume_mode,
    include_metadata,
    overwrite_existing,
    prefer_pdf_over_images,
)


class TestGetConfig:
    """Tests for get_config function."""

    def test_returns_dict(self, config_file: str) -> None:
        """Test that get_config returns a dictionary."""
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_file}):
            config_module._CONFIG_CACHE = None
            result = get_config(force_reload=True)
            assert isinstance(result, dict)

    def test_loads_from_env_path(self, config_file: str) -> None:
        """Test loading config from CHRONO_CONFIG_PATH environment variable."""
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_file}):
            config_module._CONFIG_CACHE = None
            result = get_config(force_reload=True)
            assert "providers" in result

    def test_raises_for_missing_file_and_no_example(self, temp_dir: str) -> None:
        """FileNotFoundError is raised when neither config.json nor
        config.example.json exists beside the resolved path."""
        missing_path = os.path.join(temp_dir, "nonexistent.json")
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": missing_path}):
            config_module._CONFIG_CACHE = None
            with pytest.raises(FileNotFoundError):
                get_config(force_reload=True)

    def test_caches_result(self, config_file: str) -> None:
        """Test that config is cached."""
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_file}):
            config_module._CONFIG_CACHE = None
            result1 = get_config(force_reload=True)
            result2 = get_config()
            assert result1 is result2

    def test_force_reload(
        self, config_file: str, sample_config: dict[str, Any]
    ) -> None:
        """Test that force_reload refreshes the cache."""
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_file}):
            config_module._CONFIG_CACHE = {"old": "data"}
            result = get_config(force_reload=True)
            assert "old" not in result
            assert "providers" in result

    def test_handles_invalid_json(self, temp_dir: str) -> None:
        """A present-but-unparseable config raises rather than silently
        caching an empty dict and running on bare defaults."""
        invalid_path = os.path.join(temp_dir, "invalid.json")
        with open(invalid_path, "w", encoding="utf-8") as f:
            f.write("not valid json {{{")

        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": invalid_path}):
            config_module._CONFIG_CACHE = None
            with pytest.raises(ValueError, match="invalid JSON"):
                get_config(force_reload=True)
            # The bad config must not have been cached.
            assert config_module._CONFIG_CACHE is None


class TestGetProviderSetting:
    """Tests for get_provider_setting function."""

    def test_retrieves_existing_setting(self, mock_config: dict[str, Any]) -> None:
        """Test retrieving an existing provider setting."""
        result = get_provider_setting("gallica", "max_pages")
        assert result == 500

    def test_returns_default_for_missing_setting(
        self, mock_config: dict[str, Any]
    ) -> None:
        """Test that default is returned for missing setting."""
        result = get_provider_setting("gallica", "nonexistent", default=42)
        assert result == 42

    def test_returns_default_for_missing_provider(
        self, mock_config: dict[str, Any]
    ) -> None:
        """Test that default is returned for missing provider."""
        result = get_provider_setting("unknown_provider", "max_pages", default=100)
        assert result == 100

    def test_alias_mapping(self, mock_config: dict[str, Any]) -> None:
        """Test that bnf_gallica maps to gallica."""
        result = get_provider_setting("bnf_gallica", "max_pages")
        assert result == 500


class TestMalformedConfigShapes:
    """A hand-edited config of the wrong shape must degrade to the defaults.

    ``"selection": "strict"`` or ``"bne": true`` used to raise AttributeError
    out of a plain ``.get()`` chain, aborting the run instead of falling back
    the way ``get_year_tolerance`` and ``get_search_timeout`` already do.
    """

    def test_scalar_provider_settings_section(self) -> None:
        with patch("api.core.config.get_config", return_value={"provider_settings": 1}):
            assert get_provider_setting("gallica", "max_pages", default=7) == 7

    def test_scalar_provider_entry(self) -> None:
        cfg = {"provider_settings": {"bne": True}}
        with patch("api.core.config.get_config", return_value=cfg):
            assert get_provider_setting("bne", "max_pages", default=7) == 7
            assert get_max_pages("bne") is None

    def test_scalar_alias_target(self) -> None:
        cfg = {"provider_settings": {"gallica": "strict"}}
        with patch("api.core.config.get_config", return_value=cfg):
            assert get_provider_setting("bnf_gallica", "max_pages", default=7) == 7

    def test_alias_still_resolves_for_a_well_formed_section(self) -> None:
        """The bnf_gallica -> gallica alias survives the shape guard."""
        cfg = {"provider_settings": {"gallica": {"max_pages": 500}}}
        with patch("api.core.config.get_config", return_value=cfg):
            assert get_provider_setting("bnf_gallica", "max_pages") == 500
            assert get_provider_setting("gallica", "max_pages") == 500

    def test_scalar_selection_section_for_min_title_score(self) -> None:
        with patch("api.core.config.get_config", return_value={"selection": "strict"}):
            assert get_min_title_score() == DEFAULT_MIN_TITLE_SCORE
            assert get_min_title_score("mdz") == DEFAULT_MIN_TITLE_SCORE

    def test_provider_override_still_wins_for_min_title_score(self) -> None:
        cfg = {
            "selection": {"min_title_score": 40},
            "provider_settings": {"mdz": {"min_title_score": 70}},
        }
        with patch("api.core.config.get_config", return_value=cfg):
            assert get_min_title_score("mdz") == 70.0
            assert get_min_title_score() == 40.0


class TestGetDownloadConfig:
    """Tests for get_download_config function."""

    def test_returns_download_section(self, mock_config: dict[str, Any]) -> None:
        """Test that download section is returned."""
        result = get_download_config()
        assert isinstance(result, dict)
        assert result.get("prefer_pdf_over_images") is True

    def test_applies_defaults(self) -> None:
        """Test that defaults are applied for missing keys."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_download_config()
            assert "prefer_pdf_over_images" in result
            assert "overwrite_existing" in result
            assert "include_metadata" in result

    def test_default_values(self) -> None:
        """Test default values."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_download_config()
            assert result["prefer_pdf_over_images"] is True
            assert result["overwrite_existing"] is False
            assert result["include_metadata"] is True


class TestPreferPdfOverImages:
    """Tests for prefer_pdf_over_images function."""

    def test_returns_true_by_default(self) -> None:
        """Test that True is returned by default."""
        with patch("api.core.config.get_config", return_value={}):
            assert prefer_pdf_over_images() is True

    def test_returns_configured_value(self, mock_config: dict[str, Any]) -> None:
        """Test that configured value is returned."""
        assert prefer_pdf_over_images() is True


class TestOverwriteExisting:
    """Tests for overwrite_existing function."""

    def test_returns_false_by_default(self) -> None:
        """Test that False is returned by default."""
        with patch("api.core.config.get_config", return_value={}):
            assert overwrite_existing() is False


class TestIncludeMetadata:
    """Tests for include_metadata function."""

    def test_returns_true_by_default(self) -> None:
        """Test that True is returned by default."""
        with patch("api.core.config.get_config", return_value={}):
            assert include_metadata() is True


class TestGetNetworkConfig:
    """Tests for get_network_config function."""

    def test_returns_dict(self, mock_config: dict[str, Any]) -> None:
        """Test that a dictionary is returned."""
        result = get_network_config("internet_archive")
        assert isinstance(result, dict)

    def test_applies_defaults(self) -> None:
        """Test that default values are applied."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_network_config("internet_archive")
            assert "delay_ms" in result
            assert "max_attempts" in result
            assert "base_backoff_s" in result
            assert "verify_ssl" in result

    def test_default_values(self) -> None:
        """Test specific default values."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_network_config(None)
            assert result["delay_ms"] == 0
            assert result["max_attempts"] == 5
            assert result["base_backoff_s"] == 1.5
            assert result["verify_ssl"] is True

    def test_provider_specific_settings(self, mock_config: dict[str, Any]) -> None:
        """Test that provider-specific settings are used."""
        result = get_network_config("gallica")
        assert result["max_attempts"] == 3

    def test_none_provider(self) -> None:
        """Test with None provider."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_network_config(None)
            assert isinstance(result, dict)

    def test_legacy_delay_ms_is_lifted_into_network(
        self, mock_config: dict[str, Any]
    ) -> None:
        """A provider block that only carries delay_ms still sets the delay."""
        assert get_network_config("internet_archive")["delay_ms"] == 50

    def test_alias_resolves_like_every_other_accessor(
        self, mock_config: dict[str, Any]
    ) -> None:
        """The one provider-settings accessor that skipped the alias.

        ``get_network_config("bnf_gallica")`` handed back the bare defaults
        while ``("gallica")`` returned the configured block, so the same
        provider had two network policies depending on the caller's spelling.
        """
        assert get_network_config("bnf_gallica") == get_network_config("gallica")
        assert get_network_config("bnf_gallica")["max_attempts"] == 3
        assert get_network_config("bnf_gallica")["delay_ms"] == 100

    def test_malformed_provider_settings_do_not_raise(self) -> None:
        """The shape guards the sibling accessors carry apply here too."""
        for cfg in (
            {"provider_settings": 1},
            {"provider_settings": {"gallica": "strict"}},
            {"provider_settings": {"gallica": {"network": "aggressive"}}},
        ):
            with patch("api.core.config.get_config", return_value=cfg):
                result = get_network_config("bnf_gallica")
                assert result["max_attempts"] == 5
                assert result["delay_ms"] == 0


class TestGetDownloadLimits:
    """Tests for get_download_limits function."""

    def test_returns_limits_section(self, mock_config: dict[str, Any]) -> None:
        """Test that download_limits section is returned."""
        result = get_download_limits()
        assert isinstance(result, dict)
        assert "total" in result
        assert "per_work" in result

    def test_returns_empty_dict_when_missing(self) -> None:
        """Test that empty dict is returned when section missing."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_download_limits()
            assert result == {}


class TestGetMaxPages:
    """Tests for get_max_pages function."""

    def test_returns_configured_value(self, mock_config: dict[str, Any]) -> None:
        """Test that configured max_pages is returned."""
        result = get_max_pages("gallica")
        assert result == 500

    def test_returns_none_for_missing(self, mock_config: dict[str, Any]) -> None:
        """Test that None is returned for missing config."""
        result = get_max_pages("unknown_provider")
        assert result is None

    def test_coerces_numeric_string(self) -> None:
        """A hand-written "500" must still cap the download.

        An isinstance(int) test returned None, which every caller reads as
        unlimited -- the opposite of the user's intent, on the one setting
        that bounds how many pages a work downloads.
        """
        cfg = {"provider_settings": {"gallica": {"max_pages": "500"}}}
        with patch("api.core.config.get_config", return_value=cfg):
            assert get_max_pages("gallica") == 500

    def test_non_numeric_value_falls_back_to_unlimited(self) -> None:
        cfg = {"provider_settings": {"gallica": {"max_pages": "many"}}}
        with patch("api.core.config.get_config", return_value=cfg):
            assert get_max_pages("gallica") is None

    def test_max_images_caps_when_max_pages_is_absent(self) -> None:
        """Wellcome ships "max_images"; the cap must bind the IIIF path too.

        Without the fallback the key only bounded the legacy image-service
        branch while the primary manifest path downloaded without limit.
        """
        cfg = {"provider_settings": {"wellcome": {"max_images": 40}}}
        with patch("api.core.config.get_config", return_value=cfg):
            assert get_max_pages("wellcome") == 40

    def test_max_pages_wins_over_max_images(self) -> None:
        cfg = {"provider_settings": {"wellcome": {"max_pages": 10, "max_images": 40}}}
        with patch("api.core.config.get_config", return_value=cfg):
            assert get_max_pages("wellcome") == 10


class TestGetResumeMode:
    """Tests for get_resume_mode function."""

    def test_returns_default(self) -> None:
        """Test that default resume mode is returned."""
        with patch("api.core.config.get_config", return_value={}):
            result = get_resume_mode()
            assert result == "skip_completed"

    def test_returns_configured_value(self, mock_config: dict[str, Any]) -> None:
        """Test that configured value is returned."""
        result = get_resume_mode()
        assert result == "skip_completed"


def _write_json(path: str, payload: dict[str, Any]) -> None:
    """Write a JSON payload to path."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


class TestGetApiKeyEnvvar:
    """Tests for get_api_key_envvar and the api_keys.json loader."""

    def test_returns_mapped_name(self, temp_dir: str) -> None:
        """Mapped env var name is returned when present in api_keys.json."""
        config_path = os.path.join(temp_dir, "config.json")
        _write_json(config_path, {})
        _write_json(
            os.path.join(temp_dir, "api_keys.json"),
            {"europeana": "EUROPEANA_API_KEY_2"},
        )
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_path}):
            config_module._API_KEYS_CACHE = None
            result = config_module.get_api_key_envvar("europeana", "EUROPEANA_API_KEY")
            assert result == "EUROPEANA_API_KEY_2"

    def test_returns_default_when_provider_absent(self, temp_dir: str) -> None:
        """Default is returned when the provider has no mapping entry."""
        config_path = os.path.join(temp_dir, "config.json")
        _write_json(config_path, {})
        _write_json(
            os.path.join(temp_dir, "api_keys.json"),
            {"dpla": "DPLA_API_KEY_2"},
        )
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_path}):
            config_module._API_KEYS_CACHE = None
            result = config_module.get_api_key_envvar("europeana", "EUROPEANA_API_KEY")
            assert result == "EUROPEANA_API_KEY"

    def test_returns_default_when_value_empty(self, temp_dir: str) -> None:
        """Default is returned when the mapped value is empty."""
        config_path = os.path.join(temp_dir, "config.json")
        _write_json(config_path, {})
        _write_json(
            os.path.join(temp_dir, "api_keys.json"),
            {"europeana": "   "},
        )
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_path}):
            config_module._API_KEYS_CACHE = None
            result = config_module.get_api_key_envvar("europeana", "EUROPEANA_API_KEY")
            assert result == "EUROPEANA_API_KEY"

    def test_default_used_when_file_absent(self, temp_dir: str) -> None:
        """Default name is returned when api_keys.json is absent."""
        config_path = os.path.join(temp_dir, "config.json")
        _write_json(config_path, {})
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_path}):
            config_module._API_KEYS_CACHE = None
            result = config_module.get_api_key_envvar("europeana", "EUROPEANA_API_KEY")
            assert result == "EUROPEANA_API_KEY"

    def test_loader_returns_empty_when_absent(self, temp_dir: str) -> None:
        """The api_keys.json loader returns {} when the file is absent."""
        config_path = os.path.join(temp_dir, "config.json")
        _write_json(config_path, {})
        with patch.dict(os.environ, {"CHRONO_CONFIG_PATH": config_path}):
            config_module._API_KEYS_CACHE = None
            assert config_module.get_api_keys_config(force_reload=True) == {}
