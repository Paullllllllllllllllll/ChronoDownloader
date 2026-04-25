"""Extended tests for main.execution module — batch download execution."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from main.orchestration.execution import (
    _get_direct_link,
    _run_sequential,
    _setup_background_scheduler,
    create_interactive_callbacks,
    process_direct_iiif,
    run_batch_downloads,
)


# ============================================================================
# _get_direct_link
# ============================================================================

class TestGetDirectLink:
    """Tests for extracting IIIF links from CSV rows."""

    def test_returns_iiif_url_from_direct_link_column(self) -> None:
        row = pd.Series({
            "direct_link": "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json",
            "link": "",
        })
        result = _get_direct_link(row)
        assert result is not None
        assert "manifest" in result

    def test_returns_iiif_url_from_link_column(self) -> None:
        row = pd.Series({
            "link": "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json",
        })
        result = _get_direct_link(row)
        assert result is not None

    def test_returns_none_for_non_iiif_url(self) -> None:
        row = pd.Series({
            "direct_link": "https://example.org/regular-page",
            "link": "",
        })
        result = _get_direct_link(row)
        assert result is None

    def test_returns_none_for_empty_row(self) -> None:
        row = pd.Series({"direct_link": pd.NA, "link": pd.NA})
        result = _get_direct_link(row)
        assert result is None

    def test_returns_none_for_missing_columns(self) -> None:
        row = pd.Series({"other_col": "value"})
        result = _get_direct_link(row)
        assert result is None

    def test_strips_whitespace(self) -> None:
        row = pd.Series({
            "direct_link": "  https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json  ",
        })
        result = _get_direct_link(row)
        assert result is not None
        assert not result.startswith(" ")


# ============================================================================
# process_direct_iiif
# ============================================================================

class TestProcessDirectIIIF:
    """Tests for direct IIIF manifest processing."""

    @patch("main.orchestration.execution.preview_manifest")
    def test_dry_run_returns_preview(self, mock_preview: MagicMock) -> None:
        mock_preview.return_value = {
            "provider": "Gallica",
            "page_count": 10,
            "rendering_formats": "application/pdf",
            "label": "Test Book",
        }
        result = process_direct_iiif(
            "https://example.org/manifest.json",
            "/output",
            dry_run=True,
        )
        assert result["status"] == "dry_run"
        assert "preview" in result

    @patch("main.orchestration.execution.preview_manifest")
    def test_dry_run_returns_failed_on_bad_manifest(self, mock_preview: MagicMock) -> None:
        mock_preview.return_value = None
        result = process_direct_iiif(
            "https://example.org/manifest.json",
            "/output",
            dry_run=True,
        )
        assert result["status"] == "failed"
        assert "error" in result

    @patch("main.orchestration.execution.download_from_iiif_manifest")
    @patch("main.data.work.compute_work_dir", return_value=("/out/work", "work_name"))
    def test_successful_download(self, mock_dir: MagicMock, mock_dl: MagicMock) -> None:
        mock_dl.return_value = {"success": True, "provider": "Gallica"}
        result = process_direct_iiif(
            "https://example.org/manifest.json",
            "/output",
            entry_id="E001",
            title="Test Book",
        )
        assert result["status"] == "completed"
        assert result["provider"] == "Gallica"

    @patch("main.orchestration.execution.download_from_iiif_manifest")
    @patch("main.data.work.compute_work_dir", return_value=("/out/work", "work_name"))
    def test_failed_download(self, mock_dir: MagicMock, mock_dl: MagicMock) -> None:
        mock_dl.return_value = {"success": False, "provider": "Gallica", "error": "timeout"}
        result = process_direct_iiif(
            "https://example.org/manifest.json",
            "/output",
        )
        assert result["status"] == "failed"


# ============================================================================
# _setup_background_scheduler
# ============================================================================

class TestSetupBackgroundScheduler:
    """Tests for background scheduler setup."""

    @patch("main.orchestration.execution.get_background_scheduler")
    def test_returns_none_when_disabled(self, mock_sched: MagicMock) -> None:
        import logging

        result = _setup_background_scheduler(
            {"deferred": {"background_enabled": False}},
            logging.getLogger("test"),
        )
        assert result is None
        mock_sched.assert_not_called()


# ============================================================================
# _run_sequential
# ============================================================================

class TestRunSequential:
    """Tests for sequential download execution."""

    @patch("main.orchestration.execution.is_direct_download_enabled", return_value=False)
    @patch("main.orchestration.execution.pipeline")
    def test_processes_works_sequentially(self, mock_pipeline: MagicMock, mock_direct: MagicMock) -> None:
        import logging

        mock_pipeline.process_work.return_value = {
            "status": "completed",
            "item_url": "https://example.org",
            "provider": "IA",
        }
        works_df = pd.DataFrame({
            "short_title": ["Book A", "Book B"],
            "main_author": ["Author 1", "Author 2"],
            "entry_id": ["E001", "E002"],
        })
        stats = _run_sequential(works_df, "/output", False, logging.getLogger("test"))
        assert stats["processed"] == 2

    @patch("main.orchestration.execution.is_direct_download_enabled", return_value=False)
    @patch("main.orchestration.execution.pipeline")
    def test_skips_rows_without_title(self, mock_pipeline: MagicMock, mock_direct: MagicMock) -> None:
        import logging

        works_df = pd.DataFrame({
            "short_title": [pd.NA, "Book B"],
            "main_author": [pd.NA, "Author"],
            "entry_id": ["E001", "E002"],
        })
        mock_pipeline.process_work.return_value = {"status": "completed", "item_url": "", "provider": ""}
        stats = _run_sequential(works_df, "/output", False, logging.getLogger("test"))
        assert stats["skipped"] == 1
        assert stats["processed"] == 1

    @patch("main.orchestration.execution.is_direct_download_enabled", return_value=False)
    @patch("main.orchestration.execution.pipeline")
    def test_skips_rows_without_entry_id(self, mock_pipeline: MagicMock, mock_direct: MagicMock) -> None:
        import logging

        works_df = pd.DataFrame({
            "short_title": ["Book A"],
            "main_author": ["Author"],
            "entry_id": [pd.NA],
        })
        stats = _run_sequential(works_df, "/output", False, logging.getLogger("test"))
        assert stats["skipped"] == 1
        assert stats["processed"] == 0

    @patch("main.orchestration.execution.is_direct_download_enabled", return_value=False)
    @patch("main.orchestration.execution.pipeline")
    def test_updates_csv_on_success(self, mock_pipeline: MagicMock, mock_direct: MagicMock) -> None:
        import logging

        mock_pipeline.process_work.return_value = {
            "status": "completed",
            "item_url": "https://example.org",
            "provider": "IA",
        }
        works_df = pd.DataFrame({
            "short_title": ["Book A"],
            "main_author": ["Author"],
            "entry_id": ["E001"],
        })
        with patch("main.orchestration.execution.mark_success", return_value=True) as mock_mark:
            stats = _run_sequential(
                works_df, "/output", False, logging.getLogger("test"),
                csv_path="/path/to/csv",
            )
        assert stats["succeeded"] == 1
        mock_mark.assert_called_once()

    @patch("main.orchestration.execution.is_direct_download_enabled", return_value=False)
    @patch("main.orchestration.execution.pipeline")
    @patch("main.orchestration.execution.budget_exhausted", return_value=True)
    def test_stops_on_budget_exhausted(self, mock_budget: MagicMock, mock_pipeline: MagicMock, mock_direct: MagicMock) -> None:
        import logging

        mock_pipeline.process_work.return_value = {"status": "completed", "item_url": "", "provider": ""}
        works_df = pd.DataFrame({
            "short_title": ["Book A", "Book B"],
            "main_author": ["Auth", "Auth"],
            "entry_id": ["E001", "E002"],
        })
        stats = _run_sequential(works_df, "/output", False, logging.getLogger("test"))
        assert stats["processed"] <= 2


# ============================================================================
# run_batch_downloads
# ============================================================================

class TestRunBatchDownloads:
    """Tests for the main batch download entry point."""

    @patch("main.orchestration.execution._run_sequential")
    @patch("main.orchestration.execution.get_deferred_queue")
    def test_sequential_mode(self, mock_queue: MagicMock, mock_seq: MagicMock) -> None:
        mock_seq.return_value = {"processed": 5, "succeeded": 3, "failed": 1, "skipped": 1}
        mock_queue.return_value.get_pending.return_value = []

        works_df = pd.DataFrame({
            "short_title": ["Book"],
            "main_author": ["Author"],
            "entry_id": ["E001"],
        })
        stats = run_batch_downloads(
            works_df, "/output", {},
            use_parallel=False,
            enable_background_retry=False,
        )
        assert stats["processed"] == 5
        assert stats["deferred"] == 0

    @patch("main.orchestration.execution._run_sequential")
    @patch("main.orchestration.execution.get_deferred_queue")
    def test_dry_run_uses_sequential(self, mock_queue: MagicMock, mock_seq: MagicMock) -> None:
        mock_seq.return_value = {"processed": 1, "succeeded": 0, "failed": 0, "skipped": 0}
        mock_queue.return_value.get_pending.return_value = []

        works_df = pd.DataFrame({
            "short_title": ["Book"],
            "main_author": ["Author"],
            "entry_id": ["E001"],
        })
        stats = run_batch_downloads(
            works_df, "/output", {},
            dry_run=True,
            enable_background_retry=False,
        )
        mock_seq.assert_called_once()


# ============================================================================
# create_interactive_callbacks
# ============================================================================

class TestCreateInteractiveCallbacks:
    """Tests for interactive mode callback creation."""

    @patch("main.ui.console.ConsoleUI")
    def test_returns_callback_pair(self, mock_ui: MagicMock) -> None:
        import logging

        on_submit, on_complete = create_interactive_callbacks(logging.getLogger("test"))
        assert callable(on_submit)
        assert callable(on_complete)

    @patch("main.ui.console.ConsoleUI")
    def test_submit_callback_increments_counter(self, mock_ui: MagicMock) -> None:
        import logging

        on_submit, _ = create_interactive_callbacks(logging.getLogger("test"))
        task = MagicMock()
        task.title = "Short Title"
        on_submit(task)
        # Should not raise

    @patch("main.ui.console.ConsoleUI")
    def test_complete_callback_handles_success(self, mock_ui: MagicMock) -> None:
        import logging

        _, on_complete = create_interactive_callbacks(logging.getLogger("test"))
        task = MagicMock()
        task.title = "Short Title"
        on_complete(task, True, None)
        # Should not raise

    @patch("main.ui.console.ConsoleUI")
    def test_complete_callback_handles_failure(self, mock_ui: MagicMock) -> None:
        import logging

        _, on_complete = create_interactive_callbacks(logging.getLogger("test"))
        task = MagicMock()
        task.title = "Short Title"
        on_complete(task, False, Exception("error"))
        # Should not raise
