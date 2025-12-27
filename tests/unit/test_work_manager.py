"""Unit tests for work_manager module."""
from __future__ import annotations

import json
import os
from datetime import datetime
from unittest.mock import patch

import pytest

from main.work_manager import (
    get_naming_config,
    compute_work_id,
    compute_work_dir,
    check_work_status,
    update_work_status,
    create_work_json,
    format_candidates_for_json,
    format_selected_for_json,
)


class TestGetNamingConfig:
    """Tests for get_naming_config function."""
    
    def test_returns_naming_section(self, sample_config):
        """Test returns naming section from config."""
        sample_config["naming"] = {
            "include_creator_in_work_dir": False,
            "title_slug_max_len": 100
        }
        
        with patch("main.work_manager.get_config", return_value=sample_config):
            naming = get_naming_config()
            
            assert naming["include_creator_in_work_dir"] is False
            assert naming["title_slug_max_len"] == 100
    
    def test_applies_defaults(self):
        """Test applies defaults for missing values."""
        config = {"naming": {}}
        
        with patch("main.work_manager.get_config", return_value=config):
            naming = get_naming_config()
            
            assert naming["include_creator_in_work_dir"] is True
            assert naming["include_year_in_work_dir"] is True
            assert naming["title_slug_max_len"] == 80
    
    def test_handles_missing_section(self):
        """Test handles missing naming section."""
        config = {}
        
        with patch("main.work_manager.get_config", return_value=config):
            naming = get_naming_config()
            
            assert "include_creator_in_work_dir" in naming


class TestComputeWorkId:
    """Tests for compute_work_id function."""
    
    def test_generates_consistent_id(self):
        """Test generates consistent ID for same inputs."""
        id1 = compute_work_id("The Art of Cooking", "John Smith")
        id2 = compute_work_id("The Art of Cooking", "John Smith")
        
        assert id1 == id2
        assert len(id1) == 10
    
    def test_different_title_different_id(self):
        """Test different titles produce different IDs."""
        id1 = compute_work_id("Title A", "Author")
        id2 = compute_work_id("Title B", "Author")
        
        assert id1 != id2
    
    def test_different_creator_different_id(self):
        """Test different creators produce different IDs."""
        id1 = compute_work_id("Title", "Author A")
        id2 = compute_work_id("Title", "Author B")
        
        assert id1 != id2
    
    def test_none_creator_handled(self):
        """Test None creator is handled."""
        id1 = compute_work_id("Title", None)
        id2 = compute_work_id("Title", None)
        
        assert id1 == id2
    
    def test_case_insensitive(self):
        """Test ID generation is case-insensitive."""
        id1 = compute_work_id("THE TITLE", "AUTHOR")
        id2 = compute_work_id("the title", "author")
        
        assert id1 == id2


class TestComputeWorkDir:
    """Tests for compute_work_dir function."""
    
    def test_creates_work_dir_path(self, temp_dir, sample_config):
        """Test creates work directory path."""
        with patch("main.work_manager.get_config", return_value=sample_config):
            work_dir, work_dir_name = compute_work_dir(
                temp_dir, "E0001", "The Art of Cooking"
            )
            
            assert work_dir.startswith(temp_dir)
            assert "e_0001" in work_dir_name.lower()
    
    def test_without_entry_id(self, temp_dir, sample_config):
        """Test without entry ID."""
        with patch("main.work_manager.get_config", return_value=sample_config):
            work_dir, work_dir_name = compute_work_dir(
                temp_dir, None, "The Art of Cooking"
            )
            
            assert work_dir.startswith(temp_dir)
            assert "art" in work_dir_name.lower()


class TestCheckWorkStatus:
    """Tests for check_work_status function."""
    
    def test_skip_completed_with_work_json(self, temp_dir):
        """Test skip_completed mode with completed work.json."""
        work_dir = os.path.join(temp_dir, "test_work")
        os.makedirs(work_dir)
        
        work_json_path = os.path.join(work_dir, "work.json")
        with open(work_json_path, "w") as f:
            json.dump({"status": "completed"}, f)
        
        should_skip, reason = check_work_status(work_dir, "skip_completed")
        
        assert should_skip is True
        assert "completed" in reason
    
    def test_skip_completed_with_pending_status(self, temp_dir):
        """Test skip_completed mode with pending status."""
        work_dir = os.path.join(temp_dir, "test_work")
        os.makedirs(work_dir)
        
        work_json_path = os.path.join(work_dir, "work.json")
        with open(work_json_path, "w") as f:
            json.dump({"status": "pending"}, f)
        
        should_skip, reason = check_work_status(work_dir, "skip_completed")
        
        assert should_skip is False
    
    def test_skip_if_has_objects_with_files(self, temp_dir):
        """Test skip_if_has_objects mode with existing files."""
        work_dir = os.path.join(temp_dir, "test_work")
        objects_dir = os.path.join(work_dir, "objects")
        os.makedirs(objects_dir)
        
        test_file = os.path.join(objects_dir, "test.pdf")
        with open(test_file, "w") as f:
            f.write("test")
        
        should_skip, reason = check_work_status(work_dir, "skip_if_has_objects")
        
        assert should_skip is True
        assert "1 file" in reason
    
    def test_skip_if_has_objects_empty_dir(self, temp_dir):
        """Test skip_if_has_objects mode with empty objects dir."""
        work_dir = os.path.join(temp_dir, "test_work")
        objects_dir = os.path.join(work_dir, "objects")
        os.makedirs(objects_dir)
        
        should_skip, reason = check_work_status(work_dir, "skip_if_has_objects")
        
        assert should_skip is False
    
    def test_reprocess_all_never_skips(self, temp_dir):
        """Test reprocess_all mode never skips."""
        work_dir = os.path.join(temp_dir, "test_work")
        os.makedirs(work_dir)
        
        should_skip, reason = check_work_status(work_dir, "reprocess_all")
        
        assert should_skip is False
    
    def test_nonexistent_work_dir(self, temp_dir):
        """Test with nonexistent work directory."""
        work_dir = os.path.join(temp_dir, "nonexistent")
        
        should_skip, reason = check_work_status(work_dir, "skip_completed")
        
        assert should_skip is False
    
    def test_uses_config_resume_mode(self, temp_dir):
        """Test uses resume mode from config when not specified."""
        work_dir = os.path.join(temp_dir, "test_work")
        os.makedirs(work_dir)
        
        with patch("main.work_manager.get_resume_mode", return_value="reprocess_all"):
            should_skip, reason = check_work_status(work_dir)
            
            assert should_skip is False


class TestUpdateWorkStatus:
    """Tests for update_work_status function."""
    
    def test_updates_status_field(self, temp_dir):
        """Test updates status field in work.json."""
        work_json_path = os.path.join(temp_dir, "work.json")
        with open(work_json_path, "w") as f:
            json.dump({"status": "pending", "title": "Test"}, f)
        
        update_work_status(work_json_path, "completed")
        
        with open(work_json_path, "r") as f:
            data = json.load(f)
        
        assert data["status"] == "completed"
        assert data["title"] == "Test"
        assert "updated_at" in data
    
    def test_adds_download_info(self, temp_dir):
        """Test adds download info to work.json."""
        work_json_path = os.path.join(temp_dir, "work.json")
        with open(work_json_path, "w") as f:
            json.dump({"status": "pending"}, f)
        
        download_info = {"provider": "Internet Archive", "files": 10}
        update_work_status(work_json_path, "completed", download_info)
        
        with open(work_json_path, "r") as f:
            data = json.load(f)
        
        assert data["download"] == download_info
    
    def test_handles_missing_file(self, temp_dir):
        """Test handles missing work.json file."""
        work_json_path = os.path.join(temp_dir, "nonexistent.json")
        
        update_work_status(work_json_path, "completed")
    
    def test_handles_invalid_json(self, temp_dir):
        """Test handles invalid JSON file."""
        work_json_path = os.path.join(temp_dir, "work.json")
        with open(work_json_path, "w") as f:
            f.write("invalid json{")
        
        update_work_status(work_json_path, "completed")


class TestCreateWorkJson:
    """Tests for create_work_json function."""
    
    def test_creates_work_json(self, temp_dir):
        """Test creates work.json file."""
        work_json_path = os.path.join(temp_dir, "work.json")
        
        create_work_json(
            work_json_path,
            "Test Title",
            "Test Author",
            "E0001",
            {"strategy": "collect_and_select"},
            [],
            None
        )
        
        assert os.path.exists(work_json_path)
        
        with open(work_json_path, "r") as f:
            data = json.load(f)
        
        assert data["input"]["title"] == "Test Title"
        assert data["input"]["creator"] == "Test Author"
        assert data["input"]["entry_id"] == "E0001"
        assert data["status"] == "pending"
        assert "created_at" in data
    
    def test_custom_status(self, temp_dir):
        """Test creates work.json with custom status."""
        work_json_path = os.path.join(temp_dir, "work.json")
        
        create_work_json(
            work_json_path,
            "Test Title",
            None,
            None,
            {},
            [],
            None,
            status="no_match"
        )
        
        with open(work_json_path, "r") as f:
            data = json.load(f)
        
        assert data["status"] == "no_match"


class TestFormatCandidatesForJson:
    """Tests for format_candidates_for_json function."""
    
    def test_formats_search_results(self, sample_search_results):
        """Test formats SearchResult objects."""
        formatted = format_candidates_for_json(sample_search_results)
        
        assert len(formatted) == 3
        assert formatted[0]["provider"] == "Internet Archive"
        assert formatted[0]["title"] == "The Art of Cooking"
        assert "scores" in formatted[0]
    
    def test_empty_list(self):
        """Test with empty list."""
        formatted = format_candidates_for_json([])
        
        assert formatted == []


class TestFormatSelectedForJson:
    """Tests for format_selected_for_json function."""
    
    def test_formats_selected_result(self, sample_search_result):
        """Test formats selected SearchResult."""
        formatted = format_selected_for_json(sample_search_result, "test_id")
        
        assert formatted is not None
        assert formatted["provider"] == "Internet Archive"
        assert formatted["source_id"] == "test_id"
        assert formatted["title"] == "The Art of Cooking"
    
    def test_none_selected(self):
        """Test with None selected."""
        formatted = format_selected_for_json(None, None)
        
        assert formatted is None
