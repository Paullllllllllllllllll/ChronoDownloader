"""Unit tests for mode_selector module."""
from __future__ import annotations

import argparse
import os
from unittest.mock import MagicMock, patch

import pytest

from main.mode_selector import (
    _detect_mode_and_parse_args,
    run_with_mode_detection,
    get_general_config,
)


class TestDetectModeAndParseArgs:
    """Tests for _detect_mode_and_parse_args function."""
    
    def test_interactive_mode_from_config(self, sample_config):
        """Test interactive mode detection from config."""
        sample_config["general"]["interactive_mode"] = True
        
        def parser_factory():
            parser = argparse.ArgumentParser()
            parser.add_argument("csv_file")
            return parser
        
        with patch("main.mode_selector.get_config", return_value=sample_config):
            config, interactive, args = _detect_mode_and_parse_args(
                parser_factory, "test_script"
            )
            
            assert interactive is True
            assert args is None
            assert config == sample_config
    
    def test_cli_mode_from_config(self, sample_config):
        """Test CLI mode detection from config."""
        sample_config["general"]["interactive_mode"] = False
        
        def parser_factory():
            parser = argparse.ArgumentParser()
            parser.add_argument("csv_file")
            return parser
        
        with patch("main.mode_selector.get_config", return_value=sample_config):
            with patch("sys.argv", ["script.py", "test.csv"]):
                config, interactive, args = _detect_mode_and_parse_args(
                    parser_factory, "test_script"
                )
                
                assert interactive is False
                assert args is not None
                assert args.csv_file == "test.csv"
    
    def test_force_cli_with_flag(self, sample_config):
        """Test forcing CLI mode with --cli flag."""
        sample_config["general"]["interactive_mode"] = True
        
        def parser_factory():
            parser = argparse.ArgumentParser()
            parser.add_argument("csv_file")
            parser.add_argument("--cli", action="store_true")
            return parser
        
        with patch("main.mode_selector.get_config", return_value=sample_config):
            with patch("sys.argv", ["script.py", "--cli", "test.csv"]):
                config, interactive, args = _detect_mode_and_parse_args(
                    parser_factory, "test_script"
                )
                
                assert interactive is False
                assert args is not None
                assert args.csv_file == "test.csv"
    
    def test_config_path_override(self, sample_config, temp_dir):
        """Test config path override."""
        config_path = os.path.join(temp_dir, "custom_config.json")
        
        def parser_factory():
            return argparse.ArgumentParser()
        
        with patch("main.mode_selector.get_config", return_value=sample_config):
            with patch("sys.argv", ["script.py"]):
                _detect_mode_and_parse_args(
                    parser_factory, "test_script", config_path=config_path
                )
                
                assert os.environ.get("CHRONO_CONFIG_PATH") == config_path
    
    def test_config_load_failure(self):
        """Test handling of config load failure."""
        def parser_factory():
            return argparse.ArgumentParser()
        
        with patch("main.mode_selector.get_config", side_effect=Exception("Config error")):
            with pytest.raises(SystemExit):
                _detect_mode_and_parse_args(parser_factory, "test_script")
    
    def test_default_interactive_when_missing(self, sample_config):
        """Test default to interactive when general section missing."""
        sample_config.pop("general", None)
        
        def parser_factory():
            return argparse.ArgumentParser()
        
        with patch("main.mode_selector.get_config", return_value=sample_config):
            config, interactive, args = _detect_mode_and_parse_args(
                parser_factory, "test_script"
            )
            
            assert interactive is True


class TestRunWithModeDetection:
    """Tests for run_with_mode_detection function."""
    
    def test_returns_mode_detection_results(self, sample_config):
        """Test that function returns mode detection results."""
        sample_config["general"]["interactive_mode"] = True
        
        def parser_factory():
            return argparse.ArgumentParser()
        
        def interactive_handler():
            pass
        
        def cli_handler(args, config):
            pass
        
        with patch("main.mode_selector.get_config", return_value=sample_config):
            config, interactive, args = run_with_mode_detection(
                interactive_handler,
                cli_handler,
                parser_factory,
                "test_script"
            )
            
            assert config == sample_config
            assert interactive is True
            assert args is None


class TestGetGeneralConfig:
    """Tests for get_general_config function."""
    
    def test_returns_general_section(self, sample_config):
        """Test returns general section from config."""
        with patch("main.mode_selector.get_config", return_value=sample_config):
            gen = get_general_config()
            
            assert gen["interactive_mode"] == sample_config["general"]["interactive_mode"]
            assert gen["default_output_dir"] == sample_config["general"]["default_output_dir"]
    
    def test_applies_defaults(self):
        """Test applies defaults for missing values."""
        config = {"general": {}}
        
        with patch("main.mode_selector.get_config", return_value=config):
            gen = get_general_config()
            
            assert gen["interactive_mode"] is True
            assert gen["default_output_dir"] == "downloaded_works"
            assert gen["default_csv_path"] == "sample_works.csv"
    
    def test_handles_missing_general_section(self):
        """Test handles missing general section."""
        config = {}
        
        with patch("main.mode_selector.get_config", return_value=config):
            gen = get_general_config()
            
            assert gen["interactive_mode"] is True
            assert "default_output_dir" in gen
    
    def test_handles_none_general_section(self):
        """Test handles None general section."""
        config = {"general": None}
        
        with patch("main.mode_selector.get_config", return_value=config):
            gen = get_general_config()
            
            assert gen["interactive_mode"] is True
            assert "default_output_dir" in gen
