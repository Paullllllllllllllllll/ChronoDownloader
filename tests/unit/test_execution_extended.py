"""Extended tests for main.execution module — batch download execution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from main.orchestration.execution import (
    _get_direct_link,
    _run_eager_deferred_retry,
    _run_sequential,
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
        row = pd.Series(
            {
                "direct_link": "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json",
                "link": "",
            }
        )
        result = _get_direct_link(row)
        assert result is not None
        assert "manifest" in result

    def test_returns_iiif_url_from_link_column(self) -> None:
        row = pd.Series(
            {
                "link": "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json",
            }
        )
        result = _get_direct_link(row)
        assert result is not None

    def test_returns_none_for_non_iiif_url(self) -> None:
        row = pd.Series(
            {
                "direct_link": "https://example.org/regular-page",
                "link": "",
            }
        )
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
        row = pd.Series(
            {
                "direct_link": (
                    "  https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json  "
                ),
            }
        )
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
    def test_dry_run_returns_failed_on_bad_manifest(
        self, mock_preview: MagicMock
    ) -> None:
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
        mock_dl.return_value = {
            "success": False,
            "provider": "Gallica",
            "error": "timeout",
        }
        result = process_direct_iiif(
            "https://example.org/manifest.json",
            "/output",
        )
        assert result["status"] == "failed"


# ============================================================================
# _run_eager_deferred_retry
# ============================================================================


class TestEagerDeferredRetry:
    """Tests for the synchronous eager deferred-retry at run start."""

    @patch("main.orchestration.execution.get_background_scheduler")
    def test_noop_when_disabled(self, mock_sched: MagicMock) -> None:
        import logging

        _run_eager_deferred_retry(
            {"deferred": {"background_enabled": False}},
            logging.getLogger("test"),
            None,
        )
        mock_sched.assert_not_called()

    @patch("main.orchestration.execution.get_background_scheduler")
    def test_invokes_retry_ready_now_when_enabled(self, mock_sched: MagicMock) -> None:
        import logging

        scheduler = MagicMock()
        scheduler.retry_ready_now.return_value = (
            {"attempted": 1, "succeeded": 1, "failed": 0},
            set(),
        )
        mock_sched.return_value = scheduler

        _run_eager_deferred_retry(
            {"deferred": {"background_enabled": True}},
            logging.getLogger("test"),
            "works.csv",
        )
        scheduler.retry_ready_now.assert_called_once_with(
            csv_path="works.csv", csv_entry_titles=None
        )


# ============================================================================
# _run_sequential
# ============================================================================


class TestRunSequential:
    """Tests for sequential download execution."""

    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.pipeline")
    def test_processes_works_sequentially(
        self, mock_pipeline: MagicMock, mock_direct: MagicMock
    ) -> None:
        import logging

        mock_pipeline.process_work.return_value = {
            "status": "completed",
            "item_url": "https://example.org",
            "provider": "IA",
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Book A", "Book B"],
                "main_author": ["Author 1", "Author 2"],
                "entry_id": ["E001", "E002"],
            }
        )
        stats = _run_sequential(works_df, "/output", False, logging.getLogger("test"))
        assert stats["processed"] == 2

    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.pipeline")
    def test_skips_rows_without_title(
        self, mock_pipeline: MagicMock, mock_direct: MagicMock
    ) -> None:
        import logging

        works_df = pd.DataFrame(
            {
                "short_title": [pd.NA, "Book B"],
                "main_author": [pd.NA, "Author"],
                "entry_id": ["E001", "E002"],
            }
        )
        mock_pipeline.process_work.return_value = {
            "status": "completed",
            "item_url": "",
            "provider": "",
        }
        stats = _run_sequential(works_df, "/output", False, logging.getLogger("test"))
        assert stats["skipped"] == 1
        assert stats["processed"] == 1

    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.pipeline")
    def test_skips_rows_without_entry_id(
        self, mock_pipeline: MagicMock, mock_direct: MagicMock
    ) -> None:
        import logging

        works_df = pd.DataFrame(
            {
                "short_title": ["Book A"],
                "main_author": ["Author"],
                "entry_id": [pd.NA],
            }
        )
        stats = _run_sequential(works_df, "/output", False, logging.getLogger("test"))
        assert stats["skipped"] == 1
        assert stats["processed"] == 0

    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.pipeline")
    def test_updates_csv_on_success(
        self, mock_pipeline: MagicMock, mock_direct: MagicMock
    ) -> None:
        import logging

        mock_pipeline.process_work.return_value = {
            "status": "completed",
            "item_url": "https://example.org",
            "provider": "IA",
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Book A"],
                "main_author": ["Author"],
                "entry_id": ["E001"],
            }
        )
        with patch(
            "main.orchestration.execution.mark_success", return_value=True
        ) as mock_mark:
            stats = _run_sequential(
                works_df,
                "/output",
                False,
                logging.getLogger("test"),
                csv_path="/path/to/csv",
            )
        assert stats["succeeded"] == 1
        mock_mark.assert_called_once()

    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.pipeline")
    def test_marks_deferred_in_csv(
        self, mock_pipeline: MagicMock, mock_direct: MagicMock
    ) -> None:
        """BUG-3: a quota-deferred work is marked deferred (not left silent)."""
        import logging

        mock_pipeline.process_work.return_value = {
            "status": "deferred",
            "item_url": "",
            "provider": "IA",
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Book A"],
                "main_author": ["Author"],
                "entry_id": ["E001"],
            }
        )
        with (
            patch(
                "main.orchestration.execution.mark_deferred", return_value=True
            ) as mock_def,
            patch("main.orchestration.execution.mark_success") as mock_succ,
            patch("main.orchestration.execution.mark_failed") as mock_fail,
        ):
            _run_sequential(
                works_df,
                "/output",
                False,
                logging.getLogger("test"),
                csv_path="/path/to/csv",
            )
        mock_def.assert_called_once()
        mock_succ.assert_not_called()
        mock_fail.assert_not_called()

    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.pipeline")
    @patch("main.orchestration.execution.budget_exhausted", return_value=True)
    def test_stops_on_budget_exhausted(
        self, mock_budget: MagicMock, mock_pipeline: MagicMock, mock_direct: MagicMock
    ) -> None:
        import logging

        mock_pipeline.process_work.return_value = {
            "status": "completed",
            "item_url": "",
            "provider": "",
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Book A", "Book B"],
                "main_author": ["Auth", "Auth"],
                "entry_id": ["E001", "E002"],
            }
        )
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
        mock_seq.return_value = {
            "processed": 5,
            "succeeded": 3,
            "failed": 1,
            "skipped": 1,
        }
        mock_queue.return_value.get_pending.return_value = []

        works_df = pd.DataFrame(
            {
                "short_title": ["Book"],
                "main_author": ["Author"],
                "entry_id": ["E001"],
            }
        )
        stats = run_batch_downloads(
            works_df,
            "/output",
            {},
            use_parallel=False,
            enable_background_retry=False,
        )
        assert stats["processed"] == 5
        assert stats["deferred"] == 0

    @patch("main.orchestration.execution._run_sequential")
    @patch("main.orchestration.execution.get_deferred_queue")
    def test_deferred_reports_this_runs_delta_not_the_backlog(
        self, mock_queue: MagicMock, mock_seq: MagicMock
    ) -> None:
        """get_pending() spans the whole user-level queue, so reporting it
        verbatim attributed another corpus's backlog to this batch."""
        mock_seq.return_value = {
            "processed": 1,
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
        }
        # Same backlog before and after: this run deferred nothing.
        mock_queue.return_value.get_pending.return_value = ["old1", "old2", "old3"]

        works_df = pd.DataFrame(
            {
                "short_title": ["Book"],
                "main_author": ["Author"],
                "entry_id": ["E001"],
            }
        )
        stats = run_batch_downloads(
            works_df,
            "/output",
            {},
            use_parallel=False,
            enable_background_retry=False,
        )
        assert stats["deferred"] == 0

    @patch("main.orchestration.execution._run_sequential")
    @patch("main.orchestration.execution.get_deferred_queue")
    def test_dry_run_uses_sequential(
        self, mock_queue: MagicMock, mock_seq: MagicMock
    ) -> None:
        mock_seq.return_value = {
            "processed": 1,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        }
        mock_queue.return_value.get_pending.return_value = []

        works_df = pd.DataFrame(
            {
                "short_title": ["Book"],
                "main_author": ["Author"],
                "entry_id": ["E001"],
            }
        )
        run_batch_downloads(
            works_df,
            "/output",
            {},
            dry_run=True,
            enable_background_retry=False,
        )
        mock_seq.assert_called_once()

    @patch("main.orchestration.execution._run_eager_deferred_retry")
    @patch("main.orchestration.execution._run_sequential")
    @patch("main.orchestration.execution.get_deferred_queue")
    def test_eager_retry_completed_row_not_processed(
        self,
        mock_queue: MagicMock,
        mock_seq: MagicMock,
        mock_eager: MagicMock,
    ) -> None:
        # The eager retry reports E001 as completed; that row must be dropped
        # before the batch loop runs so it is not re-downloaded/re-deferred.
        mock_eager.return_value = {"E001"}
        mock_seq.return_value = {
            "processed": 1,
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
        }
        mock_queue.return_value.get_pending.return_value = []

        works_df = pd.DataFrame(
            {
                "short_title": ["Book One", "Book Two"],
                "main_author": ["Author One", "Author Two"],
                "entry_id": ["E001", "E002"],
            }
        )
        run_batch_downloads(
            works_df,
            "/output",
            {},
            use_parallel=False,
            enable_background_retry=True,
        )

        mock_seq.assert_called_once()
        passed_df = mock_seq.call_args[0][0]
        assert list(passed_df["entry_id"]) == ["E002"]


# ============================================================================
# _run_parallel — direct IIIF stats folding
# ============================================================================


class TestRunParallelDirectIIIF:
    """Tests that synchronous direct-IIIF outcomes reach the batch stats."""

    @patch("main.orchestration.execution.process_direct_iiif")
    @patch("main.orchestration.execution.is_direct_download_enabled", return_value=True)
    @patch("main.orchestration.execution.get_parallel_download_config")
    def test_direct_iiif_success_counted_in_stats(
        self,
        mock_cfg: MagicMock,
        mock_direct_enabled: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        """A completed direct-IIIF row (handled outside the scheduler) must be
        folded into the returned ``succeeded`` stat rather than lost."""
        import logging

        from main.orchestration.execution import _run_parallel

        mock_cfg.return_value = {
            "max_parallel_downloads": 2,
            "provider_concurrency": {},
            "worker_timeout_s": 0,
        }
        mock_process.return_value = {
            "status": "completed",
            "provider": "Gallica",
            "item_url": "https://example.org/item",
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Book A"],
                "main_author": ["Author"],
                "entry_id": ["E001"],
                "direct_link": [
                    "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json"
                ],
            }
        )
        stats = _run_parallel(
            works_df,
            "/output",
            {},
            None,
            logging.getLogger("test"),
        )
        assert stats["succeeded"] == 1
        assert stats["failed"] == 0
        assert stats["processed"] == 1
        mock_process.assert_called_once()


class TestRunParallelNoMatchStats:
    """A genuine no-match must count as processed+failed, as it does
    sequentially: the CLI derives its exit code from ``failed``, so an
    all-no-match batch used to exit 0 in parallel mode and 1 sequentially."""

    @patch("main.orchestration.execution.pipeline.search_and_select", return_value=None)
    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.get_parallel_download_config")
    def test_no_match_rows_counted_as_failed(
        self,
        mock_cfg: MagicMock,
        mock_direct_enabled: MagicMock,
        mock_search: MagicMock,
    ) -> None:
        import logging

        from main.orchestration.execution import _run_parallel

        mock_cfg.return_value = {
            "max_parallel_downloads": 2,
            "provider_concurrency": {},
            "worker_timeout_s": 0,
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Book A", "Book B"],
                "main_author": ["Author A", "Author B"],
                "entry_id": ["E001", "E002"],
            }
        )
        stats = _run_parallel(
            works_df,
            "/output",
            {},
            None,
            logging.getLogger("test"),
        )
        assert stats["failed"] == 2
        assert stats["processed"] == 2
        assert stats["succeeded"] == 0

    @patch("main.data.work.check_work_status", return_value=(True, "status=completed"))
    @patch("main.orchestration.execution.pipeline.search_and_select", return_value=None)
    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.get_parallel_download_config")
    def test_resume_skipped_rows_are_not_failures(
        self,
        mock_cfg: MagicMock,
        mock_direct_enabled: MagicMock,
        mock_search: MagicMock,
        mock_status: MagicMock,
    ) -> None:
        """search_and_select returns None for a resume-skip as well as for a
        genuine no-match. Counting both as failures made a re-run of an
        already-downloaded corpus exit 1."""
        import logging

        from main.orchestration.execution import _run_parallel

        mock_cfg.return_value = {
            "max_parallel_downloads": 2,
            "provider_concurrency": {},
            "worker_timeout_s": 0,
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Book A", "Book B"],
                "main_author": ["Author A", "Author B"],
                "entry_id": ["E001", "E002"],
            }
        )
        stats = _run_parallel(
            works_df,
            "/output",
            {},
            None,
            logging.getLogger("test"),
        )
        assert stats["failed"] == 0
        assert stats["processed"] == 2


class TestParallelDeferredNotCountedAsFailure:
    """A quota deferral is documented as explicitly not a failure, and the CLI
    derives its exit code from ``failed``."""

    @patch(
        "main.orchestration.execution.is_direct_download_enabled", return_value=False
    )
    @patch("main.orchestration.execution.get_parallel_download_config")
    def test_deferred_task_nets_out_of_failed(
        self, mock_cfg: MagicMock, mock_direct_enabled: MagicMock
    ) -> None:
        import logging

        from main.orchestration.execution import _run_parallel
        from main.orchestration.scheduler import DownloadTask

        mock_cfg.return_value = {
            "max_parallel_downloads": 2,
            "provider_concurrency": {},
            "worker_timeout_s": 0,
        }
        works_df = pd.DataFrame(
            {
                "short_title": ["Book A"],
                "main_author": ["Author A"],
                "entry_id": ["E001"],
            }
        )

        from api.model import SearchResult

        task = DownloadTask(
            work_id="w1",
            entry_id="E001",
            title="Book A",
            creator="Author A",
            work_dir="/output/w1",
            work_stem="book_a",
            selected_result=SearchResult(provider="P", title="Book A"),
            provider_key="annas_archive",
            provider_tuple=(
                "annas_archive",
                lambda *a, **k: [],
                lambda *a, **k: False,
                "Anna's Archive",
            ),
            work_json_path="/output/w1/work.json",
        )

        def _defer(*_args: object, **_kwargs: object) -> bool:
            task.status = "deferred"
            return False

        with (
            patch(
                "main.orchestration.execution.pipeline.search_and_select",
                return_value=task,
            ),
            patch(
                "main.orchestration.execution.pipeline.execute_download",
                side_effect=_defer,
            ),
        ):
            stats = _run_parallel(
                works_df,
                "/output",
                {},
                None,
                logging.getLogger("test"),
            )

        assert stats["failed"] == 0
        assert stats["succeeded"] == 0


class TestProcessDirectIIIFCreator:
    """The creator must reach compute_work_dir, or a direct-IIIF work lands in
    a different directory than the search path computes for the same work when
    ``naming.include_creator_in_work_dir`` is enabled."""

    @patch("main.orchestration.execution.download_from_iiif_manifest")
    @patch("main.data.work.compute_work_dir")
    def test_creator_forwarded_to_compute_work_dir(
        self, mock_dir: MagicMock, mock_dl: MagicMock
    ) -> None:
        mock_dir.return_value = ("/out/work", "work")
        mock_dl.return_value = {"success": True, "provider": "Gallica"}

        process_direct_iiif(
            manifest_url="https://example.org/manifest.json",
            output_dir="/out",
            entry_id="E001",
            title="Le Viandier",
            creator="Taillevent",
        )

        assert mock_dir.call_args.kwargs["creator"] == "Taillevent"


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
