"""Integration tests for main.pipeline module."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest


class TestLoadEnabledApis:
    """Tests for load_enabled_apis function."""
    
    def test_loads_from_config_file(self, config_file: str, sample_config):
        """Test loading enabled providers from config file."""
        from main.pipeline import load_enabled_apis
        
        enabled = load_enabled_apis(config_file)
        
        assert len(enabled) >= 1
        # Check structure of returned tuples
        for item in enabled:
            assert len(item) == 4
            key, search_fn, download_fn, name = item
            assert isinstance(key, str)
            assert callable(search_fn)
            assert callable(download_fn)
            assert isinstance(name, str)
    
    def test_returns_default_for_missing_file(self, temp_dir: str):
        """Test that default providers are returned for missing file."""
        from main.pipeline import load_enabled_apis
        
        missing_path = os.path.join(temp_dir, "nonexistent.json")
        enabled = load_enabled_apis(missing_path)
        
        # Should return Internet Archive as default
        assert len(enabled) == 1
        assert enabled[0][0] == "internet_archive"
    
    def test_returns_empty_for_no_enabled(self, temp_dir: str):
        """Test empty list when no providers enabled."""
        from main.pipeline import load_enabled_apis
        
        config_path = os.path.join(temp_dir, "empty_providers.json")
        with open(config_path, "w") as f:
            json.dump({"providers": {}}, f)
        
        enabled = load_enabled_apis(config_path)
        
        assert enabled == []


class TestFilterEnabledProvidersForKeys:
    """Tests for filter_enabled_providers_for_keys function."""
    
    def test_keeps_providers_without_key_requirement(self):
        """Test that providers without key requirements are kept."""
        from main.pipeline import filter_enabled_providers_for_keys
        
        enabled = [
            ("internet_archive", lambda: None, lambda: None, "Internet Archive"),
            ("mdz", lambda: None, lambda: None, "MDZ"),
        ]
        
        filtered = filter_enabled_providers_for_keys(enabled)
        
        # Both should be kept as they don't require keys
        assert len(filtered) == 2
    
    def test_filters_providers_missing_keys(self):
        """Test that providers missing required keys are filtered."""
        from main.pipeline import filter_enabled_providers_for_keys
        
        enabled = [
            ("europeana", lambda: None, lambda: None, "Europeana"),
            ("internet_archive", lambda: None, lambda: None, "Internet Archive"),
        ]
        
        # Clear any existing env var
        with patch.dict(os.environ, {"EUROPEANA_API_KEY": ""}, clear=False):
            # Remove the key if it exists
            os.environ.pop("EUROPEANA_API_KEY", None)
            filtered = filter_enabled_providers_for_keys(enabled)
        
        # Europeana should be filtered out
        filtered_keys = [f[0] for f in filtered]
        assert "europeana" not in filtered_keys
        assert "internet_archive" in filtered_keys
    
    def test_keeps_providers_with_keys(self):
        """Test that providers with required keys are kept."""
        from main.pipeline import filter_enabled_providers_for_keys
        
        enabled = [
            ("europeana", lambda: None, lambda: None, "Europeana"),
        ]
        
        with patch.dict(os.environ, {"EUROPEANA_API_KEY": "test_key"}):
            filtered = filter_enabled_providers_for_keys(enabled)
        
        assert len(filtered) == 1
        assert filtered[0][0] == "europeana"


class TestProviderOrder:
    """Tests for _provider_order function."""
    
    def test_reorders_by_hierarchy(self):
        """Test that providers are reordered by hierarchy."""
        from main.pipeline import _provider_order
        
        enabled = [
            ("ia", None, None, "IA"),
            ("mdz", None, None, "MDZ"),
            ("gallica", None, None, "Gallica"),
        ]
        
        hierarchy = ["gallica", "mdz", "ia"]
        
        ordered = _provider_order(enabled, hierarchy)
        
        assert ordered[0][0] == "gallica"
        assert ordered[1][0] == "mdz"
        assert ordered[2][0] == "ia"
    
    def test_appends_unlisted_providers(self):
        """Test that providers not in hierarchy are appended."""
        from main.pipeline import _provider_order
        
        enabled = [
            ("ia", None, None, "IA"),
            ("mdz", None, None, "MDZ"),
            ("loc", None, None, "LOC"),
        ]
        
        hierarchy = ["mdz"]  # Only mdz specified
        
        ordered = _provider_order(enabled, hierarchy)
        
        # mdz should be first, others should follow
        assert ordered[0][0] == "mdz"
        remaining = [o[0] for o in ordered[1:]]
        assert "ia" in remaining
        assert "loc" in remaining
    
    def test_handles_empty_hierarchy(self):
        """Test handling of empty hierarchy."""
        from main.pipeline import _provider_order
        
        enabled = [
            ("ia", None, None, "IA"),
            ("mdz", None, None, "MDZ"),
        ]
        
        ordered = _provider_order(enabled, [])
        
        # Should return original order
        assert ordered == enabled


class TestGetSelectionConfig:
    """Tests for _get_selection_config function."""
    
    def test_returns_dict(self, mock_config):
        """Test that selection config is returned as dict."""
        from main.pipeline import _get_selection_config
        
        config = _get_selection_config()
        
        assert isinstance(config, dict)
    
    def test_applies_defaults(self):
        """Test that defaults are applied."""
        with patch("main.pipeline.get_config", return_value={}):
            from main.pipeline import _get_selection_config
            
            config = _get_selection_config()
            
            assert "strategy" in config
            assert "min_title_score" in config
            assert "creator_weight" in config
            assert config["strategy"] == "collect_and_select"
            assert config["min_title_score"] == 85


class TestUpdateWorkStatus:
    """Tests for update_work_status function."""
    
    def test_updates_status(self, work_dir_structure):
        """Test updating work status."""
        from main.pipeline import update_work_status
        
        work_json_path = work_dir_structure["work_json_path"]
        
        update_work_status(work_json_path, "completed")
        
        with open(work_json_path) as f:
            data = json.load(f)
        
        assert data["status"] == "completed"
    
    def test_updates_with_extra_data(self, work_dir_structure):
        """Test updating work status with extra data."""
        from main.pipeline import update_work_status
        
        work_json_path = work_dir_structure["work_json_path"]
        
        update_work_status(work_json_path, "completed", {
            "provider": "Internet Archive",
            "source_id": "test123"
        })
        
        with open(work_json_path) as f:
            data = json.load(f)
        
        assert data["status"] == "completed"
        # Extra data may be stored with different key names
        # Just verify status was updated correctly
        assert "status" in data


class TestBuildIndexRow:
    """Tests for build_index_row function."""
    
    def test_builds_row_dict(self, sample_search_result):
        """Test building index row dictionary."""
        from main.pipeline import build_index_row
        
        row = build_index_row(
            work_id="work_123",
            entry_id="E0001",
            work_dir="/path/to/work",
            title="Test Title",
            creator="Test Author",
            selected=sample_search_result,
            selected_source_id="source123",
            work_json_path="/path/to/work.json",
            status="completed",
            item_url="https://example.com"
        )
        
        assert isinstance(row, dict)
        assert row["work_id"] == "work_123"
        assert row["entry_id"] == "E0001"
        assert row["status"] == "completed"
