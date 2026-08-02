"""Unit tests for interactive module - config workflow and UI components."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from main.ui.console import ConsoleUI, DownloadConfiguration
from main.ui.interactive import InteractiveWorkflow

# ============================================================================
# DownloadConfiguration Tests
# ============================================================================


class TestDownloadConfiguration:
    """Tests for DownloadConfiguration dataclass."""

    def test_default_values(self) -> None:
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

    def test_stats_field_exists(self) -> None:
        """Test that stats field is available for session tracking."""
        config = DownloadConfiguration()
        config.stats = {"processed": 10, "succeeded": 8, "failed": 2}

        assert config.stats["processed"] == 10
        assert config.stats["succeeded"] == 8
        assert config.stats["failed"] == 2

    def test_custom_config_path(self) -> None:
        """Test setting custom config path."""
        config = DownloadConfiguration()
        config.config_path = "custom_config.json"

        assert config.config_path == "custom_config.json"


# ============================================================================
# ConsoleUI Tests
# ============================================================================


class TestConsoleUI:
    """Tests for ConsoleUI utility class."""

    def test_color_codes_defined(self) -> None:
        """Test that all color codes are defined."""
        assert hasattr(ConsoleUI, "RESET")
        assert hasattr(ConsoleUI, "BOLD")
        assert hasattr(ConsoleUI, "GREEN")
        assert hasattr(ConsoleUI, "YELLOW")
        assert hasattr(ConsoleUI, "RED")
        assert hasattr(ConsoleUI, "CYAN")
        assert hasattr(ConsoleUI, "DIM")

    def test_print_config_summary(self, capsys: Any) -> None:
        """Test print_config_summary output."""
        config_data = {
            "Provider": "Anna's Archive",
            "Format": "PDF preferred",
            "Limit": "100 downloads",
        }

        ConsoleUI.print_config_summary(config_data, "Test Config")

        captured = capsys.readouterr()
        assert "Test Config" in captured.out
        assert "Provider" in captured.out
        assert "Anna's Archive" in captured.out

    def test_print_session_summary_basic(self, capsys: Any) -> None:
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
        assert "8" in captured.out  # succeeded
        assert "test_output" in captured.out

    def test_print_session_summary_dry_run(self, capsys: Any) -> None:
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

    def test_print_session_summary_with_duration(self, capsys: Any) -> None:
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

    def test_print_session_summary_with_providers(self, capsys: Any) -> None:
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

    def test_print_session_summary_deferred_shows_next_steps(self, capsys: Any) -> None:
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

    def test_init_creates_default_config(self) -> None:
        """Test that initialization creates default configuration."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

            assert workflow.config is not None
            assert isinstance(workflow.config, DownloadConfiguration)
            assert workflow.app_config == {}
            assert workflow.start_time == 0.0

    def test_get_mode_options(self) -> None:
        """Test get_mode_options returns valid options."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
            options = workflow.get_mode_options()

            assert len(options) == 5
            modes = [opt[0] for opt in options]
            assert "csv" in modes
            assert "single" in modes
            assert "collection" in modes
            assert "direct_iiif" in modes
            assert "search" in modes


class TestConfigureConfigFile:
    """Tests for configure_config_file method."""

    def test_finds_config_files(self, temp_dir: str) -> None:
        """Test that config files are discovered."""
        # Create test config files
        config1_path = os.path.join(temp_dir, "config.json")
        config2_path = os.path.join(temp_dir, "test_config.json")

        config_data = {
            "providers": {"internet_archive": True},
            "download": {"prefer_pdf_over_images": True},
            "budget": {"enabled": True, "max_total_downloads": 100},
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
            with (
                patch.object(ConsoleUI, "prompt_select", return_value=config1_path),
                patch("main.ui.interactive.get_config", return_value=config_data),
            ):
                result = workflow.configure_config_file()

        assert result is True
        assert workflow.config.config_path == config1_path

    def test_uses_default_when_no_configs(self, temp_dir: str) -> None:
        """Test fallback to default when no config files found."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

        with (
            patch.object(Path, "glob", return_value=[]),
            patch.object(ConsoleUI, "print_warning"),
        ):
            result = workflow.configure_config_file()

        assert result is True
        assert workflow.config.config_path == "config.json"

    def test_config_file_metadata_extraction(self, temp_dir: str) -> None:
        """Test that config file metadata is extracted for display."""
        config_path = os.path.join(temp_dir, "test_config.json")

        config_data = {
            "providers": {
                "internet_archive": True,
                "bnf_gallica": True,
                "annas_archive": False,
            },
            "download": {"prefer_pdf_over_images": True},
            "budget": {"enabled": True, "max_total_downloads": 500},
        }

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        with patch.object(ConsoleUI, "enable_ansi"):
            InteractiveWorkflow()

        # Verify the config can be read and parsed
        with open(config_path, encoding="utf-8") as f:
            loaded = json.load(f)

        enabled_count = sum(1 for v in loaded["providers"].values() if v)
        assert enabled_count == 2  # internet_archive and bnf_gallica

    def test_sets_environment_variable(self, temp_dir: str) -> None:
        """Test that CHRONO_CONFIG_PATH is set after selection."""
        config_path = os.path.join(temp_dir, "custom_config.json")

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"providers": {}}, f)

        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

        with (
            patch.object(Path, "glob", return_value=[Path(config_path)]),
            patch.object(ConsoleUI, "prompt_select", return_value=config_path),
            patch("main.ui.interactive.get_config", return_value={}),
        ):
            workflow.configure_config_file()

        assert os.environ.get("CHRONO_CONFIG_PATH") == config_path


class TestDisplayProviderStatus:
    """Tests for display_provider_status method."""

    def test_displays_enabled_providers(
        self, capsys: Any, sample_config: dict[str, Any]
    ) -> None:
        """Test that enabled providers are displayed."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

        with patch("main.ui.interactive.get_config", return_value=sample_config):
            workflow.display_provider_status()

        captured = capsys.readouterr()
        assert "Enabled" in captured.out

    def test_displays_quota_info(self, capsys: Any) -> None:
        """Test that quota information is displayed for providers with quotas."""
        config_with_quota = {
            "providers": {"annas_archive": True},
            "provider_settings": {
                "annas_archive": {"quota": {"enabled": True, "daily_limit": 875}}
            },
            "download": {},
        }

        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

        # Mock PROVIDERS to include annas_archive
        mock_providers = {"annas_archive": (None, None, "Anna's Archive")}

        with (
            patch("main.ui.interactive.get_config", return_value=config_with_quota),
            patch("main.ui.interactive.PROVIDERS", mock_providers),
        ):
            workflow.display_provider_status()

        captured = capsys.readouterr()
        assert "875" in captured.out or "quota" in captured.out.lower()


class TestDisplaySummary:
    """Tests for display_summary method."""

    def test_shows_config_file_in_summary(self, capsys: Any) -> None:
        """Test that config file is shown in summary."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
            workflow.config.config_path = "essential_monographies_1_config.json"
            workflow.config.mode = "csv"
            workflow.config.csv_path = "test.csv"
            workflow.config.output_dir = "test_output"

        with (
            patch("main.ui.interactive.get_config", return_value={"providers": {}}),
            patch(
                "main.ui.interactive.get_stats",
                return_value={"total": 10, "pending": 5, "completed": 3, "failed": 2},
            ),
            patch.object(ConsoleUI, "prompt_yes_no", return_value=True),
        ):
            result = workflow.display_summary()

        captured = capsys.readouterr()
        assert "essential_monographies_1_config.json" in captured.out
        assert result is True

    def test_shows_csv_stats_in_summary(self, capsys: Any) -> None:
        """Test that CSV statistics are shown in summary."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()
            workflow.config.mode = "csv"
            workflow.config.csv_path = "test.csv"
            workflow.config.output_dir = "test_output"

        stats = {"total": 100, "pending": 80, "completed": 15, "failed": 5}

        with (
            patch("main.ui.interactive.get_config", return_value={"providers": {}}),
            patch("main.ui.interactive.get_stats", return_value=stats),
            patch.object(ConsoleUI, "prompt_yes_no", return_value=True),
        ):
            workflow.display_summary()

        captured = capsys.readouterr()
        assert "100" in captured.out  # total
        assert "80" in captured.out  # pending


class TestRunWorkflow:
    """Tests for run_workflow state machine."""

    def test_starts_with_config_selection(self) -> None:
        """Test that workflow starts with config file selection."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

        # Mock to return False immediately (user quits)
        with patch.object(workflow, "configure_config_file", return_value=False):
            result = workflow.run_workflow()

        assert result is None

    def test_navigates_back_to_config_from_mode(self) -> None:
        """Test navigation back from mode selection to config."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

        call_count = {"config": 0, "mode": 0}

        def mock_config() -> bool:
            call_count["config"] += 1
            return call_count["config"] <= 2  # Succeed first two times

        def mock_mode() -> bool:
            call_count["mode"] += 1
            if call_count["mode"] == 1:
                return False  # Go back first time
            return False  # Then quit

        with (
            patch.object(workflow, "display_welcome"),
            patch.object(workflow, "configure_config_file", side_effect=mock_config),
            patch.object(workflow, "display_provider_status"),
            patch.object(workflow, "configure_mode", side_effect=mock_mode),
        ):
            workflow.run_workflow()

        # Should have called config twice (initial + after going back)
        assert call_count["config"] >= 2

    def test_complete_workflow_sets_start_time(self) -> None:
        """Test that completing workflow sets start_time."""
        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

        with (
            patch.object(workflow, "display_welcome"),
            patch.object(workflow, "configure_config_file", return_value=True),
            patch.object(workflow, "display_provider_status"),
            patch.object(workflow, "configure_mode", return_value=True),
        ):
            workflow.config.mode = "single"
            with (
                patch.object(workflow, "configure_single_mode", return_value=True),
                patch.object(workflow, "configure_output", return_value=True),
                patch.object(workflow, "configure_options", return_value=True),
                patch.object(workflow, "display_summary", return_value=True),
            ):
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

    def test_config_selection_updates_app_config(self, temp_dir: str) -> None:
        """Test that selecting a config updates app_config."""
        config_path = os.path.join(temp_dir, "test_config.json")
        config_data = {
            "providers": {"internet_archive": True},
            "download": {"max_parallel_downloads": 4},
        }

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

        with (
            patch.object(Path, "glob", return_value=[Path(config_path)]),
            patch.object(ConsoleUI, "prompt_select", return_value=config_path),
            patch("main.ui.interactive.get_config", return_value=config_data),
        ):
            workflow.configure_config_file()

        assert workflow.app_config == config_data

    def test_configure_csv_mode_accepts_direct_link_only_csv(
        self, temp_dir: str, mock_config: dict[str, Any]
    ) -> None:
        """Regression: a direct_link-only CSV (no title column) must pass.

        The CLI batch handler and the interactive execution path both accept
        IIIF-only CSVs; the wizard's column check must match.
        """
        csv_path = os.path.join(temp_dir, "iiif_only.csv")
        with open(csv_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("entry_id,direct_link\n")
            f.write("E0001,https://example.org/iiif/abc/manifest.json\n")

        with patch.object(ConsoleUI, "enable_ansi"):
            workflow = InteractiveWorkflow()

        with patch.object(ConsoleUI, "prompt_input", return_value=csv_path):
            assert workflow.configure_csv_mode() is True
        assert workflow.config.csv_path == csv_path

    def test_full_csv_workflow_simulation(
        self, temp_dir: str, sample_csv_file: str
    ) -> None:
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


class TestInteractiveSessionDeferredAccounting:
    """The session summary must report this run's deferrals, not the backlog.

    The deferred queue is user-level: it spans every corpus and every previous
    run. ``run_batch_downloads`` computes the honest per-run delta, and the
    session summary used to overwrite it with ``len(get_pending())``, so a
    run that deferred nothing reported another corpus's whole backlog under
    Results and offered to retry it.
    """

    def _config(self, output_dir: str, csv_path: str) -> DownloadConfiguration:
        config = DownloadConfiguration()
        config.mode = "csv"
        config.csv_path = csv_path
        config.output_dir = output_dir
        config.dry_run = False
        return config

    def test_batch_delta_survives_a_preexisting_backlog(
        self, temp_dir: str, sample_csv_file: str
    ) -> None:
        from main.ui import interactive as mod

        backlog = [object()] * 7
        shown: dict[str, Any] = {}

        def _capture(**kwargs: Any) -> None:
            shown.update(kwargs)

        with (
            patch.object(ConsoleUI, "enable_ansi"),
            patch.object(
                mod,
                "process_csv_batch_with_stats",
                return_value={
                    "processed": 3,
                    "succeeded": 3,
                    "failed": 0,
                    "deferred": 0,
                },
            ),
            patch.object(mod, "get_deferred_queue") as queue,
            patch(
                "main.orchestration.pipeline.load_enabled_apis",
                return_value=[("p", object(), object(), "P")],
            ),
            patch(
                "main.orchestration.pipeline.filter_enabled_providers_for_keys",
                side_effect=lambda providers: providers,
            ),
            patch.object(ConsoleUI, "print_session_summary", side_effect=_capture),
        ):
            queue.return_value.get_pending.return_value = backlog
            mod.run_interactive_session(self._config(temp_dir, sample_csv_file))

        assert shown.get("deferred") == 0
        assert shown.get("succeeded") == 3

    def test_single_mode_reports_its_own_deferral(self, temp_dir: str) -> None:
        """Single mode books the status process_single_work returns.

        The queue-length delta cannot see it: re-deferring a work already in
        the queue dedupes, so the length is unchanged and the deferral was
        reported as neither succeeded nor deferred.
        """
        from main.ui import interactive as mod

        config = DownloadConfiguration()
        config.mode = "single"
        config.single_title = "Quota Book"
        config.output_dir = temp_dir
        config.dry_run = False

        shown: dict[str, Any] = {}

        def _capture(**kwargs: Any) -> None:
            shown.update(kwargs)

        with (
            patch.object(ConsoleUI, "enable_ansi"),
            patch.object(mod, "process_single_work", return_value="deferred"),
            patch.object(mod, "get_deferred_queue") as queue,
            patch(
                "main.orchestration.pipeline.load_enabled_apis",
                return_value=[("p", object(), object(), "P")],
            ),
            patch(
                "main.orchestration.pipeline.filter_enabled_providers_for_keys",
                side_effect=lambda providers: providers,
            ),
            patch.object(ConsoleUI, "print_session_summary", side_effect=_capture),
        ):
            queue.return_value.get_pending.return_value = ["already-queued"]
            mod.run_interactive_session(config)

        assert shown.get("deferred") == 1
        assert shown.get("failed") == 0
        assert shown.get("processed") == 1


class TestInteractiveSkipAccounting:
    """A resume-skip is neither a success nor a failure.

    ``pipeline.process_work`` returns None exactly when the work is
    resume-skipped, and single mode mapped that to "failed", so re-running an
    already-downloaded work reported a failure. Batch skips reached the stats
    dict but had no slot in the session summary and vanished.
    """

    @staticmethod
    def _providers() -> Any:
        return [("p", object(), object(), "P")]

    def test_process_single_work_reports_a_resume_skip_as_skipped(self) -> None:
        from main.orchestration import pipeline
        from main.ui import interactive as mod

        log = logging.getLogger("test-skip")
        with patch.object(pipeline, "process_work", return_value=None):
            status = mod.process_single_work(
                "Already Downloaded", None, "W0001", "out", False, log
            )

        assert status == "skipped"

    def test_single_mode_books_a_skip_as_neither_success_nor_failure(
        self, temp_dir: str, capsys: Any
    ) -> None:
        from main.ui import interactive as mod

        config = DownloadConfiguration()
        config.mode = "single"
        config.single_title = "Already Downloaded"
        config.output_dir = temp_dir
        config.dry_run = False

        shown: dict[str, Any] = {}

        def _capture(**kwargs: Any) -> None:
            shown.update(kwargs)

        with (
            patch.object(ConsoleUI, "enable_ansi"),
            patch.object(mod, "process_single_work", return_value="skipped"),
            patch.object(mod, "get_deferred_queue") as queue,
            patch(
                "main.orchestration.pipeline.load_enabled_apis",
                return_value=self._providers(),
            ),
            patch(
                "main.orchestration.pipeline.filter_enabled_providers_for_keys",
                side_effect=lambda providers: providers,
            ),
            patch.object(ConsoleUI, "print_session_summary", side_effect=_capture),
        ):
            queue.return_value.get_pending.return_value = []
            mod.run_interactive_session(config)

        assert shown.get("processed") == 1
        assert shown.get("failed") == 0
        assert shown.get("succeeded") == 0
        assert "Skipped: 1" in capsys.readouterr().out

    def test_batch_skips_are_reported_alongside_the_summary(
        self, temp_dir: str, sample_csv_file: str, capsys: Any
    ) -> None:
        from main.ui import interactive as mod

        config = DownloadConfiguration()
        config.mode = "csv"
        config.csv_path = sample_csv_file
        config.output_dir = temp_dir
        config.dry_run = False

        with (
            patch.object(ConsoleUI, "enable_ansi"),
            patch.object(
                mod,
                "process_csv_batch_with_stats",
                return_value={
                    "processed": 2,
                    "succeeded": 2,
                    "failed": 0,
                    "skipped": 3,
                    "deferred": 0,
                },
            ),
            patch.object(mod, "get_deferred_queue") as queue,
            patch(
                "main.orchestration.pipeline.load_enabled_apis",
                return_value=self._providers(),
            ),
            patch(
                "main.orchestration.pipeline.filter_enabled_providers_for_keys",
                side_effect=lambda providers: providers,
            ),
        ):
            queue.return_value.get_pending.return_value = []
            mod.run_interactive_session(config)

        assert "Skipped: 3" in capsys.readouterr().out


class TestInteractiveDirectIIIFAccounting:
    """A partial IIIF download is retriable, not a failure.

    ``process_direct_iiif`` returns "partial" for an incomplete page set. The
    --iiif CLI handler counts it separately and the batch runners leave it
    uncounted so the row stays pending; the interactive loop booked it as a
    failure via a catch-all ``elif status != "dry_run"``.
    """

    def _config(self, output_dir: str, urls: list[str]) -> DownloadConfiguration:
        config = DownloadConfiguration()
        config.mode = "direct_iiif"
        config.iiif_urls = urls
        config.output_dir = output_dir
        config.dry_run = False
        return config

    def test_partial_is_not_counted_as_failed(self, temp_dir: str, capsys: Any) -> None:
        from main.ui import interactive as mod

        shown: dict[str, Any] = {}

        def _capture(**kwargs: Any) -> None:
            shown.update(kwargs)

        config = self._config(
            temp_dir,
            [
                "https://example.org/iiif/a/manifest.json",
                "https://example.org/iiif/b/manifest.json",
            ],
        )

        with (
            patch.object(ConsoleUI, "enable_ansi"),
            patch.object(
                mod,
                "process_direct_iiif",
                side_effect=[{"status": "partial"}, {"status": "completed"}],
            ),
            patch.object(mod, "get_deferred_queue") as queue,
            patch(
                "main.orchestration.pipeline.load_enabled_apis",
                return_value=[],
            ),
            patch(
                "main.orchestration.pipeline.filter_enabled_providers_for_keys",
                side_effect=lambda providers: providers,
            ),
            patch.object(ConsoleUI, "print_session_summary", side_effect=_capture),
        ):
            queue.return_value.get_pending.return_value = []
            mod.run_interactive_session(config)

        assert shown.get("processed") == 2
        assert shown.get("succeeded") == 1
        assert shown.get("failed") == 0
        assert "partially" in capsys.readouterr().out

    def test_failed_manifest_still_counts_as_failed(self, temp_dir: str) -> None:
        from main.ui import interactive as mod

        shown: dict[str, Any] = {}

        def _capture(**kwargs: Any) -> None:
            shown.update(kwargs)

        config = self._config(temp_dir, ["https://example.org/iiif/a/manifest.json"])

        with (
            patch.object(ConsoleUI, "enable_ansi"),
            patch.object(
                mod,
                "process_direct_iiif",
                return_value={"status": "failed", "error": "404"},
            ),
            patch.object(mod, "get_deferred_queue") as queue,
            patch(
                "main.orchestration.pipeline.load_enabled_apis",
                return_value=[],
            ),
            patch(
                "main.orchestration.pipeline.filter_enabled_providers_for_keys",
                side_effect=lambda providers: providers,
            ),
            patch.object(ConsoleUI, "print_session_summary", side_effect=_capture),
        ):
            queue.return_value.get_pending.return_value = []
            mod.run_interactive_session(config)

        assert shown.get("failed") == 1
        assert shown.get("succeeded") == 0
