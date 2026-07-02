"""Tests for the CLI agent contract and the --verify command.

Exit codes: 0 full success, 1 failures/partial, 2 usage error, 130 interrupt.
--json: one machine-readable summary line on stdout.
--dry-run: no side effects (no work dirs, work.json, or index rows).
--verify: flags incomplete works and flips them to partial.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def _batch_args(**overrides: Any) -> argparse.Namespace:
    args = argparse.Namespace(
        csv_file=None,
        output_dir="out",
        dry_run=False,
        log_level="INFO",
        config="config.json",
        interactive=False,
        cli=True,
        non_interactive=False,
        json_summary=False,
        pending_mode="all",
        entry_ids=None,
        limit=None,
        iiif_urls=None,
        name=None,
        verify=False,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class TestBatchExitCodes:
    def test_missing_csv_returns_usage_error(self, mock_config: dict[str, Any]) -> None:
        from main.cli.commands.batch import run_batch_cli

        code = run_batch_cli(
            _batch_args(csv_file=None), mock_config, logging.getLogger("t")
        )
        assert code == 2

    def test_nonexistent_csv_returns_usage_error(
        self, mock_config: dict[str, Any]
    ) -> None:
        from main.cli.commands.batch import run_batch_cli

        code = run_batch_cli(
            _batch_args(csv_file="/does/not/exist.csv"),
            mock_config,
            logging.getLogger("t"),
        )
        assert code == 2

    def test_failed_downloads_return_one(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        from main.cli.commands.batch import run_batch_cli

        csv_path = str(tmp_path / "works.csv")
        pd.DataFrame(
            {
                "entry_id": ["E1"],
                "short_title": ["T"],
                "retrievable": [pd.NA],
            }
        ).to_csv(csv_path, index=False)

        with patch(
            "main.cli.commands.batch.run_batch_downloads",
            return_value={
                "processed": 1,
                "succeeded": 0,
                "failed": 1,
                "deferred": 0,
                "skipped": 0,
            },
        ), patch("main.cli.commands.batch.get_deferred_queue") as mq:
            mq.return_value.get_pending.return_value = []
            code = run_batch_cli(
                _batch_args(csv_file=csv_path),
                mock_config,
                logging.getLogger("t"),
            )
        assert code == 1

    def test_success_returns_zero_and_json_summary(
        self,
        tmp_path: Any,
        mock_config: dict[str, Any],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from main.cli.commands.batch import run_batch_cli

        csv_path = str(tmp_path / "works.csv")
        pd.DataFrame(
            {
                "entry_id": ["E1"],
                "short_title": ["T"],
                "retrievable": [pd.NA],
            }
        ).to_csv(csv_path, index=False)

        with patch(
            "main.cli.commands.batch.run_batch_downloads",
            return_value={
                "processed": 1,
                "succeeded": 1,
                "failed": 0,
                "deferred": 0,
                "skipped": 0,
            },
        ), patch("main.cli.commands.batch.get_deferred_queue") as mq:
            mq.return_value.get_pending.return_value = []
            code = run_batch_cli(
                _batch_args(csv_file=csv_path, json_summary=True),
                mock_config,
                logging.getLogger("t"),
            )

        assert code == 0
        out_lines = [
            ln for ln in capsys.readouterr().out.strip().splitlines() if ln.strip()
        ]
        summary = json.loads(out_lines[-1])
        assert summary["command"] == "batch"
        assert summary["succeeded"] == 1
        assert summary["failed"] == 0


class TestDryRunHygiene:
    """--dry-run must not create work dirs, work.json, or index rows."""

    def test_process_work_dry_run_writes_nothing(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        from api.model import SearchResult
        from main.orchestration import pipeline

        out_dir = str(tmp_path / "output")
        sr = SearchResult(
            provider="Test",
            title="Dry Book",
            creators=[],
            provider_key="test",
            raw={"__matching__": {"score": 100, "total": 100}},
        )
        provider_tuple = ("test", MagicMock(), MagicMock(), "Test")

        with patch.object(
            pipeline,
            "_collect_and_select",
            return_value=([sr], sr, provider_tuple),
        ):
            result = pipeline.process_work(
                "Dry Book", None, "E1", out_dir, dry_run=True
            )

        assert result is not None
        assert result["status"] == "dry_run"
        # Nothing on disk: no work directory, no index.csv.
        assert not os.path.exists(out_dir) or os.listdir(out_dir) == []


class TestVerifyCommand:
    def _make_work(
        self,
        out_dir: str,
        name: str,
        objects: dict[str, bytes],
        work_json: dict[str, Any] | None = None,
    ) -> str:
        work_dir = os.path.join(out_dir, name)
        obj_dir = os.path.join(work_dir, "objects")
        os.makedirs(obj_dir, exist_ok=True)
        for fname, content in objects.items():
            with open(os.path.join(obj_dir, fname), "wb") as f:
                f.write(content)
        if work_json is not None:
            with open(os.path.join(work_dir, "work.json"), "w", encoding="utf-8") as f:
                json.dump(work_json, f)
        return work_dir

    def test_verify_flags_bad_pdf_as_partial(self, tmp_path: Any) -> None:
        from main.cli.commands.verify import run_verify

        out_dir = str(tmp_path / "out")
        self._make_work(
            out_dir,
            "bad_work",
            {"item.pdf": b"<html>error page</html>"},
            work_json={"status": "completed"},
        )
        self._make_work(
            out_dir,
            "good_work",
            {"item.pdf": b"%PDF-1.4 content"},
            work_json={"status": "completed"},
        )

        stats = run_verify(out_dir)
        assert stats["total"] == 2
        assert stats["ok"] == 1
        assert stats["partial"] == 1

        with open(
            os.path.join(out_dir, "bad_work", "work.json"), encoding="utf-8"
        ) as f:
            assert json.load(f)["status"] == "partial"
        with open(
            os.path.join(out_dir, "good_work", "work.json"), encoding="utf-8"
        ) as f:
            assert json.load(f)["status"] == "completed"

    def test_verify_flags_incomplete_pages(self, tmp_path: Any) -> None:
        from main.cli.commands.verify import run_verify

        out_dir = str(tmp_path / "out")
        self._make_work(
            out_dir,
            "partial_pages",
            {"p1.jpg": b"\xff\xd8\xff" + b"x" * 10},
            work_json={
                "status": "completed",
                "pages_expected": 3,
                "pages_downloaded": 1,
            },
        )

        stats = run_verify(out_dir)
        assert stats["partial"] == 1

    def test_verify_flags_empty_objects(self, tmp_path: Any) -> None:
        from main.cli.commands.verify import verify_work

        out_dir = str(tmp_path / "out")
        work_dir = self._make_work(out_dir, "zero", {"file.jpg": b""})
        ok, reason = verify_work(work_dir)
        assert ok is False
        assert "zero" in reason


class TestParallelNoMatchCsv:
    """B9: parallel mode marks genuine no-matches failed like sequential mode."""

    def test_no_match_marked_failed(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        from main.orchestration.execution import _run_parallel

        csv_path = str(tmp_path / "works.csv")
        pd.DataFrame(
            {
                "entry_id": ["E1"],
                "short_title": ["Nowhere Book"],
                "main_author": ["A"],
                "retrievable": [pd.NA],
            }
        ).to_csv(csv_path, index=False)
        works_df = pd.read_csv(csv_path)

        with (
            patch(
                "main.orchestration.execution.pipeline.search_and_select",
                return_value=None,
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
                logger=logging.getLogger("t"),
                csv_path=csv_path,
            )

        df = pd.read_csv(csv_path)
        status = str(df.loc[df["entry_id"] == "E1", "retrievable"].iloc[0])
        assert status.strip().lower() == "false"
