"""Unit tests for interactive mode Direct IIIF support."""
import pytest
from unittest.mock import patch, MagicMock, call

from main.console_ui import DownloadConfiguration


class TestDownloadConfigurationIIIFFields:
    """Tests for DownloadConfiguration direct_iiif fields."""
    
    def test_default_iiif_fields(self):
        """Test that IIIF fields have correct defaults."""
        config = DownloadConfiguration()
        assert config.iiif_urls == []
        assert config.iiif_name is None
    
    def test_iiif_fields_settable(self):
        """Test that IIIF fields can be set."""
        config = DownloadConfiguration(
            mode="direct_iiif",
            iiif_urls=["https://example.org/manifest.json"],
            iiif_name="TestWork",
        )
        assert config.mode == "direct_iiif"
        assert config.iiif_urls == ["https://example.org/manifest.json"]
        assert config.iiif_name == "TestWork"
    
    def test_mode_includes_direct_iiif(self):
        """Test that direct_iiif is a valid mode value."""
        config = DownloadConfiguration(mode="direct_iiif")
        assert config.mode == "direct_iiif"


class TestInteractiveWorkflowModeOptions:
    """Tests for mode options including direct_iiif."""
    
    @patch('main.interactive.get_config')
    @patch('main.interactive.get_general_config')
    def test_mode_options_include_direct_iiif(self, mock_general, mock_config):
        """Test that get_mode_options includes direct_iiif."""
        mock_config.return_value = {}
        mock_general.return_value = {}
        
        from main.interactive import InteractiveWorkflow
        workflow = InteractiveWorkflow.__new__(InteractiveWorkflow)
        workflow.config = DownloadConfiguration()
        
        options = workflow.get_mode_options()
        option_keys = [key for key, _label in options]
        assert "direct_iiif" in option_keys
    
    @patch('main.interactive.get_config')
    @patch('main.interactive.get_general_config')
    def test_mode_options_has_four_entries(self, mock_general, mock_config):
        """Test that there are exactly 4 mode options."""
        mock_config.return_value = {}
        mock_general.return_value = {}
        
        from main.interactive import InteractiveWorkflow
        workflow = InteractiveWorkflow.__new__(InteractiveWorkflow)
        workflow.config = DownloadConfiguration()
        
        options = workflow.get_mode_options()
        assert len(options) == 4


class TestConfigureDirectIIIFMode:
    """Tests for configure_direct_iiif_mode method."""
    
    @patch('main.interactive.get_config')
    @patch('main.interactive.get_general_config')
    @patch('main.interactive.ConsoleUI')
    def test_single_valid_url(self, mock_ui_class, mock_general, mock_config):
        """Test adding a single valid IIIF URL."""
        mock_config.return_value = {}
        mock_general.return_value = {}
        
        from main.interactive import InteractiveWorkflow
        workflow = InteractiveWorkflow.__new__(InteractiveWorkflow)
        workflow.config = DownloadConfiguration()
        
        # Simulate user input: one URL, then empty to finish, then empty name
        mock_ui_class.prompt_input.side_effect = [
            "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json",
            "",  # empty to finish URL input
            "",  # empty name (auto-detect)
        ]
        mock_ui_class.DIM = ""
        mock_ui_class.RESET = ""
        
        result = workflow.configure_direct_iiif_mode()
        
        assert result is True
        assert len(workflow.config.iiif_urls) == 1
        assert "gallica.bnf.fr" in workflow.config.iiif_urls[0]
        assert workflow.config.iiif_name is None
    
    @patch('main.interactive.get_config')
    @patch('main.interactive.get_general_config')
    @patch('main.interactive.ConsoleUI')
    def test_url_with_custom_name(self, mock_ui_class, mock_general, mock_config):
        """Test adding a URL with a custom name stem."""
        mock_config.return_value = {}
        mock_general.return_value = {}
        
        from main.interactive import InteractiveWorkflow
        workflow = InteractiveWorkflow.__new__(InteractiveWorkflow)
        workflow.config = DownloadConfiguration()
        
        mock_ui_class.prompt_input.side_effect = [
            "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json",
            "",  # empty to finish
            "Taillevent_Viandier",  # custom name
        ]
        mock_ui_class.DIM = ""
        mock_ui_class.RESET = ""
        
        result = workflow.configure_direct_iiif_mode()
        
        assert result is True
        assert workflow.config.iiif_name == "Taillevent_Viandier"
    
    @patch('main.interactive.get_config')
    @patch('main.interactive.get_general_config')
    @patch('main.interactive.ConsoleUI')
    def test_invalid_url_rejected_then_accepted(self, mock_ui_class, mock_general, mock_config):
        """Test that invalid URLs trigger a warning and user can reject."""
        mock_config.return_value = {}
        mock_general.return_value = {}
        
        from main.interactive import InteractiveWorkflow
        workflow = InteractiveWorkflow.__new__(InteractiveWorkflow)
        workflow.config = DownloadConfiguration()
        
        mock_ui_class.prompt_input.side_effect = [
            "https://not-a-manifest.com/page.html",  # invalid URL
            "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json",  # valid URL
            "",  # empty to finish
            "",  # empty name
        ]
        mock_ui_class.prompt_yes_no.return_value = False  # reject invalid URL
        mock_ui_class.DIM = ""
        mock_ui_class.RESET = ""
        
        result = workflow.configure_direct_iiif_mode()
        
        assert result is True
        assert len(workflow.config.iiif_urls) == 1
        # The invalid URL should not be in the list
        assert "not-a-manifest" not in workflow.config.iiif_urls[0]
