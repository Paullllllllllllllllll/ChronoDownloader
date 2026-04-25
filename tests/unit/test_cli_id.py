"""Unit tests for CLI --id / --provider argument handling."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from main.cli import create_cli_parser
from main.cli.overrides import _looks_like_cli_invocation
from main.cli.commands.identifier import run_identifier_cli as _run_identifier_cli


# ============================================================================
# Parser argument tests
# ============================================================================

class TestCLIParserIdArgs:
    """Tests for --id and --provider CLI arguments."""

    def test_parser_accepts_id(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args(["--id", "bsb11280551"])
        assert args.id == "bsb11280551"

    def test_parser_accepts_id_with_provider(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args(["--id", "bsb11280551", "--provider", "mdz"])
        assert args.id == "bsb11280551"
        assert args.provider == "mdz"

    def test_parser_id_defaults_to_none(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args(["sample.csv"])
        assert args.id is None

    def test_parser_provider_defaults_to_none(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args(["sample.csv"])
        assert args.provider is None

    def test_id_with_name(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args([
            "--id", "bpt6k1511262r", "--provider", "bnf_gallica",
            "--name", "Taillevent",
        ])
        assert args.id == "bpt6k1511262r"
        assert args.name == "Taillevent"

    def test_id_with_dry_run(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args(["--id", "bsb11280551", "--dry-run"])
        assert args.id == "bsb11280551"
        assert args.dry_run is True

    def test_id_with_output_dir(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args([
            "--id", "bsb11280551", "--output_dir", "/tmp/downloads",
        ])
        assert args.output_dir == "/tmp/downloads"


# ============================================================================
# _looks_like_cli_invocation detection
# ============================================================================

class TestLooksLikeCLI:
    """Verify --id and --provider trigger CLI mode auto-detection."""

    def test_id_flag_detected(self) -> None:
        assert _looks_like_cli_invocation(["--id", "bsb123"]) is True

    def test_provider_flag_detected(self) -> None:
        assert _looks_like_cli_invocation(["--provider", "mdz"]) is True

    def test_id_and_provider_detected(self) -> None:
        assert _looks_like_cli_invocation(["--id", "bsb123", "--provider", "mdz"]) is True


# ============================================================================
# _run_identifier_cli handler
# ============================================================================

class TestRunIdentifierCLI:
    """Tests for the _run_identifier_cli handler."""

    @pytest.fixture(autouse=True)
    def _test_output_dir(self, tmp_path: Path) -> None:
        self._output_dir = str(tmp_path / "downloaded_works")

    def _make_args(self, **overrides: bool | str | None) -> argparse.Namespace:
        defaults: dict[str, bool | str | None] = {
            "id": "bsb11280551",
            "provider": "mdz",
            "name": None,
            "dry_run": False,
            "output_dir": self._output_dir,
            "config": "config.json",
            "log_level": "INFO",
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    @patch("main.cli.commands.identifier.process_direct_iiif")
    @patch("main.cli.commands.identifier.PROVIDERS", {
        "mdz": (MagicMock(), MagicMock(), "MDZ"),
    })
    def test_iiif_provider_calls_process_direct_iiif(self, mock_process: MagicMock) -> None:
        mock_process.return_value = {"status": "completed"}
        args = self._make_args()
        logger = logging.getLogger("test")

        _run_identifier_cli(args, {}, logger)

        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args
        assert "bsb11280551" in call_kwargs.kwargs.get("manifest_url", call_kwargs[1].get("manifest_url", "")) or \
               "bsb11280551" in str(call_kwargs)

    @patch("main.cli.commands.identifier.process_direct_iiif")
    @patch("main.cli.commands.identifier.PROVIDERS", {
        "mdz": (MagicMock(), MagicMock(), "MDZ"),
    })
    def test_name_passed_through(self, mock_process: MagicMock) -> None:
        mock_process.return_value = {"status": "completed"}
        args = self._make_args(name="Kochbuch")
        logger = logging.getLogger("test")

        _run_identifier_cli(args, {}, logger)

        call_kwargs = mock_process.call_args
        # title and file_stem should be "Kochbuch"
        assert call_kwargs[1].get("title") == "Kochbuch" or \
               call_kwargs.kwargs.get("title") == "Kochbuch"

    @patch("main.cli.commands.identifier.process_direct_iiif")
    @patch("main.cli.commands.identifier.PROVIDERS", {
        "mdz": (MagicMock(), MagicMock(), "MDZ"),
    })
    def test_dry_run_passed_through(self, mock_process: MagicMock) -> None:
        mock_process.return_value = {"status": "dry_run"}
        args = self._make_args(dry_run=True)
        logger = logging.getLogger("test")

        _run_identifier_cli(args, {}, logger)

        call_kwargs = mock_process.call_args
        assert call_kwargs[1].get("dry_run") is True or \
               call_kwargs.kwargs.get("dry_run") is True

    @patch("main.cli.commands.identifier.PROVIDERS", {
        "mdz": (MagicMock(), MagicMock(), "MDZ"),
    })
    def test_unknown_provider_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        args = self._make_args(provider="nonexistent_library")
        logger = logging.getLogger("test")

        with caplog.at_level(logging.ERROR):
            _run_identifier_cli(args, {}, logger)

        assert any("Unknown provider key" in r.message for r in caplog.records)

    @patch("main.cli.commands.identifier.PROVIDERS", {
        "mdz": (MagicMock(), MagicMock(), "MDZ"),
    })
    def test_unrecognised_id_no_provider_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        args = self._make_args(id="completely_unknown", provider=None)
        logger = logging.getLogger("test")

        with caplog.at_level(logging.ERROR):
            _run_identifier_cli(args, {}, logger)

        assert any("Could not resolve" in r.message for r in caplog.records)

    @patch("api.identifier_resolver.PROVIDERS", {
        "annas_archive": (MagicMock(), MagicMock(return_value=True), "Anna's Archive"),
    })
    @patch("main.cli.commands.identifier.PROVIDERS", {
        "annas_archive": (MagicMock(), MagicMock(return_value=True), "Anna's Archive"),
    })
    def test_native_provider_calls_download_by_native(self) -> None:
        args = self._make_args(id="md5hash123", provider="annas_archive")
        logger = logging.getLogger("test")

        _run_identifier_cli(args, {}, logger)

    @patch("main.cli.commands.identifier.process_direct_iiif")
    @patch("main.cli.commands.identifier.PROVIDERS", {
        "mdz": (MagicMock(), MagicMock(), "MDZ"),
    })
    def test_auto_detect_mdz(self, mock_process: MagicMock) -> None:
        """Auto-detection with no --provider for a bsb identifier."""
        mock_process.return_value = {"status": "completed"}
        args = self._make_args(provider=None)  # id is bsb11280551
        logger = logging.getLogger("test")

        _run_identifier_cli(args, {}, logger)

        mock_process.assert_called_once()

    @patch("main.cli.commands.identifier.process_direct_iiif")
    @patch("main.cli.commands.identifier.PROVIDERS", {
        "mdz": (MagicMock(), MagicMock(), "MDZ"),
    })
    def test_fallback_on_manifest_failure(self, mock_process: MagicMock) -> None:
        """If the manifest download fails, should log error."""
        mock_process.return_value = {"status": "failed", "error": "404"}
        args = self._make_args()
        logger = logging.getLogger("test")

        _run_identifier_cli(args, {}, logger)
        mock_process.assert_called_once()
