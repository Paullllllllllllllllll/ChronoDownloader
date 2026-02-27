"""Extended tests for main.pipeline module — core orchestration."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from api.model import QuotaDeferredException, SearchResult
from main.pipeline import (
    _compute_selected_source_id,
    _get_selection_config,
    _persist_work_json,
    _PreparedWork,
    _provider_order,
    _required_provider_envvars,
    _run_download_with_fallback,
    execute_download,
    filter_enabled_providers_for_keys,
    load_enabled_apis,
    search_and_select,
)


def _make_sr(title="Test", provider_key="ia", source_id="id1", **kwargs):
    defaults = dict(
        provider="Test Provider",
        title=title,
        creators=[],
        source_id=source_id,
        provider_key=provider_key,
        raw={},
    )
    defaults.update(kwargs)
    return SearchResult(**defaults)


def _make_provider_tuple(key="ia", name="Internet Archive"):
    return (key, MagicMock(name=f"search_{key}"), MagicMock(name=f"download_{key}"), name)


# ============================================================================
# _compute_selected_source_id
# ============================================================================

class TestComputeSelectedSourceId:
    """Tests for source ID computation."""

    def test_returns_source_id(self):
        sr = _make_sr(source_id="abc123")
        assert _compute_selected_source_id(sr) == "abc123"

    def test_returns_identifier_from_raw(self):
        sr = _make_sr(source_id=None)
        sr.raw["identifier"] = "raw_id"
        assert _compute_selected_source_id(sr) == "raw_id"

    def test_returns_ark_id_from_raw(self):
        sr = _make_sr(source_id=None)
        sr.raw["ark_id"] = "ark:/12148/abc"
        assert _compute_selected_source_id(sr) == "ark:/12148/abc"

    def test_returns_none_for_none_selected(self):
        assert _compute_selected_source_id(None) is None


# ============================================================================
# _get_selection_config
# ============================================================================

class TestGetSelectionConfigExtended:
    """Extended tests for selection configuration."""

    @patch("main.pipeline.get_config", return_value={})
    def test_applies_all_defaults(self, mock_cfg):
        sel = _get_selection_config()
        assert sel["strategy"] == "collect_and_select"
        assert sel["min_title_score"] == 85
        assert sel["creator_weight"] == 0.2
        assert sel["max_candidates_per_provider"] == 5
        assert sel["download_strategy"] == "selected_only"
        assert sel["keep_non_selected_metadata"] is True
        assert sel["year_tolerance"] == 2
        assert sel["max_parallel_searches"] == 1

    @patch("main.pipeline.get_config", return_value={
        "selection": {"strategy": "sequential_first_hit", "min_title_score": 90}
    })
    def test_merges_with_config(self, mock_cfg):
        sel = _get_selection_config()
        assert sel["strategy"] == "sequential_first_hit"
        assert sel["min_title_score"] == 90
        assert sel["creator_weight"] == 0.2  # Default still applied


# ============================================================================
# _provider_order
# ============================================================================

class TestProviderOrderExtended:
    """Extended tests for provider ordering."""

    def test_no_hierarchy_preserves_order(self):
        p1 = _make_provider_tuple("ia", "IA")
        p2 = _make_provider_tuple("mdz", "MDZ")
        result = _provider_order([p1, p2], [])
        assert result == [p1, p2]

    def test_reorders_by_hierarchy(self):
        p1 = _make_provider_tuple("ia", "IA")
        p2 = _make_provider_tuple("mdz", "MDZ")
        result = _provider_order([p1, p2], ["mdz", "ia"])
        assert result[0][0] == "mdz"
        assert result[1][0] == "ia"

    def test_appends_unlisted_providers(self):
        p1 = _make_provider_tuple("ia", "IA")
        p2 = _make_provider_tuple("mdz", "MDZ")
        p3 = _make_provider_tuple("gallica", "Gallica")
        result = _provider_order([p1, p2, p3], ["mdz"])
        assert result[0][0] == "mdz"
        assert len(result) == 3


# ============================================================================
# _required_provider_envvars
# ============================================================================

class TestRequiredProviderEnvvars:
    """Tests for provider API key requirements."""

    def test_returns_dict(self):
        result = _required_provider_envvars()
        assert isinstance(result, dict)
        assert "europeana" in result
        assert "dpla" in result

    def test_europeana_requires_key(self):
        assert "EUROPEANA_API_KEY" in _required_provider_envvars()["europeana"]


# ============================================================================
# filter_enabled_providers_for_keys
# ============================================================================

class TestFilterEnabledProvidersForKeysExtended:
    """Extended tests for API key filtering."""

    @patch.dict(os.environ, {"EUROPEANA_API_KEY": "test_key"})
    def test_keeps_provider_with_key(self):
        p = _make_provider_tuple("europeana", "Europeana")
        result = filter_enabled_providers_for_keys([p])
        assert len(result) == 1

    @patch.dict(os.environ, {}, clear=True)
    def test_filters_provider_without_key(self):
        p = _make_provider_tuple("europeana", "Europeana")
        # Remove the key if it exists
        os.environ.pop("EUROPEANA_API_KEY", None)
        result = filter_enabled_providers_for_keys([p])
        assert len(result) == 0

    def test_keeps_provider_without_key_requirement(self):
        p = _make_provider_tuple("mdz", "MDZ")
        result = filter_enabled_providers_for_keys([p])
        assert len(result) == 1


# ============================================================================
# _run_download_with_fallback
# ============================================================================

class TestRunDownloadWithFallback:
    """Tests for download-with-fallback logic."""

    def test_successful_primary_download(self):
        download_func = MagicMock(return_value=True)
        sel_cfg = {"min_title_score": 85, "download_strategy": "selected_only"}
        sr = _make_sr()

        succeeded, deferred = _run_download_with_fallback(
            selected=sr,
            pkey="ia",
            pname="Internet Archive",
            download_func=download_func,
            work_dir="/out",
            all_candidates=[sr],
            provider_list=[_make_provider_tuple()],
            provider_map={"ia": (MagicMock(), download_func, "IA")},
            sel_cfg=sel_cfg,
            title="Test",
            creator=None,
            entry_id="E001",
            base_output_dir="/base",
            selected_source_id="id1",
        )
        assert succeeded is True
        assert deferred is False

    def test_failed_primary_triggers_fallback(self):
        primary_dl = MagicMock(return_value=False)
        fallback_dl = MagicMock(return_value=True)
        sel_cfg = {"min_title_score": 85, "download_strategy": "selected_only"}

        sr1 = _make_sr(source_id="id1", provider_key="ia")
        sr1.raw["__matching__"] = {"score": 90, "total": 90}
        sr2 = _make_sr(source_id="id2", provider_key="mdz")
        sr2.raw["__matching__"] = {"score": 90, "total": 90}

        p_ia = _make_provider_tuple("ia", "IA")
        p_mdz = _make_provider_tuple("mdz", "MDZ")

        succeeded, deferred = _run_download_with_fallback(
            selected=sr1,
            pkey="ia",
            pname="Internet Archive",
            download_func=primary_dl,
            work_dir="/out",
            all_candidates=[sr1, sr2],
            provider_list=[p_ia, p_mdz],
            provider_map={
                "ia": (MagicMock(), primary_dl, "IA"),
                "mdz": (MagicMock(), fallback_dl, "MDZ"),
            },
            sel_cfg=sel_cfg,
            title="Test",
            creator=None,
            entry_id="E001",
            base_output_dir="/base",
            selected_source_id="id1",
        )
        assert succeeded is True
        fallback_dl.assert_called_once()

    @patch("main.pipeline.get_deferred_queue")
    def test_quota_deferred_exception(self, mock_queue):
        download_func = MagicMock(side_effect=QuotaDeferredException("ia"))
        sel_cfg = {"min_title_score": 85, "download_strategy": "selected_only"}
        sr = _make_sr()

        succeeded, deferred = _run_download_with_fallback(
            selected=sr,
            pkey="ia",
            pname="Internet Archive",
            download_func=download_func,
            work_dir="/out",
            all_candidates=[sr],
            provider_list=[_make_provider_tuple()],
            provider_map={"ia": (MagicMock(), download_func, "IA")},
            sel_cfg=sel_cfg,
            title="Test",
            creator=None,
            entry_id="E001",
            base_output_dir="/base",
            selected_source_id="id1",
        )
        assert deferred is True
        assert succeeded is False
        mock_queue.return_value.add.assert_called_once()

    def test_download_all_strategy(self):
        primary_dl = MagicMock(return_value=True)
        extra_dl = MagicMock(return_value=True)
        sel_cfg = {"min_title_score": 85, "download_strategy": "all"}

        sr1 = _make_sr(source_id="id1", provider_key="ia")
        sr2 = _make_sr(source_id="id2", provider_key="mdz")

        p_ia = _make_provider_tuple("ia", "IA")
        p_mdz = _make_provider_tuple("mdz", "MDZ")

        succeeded, deferred = _run_download_with_fallback(
            selected=sr1,
            pkey="ia",
            pname="Internet Archive",
            download_func=primary_dl,
            work_dir="/out",
            all_candidates=[sr1, sr2],
            provider_list=[p_ia, p_mdz],
            provider_map={
                "ia": (MagicMock(), primary_dl, "IA"),
                "mdz": (MagicMock(), extra_dl, "MDZ"),
            },
            sel_cfg=sel_cfg,
            title="Test",
            creator=None,
            entry_id="E001",
            base_output_dir="/base",
            selected_source_id="id1",
        )
        assert succeeded is True
        extra_dl.assert_called_once()


# ============================================================================
# execute_download
# ============================================================================

class TestExecuteDownload:
    """Tests for download task execution."""

    def test_dry_run_returns_true(self):
        task = MagicMock()
        task.title = "Test"
        result = execute_download(task, dry_run=True)
        assert result is True

    @patch("main.pipeline.update_index_csv")
    @patch("main.pipeline.build_index_row")
    @patch("main.pipeline.update_work_status")
    @patch("main.pipeline._run_download_with_fallback")
    def test_successful_download(self, mock_fallback, mock_status, mock_row, mock_csv):
        mock_fallback.return_value = (True, False)
        mock_row.return_value = {}

        task = MagicMock()
        task.title = "Test"
        task.provider_tuple = ("ia", MagicMock(), MagicMock(return_value=True), "IA")
        task.selected_result = _make_sr()
        task.work_dir = "/out"
        task.work_json_path = "/out/work.json"
        task.all_candidates = []
        task.provider_map = {}
        task.selection_config = {"min_title_score": 85}
        task.base_output_dir = "/base"
        task.work_id = "w1"
        task.entry_id = "E001"
        task.creator = None

        result = execute_download(task)
        assert result is True
        mock_status.assert_called()


# ============================================================================
# load_enabled_apis
# ============================================================================

class TestLoadEnabledApisExtended:
    """Extended tests for provider loading."""

    def test_nonexistent_config_returns_default(self, tmp_path):
        result = load_enabled_apis(str(tmp_path / "nonexistent.json"))
        assert len(result) == 1
        assert result[0][0] == "internet_archive"

    def test_invalid_json_returns_default(self, tmp_path):
        config_file = tmp_path / "bad.json"
        config_file.write_text("not valid json")
        result = load_enabled_apis(str(config_file))
        assert len(result) == 1
        assert result[0][0] == "internet_archive"

    def test_no_enabled_returns_empty(self, tmp_path):
        import json
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"providers": {"internet_archive": False}}))
        result = load_enabled_apis(str(config_file))
        assert result == []
