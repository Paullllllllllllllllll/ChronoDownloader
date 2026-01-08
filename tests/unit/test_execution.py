"""Unit tests for main.execution module, specifically CSV sync in parallel execution."""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from main.download_scheduler import DownloadTask
from main.unified_csv import ENTRY_ID_COL, STATUS_COL, LINK_COL


class TestParallelExecutionCsvSync:
    """Tests for CSV sync behavior in parallel execution with custom callbacks.
    
    This test class specifically verifies the bug fix where CSV sync was skipped
    when custom callbacks (like interactive mode callbacks) were provided.
    The fix ensures wrapped_complete always performs CSV sync regardless of
    whether custom callbacks are provided.
    """
    
    @pytest.fixture
    def mock_task(self):
        """Create a mock DownloadTask."""
        task = MagicMock(spec=DownloadTask)
        task.work_id = "work_123"
        task.entry_id = "E0001"
        task.title = "Test Title"
        task.creator = "Test Author"
        task.provider_key = "internet_archive"
        task.item_url = "https://example.com/item"
        task.provider = "Internet Archive"
        return task
    
    @pytest.fixture
    def test_csv_file(self, temp_dir: str) -> str:
        """Create a test CSV file."""
        csv_path = os.path.join(temp_dir, "test_works.csv")
        pd.DataFrame({
            ENTRY_ID_COL: ["E0001", "E0002", "E0003"],
            "short_title": ["Work A", "Work B", "Work C"],
            STATUS_COL: [pd.NA, pd.NA, pd.NA],
            LINK_COL: [pd.NA, pd.NA, pd.NA]
        }).to_csv(csv_path, index=False)
        return csv_path
    
    def test_csv_sync_with_custom_callback_success(self, temp_dir: str, test_csv_file: str, mock_task):
        """Test that CSV is synced even when custom on_complete callback is provided.
        
        This is the core test for the bug fix: before the fix, providing a custom
        callback would skip CSV sync entirely. After the fix, CSV sync should
        always happen in wrapped_complete.
        """
        from main.execution import _run_parallel
        from main.unified_csv import mark_success
        
        custom_callback_called = threading.Event()
        
        def custom_on_complete(task, success, error):
            """Custom callback that does NOT call mark_success."""
            custom_callback_called.set()
        
        # Mock the pipeline and scheduler to simulate a successful download
        with patch("main.execution.pipeline") as mock_pipeline, \
             patch("main.execution.DownloadScheduler") as MockScheduler, \
             patch("main.execution.mark_success") as mock_mark_success, \
             patch("main.execution.mark_failed") as mock_mark_failed, \
             patch("main.execution.get_parallel_download_config") as mock_config, \
             patch("main.execution.utils"):
            
            # Configure mock scheduler
            mock_scheduler_instance = MagicMock()
            MockScheduler.return_value = mock_scheduler_instance
            mock_scheduler_instance.pending_count = 0
            mock_scheduler_instance.get_stats.return_value = {"completed": 1, "succeeded": 1, "failed": 0}
            
            # Capture the on_complete callback passed to scheduler
            captured_on_complete = [None]
            def capture_init(**kwargs):
                captured_on_complete[0] = kwargs.get("on_complete")
                return mock_scheduler_instance
            MockScheduler.side_effect = capture_init
            
            # Configure config
            mock_config.return_value = {"max_parallel_downloads": 1, "provider_concurrency": {}, "worker_timeout_s": 60}
            
            # Configure pipeline to return a task
            mock_pipeline.search_and_select.return_value = mock_task
            
            # Create test DataFrame
            test_df = pd.DataFrame({
                ENTRY_ID_COL: ["E0001"],
                "short_title": ["Work A"],
                "main_author": ["Author A"]
            })
            
            # Run parallel execution with custom callback
            import logging
            logger = logging.getLogger("test")
            
            _run_parallel(
                works_df=test_df,
                output_dir=temp_dir,
                config={},
                max_workers_override=1,
                logger=logger,
                on_submit=None,
                on_complete=custom_on_complete,  # Custom callback provided
                csv_path=test_csv_file,
            )
            
            # Now simulate the scheduler calling on_complete
            assert captured_on_complete[0] is not None, "on_complete should be captured"
            
            # Call the captured on_complete (this is wrapped_complete)
            captured_on_complete[0](mock_task, True, None)
            
            # Verify mark_success was called (CSV sync happened)
            mock_mark_success.assert_called_once_with(
                test_csv_file,
                mock_task.entry_id,
                mock_task.item_url,
                mock_task.provider
            )
            
            # Verify custom callback was also called
            assert custom_callback_called.is_set(), "Custom callback should be called after CSV sync"
    
    def test_csv_sync_with_custom_callback_failure(self, temp_dir: str, test_csv_file: str, mock_task):
        """Test that CSV is synced on failure even when custom callback is provided."""
        from main.execution import _run_parallel
        
        custom_callback_called = threading.Event()
        
        def custom_on_complete(task, success, error):
            custom_callback_called.set()
        
        with patch("main.execution.pipeline") as mock_pipeline, \
             patch("main.execution.DownloadScheduler") as MockScheduler, \
             patch("main.execution.mark_success") as mock_mark_success, \
             patch("main.execution.mark_failed") as mock_mark_failed, \
             patch("main.execution.get_parallel_download_config") as mock_config, \
             patch("main.execution.utils"):
            
            mock_scheduler_instance = MagicMock()
            MockScheduler.return_value = mock_scheduler_instance
            mock_scheduler_instance.pending_count = 0
            mock_scheduler_instance.get_stats.return_value = {"completed": 1, "succeeded": 0, "failed": 1}
            
            captured_on_complete = [None]
            def capture_init(**kwargs):
                captured_on_complete[0] = kwargs.get("on_complete")
                return mock_scheduler_instance
            MockScheduler.side_effect = capture_init
            
            mock_config.return_value = {"max_parallel_downloads": 1, "provider_concurrency": {}, "worker_timeout_s": 60}
            mock_pipeline.search_and_select.return_value = mock_task
            
            test_df = pd.DataFrame({
                ENTRY_ID_COL: ["E0001"],
                "short_title": ["Work A"],
                "main_author": ["Author A"]
            })
            
            import logging
            logger = logging.getLogger("test")
            
            _run_parallel(
                works_df=test_df,
                output_dir=temp_dir,
                config={},
                max_workers_override=1,
                logger=logger,
                on_complete=custom_on_complete,
                csv_path=test_csv_file,
            )
            
            assert captured_on_complete[0] is not None
            
            # Simulate failure
            captured_on_complete[0](mock_task, False, Exception("Download failed"))
            
            # Verify mark_failed was called
            mock_mark_failed.assert_called_once_with(test_csv_file, mock_task.entry_id)
            
            # Verify custom callback was also called
            assert custom_callback_called.is_set()
    
    def test_csv_sync_without_custom_callback(self, temp_dir: str, test_csv_file: str, mock_task):
        """Test that CSV sync works correctly when no custom callback is provided."""
        from main.execution import _run_parallel
        
        with patch("main.execution.pipeline") as mock_pipeline, \
             patch("main.execution.DownloadScheduler") as MockScheduler, \
             patch("main.execution.mark_success") as mock_mark_success, \
             patch("main.execution.mark_failed") as mock_mark_failed, \
             patch("main.execution.get_parallel_download_config") as mock_config, \
             patch("main.execution.utils"):
            
            mock_scheduler_instance = MagicMock()
            MockScheduler.return_value = mock_scheduler_instance
            mock_scheduler_instance.pending_count = 0
            mock_scheduler_instance.get_stats.return_value = {"completed": 1, "succeeded": 1, "failed": 0}
            
            captured_on_complete = [None]
            def capture_init(**kwargs):
                captured_on_complete[0] = kwargs.get("on_complete")
                return mock_scheduler_instance
            MockScheduler.side_effect = capture_init
            
            mock_config.return_value = {"max_parallel_downloads": 1, "provider_concurrency": {}, "worker_timeout_s": 60}
            mock_pipeline.search_and_select.return_value = mock_task
            
            test_df = pd.DataFrame({
                ENTRY_ID_COL: ["E0001"],
                "short_title": ["Work A"],
                "main_author": ["Author A"]
            })
            
            import logging
            logger = logging.getLogger("test")
            
            # No custom callbacks provided
            _run_parallel(
                works_df=test_df,
                output_dir=temp_dir,
                config={},
                max_workers_override=1,
                logger=logger,
                on_submit=None,
                on_complete=None,
                csv_path=test_csv_file,
            )
            
            assert captured_on_complete[0] is not None
            
            # Simulate success
            captured_on_complete[0](mock_task, True, None)
            
            # Verify mark_success was called
            mock_mark_success.assert_called_once()
    
    def test_csv_sync_skipped_without_csv_path(self, temp_dir: str, mock_task):
        """Test that CSV sync is skipped when no csv_path is provided."""
        from main.execution import _run_parallel
        
        with patch("main.execution.pipeline") as mock_pipeline, \
             patch("main.execution.DownloadScheduler") as MockScheduler, \
             patch("main.execution.mark_success") as mock_mark_success, \
             patch("main.execution.mark_failed") as mock_mark_failed, \
             patch("main.execution.get_parallel_download_config") as mock_config, \
             patch("main.execution.utils"):
            
            mock_scheduler_instance = MagicMock()
            MockScheduler.return_value = mock_scheduler_instance
            mock_scheduler_instance.pending_count = 0
            mock_scheduler_instance.get_stats.return_value = {"completed": 1, "succeeded": 1, "failed": 0}
            
            captured_on_complete = [None]
            def capture_init(**kwargs):
                captured_on_complete[0] = kwargs.get("on_complete")
                return mock_scheduler_instance
            MockScheduler.side_effect = capture_init
            
            mock_config.return_value = {"max_parallel_downloads": 1, "provider_concurrency": {}, "worker_timeout_s": 60}
            mock_pipeline.search_and_select.return_value = mock_task
            
            test_df = pd.DataFrame({
                ENTRY_ID_COL: ["E0001"],
                "short_title": ["Work A"],
                "main_author": ["Author A"]
            })
            
            import logging
            logger = logging.getLogger("test")
            
            # No csv_path provided
            _run_parallel(
                works_df=test_df,
                output_dir=temp_dir,
                config={},
                max_workers_override=1,
                logger=logger,
                csv_path=None,
            )
            
            assert captured_on_complete[0] is not None
            captured_on_complete[0](mock_task, True, None)
            
            # Verify mark_success was NOT called (no csv_path)
            mock_mark_success.assert_not_called()
    
    def test_csv_sync_skipped_without_entry_id(self, temp_dir: str, test_csv_file: str):
        """Test that CSV sync is skipped when task has no entry_id."""
        from main.execution import _run_parallel
        
        # Task without entry_id
        task_no_entry = MagicMock(spec=DownloadTask)
        task_no_entry.work_id = "work_123"
        task_no_entry.entry_id = None  # No entry_id
        task_no_entry.title = "Test Title"
        task_no_entry.provider_key = "internet_archive"
        
        with patch("main.execution.pipeline") as mock_pipeline, \
             patch("main.execution.DownloadScheduler") as MockScheduler, \
             patch("main.execution.mark_success") as mock_mark_success, \
             patch("main.execution.mark_failed") as mock_mark_failed, \
             patch("main.execution.get_parallel_download_config") as mock_config, \
             patch("main.execution.utils"):
            
            mock_scheduler_instance = MagicMock()
            MockScheduler.return_value = mock_scheduler_instance
            mock_scheduler_instance.pending_count = 0
            mock_scheduler_instance.get_stats.return_value = {"completed": 1, "succeeded": 1, "failed": 0}
            
            captured_on_complete = [None]
            def capture_init(**kwargs):
                captured_on_complete[0] = kwargs.get("on_complete")
                return mock_scheduler_instance
            MockScheduler.side_effect = capture_init
            
            mock_config.return_value = {"max_parallel_downloads": 1, "provider_concurrency": {}, "worker_timeout_s": 60}
            mock_pipeline.search_and_select.return_value = task_no_entry
            
            test_df = pd.DataFrame({
                ENTRY_ID_COL: ["E0001"],
                "short_title": ["Work A"],
                "main_author": ["Author A"]
            })
            
            import logging
            logger = logging.getLogger("test")
            
            _run_parallel(
                works_df=test_df,
                output_dir=temp_dir,
                config={},
                max_workers_override=1,
                logger=logger,
                csv_path=test_csv_file,
            )
            
            assert captured_on_complete[0] is not None
            captured_on_complete[0](task_no_entry, True, None)
            
            # Verify mark_success was NOT called (no entry_id)
            mock_mark_success.assert_not_called()


class TestInteractiveCallbacksCsvSync:
    """Tests verifying that interactive mode callbacks work with CSV sync."""
    
    def test_create_interactive_callbacks_csv_sync_preserved(self, temp_dir: str):
        """Test that using create_interactive_callbacks still results in CSV sync.
        
        This test ensures the bug fix works for the interactive mode use case:
        when create_interactive_callbacks is used, CSV sync must still happen.
        """
        from main.execution import create_interactive_callbacks, _run_parallel
        
        with patch("main.execution.pipeline") as mock_pipeline, \
             patch("main.execution.DownloadScheduler") as MockScheduler, \
             patch("main.execution.mark_success") as mock_mark_success, \
             patch("main.execution.get_parallel_download_config") as mock_config, \
             patch("main.execution.utils"), \
             patch("main.console_ui.ConsoleUI"):
            
            mock_scheduler_instance = MagicMock()
            MockScheduler.return_value = mock_scheduler_instance
            mock_scheduler_instance.pending_count = 0
            mock_scheduler_instance.get_stats.return_value = {"completed": 1, "succeeded": 1, "failed": 0}
            
            captured_on_complete = [None]
            def capture_init(**kwargs):
                captured_on_complete[0] = kwargs.get("on_complete")
                return mock_scheduler_instance
            MockScheduler.side_effect = capture_init
            
            mock_config.return_value = {"max_parallel_downloads": 1, "provider_concurrency": {}, "worker_timeout_s": 60}
            
            mock_task = MagicMock(spec=DownloadTask)
            mock_task.work_id = "work_123"
            mock_task.entry_id = "E0001"
            mock_task.title = "Test Title"
            mock_task.provider_key = "ia"
            mock_task.item_url = "https://example.com"
            mock_task.provider = "IA"
            mock_pipeline.search_and_select.return_value = mock_task
            
            test_df = pd.DataFrame({
                "entry_id": ["E0001"],
                "short_title": ["Work A"],
                "main_author": ["Author A"]
            })
            
            # Create interactive callbacks (simulates what interactive.py does)
            import logging
            logger = logging.getLogger("test")
            on_submit, on_complete = create_interactive_callbacks(logger)
            
            csv_path = os.path.join(temp_dir, "test.csv")
            pd.DataFrame({
                "entry_id": ["E0001"],
                "short_title": ["Work A"],
                "retrievable": [pd.NA],
                "link": [pd.NA]
            }).to_csv(csv_path, index=False)
            
            _run_parallel(
                works_df=test_df,
                output_dir=temp_dir,
                config={},
                max_workers_override=1,
                logger=logger,
                on_submit=on_submit,
                on_complete=on_complete,
                csv_path=csv_path,
            )
            
            assert captured_on_complete[0] is not None
            captured_on_complete[0](mock_task, True, None)
            
            # This is the key assertion: CSV sync must happen even with interactive callbacks
            mock_mark_success.assert_called_once()
