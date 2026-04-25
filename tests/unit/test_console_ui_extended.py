"""Extended tests for main.console_ui module — console UI utilities."""
from __future__ import annotations

import io
import sys
from unittest.mock import patch

import pytest

from main.ui.console import ConsoleUI, DownloadConfiguration


# ============================================================================
# DownloadConfiguration
# ============================================================================

class TestDownloadConfiguration:
    """Tests for DownloadConfiguration dataclass."""

    def test_default_values(self) -> None:
        config = DownloadConfiguration()
        assert config.mode == "csv"
        assert config.csv_path is None
        assert config.output_dir == "downloaded_works"
        assert config.dry_run is False
        assert config.use_parallel is True
        assert config.max_workers_override is None
        assert config.provider_hierarchy == []
        assert config.selected_works == []
        assert config.stats == {}

    def test_custom_values(self) -> None:
        config = DownloadConfiguration(
            mode="single",
            single_title="Test Book",
            single_creator="Author",
            output_dir="/custom/path",
            dry_run=True,
        )
        assert config.mode == "single"
        assert config.single_title == "Test Book"
        assert config.output_dir == "/custom/path"
        assert config.dry_run is True

    def test_iiif_mode(self) -> None:
        config = DownloadConfiguration(
            mode="direct_iiif",
            iiif_urls=["https://example.org/manifest1", "https://example.org/manifest2"],
            iiif_name="test_collection",
        )
        assert config.mode == "direct_iiif"
        assert len(config.iiif_urls) == 2
        assert config.iiif_name == "test_collection"


# ============================================================================
# ConsoleUI static methods
# ============================================================================

class TestConsoleUIPrintMethods:
    """Tests for ConsoleUI print methods."""

    def test_print_header(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_header("Test Title", "subtitle text")
        output = capsys.readouterr().out
        assert "Test Title" in output
        assert "subtitle text" in output

    def test_print_header_no_subtitle(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_header("Title Only")
        output = capsys.readouterr().out
        assert "Title Only" in output

    def test_print_separator(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_separator("-", 40)
        output = capsys.readouterr().out
        assert "-" * 40 in output

    def test_print_info_with_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_info("Label", "Message text")
        output = capsys.readouterr().out
        assert "Label" in output
        assert "Message text" in output

    def test_print_info_label_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_info("Just a label")
        output = capsys.readouterr().out
        assert "Just a label" in output

    def test_print_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_success("Operation completed")
        output = capsys.readouterr().out
        assert "Operation completed" in output

    def test_print_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_warning("Something went wrong")
        output = capsys.readouterr().out
        assert "Something went wrong" in output

    def test_print_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_error("Fatal error occurred")
        output = capsys.readouterr().out
        assert "Fatal error occurred" in output

    def test_print_config_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_config_summary(
            {"key1": "value1", "key2": "value2"},
            title="Test Config",
        )
        output = capsys.readouterr().out
        assert "Test Config" in output
        assert "key1" in output
        assert "value1" in output

    def test_print_session_summary_basic(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_session_summary(
            processed=10,
            succeeded=8,
            failed=1,
            deferred=1,
            output_dir="/output",
        )
        output = capsys.readouterr().out
        assert "SESSION COMPLETE" in output
        assert "10" in output
        assert "8" in output
        assert "/output" in output

    def test_print_session_summary_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleUI.print_session_summary(
            processed=5,
            succeeded=0,
            failed=0,
            deferred=0,
            output_dir="/output",
            dry_run=True,
        )
        output = capsys.readouterr().out
        assert "DRY RUN" in output

    def test_print_session_summary_with_duration_seconds(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ConsoleUI.print_session_summary(
            processed=5, succeeded=5, failed=0, deferred=0,
            output_dir="/output", duration_seconds=30.5,
        )
        output = capsys.readouterr().out
        assert "30.5 seconds" in output

    def test_print_session_summary_with_duration_minutes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ConsoleUI.print_session_summary(
            processed=5, succeeded=5, failed=0, deferred=0,
            output_dir="/output", duration_seconds=120.0,
        )
        output = capsys.readouterr().out
        assert "minutes" in output

    def test_print_session_summary_with_duration_hours(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ConsoleUI.print_session_summary(
            processed=5, succeeded=5, failed=0, deferred=0,
            output_dir="/output", duration_seconds=7200.0,
        )
        output = capsys.readouterr().out
        assert "hours" in output

    def test_print_session_summary_with_providers(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ConsoleUI.print_session_summary(
            processed=5, succeeded=5, failed=0, deferred=0,
            output_dir="/output",
            providers_used=["Internet Archive", "Gallica"],
        )
        output = capsys.readouterr().out
        assert "Internet Archive" in output
        assert "Gallica" in output

    def test_print_session_summary_with_deferred_shows_next_steps(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ConsoleUI.print_session_summary(
            processed=5, succeeded=3, failed=0, deferred=2,
            output_dir="/output",
        )
        output = capsys.readouterr().out
        assert "Next Steps" in output
        assert "quota" in output.lower()


class TestConsoleUIPromptMethods:
    """Tests for ConsoleUI interactive prompts."""

    def test_prompt_select_valid_choice(self) -> None:
        options = [("csv", "CSV mode"), ("single", "Single work")]
        with patch("builtins.input", return_value="1"):
            result = ConsoleUI.prompt_select("Choose mode:", options)
        assert result == "csv"

    def test_prompt_select_back(self) -> None:
        options = [("csv", "CSV mode")]
        with patch("builtins.input", return_value="b"):
            result = ConsoleUI.prompt_select("Choose:", options)
        assert result is None

    def test_prompt_select_quit(self) -> None:
        options = [("csv", "CSV mode")]
        with patch("builtins.input", return_value="q"):
            with pytest.raises(KeyboardInterrupt):
                ConsoleUI.prompt_select("Choose:", options)

    def test_prompt_select_eof(self) -> None:
        options = [("csv", "CSV mode")]
        with patch("builtins.input", side_effect=EOFError):
            with pytest.raises(KeyboardInterrupt):
                ConsoleUI.prompt_select("Choose:", options)

    def test_prompt_input_with_value(self) -> None:
        with patch("builtins.input", return_value="test_value"):
            result = ConsoleUI.prompt_input("Enter value:")
        assert result == "test_value"

    def test_prompt_input_uses_default(self) -> None:
        with patch("builtins.input", return_value=""):
            result = ConsoleUI.prompt_input("Enter value:", default="default_val")
        assert result == "default_val"

    def test_prompt_input_eof(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            with pytest.raises(KeyboardInterrupt):
                ConsoleUI.prompt_input("Enter value:")

    def test_prompt_yes_no_default_yes(self) -> None:
        with patch("builtins.input", return_value=""):
            assert ConsoleUI.prompt_yes_no("Continue?") is True

    def test_prompt_yes_no_default_no(self) -> None:
        with patch("builtins.input", return_value=""):
            assert ConsoleUI.prompt_yes_no("Continue?", default=False) is False

    def test_prompt_yes_no_explicit_yes(self) -> None:
        with patch("builtins.input", return_value="y"):
            assert ConsoleUI.prompt_yes_no("Continue?") is True

    def test_prompt_yes_no_explicit_no(self) -> None:
        with patch("builtins.input", return_value="n"):
            assert ConsoleUI.prompt_yes_no("Continue?") is False

    def test_prompt_yes_no_eof(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            with pytest.raises(KeyboardInterrupt):
                ConsoleUI.prompt_yes_no("Continue?")


class TestConsoleUIEnableAnsi:
    """Tests for ANSI enabling on Windows."""

    @patch("sys.platform", "win32")
    def test_enable_ansi_on_windows(self) -> None:
        # Should not raise even if ctypes fails
        with patch("builtins.__import__", side_effect=ImportError):
            ConsoleUI.enable_ansi()

    @patch("sys.platform", "linux")
    def test_enable_ansi_noop_on_linux(self) -> None:
        ConsoleUI.enable_ansi()
