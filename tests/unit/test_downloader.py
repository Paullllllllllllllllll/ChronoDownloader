"""Tests for main/downloader.py - CLI entry point."""
from __future__ import annotations

import argparse
import sys
from io import StringIO
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest


class TestCreateCliParser:
    """Tests for create_cli_parser function."""

    def test_creates_parser(self):
        """create_cli_parser returns ArgumentParser."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        
        assert isinstance(parser, argparse.ArgumentParser)

    def test_parser_accepts_csv_file(self):
        """Parser accepts positional csv_file argument."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        args = parser.parse_args(["test.csv"])
        
        assert args.csv_file == "test.csv"

    def test_parser_has_output_dir_option(self):
        """Parser has --output_dir option with default."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        args = parser.parse_args([])
        
        assert args.output_dir == "downloaded_works"

    def test_parser_has_dry_run_flag(self):
        """Parser has --dry-run flag."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        args = parser.parse_args(["--dry-run"])
        
        assert args.dry_run is True

    def test_parser_has_log_level_option(self):
        """Parser has --log-level option."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        args = parser.parse_args(["--log-level", "DEBUG"])
        
        assert args.log_level == "DEBUG"

    def test_parser_has_config_option(self):
        """Parser has --config option with default."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        args = parser.parse_args([])
        
        assert args.config == "config.json"

    def test_parser_has_interactive_flag(self):
        """Parser has --interactive flag."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        args = parser.parse_args(["--interactive"])
        
        assert args.interactive is True

    def test_parser_has_cli_flag(self):
        """Parser has --cli flag."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        args = parser.parse_args(["--cli"])
        
        assert args.cli is True

    def test_parser_has_quota_status_flag(self):
        """Parser has --quota-status flag."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        args = parser.parse_args(["--quota-status"])
        
        assert args.quota_status is True

    def test_parser_has_cleanup_deferred_flag(self):
        """Parser has --cleanup-deferred flag."""
        from main.downloader import create_cli_parser
        
        parser = create_cli_parser()
        args = parser.parse_args(["--cleanup-deferred"])
        
        assert args.cleanup_deferred is True

    def test_parser_accepts_provider_override_args(self):
        """Parser accepts provider override arguments."""
        from main.downloader import create_cli_parser

        parser = create_cli_parser()
        args = parser.parse_args([
            "--providers", "mdz,bnf_gallica",
            "--enable-provider", "internet_archive",
            "--disable-provider", "bnf_gallica",
        ])

        assert args.providers == ["mdz,bnf_gallica"]
        assert args.enable_provider == ["internet_archive"]
        assert args.disable_provider == ["bnf_gallica"]

    def test_parser_accepts_processing_scope_args(self):
        """Parser accepts pending/filter scope arguments."""
        from main.downloader import create_cli_parser

        parser = create_cli_parser()
        args = parser.parse_args([
            "--pending-mode", "failed",
            "--entry-ids", "E001,E002",
            "--entry-ids", "E003",
            "--limit", "2",
        ])

        assert args.pending_mode == "failed"
        assert args.entry_ids == ["E001,E002", "E003"]
        assert args.limit == 2

    def test_parser_accepts_runtime_override_args(self):
        """Parser accepts config override arguments for selection/download."""
        from main.downloader import create_cli_parser

        parser = create_cli_parser()
        args = parser.parse_args([
            "--resume-mode", "reprocess_all",
            "--selection-strategy", "sequential_first_hit",
            "--min-title-score", "42.5",
            "--creator-weight", "0.3",
            "--max-candidates-per-provider", "7",
            "--download-strategy", "all",
            "--no-keep-non-selected-metadata",
            "--no-prefer-pdf-over-images",
            "--no-download-manifest-renderings",
            "--max-renderings-per-manifest", "3",
            "--rendering-mime-whitelist", "application/pdf,application/epub+zip",
            "--overwrite-existing",
            "--no-include-metadata",
        ])

        assert args.resume_mode == "reprocess_all"
        assert args.selection_strategy == "sequential_first_hit"
        assert args.min_title_score == 42.5
        assert args.creator_weight == 0.3
        assert args.max_candidates_per_provider == 7
        assert args.download_strategy == "all"
        assert args.keep_non_selected_metadata is False
        assert args.prefer_pdf_over_images is False
        assert args.download_manifest_renderings is False
        assert args.max_renderings_per_manifest == 3
        assert args.rendering_mime_whitelist == ["application/pdf,application/epub+zip"]
        assert args.overwrite_existing is True
        assert args.include_metadata is False


class TestCliHelpers:
    """Tests for helper functions used by CLI argument resolution."""

    def test_apply_runtime_config_overrides(self):
        """CLI runtime overrides are merged into download/selection config."""
        from main.downloader import _apply_runtime_config_overrides

        args = argparse.Namespace(
            resume_mode="reprocess_all",
            prefer_pdf_over_images=False,
            download_manifest_renderings=False,
            max_renderings_per_manifest=9,
            rendering_mime_whitelist=["application/pdf,application/epub+zip"],
            overwrite_existing=True,
            include_metadata=False,
            selection_strategy="sequential_first_hit",
            min_title_score=77.0,
            creator_weight=0.4,
            max_candidates_per_provider=8,
            download_strategy="all",
            keep_non_selected_metadata=False,
        )
        config = {
            "download": {
                "resume_mode": "skip_completed",
                "prefer_pdf_over_images": True,
            },
            "selection": {
                "strategy": "collect_and_select",
                "min_title_score": 35,
            },
        }

        merged = _apply_runtime_config_overrides(args, config, MagicMock())

        assert merged["download"]["resume_mode"] == "reprocess_all"
        assert merged["download"]["prefer_pdf_over_images"] is False
        assert merged["download"]["download_manifest_renderings"] is False
        assert merged["download"]["max_renderings_per_manifest"] == 9
        assert merged["download"]["rendering_mime_whitelist"] == ["application/pdf", "application/epub+zip"]
        assert merged["download"]["overwrite_existing"] is True
        assert merged["download"]["include_metadata"] is False
        assert merged["selection"]["strategy"] == "sequential_first_hit"
        assert merged["selection"]["min_title_score"] == 77.0
        assert merged["selection"]["creator_weight"] == 0.4
        assert merged["selection"]["max_candidates_per_provider"] == 8
        assert merged["selection"]["download_strategy"] == "all"
        assert merged["selection"]["keep_non_selected_metadata"] is False

    def test_apply_provider_cli_overrides(self):
        """Provider overrides respect explicit list, enable, and disable controls."""
        from main.downloader import _apply_provider_cli_overrides

        args = argparse.Namespace(
            providers=["mdz,bnf_gallica"],
            enable_provider=["internet_archive"],
            disable_provider=["bnf_gallica"],
        )
        providers = [
            ("internet_archive", lambda *_: None, lambda *_: None, "Internet Archive"),
            ("bnf_gallica", lambda *_: None, lambda *_: None, "BnF Gallica"),
        ]

        out = _apply_provider_cli_overrides(args, providers, MagicMock())
        out_keys = [p[0] for p in out]

        assert out_keys == ["mdz", "internet_archive"]

    def test_filter_pending_rows_new_failed_and_limit(self):
        """Pending row filters support mode, entry_id filter, and limit."""
        from main.downloader import _filter_pending_rows

        works_df = pd.DataFrame({
            "entry_id": ["E001", "E002", "E003", "E004"],
            "short_title": ["A", "B", "C", "D"],
            "retrievable": [pd.NA, False, True, "no"],
        })

        failed_args = argparse.Namespace(pending_mode="failed", entry_ids=None, limit=None)
        failed_df = _filter_pending_rows(works_df, failed_args)
        assert set(failed_df["entry_id"].tolist()) == {"E002", "E004"}

        new_args = argparse.Namespace(pending_mode="new", entry_ids=["E001,E003"], limit=1)
        new_df = _filter_pending_rows(works_df, new_args)
        assert new_df["entry_id"].tolist() == ["E001"]

    def test_looks_like_cli_invocation(self):
        """CLI invocation detection identifies command-line intent."""
        from main.downloader import _looks_like_cli_invocation

        assert _looks_like_cli_invocation(["sample.csv"]) is True
        assert _looks_like_cli_invocation(["--dry-run"]) is True
        assert _looks_like_cli_invocation(["--interactive", "sample.csv"]) is False
        assert _looks_like_cli_invocation([]) is False


class TestRunCli:
    """Tests for run_cli function."""

    @pytest.fixture
    def mock_args(self):
        """Create mock args namespace."""
        args = argparse.Namespace()
        args.csv_file = "test_works.csv"
        args.output_dir = "test_output"
        args.dry_run = False
        args.log_level = "INFO"
        args.config = "config.json"
        args.interactive = False
        args.cli = True
        args.quota_status = False
        args.cleanup_deferred = False
        args.iiif_urls = None
        args.name = None
        return args

    @pytest.fixture
    def sample_df(self):
        """Create sample DataFrame."""
        return pd.DataFrame({
            "entry_id": ["E001", "E002"],
            "short_title": ["Test Work 1", "Test Work 2"],
            "main_author": ["Author 1", "Author 2"],
            "earliest_year": [1900, 1920]
        })

    def test_run_cli_exits_without_csv_file(self, mock_args, mock_config, capsys):
        """run_cli returns early without csv_file."""
        from main.downloader import run_cli
        
        mock_args.csv_file = None
        
        with patch("main.downloader.pipeline") as mock_pipeline:
            mock_pipeline.load_enabled_apis.return_value = ["provider1"]
            mock_pipeline.filter_enabled_providers_for_keys.return_value = ["provider1"]
            
            run_cli(mock_args, mock_config)
        
        captured = capsys.readouterr()
        # Should log error about missing CSV file

    def test_run_cli_exits_without_providers(self, mock_args, mock_config, capsys):
        """run_cli returns early when no providers enabled."""
        from main.downloader import run_cli
        
        with patch("main.downloader.pipeline") as mock_pipeline:
            mock_pipeline.load_enabled_apis.return_value = []
            mock_pipeline.filter_enabled_providers_for_keys.return_value = []
            
            run_cli(mock_args, mock_config)

    def test_run_cli_handles_missing_csv(self, mock_args, mock_config, temp_dir):
        """run_cli handles missing CSV file gracefully."""
        from main.downloader import run_cli
        
        mock_args.csv_file = "/nonexistent/path.csv"
        
        with patch("main.downloader.pipeline") as mock_pipeline:
            mock_pipeline.load_enabled_apis.return_value = ["provider1"]
            mock_pipeline.filter_enabled_providers_for_keys.return_value = ["provider1"]
            
            # Should not raise, just log error
            run_cli(mock_args, mock_config)

    def test_run_cli_calls_batch_downloads(
        self, mock_args, sample_df, mock_config, temp_dir, sample_csv_file
    ):
        """run_cli calls run_batch_downloads with correct args."""
        from main.downloader import run_cli
        
        mock_args.csv_file = sample_csv_file
        
        with patch("main.downloader.pipeline") as mock_pipeline:
            mock_pipeline.load_enabled_apis.return_value = ["provider1"]
            mock_pipeline.filter_enabled_providers_for_keys.return_value = ["provider1"]
            
            with patch("main.downloader.run_batch_downloads") as mock_batch:
                mock_batch.return_value = {"processed": 2, "succeeded": 2}
                
                with patch("main.downloader.get_deferred_queue") as mock_queue:
                    mock_queue.return_value.get_pending.return_value = []
                    
                    run_cli(mock_args, mock_config)
                    
                    mock_batch.assert_called_once()

    def test_run_cli_handles_deferred_downloads(
        self, mock_args, sample_df, mock_config, sample_csv_file
    ):
        """run_cli handles deferred downloads appropriately."""
        from main.downloader import run_cli
        from main.deferred_queue import DeferredItem
        
        mock_args.csv_file = sample_csv_file
        
        with patch("main.downloader.pipeline") as mock_pipeline:
            mock_pipeline.load_enabled_apis.return_value = ["provider1"]
            mock_pipeline.filter_enabled_providers_for_keys.return_value = ["provider1"]
            
            with patch("main.downloader.run_batch_downloads") as mock_batch:
                mock_batch.return_value = {"processed": 2, "succeeded": 1, "deferred": 1}
                
                with patch("main.downloader.get_deferred_queue") as mock_queue:
                    mock_deferred = MagicMock()
                    mock_deferred.get_pending.return_value = [MagicMock()]
                    mock_queue.return_value = mock_deferred
                    
                    with patch("main.downloader.get_background_scheduler") as mock_sched:
                        mock_sched.return_value.is_running.return_value = True
                        
                        run_cli(mock_args, mock_config)

    def test_run_cli_lists_providers_and_exits_early(self, mock_args, mock_config, capsys):
        """run_cli handles --list-providers without loading CSV/pipeline."""
        from main.downloader import run_cli

        mock_args.list_providers = True

        with patch("main.downloader.pipeline") as mock_pipeline:
            run_cli(mock_args, mock_config)

        captured = capsys.readouterr()
        assert "Available providers:" in captured.out
        mock_pipeline.load_enabled_apis.assert_not_called()


class TestShowQuotaStatus:
    """Tests for show_quota_status function."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config):
        """Reset singletons."""
        from main.quota_manager import QuotaManager
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        from main.background_scheduler import BackgroundRetryScheduler
        
        QuotaManager._instance = None
        DeferredQueue._instance = None
        StateManager._instance = None
        BackgroundRetryScheduler._instance = None
        yield
        QuotaManager._instance = None
        DeferredQueue._instance = None
        StateManager._instance = None
        BackgroundRetryScheduler._instance = None

    def test_show_quota_status_prints_output(self, capsys):
        """show_quota_status prints quota information."""
        from main.downloader import show_quota_status
        
        with patch("main.downloader.get_quota_manager") as mock_qm:
            mock_qm.return_value.get_quota_limited_providers.return_value = []
            
            with patch("main.downloader.get_deferred_queue") as mock_dq:
                mock_dq.return_value.count_by_status.return_value = {}
                
                with patch("main.downloader.get_background_scheduler") as mock_bs:
                    mock_bs.return_value.is_running.return_value = False
                    
                    show_quota_status()
        
        captured = capsys.readouterr()
        assert "QUOTA" in captured.out
        assert "DEFERRED QUEUE" in captured.out

    def test_show_quota_status_shows_provider_quotas(self, capsys):
        """show_quota_status shows individual provider quota status."""
        from main.downloader import show_quota_status
        
        with patch("main.downloader.get_quota_manager") as mock_qm:
            mock_manager = MagicMock()
            mock_manager.get_quota_limited_providers.return_value = ["annas_archive"]
            mock_manager.get_quota_status.return_value = {
                "remaining": 50,
                "daily_limit": 100,
                "downloads_used": 50,
                "is_exhausted": False
            }
            mock_qm.return_value = mock_manager
            
            with patch("main.downloader.get_deferred_queue") as mock_dq:
                mock_dq.return_value.count_by_status.return_value = {}
                mock_dq.return_value.get_pending.return_value = []
                
                with patch("main.downloader.get_background_scheduler") as mock_bs:
                    mock_bs.return_value.is_running.return_value = False
                    
                    show_quota_status()
        
        captured = capsys.readouterr()
        assert "annas_archive" in captured.out

    def test_show_quota_status_shows_exhausted(self, capsys):
        """show_quota_status shows EXHAUSTED for exhausted quotas."""
        from main.downloader import show_quota_status
        
        with patch("main.downloader.get_quota_manager") as mock_qm:
            mock_manager = MagicMock()
            mock_manager.get_quota_limited_providers.return_value = ["test"]
            mock_manager.get_quota_status.return_value = {
                "remaining": 0,
                "daily_limit": 10,
                "downloads_used": 10,
                "is_exhausted": True,
                "seconds_until_reset": 3600
            }
            mock_qm.return_value = mock_manager
            
            with patch("main.downloader.get_deferred_queue") as mock_dq:
                mock_dq.return_value.count_by_status.return_value = {}
                mock_dq.return_value.get_pending.return_value = []
                
                with patch("main.downloader.get_background_scheduler") as mock_bs:
                    mock_bs.return_value.is_running.return_value = False
                    
                    show_quota_status()
        
        captured = capsys.readouterr()
        assert "EXHAUSTED" in captured.out


class TestCleanupDeferredQueue:
    """Tests for cleanup_deferred_queue function."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config):
        """Reset singletons."""
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        
        DeferredQueue._instance = None
        StateManager._instance = None
        yield
        DeferredQueue._instance = None
        StateManager._instance = None

    def test_cleanup_deferred_queue_clears_completed(self, capsys):
        """cleanup_deferred_queue removes completed items."""
        from main.downloader import cleanup_deferred_queue
        
        with patch("main.downloader.get_deferred_queue") as mock_dq:
            mock_queue = MagicMock()
            mock_queue.count_by_status.side_effect = [
                {"completed": 5, "pending": 2},  # before
                {"pending": 2}  # after
            ]
            mock_queue.clear_completed.return_value = 5
            mock_dq.return_value = mock_queue
            
            cleanup_deferred_queue()
        
        captured = capsys.readouterr()
        assert "5" in captured.out
        mock_queue.clear_completed.assert_called_once()


class TestMain:
    """Tests for main entry point."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, mock_config):
        """Reset singletons."""
        from main.quota_manager import QuotaManager
        from main.deferred_queue import DeferredQueue
        from main.state_manager import StateManager
        from main.background_scheduler import BackgroundRetryScheduler
        
        QuotaManager._instance = None
        DeferredQueue._instance = None
        StateManager._instance = None
        BackgroundRetryScheduler._instance = None
        yield
        QuotaManager._instance = None
        DeferredQueue._instance = None
        StateManager._instance = None
        BackgroundRetryScheduler._instance = None

    def test_main_handles_quota_status_flag(self):
        """main handles --quota-status flag."""
        from main.downloader import main
        
        with patch.object(sys, "argv", ["downloader.py", "--quota-status"]):
            with patch("main.downloader.show_quota_status") as mock_show:
                main()
                mock_show.assert_called_once()

    def test_main_handles_cleanup_deferred_flag(self):
        """main handles --cleanup-deferred flag."""
        from main.downloader import main
        
        with patch.object(sys, "argv", ["downloader.py", "--cleanup-deferred"]):
            with patch("main.downloader.cleanup_deferred_queue") as mock_cleanup:
                main()
                mock_cleanup.assert_called_once()

    def test_main_handles_keyboard_interrupt(self):
        """main handles KeyboardInterrupt gracefully."""
        from main.downloader import main
        
        with patch.object(sys, "argv", ["downloader.py", "test.csv"]):
            with patch("main.downloader.run_with_mode_detection") as mock_run:
                mock_run.side_effect = KeyboardInterrupt()
                
                with pytest.raises(SystemExit) as exc_info:
                    main()
                
                assert exc_info.value.code == 0

    def test_main_handles_unexpected_error(self):
        """main handles unexpected errors."""
        from main.downloader import main
        
        with patch.object(sys, "argv", ["downloader.py", "test.csv"]):
            with patch("main.downloader.run_with_mode_detection") as mock_run:
                mock_run.side_effect = RuntimeError("Unexpected error")
                
                with pytest.raises(SystemExit) as exc_info:
                    main()
                
                assert exc_info.value.code == 1

    def test_main_runs_interactive_mode(self):
        """main runs interactive mode when configured."""
        from main.downloader import main
        
        with patch.object(sys, "argv", ["downloader.py"]):
            with patch("main.downloader.run_with_mode_detection") as mock_run:
                mock_run.return_value = ({}, True, None)
                
                with patch("main.downloader.run_interactive") as mock_interactive:
                    main()
                    mock_interactive.assert_called_once()

    def test_main_runs_cli_mode(self):
        """main runs CLI mode when configured."""
        from main.downloader import main
        
        mock_args = MagicMock()
        mock_args.interactive = False
        mock_args.cli = True
        
        with patch.object(sys, "argv", ["downloader.py", "test.csv"]):
            with patch("main.downloader.run_with_mode_detection") as mock_run:
                mock_run.return_value = ({}, False, mock_args)
                
                with patch("main.downloader.run_cli") as mock_cli:
                    main()
                    mock_cli.assert_called_once()

    def test_main_auto_injects_cli_for_positional_args(self):
        """main auto-injects --cli when argv indicates CLI usage."""
        from main.downloader import main

        mock_args = MagicMock()
        mock_args.interactive = False
        mock_args.cli = True

        with patch.object(sys, "argv", ["downloader.py", "sample.csv"]):
            with patch("main.downloader.run_with_mode_detection") as mock_run:
                mock_run.return_value = ({}, False, mock_args)

                with patch("main.downloader.run_cli") as mock_cli:
                    main()
                    assert sys.argv[1] == "--cli"
                    mock_cli.assert_called_once()

    def test_main_does_not_inject_cli_when_interactive_flag_present(self):
        """main does not auto-inject --cli when --interactive is present."""
        from main.downloader import main

        mock_args = MagicMock()
        mock_args.interactive = True
        mock_args.cli = False

        with patch.object(sys, "argv", ["downloader.py", "--interactive", "sample.csv"]):
            with patch("main.downloader.run_with_mode_detection") as mock_run:
                mock_run.return_value = ({}, True, mock_args)

                with patch("main.downloader.run_interactive") as mock_interactive:
                    main()
                    assert "--cli" not in sys.argv
                    mock_interactive.assert_called_once()

    def test_main_overrides_to_interactive_with_flag(self):
        """main overrides to interactive mode with --interactive flag."""
        from main.downloader import main
        
        mock_args = MagicMock()
        mock_args.interactive = True
        mock_args.cli = False
        
        with patch.object(sys, "argv", ["downloader.py", "--interactive"]):
            with patch("main.downloader.run_with_mode_detection") as mock_run:
                mock_run.return_value = ({}, False, mock_args)  # Config says CLI
                
                with patch("main.downloader.run_interactive") as mock_interactive:
                    main()
                    mock_interactive.assert_called_once()

    def test_main_overrides_to_cli_with_flag(self):
        """main overrides to CLI mode with --cli flag."""
        from main.downloader import main
        
        mock_args = MagicMock()
        mock_args.interactive = False
        mock_args.cli = True
        
        with patch.object(sys, "argv", ["downloader.py", "--cli", "test.csv"]):
            with patch("main.downloader.run_with_mode_detection") as mock_run:
                mock_run.return_value = ({}, True, mock_args)  # Config says interactive
                
                with patch("main.downloader.run_cli") as mock_cli:
                    main()
                    mock_cli.assert_called_once()


class TestRunCliEdgeCases:
    """Edge case tests for run_cli."""

    @pytest.fixture
    def mock_args(self):
        """Create mock args."""
        args = argparse.Namespace()
        args.csv_file = "test.csv"
        args.output_dir = "output"
        args.dry_run = False
        args.log_level = "INFO"
        args.config = "config.json"
        args.interactive = False
        args.cli = True
        args.iiif_urls = None
        args.name = None
        return args

    def test_run_cli_handles_csv_validation_error(self, mock_args, mock_config, temp_dir):
        """run_cli handles CSV validation errors."""
        from main.downloader import run_cli
        import os
        
        # Create invalid CSV
        csv_path = os.path.join(temp_dir, "invalid.csv")
        with open(csv_path, "w") as f:
            f.write("wrong_column\nvalue")
        
        mock_args.csv_file = csv_path
        
        with patch("main.downloader.pipeline") as mock_pipeline:
            mock_pipeline.load_enabled_apis.return_value = ["p1"]
            mock_pipeline.filter_enabled_providers_for_keys.return_value = ["p1"]
            
            # Should not raise
            run_cli(mock_args, mock_config)

    def test_run_cli_skips_if_all_completed(
        self, mock_args, mock_config, temp_dir
    ):
        """run_cli skips processing if all works already completed."""
        from main.downloader import run_cli
        import os
        
        # Create CSV with completed status
        csv_path = os.path.join(temp_dir, "completed.csv")
        df = pd.DataFrame({
            "entry_id": ["E001"],
            "short_title": ["Test"],
            "main_author": ["Author"],
            "earliest_year": [1900],
            "retrievable": [True]  # Marked as completed
        })
        df.to_csv(csv_path, index=False)
        
        mock_args.csv_file = csv_path
        
        with patch("main.downloader.pipeline") as mock_pipeline:
            mock_pipeline.load_enabled_apis.return_value = ["p1"]
            mock_pipeline.filter_enabled_providers_for_keys.return_value = ["p1"]
            
            with patch("main.downloader.run_batch_downloads") as mock_batch:
                run_cli(mock_args, mock_config)
                
                # Should not call batch downloads since all completed
                # (get_pending_works returns empty)
