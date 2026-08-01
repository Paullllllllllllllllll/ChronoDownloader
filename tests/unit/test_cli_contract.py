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

        with (
            patch(
                "main.cli.commands.batch.run_batch_downloads",
                return_value={
                    "processed": 1,
                    "succeeded": 0,
                    "failed": 1,
                    "deferred": 0,
                    "skipped": 0,
                },
            ),
        ):
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

        with (
            patch(
                "main.cli.commands.batch.run_batch_downloads",
                return_value={
                    "processed": 1,
                    "succeeded": 1,
                    "failed": 0,
                    "deferred": 0,
                    "skipped": 0,
                },
            ),
        ):
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

    def _write_index(self, out_dir: str, rows: list[dict[str, str]]) -> str:
        from main.data.index import INDEX_COLUMNS

        os.makedirs(out_dir, exist_ok=True)
        index_path = os.path.join(out_dir, "index.csv")
        pd.DataFrame(rows, columns=INDEX_COLUMNS).to_csv(index_path, index=False)
        return index_path

    def test_verify_updates_existing_index_row(self, tmp_path: Any) -> None:
        """The ledger is keyed on (work_id, entry_id): the verify row must
        carry the entry_id so it replaces the real row instead of appending."""
        from main.cli.commands.verify import run_verify

        out_dir = str(tmp_path / "out")
        work_dir = self._make_work(
            out_dir,
            "bad_work",
            {"item.pdf": b"<html>error page</html>"},
            work_json={"status": "completed"},
        )
        index_path = self._write_index(
            out_dir,
            [
                {
                    "work_id": "W1",
                    "entry_id": "E1",
                    "work_dir": work_dir,
                    "status": "completed",
                }
            ],
        )

        stats = run_verify(out_dir)
        assert stats["partial"] == 1

        df = pd.read_csv(index_path)
        assert len(df) == 1
        assert str(df.loc[0, "status"]) == "partial"
        assert str(df.loc[0, "entry_id"]) == "E1"

    def test_verify_matches_zero_padded_entry_ids(self, tmp_path: Any) -> None:
        """dtype inference read "00123" back as int 123, so the upsert key
        never matched and every run appended one titleless duplicate while
        the real row kept advertising "completed"."""
        from main.cli.commands.verify import run_verify

        out_dir = str(tmp_path / "out")
        work_dir = self._make_work(
            out_dir,
            "bad_work",
            {"item.pdf": b"<html>error page</html>"},
            work_json={"status": "completed"},
        )
        index_path = self._write_index(
            out_dir,
            [
                {
                    "work_id": "W1",
                    "entry_id": "00123",
                    "work_dir": work_dir,
                    "title": "A Work",
                    "status": "completed",
                }
            ],
        )

        run_verify(out_dir)

        df = pd.read_csv(index_path, dtype=str)
        assert len(df) == 1
        assert str(df.loc[0, "entry_id"]) == "00123"
        assert str(df.loc[0, "status"]) == "partial"

    def test_verify_skips_rows_that_were_never_downloaded(self, tmp_path: Any) -> None:
        """no_match/failed/deferred rows have empty directories by
        construction; verifying them would report a spurious partial."""
        from main.cli.commands.verify import run_verify
        from main.cli.entry import _run_verify_command

        out_dir = str(tmp_path / "out")
        good_dir = self._make_work(
            out_dir,
            "good_work",
            {"item.pdf": b"%PDF-1.4 content"},
            work_json={"status": "completed"},
        )
        rows = [
            {
                "work_id": "W1",
                "entry_id": "E1",
                "work_dir": good_dir,
                "status": "completed",
            }
        ]
        for name, status in (
            ("no_match_work", "no_match"),
            ("failed_work", "failed"),
            ("deferred_work", "deferred"),
        ):
            empty_dir = os.path.join(out_dir, name)
            os.makedirs(empty_dir, exist_ok=True)
            rows.append(
                {
                    "work_id": f"W_{status}",
                    "entry_id": f"E_{status}",
                    "work_dir": empty_dir,
                    "status": status,
                }
            )
        self._write_index(out_dir, rows)

        stats = run_verify(out_dir)
        assert stats == {"total": 1, "ok": 1, "partial": 0}

        args = _batch_args(output_dir=out_dir, verify=True)
        assert _run_verify_command(args) == 0

    def test_verify_sees_direct_iiif_works_beside_index_rows(
        self, tmp_path: Any
    ) -> None:
        """Direct-IIIF downloads write no index row, by documented design.

        The index scan used to short-circuit the directory scan, so in any
        mixed corpus every direct-IIIF work became invisible to --verify: a
        corrupt one stayed "completed", was never re-downloaded, and the
        command still exited 0.
        """
        from main.cli.commands.verify import run_verify

        out_dir = str(tmp_path / "out")
        search_dir = self._make_work(
            out_dir,
            "search_work",
            {"item.pdf": b"%PDF-1.4 content"},
            work_json={"status": "completed"},
        )
        self._make_work(
            out_dir,
            "iiif_work",
            {"item.pdf": b"<html>error page</html>"},
            work_json={"status": "completed"},
        )
        self._write_index(
            out_dir,
            [
                {
                    "work_id": "W1",
                    "entry_id": "E1",
                    "work_dir": search_dir,
                    "status": "completed",
                }
            ],
        )

        stats = run_verify(out_dir)
        assert stats == {"total": 2, "ok": 1, "partial": 1}
        with open(
            os.path.join(out_dir, "iiif_work", "work.json"), encoding="utf-8"
        ) as f:
            assert json.load(f)["status"] == "partial"

    def test_verify_does_not_double_count_indexed_work_dirs(
        self, tmp_path: Any
    ) -> None:
        """A work present in both the index and the scan is verified once."""
        from main.cli.commands.verify import run_verify

        out_dir = str(tmp_path / "out")
        work_dir = self._make_work(
            out_dir,
            "search_work",
            {"item.pdf": b"%PDF-1.4 content"},
            work_json={"status": "completed"},
        )
        self._write_index(
            out_dir,
            [
                {
                    "work_id": "W1",
                    "entry_id": "E1",
                    "work_dir": work_dir,
                    "status": "completed",
                }
            ],
        )

        assert run_verify(out_dir) == {"total": 1, "ok": 1, "partial": 0}


class TestLimitArgument:
    """A negative --limit is a usage error, not a silent no-op."""

    def test_negative_limit_is_rejected(self) -> None:
        from main.cli.parser import create_cli_parser

        with pytest.raises(SystemExit) as excinfo:
            create_cli_parser().parse_args(["works.csv", "--limit", "-1"])
        assert excinfo.value.code == 2

    def test_zero_limit_is_accepted(self) -> None:
        from main.cli.parser import create_cli_parser

        args = create_cli_parser().parse_args(["works.csv", "--limit", "0"])
        assert args.limit == 0


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
