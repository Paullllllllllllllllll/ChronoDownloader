"""Unit tests for CLI --iiif argument handling."""
import argparse
import pytest
from unittest.mock import patch, MagicMock

from main.downloader import create_cli_parser, _run_direct_iiif_cli


class TestCLIParserIIIFArgs:
    """Tests for --iiif and --name CLI arguments."""
    
    def test_parser_accepts_single_iiif(self):
        """Test that parser accepts a single --iiif URL."""
        parser = create_cli_parser()
        args = parser.parse_args(["--iiif", "https://example.org/manifest.json"])
        assert args.iiif_urls == ["https://example.org/manifest.json"]
    
    def test_parser_accepts_multiple_iiif(self):
        """Test that parser accepts multiple --iiif URLs."""
        parser = create_cli_parser()
        args = parser.parse_args([
            "--iiif", "https://example.org/manifest1.json",
            "--iiif", "https://example.org/manifest2.json",
        ])
        assert args.iiif_urls == [
            "https://example.org/manifest1.json",
            "https://example.org/manifest2.json",
        ]
    
    def test_parser_accepts_name(self):
        """Test that parser accepts --name argument."""
        parser = create_cli_parser()
        args = parser.parse_args([
            "--iiif", "https://example.org/manifest.json",
            "--name", "Taillevent",
        ])
        assert args.name == "Taillevent"
    
    def test_parser_iiif_defaults_to_none(self):
        """Test that iiif_urls is None when not provided."""
        parser = create_cli_parser()
        args = parser.parse_args(["sample.csv"])
        assert args.iiif_urls is None
    
    def test_parser_name_defaults_to_none(self):
        """Test that name is None when not provided."""
        parser = create_cli_parser()
        args = parser.parse_args(["sample.csv"])
        assert args.name is None
    
    def test_iiif_with_dry_run(self):
        """Test --iiif combined with --dry-run."""
        parser = create_cli_parser()
        args = parser.parse_args([
            "--iiif", "https://example.org/manifest.json",
            "--dry-run",
        ])
        assert args.iiif_urls == ["https://example.org/manifest.json"]
        assert args.dry_run is True
    
    def test_iiif_with_output_dir(self):
        """Test --iiif combined with --output_dir."""
        parser = create_cli_parser()
        args = parser.parse_args([
            "--iiif", "https://example.org/manifest.json",
            "--output_dir", "/tmp/downloads",
        ])
        assert args.output_dir == "/tmp/downloads"


class TestRunDirectIIIFCLI:
    """Tests for _run_direct_iiif_cli handler."""
    
    @patch('main.downloader.process_direct_iiif')
    def test_single_url_with_name(self, mock_process):
        """Test processing a single URL with a name stem."""
        mock_process.return_value = {"status": "completed", "item_url": "url", "provider": "Test"}
        
        args = argparse.Namespace(
            iiif_urls=["https://example.org/manifest.json"],
            name="TestWork",
            dry_run=False,
            output_dir="downloads",
        )
        logger = MagicMock()
        config = {}
        
        _run_direct_iiif_cli(args, config, logger)
        
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args
        assert call_kwargs.kwargs["manifest_url"] == "https://example.org/manifest.json"
        assert call_kwargs.kwargs["file_stem"] == "TestWork"
        assert call_kwargs.kwargs["title"] == "TestWork"
        assert call_kwargs.kwargs["entry_id"] == "IIIF_TestWork"
    
    @patch('main.downloader.process_direct_iiif')
    def test_multiple_urls_without_name(self, mock_process):
        """Test processing multiple URLs without a name stem."""
        mock_process.return_value = {"status": "completed", "item_url": "url", "provider": "Test"}
        
        args = argparse.Namespace(
            iiif_urls=["https://example.org/m1.json", "https://example.org/m2.json"],
            name=None,
            dry_run=False,
            output_dir="downloads",
        )
        logger = MagicMock()
        config = {}
        
        _run_direct_iiif_cli(args, config, logger)
        
        assert mock_process.call_count == 2
        
        # First call
        first_call = mock_process.call_args_list[0]
        assert first_call.kwargs["entry_id"] == "IIIF_0001"
        assert first_call.kwargs["title"] is None
        assert first_call.kwargs["file_stem"] is None
        
        # Second call
        second_call = mock_process.call_args_list[1]
        assert second_call.kwargs["entry_id"] == "IIIF_0002"
    
    @patch('main.downloader.process_direct_iiif')
    def test_dry_run_passed_through(self, mock_process):
        """Test that dry_run flag is passed to process_direct_iiif."""
        mock_process.return_value = {"status": "dry_run", "item_url": "url", "provider": "Test"}
        
        args = argparse.Namespace(
            iiif_urls=["https://example.org/manifest.json"],
            name=None,
            dry_run=True,
            output_dir="downloads",
        )
        logger = MagicMock()
        config = {}
        
        _run_direct_iiif_cli(args, config, logger)
        
        call_kwargs = mock_process.call_args
        assert call_kwargs.kwargs["dry_run"] is True
    
    @patch('main.downloader.process_direct_iiif')
    def test_failure_counted(self, mock_process):
        """Test that failed downloads are counted correctly."""
        mock_process.return_value = {
            "status": "failed", "item_url": "url",
            "provider": "Test", "error": "Network error"
        }
        
        args = argparse.Namespace(
            iiif_urls=["https://example.org/manifest.json"],
            name=None,
            dry_run=False,
            output_dir="downloads",
        )
        logger = MagicMock()
        config = {}
        
        _run_direct_iiif_cli(args, config, logger)
        
        # Should log a warning for the failure
        assert any("failed" in str(call).lower() for call in logger.warning.call_args_list)
