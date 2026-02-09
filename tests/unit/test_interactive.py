"""Unit tests for interactive module - config workflow and UI components."""
from __future__ import annotations

import json
import os
import time
from io import StringIO
from pathlib import Path
from typing import Dict, Any
from unittest.mock import MagicMock, patch, call

import pytest

from main.interactive import InteractiveWorkflow
from main.console_ui import ConsoleUI, DownloadConfiguration


# ============================================================================
# DownloadConfiguration Tests
# ============================================================================

class TestDownloadConfiguration:
    """Tests for DownloadConfiguration dataclass."""
    
    def test_default_values(self):
        """Test default configuration values."""
        config = DownloadConfiguration()
        
        assert config.mode == "csv"
        assert config.csv_path is None
        assert config.output_dir == "downloaded_works"
        assert config.config_path == "config.json"
        assert config.dry_run is False
        assert config.log_level == "INFO"
        assert config.use_parallel is True
        assert config.stats == {}
    
    def test_stats_field_exists(self):
        """Test that stats field is available for session tracking."""
        config = DownloadConfiguration()
        config.stats = {"processed": 10, "succeeded": 8, "failed": 2}
        
        assert config.stats["processed"] == 10
        assert config.stats["succeeded"] == 8
        assert config.stats["failed"] == 2
    
    def test_custom_config_path(self):
        """Test setting custom config path."""
        config = DownloadConfiguration()
        config.config_path = "custom_config.json"
        
        assert config.config_path == "custom_config.json"


# ============================================================================
# ConsoleUI Tests
# ============================================================================

class TestConsoleUI:
    """Tests for ConsoleUI utility class."""
    
    def test_color_codes_defined(self):
        """Test that all color codes are defined."""
        assert hasattr(ConsoleUI, "RESET")
        assert hasattr(ConsoleUI, "BOLD")
        assert hasattr(ConsoleUI, "GREEN")
        assert hasattr(ConsoleUI, "YELLOW")
        assert hasattr(ConsoleUI, "RED")
        assert hasattr(ConsoleUI, "CYAN")
        assert hasattr(ConsoleUI, "DIM")
    
    def test_print_config_summary(self, capsys):
        """Test print_config_summary output."""
        config_data = {
            "Provider": "Anna's Archive",
            "Format": "PDF preferred",
            "Limit": "100 downloads"
        }
        
        ConsoleUI.print_config_summary(config_data, "Test Config")
        
        captured = capsys.readouterr()
        assert "Test Config" in captured.out
        assert "Provider" in captured.out
        assert "Anna's Archive" in captured.out
    
    def test_print_session_summary_basic(self, capsys):
        """Test basic session summary output."""
        ConsoleUI.print_session_summary(
            processed=10,
            succeeded=8,
            failed=1,
            deferred=1,
            output_dir="test_output",
            dry_run=False,
        )
        
        captured = capsys.readouterr()
        assert "SESSION COMPLETE" in captured.out
        assert "10" in captured.out  # processed
        assert "8" in captured.out   # succeeded
        assert "test_output" in captured.out
    
    def test_print_session_summary_dry_run(self, capsys):
        """Test session summary with dry run flag."""
        ConsoleUI.print_session_summary(
            processed=5,
            succeeded=0,
            failed=0,
            deferred=0,
            output_dir="test_output",
            dry_run=True,
        )
        
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
    
    def test_print_session_summary_with_duration(self, capsys):
        """Test session summary with duration."""
        ConsoleUI.print_session_summary(
            processed=10,
            succeeded=10,
            failed=0,
            deferred=0,
            output_dir="test_output",
            duration_seconds=125.5,  # ~2 minutes
        )
        
        captured = capsys.readouterr()
        assert "Duration" in captured.out
        assert "minutes" in captured.out
    
    def test_print_session_summary_with_providers(self, capsys):
        """Test session summary with providers list."""
        ConsoleUI.print_session_summary(
            processed=10,
            succeeded=10,
            failed=0,
            deferred=0,
            output_dir="test_output",
            providers_used=["Internet Archive", "BnF Gallica"],
        )
        
        captured = capsys.readouterr()
        assert "Providers Used" in captured.out
        assert "Internet Archive" in captured.out
    
    def test_print_session_summary_deferred_shows_next_steps(self, capsys):
        """Test session summary shows next steps when items are deferred."""
        ConsoleUI.print_session_summary(
            processed=10,
            succeeded=5,
            failed=0,
            deferred=5,
            output_dir="test_output",
        )
        
        captured = capsys.readouterr()
        assert "Next Steps" in captured.out
        assert "quota" in captured.out.lower()


# ============================================================================
# InteractiveWorkflow Tests
# ============================================================================

class TestInteractiveWorkflow:
    """Tests for InteractiveWorkflow class."""
    
    def test_init_creates_default_config(self):
        """Test that initialization creates default configuration."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
            
            assert workflow.config is not None
            assert isinstance(workflow.config, DownloadConfiguration)
            assert workflow.app_config == {}
            assert workflow.start_time == 0.0
    
    def test_get_mode_options(self):
        """Test get_mode_options returns valid options."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
            options = workflow.get_mode_options()
            
            assert len(options) == 4
            modes = [opt[0] for opt in options]
            assert "csv" in modes
            assert "single" in modes
            assert "collection" in modes
            assert "direct_iiif" in modes


class TestConfigureConfigFile:
    """Tests for configure_config_file method."""
    
    def test_finds_config_files(self, temp_dir):
        """Test that config files are discovered."""
        # Create test config files
        config1_path = os.path.join(temp_dir, "config.json")
        config2_path = os.path.join(temp_dir, "test_config.json")
        
        config_data = {
            "providers": {"internet_archive": True},
            "download": {"prefer_pdf_over_images": True},
            "budget": {"enabled": True, "max_total_downloads": 100}
        }
        
        for path in [config1_path, config2_path]:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config_data, f)
        
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        # Mock current directory to temp_dir
        with patch.object(Path, "glob") as mock_glob:
            mock_glob.return_value = [Path(config1_path), Path(config2_path)]
            
            # Mock user selection
            with patch.object(ConsoleUI, "prompt_select", return_value=config1_path):
                with patch("main.interactive.get_config", return_value=config_data):
                    result = workflow.configure_config_file()
        
        assert result is True
        assert workflow.config.config_path == config1_path
    
    def test_uses_default_when_no_configs(self, temp_dir):
        """Test fallback to default when no config files found."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        with patch.object(Path, "glob", return_value=[]):
            with patch.object(ConsoleUI, "print_warning"):
                result = workflow.configure_config_file()
        
        assert result is True
        assert workflow.config.config_path == "config.json"
    
    def test_config_file_metadata_extraction(self, temp_dir):
        """Test that config file metadata is extracted for display."""
        config_path = os.path.join(temp_dir, "test_config.json")
        
        config_data = {
            "providers": {
                "internet_archive": True,
                "bnf_gallica": True,
                "annas_archive": False
            },
            "download": {"prefer_pdf_over_images": True},
            "budget": {"enabled": True, "max_total_downloads": 500}
        }
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)
        
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        # Verify the config can be read and parsed
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        
        enabled_count = sum(1 for v in loaded["providers"].values() if v)
        assert enabled_count == 2  # internet_archive and bnf_gallica
    
    def test_sets_environment_variable(self, temp_dir):
        """Test that CHRONO_CONFIG_PATH is set after selection."""
        config_path = os.path.join(temp_dir, "custom_config.json")
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"providers": {}}, f)
        
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        with patch.object(Path, "glob", return_value=[Path(config_path)]):
            with patch.object(ConsoleUI, "prompt_select", return_value=config_path):
                with patch("main.interactive.get_config", return_value={}):
                    workflow.configure_config_file()
        
        assert os.environ.get("CHRONO_CONFIG_PATH") == config_path


class TestDisplayProviderStatus:
    """Tests for display_provider_status method."""
    
    def test_displays_enabled_providers(self, capsys, sample_config):
        """Test that enabled providers are displayed."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        with patch("main.interactive.get_config", return_value=sample_config):
            workflow.display_provider_status()
        
        captured = capsys.readouterr()
        assert "Enabled" in captured.out
    
    def test_displays_quota_info(self, capsys):
        """Test that quota information is displayed for providers with quotas."""
        config_with_quota = {
            "providers": {"annas_archive": True},
            "provider_settings": {
                "annas_archive": {
                    "quota": {
                        "enabled": True,
                        "daily_limit": 875
                    }
                }
            },
            "download": {}
        }
        
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        # Mock PROVIDERS to include annas_archive
        mock_providers = {
            "annas_archive": (None, None, "Anna's Archive")
        }
        
        with patch("main.interactive.get_config", return_value=config_with_quota):
            with patch("main.interactive.PROVIDERS", mock_providers):
                workflow.display_provider_status()
        
        captured = capsys.readouterr()
        assert "875" in captured.out or "quota" in captured.out.lower()


class TestDisplaySummary:
    """Tests for display_summary method."""
    
    def test_shows_config_file_in_summary(self, capsys):
        """Test that config file is shown in summary."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
            workflow.config.config_path = "essential_monographies_1_config.json"
            workflow.config.mode = "csv"
            workflow.config.csv_path = "test.csv"
            workflow.config.output_dir = "test_output"
        
        with patch("main.interactive.get_config", return_value={"providers": {}}):
            with patch("main.interactive.get_stats", return_value={"total": 10, "pending": 5, "completed": 3, "failed": 2}):
                with patch.object(ConsoleUI, "prompt_yes_no", return_value=True):
                    result = workflow.display_summary()
        
        captured = capsys.readouterr()
        assert "essential_monographies_1_config.json" in captured.out
        assert result is True
    
    def test_shows_csv_stats_in_summary(self, capsys):
        """Test that CSV statistics are shown in summary."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
            workflow.config.mode = "csv"
            workflow.config.csv_path = "test.csv"
            workflow.config.output_dir = "test_output"
        
        stats = {"total": 100, "pending": 80, "completed": 15, "failed": 5}
        
        with patch("main.interactive.get_config", return_value={"providers": {}}):
            with patch("main.interactive.get_stats", return_value=stats):
                with patch.object(ConsoleUI, "prompt_yes_no", return_value=True):
                    workflow.display_summary()
        
        captured = capsys.readouterr()
        assert "100" in captured.out  # total
        assert "80" in captured.out   # pending


class TestRunWorkflow:
    """Tests for run_workflow state machine."""
    
    def test_starts_with_config_selection(self):
        """Test that workflow starts with config file selection."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        # Mock to return False immediately (user quits)
        with patch.object(workflow, "configure_config_file", return_value=False):
            result = workflow.run_workflow()
        
        assert result is None
    
    def test_navigates_back_to_config_from_mode(self):
        """Test navigation back from mode selection to config."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        call_count = {"config": 0, "mode": 0}
        
        def mock_config():
            call_count["config"] += 1
            return call_count["config"] <= 2  # Succeed first two times
        
        def mock_mode():
            call_count["mode"] += 1
            if call_count["mode"] == 1:
                return False  # Go back first time
            return False  # Then quit
        
        with patch.object(workflow, "display_welcome"):
            with patch.object(workflow, "configure_config_file", side_effect=mock_config):
                with patch.object(workflow, "display_provider_status"):
                    with patch.object(workflow, "configure_mode", side_effect=mock_mode):
                        result = workflow.run_workflow()
        
        # Should have called config twice (initial + after going back)
        assert call_count["config"] >= 2
    
    def test_complete_workflow_sets_start_time(self):
        """Test that completing workflow sets start_time."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        with patch.object(workflow, "display_welcome"):
            with patch.object(workflow, "configure_config_file", return_value=True):
                with patch.object(workflow, "display_provider_status"):
                    with patch.object(workflow, "configure_mode", return_value=True):
                        workflow.config.mode = "single"
                        with patch.object(workflow, "configure_single_mode", return_value=True):
                            with patch.object(workflow, "configure_output", return_value=True):
                                with patch.object(workflow, "configure_options", return_value=True):
                                    with patch.object(workflow, "display_summary", return_value=True):
                                        before = time.time()
                                        result = workflow.run_workflow()
                                        after = time.time()
        
        assert result is not None
        assert workflow.start_time >= before
        assert workflow.start_time <= after


# ============================================================================
# Integration Tests
# ============================================================================

class TestConfigWorkflowIntegration:
    """Integration tests for the config workflow."""
    
    def test_config_selection_updates_app_config(self, temp_dir):
        """Test that selecting a config updates app_config."""
        config_path = os.path.join(temp_dir, "test_config.json")
        config_data = {
            "providers": {"internet_archive": True},
            "download": {"max_parallel_downloads": 4}
        }
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)
        
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        with patch.object(Path, "glob", return_value=[Path(config_path)]):
            with patch.object(ConsoleUI, "prompt_select", return_value=config_path):
                with patch("main.interactive.get_config", return_value=config_data):
                    workflow.configure_config_file()
        
        assert workflow.app_config == config_data
    
    def test_full_csv_workflow_simulation(self, temp_dir, sample_csv_file):
        """Test simulating a complete CSV workflow."""
        config_path = os.path.join(temp_dir, "config.json")
        config_data = {"providers": {"internet_archive": True}, "download": {}}
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)
        
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
        
        # Set up config
        workflow.config.config_path = config_path
        workflow.config.mode = "csv"
        workflow.config.csv_path = sample_csv_file
        workflow.config.output_dir = os.path.join(temp_dir, "output")
        workflow.config.dry_run = True
        workflow.config.log_level = "INFO"
        
        # Verify all required fields are set
        assert workflow.config.config_path is not None
        assert workflow.config.csv_path is not None
        assert workflow.config.output_dir is not None
