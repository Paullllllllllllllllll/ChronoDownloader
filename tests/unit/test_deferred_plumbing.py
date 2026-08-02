"""Regression tests for deferred-status plumbing (audit B2, B3, B4).

B2: In parallel mode a quota-deferred task was marked FAILED in the works CSV
    (``wrapped_complete`` only knew success/failure), so with
    ``--pending-mode new`` it was never retried.
B3: A successful background/eager retry only updated the state file; the works
    CSV, work.json, and index.csv still said deferred/failed, so the next run
    re-downloaded the work and burned another quota unit.
B4: ``datetime.replace(second=now.second + wait_seconds)`` raised ValueError
    for any wait over a minute and aborted the whole retry sweep.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd

from main.state.deferred import DeferredItem


def _make_csv(tmp_path: Any) -> str:
    csv_path = str(tmp_path / "works.csv")
    pd.DataFrame(
        {
            "entry_id": ["E001"],
            "short_title": ["Deferred Book"],
            "main_author": ["Author"],
            "retrievable": [pd.NA],
            "link": [pd.NA],
        }
    ).to_csv(csv_path, index=False)
    return csv_path


class TestParallelDeferredCsv:
    """B2: parallel mode records quota deferrals as deferred, not failed."""

    def test_deferred_task_marked_deferred_in_csv(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        from main.orchestration.execution import _run_parallel

        csv_path = _make_csv(tmp_path)
        works_df = pd.read_csv(csv_path)

        task = MagicMock()
        task.entry_id = "E001"
        task.title = "Deferred Book"
        task.provider_key = "annas_archive"
        task.work_dir = str(tmp_path / "work")
        task.status = None

        def fake_execute(t: Any, dry_run: bool = False) -> bool:
            # Production behavior: execute_download sets task.status and
            # returns False when the download was quota-deferred.
            t.status = "deferred"
            return False

        with (
            patch(
                "main.orchestration.execution.pipeline.search_and_select",
                return_value=task,
            ),
            patch(
                "main.orchestration.execution.pipeline.execute_download",
                side_effect=fake_execute,
            ),
            patch(
                "main.orchestration.execution.is_direct_download_enabled",
                return_value=False,
            ),
        ):
            _run_parallel(
                works_df,
                str(tmp_path / "out"),
                mock_config,
                max_workers_override=2,
                logger=logging.getLogger("test"),
                csv_path=csv_path,
            )

        df = pd.read_csv(csv_path)
        status = str(df.loc[df["entry_id"] == "E001", "retrievable"].iloc[0])
        # Pre-fix this was "False" (failed); it must be the retriable
        # "deferred" status.
        assert status.strip().lower() == "deferred"


class TestEagerRetryPersistence:
    """B3: a successful retry writes through all three ledgers."""

    def test_retry_success_updates_csv_workjson_index(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        from main.state.background import BackgroundRetryScheduler

        BackgroundRetryScheduler._instance = None
        try:
            csv_path = _make_csv(tmp_path)
            out_dir = str(tmp_path / "out")
            work_dir = os.path.join(out_dir, "e_001_deferred_book")
            os.makedirs(work_dir, exist_ok=True)
            work_json_path = os.path.join(work_dir, "work.json")
            with open(work_json_path, "w", encoding="utf-8") as f:
                json.dump({"status": "deferred"}, f)

            scheduler = BackgroundRetryScheduler()
            item = DeferredItem(
                id="item-1",
                title="Deferred Book",
                creator="Author",
                entry_id="E001",
                provider_key="testprov",
                provider_name="Test Provider",
                source_id="src-1",
                work_dir=work_dir,
                base_output_dir=out_dir,
                item_url="https://example.org/item",
                status="pending",
            )

            queue = MagicMock()
            queue.get_ready.return_value = [item]
            queue.mark_completed.return_value = True
            scheduler._queue = queue
            scheduler._quota_manager = MagicMock()
            scheduler._quota_manager.can_download.return_value = (True, None)
            scheduler.set_provider_download_fn("testprov", lambda sr, wd: True)

            with patch("main.state.background.get_deferred_queue", return_value=queue):
                stats, completed_entry_ids = scheduler.retry_ready_now(
                    csv_path=csv_path
                )

            assert stats["succeeded"] == 1
            assert completed_entry_ids == {"E001"}

            # work.json flipped to completed
            with open(work_json_path, encoding="utf-8") as f:
                meta = json.load(f)
            assert meta["status"] == "completed"

            # index.csv has a completed row for the work
            index_path = os.path.join(out_dir, "index.csv")
            assert os.path.exists(index_path)
            idx = pd.read_csv(index_path)
            assert (idx["status"] == "completed").any()

            # works CSV marked success
            df = pd.read_csv(csv_path)
            status = df.loc[df["entry_id"] == "E001", "retrievable"].iloc[0]
            assert str(status).strip().lower() == "true"
        finally:
            BackgroundRetryScheduler._instance = None

    def test_entry_id_collision_does_not_stamp_a_foreign_csv(
        self, tmp_path: Any
    ) -> None:
        """entry_id schemes repeat across sampling CSVs. Matching on the id
        alone wrote this item's link, provider and timestamp into an unrelated
        work's row and then dropped that row from the batch, so the work was
        never downloaded."""
        from main.state.background import BackgroundRetryScheduler

        BackgroundRetryScheduler._instance = None
        try:
            csv_path = _make_csv(tmp_path)
            out_dir = str(tmp_path / "out")
            work_dir = os.path.join(out_dir, "e_001_other_corpus_book")
            os.makedirs(work_dir, exist_ok=True)

            scheduler = BackgroundRetryScheduler()
            item = DeferredItem(
                id="item-1",
                title="A Book From Another Corpus",
                creator="Author",
                entry_id="E001",
                provider_key="testprov",
                provider_name="Test Provider",
                source_id="src-1",
                work_dir=work_dir,
                base_output_dir=out_dir,
                item_url="https://example.org/item",
                status="pending",
            )

            queue = MagicMock()
            queue.get_ready.return_value = [item]
            queue.mark_completed.return_value = True
            scheduler._queue = queue
            scheduler._quota_manager = MagicMock()
            scheduler._quota_manager.can_download.return_value = (True, None)
            scheduler.set_provider_download_fn("testprov", lambda sr, wd: True)

            with patch("main.state.background.get_deferred_queue", return_value=queue):
                stats, completed_entry_ids = scheduler.retry_ready_now(
                    csv_path=csv_path,
                    csv_entry_titles={"E001": "Deferred Book"},
                )

            # The download itself still succeeded and is recorded under its own
            # output directory; only the foreign CSV is left alone.
            assert stats["succeeded"] == 1
            assert completed_entry_ids == set()

            df = pd.read_csv(csv_path)
            status = df.loc[df["entry_id"] == "E001", "retrievable"].iloc[0]
            assert str(status).strip().lower() != "true"
        finally:
            BackgroundRetryScheduler._instance = None


class TestQuotaWaitDatetime:
    """B4: long quota waits must not crash the retry sweep."""

    def test_hours_long_wait_updates_reset_time(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        from datetime import datetime

        from main.state.background import RETRY_POSTPONED, BackgroundRetryScheduler

        BackgroundRetryScheduler._instance = None
        try:
            scheduler = BackgroundRetryScheduler()
            item = DeferredItem(
                id="item-2",
                title="Quota Book",
                creator=None,
                entry_id="E002",
                provider_key="testprov",
                provider_name="Test Provider",
                source_id=None,
                work_dir=str(tmp_path / "w"),
                base_output_dir=str(tmp_path),
                status="pending",
            )

            queue = MagicMock()
            scheduler._queue = queue
            qm = MagicMock()
            # 2-hour wait: pre-fix datetime.replace(second=now.second + 7200)
            # raised ValueError and aborted the sweep.
            qm.can_download.return_value = (False, 7200.0)
            scheduler._quota_manager = qm
            scheduler.set_provider_download_fn("testprov", lambda sr, wd: True)

            result = scheduler._retry_item(item)

            # Still quota-limited (postponed, not failed), and no crash. The
            # new reset time goes through the queue's public mutator, which
            # owns both the field update and the save.
            assert result == RETRY_POSTPONED
            queue.update_reset_time.assert_called_once()
            called_id, new_reset = queue.update_reset_time.call_args[0]
            assert called_id == "item-2"
            assert isinstance(new_reset, datetime)
            delta = (new_reset - datetime.now(UTC)).total_seconds()
            assert 7000 < delta < 7400
        finally:
            BackgroundRetryScheduler._instance = None

    def test_reset_time_update_does_not_poke_private_save(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """The retry helper must not mutate items or save behind the queue.

        Mutating ``item.reset_time`` and calling the queue's private
        ``_save_queue`` held neither of the queue's locks, so a concurrent
        mutator's newer snapshot could be overwritten on disk by this stale
        one.
        """
        from main.state.background import BackgroundRetryScheduler

        BackgroundRetryScheduler._instance = None
        try:
            scheduler = BackgroundRetryScheduler()
            item = DeferredItem(
                id="item-3",
                title="Quota Book",
                creator=None,
                entry_id="E003",
                provider_key="testprov",
                provider_name="Test Provider",
                source_id=None,
                work_dir=str(tmp_path / "w"),
                base_output_dir=str(tmp_path),
                status="pending",
            )

            queue = MagicMock()
            scheduler._queue = queue
            qm = MagicMock()
            qm.can_download.return_value = (False, 3600.0)
            scheduler._quota_manager = qm
            scheduler.set_provider_download_fn("testprov", lambda sr, wd: True)

            scheduler._retry_item(item)

            queue._save_queue.assert_not_called()
            queue.update_reset_time.assert_called_once()
            # The item itself is left to the queue to mutate.
            assert item.reset_time is None
        finally:
            BackgroundRetryScheduler._instance = None
