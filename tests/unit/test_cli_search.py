"""Tests for the --search / --search-only CLI mode.

Search mode contract: structured candidate metadata on stdout, no side
effects (no work dirs, work.json, or index rows), exit 0 only when every
queried work produced a confident match.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from api.model import SearchResult
from main.cli.commands.search import run_search_cli
from main.cli.exit_codes import EXIT_FAILURES, EXIT_OK, EXIT_USAGE
from main.cli.overrides import _looks_like_cli_invocation
from main.cli.parser import create_cli_parser
from main.orchestration import pipeline

logger = logging.getLogger("test_cli_search")


def _make_candidate(
    provider: str = "Internet Archive",
    provider_key: str = "internet_archive",
    score: float = 90.0,
    total: float = 95.0,
    source_id: str = "test123",
) -> SearchResult:
    return SearchResult(
        provider=provider,
        title="Test Book",
        creators=["Author, Test"],
        date="1590",
        source_id=source_id,
        item_url=f"https://example.org/{source_id}",
        provider_key=provider_key,
        raw={"__matching__": {"score": score, "total": total}},
    )


class TestParserSearchFlags:
    def test_search_flag_parsed(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args(["--search", "Le Viandier", "--creator", "Taillevent"])
        assert args.search == "Le Viandier"
        assert args.creator == "Taillevent"
        assert args.search_only is False

    def test_search_only_flag_parsed(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args(["works.csv", "--search-only", "--json"])
        assert args.search_only is True
        assert args.csv_file == "works.csv"
        assert args.json_summary is True

    def test_defaults(self) -> None:
        parser = create_cli_parser()
        args = parser.parse_args(["works.csv"])
        assert args.search is None
        assert args.creator is None
        assert args.search_only is False


class TestCliDetection:
    def test_search_flag_detected_as_cli(self) -> None:
        assert _looks_like_cli_invocation(["--search", "Title"]) is True

    def test_search_only_flag_detected_as_cli(self) -> None:
        assert _looks_like_cli_invocation(["--search-only"]) is True


class TestSearchWork:
    def test_match_returns_structured_result(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            pipeline,
            "_get_selection_config",
            lambda: {"provider_hierarchy": []},
        )
        candidate = _make_candidate()
        provider_tuple = (
            "internet_archive",
            MagicMock(),
            MagicMock(),
            "Internet Archive",
        )
        monkeypatch.setattr(
            pipeline,
            "_collect_and_select",
            lambda *a, **k: ([candidate], candidate, provider_tuple),
        )

        result = pipeline.search_work("Test Book", "Author, Test", "E1")

        assert result["status"] == "match"
        assert result["entry_id"] == "E1"
        assert result["query"] == {"title": "Test Book", "creator": "Author, Test"}
        assert result["selected"]["provider_key"] == "internet_archive"
        assert result["selected"]["source_id"] == "test123"
        assert result["selected"]["item_url"] == "https://example.org/test123"
        assert result["selected"]["scores"]["score"] == 90.0
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["scores"]["total"] == 95.0
        # Fully side-effect-free: nothing written to the working directory.
        assert list(tmp_path.iterdir()) == []

    def test_no_candidates(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            pipeline, "_collect_and_select", lambda *a, **k: ([], None, None)
        )
        result = pipeline.search_work("Unknown Work")
        assert result["status"] == "no_candidates"
        assert result["selected"] is None
        assert result["candidates"] == []
        assert result["entry_id"] is None

    def test_candidates_without_selection(self, monkeypatch: Any) -> None:
        candidate = _make_candidate(score=10.0, total=10.0)
        monkeypatch.setattr(
            pipeline, "_collect_and_select", lambda *a, **k: ([candidate], None, None)
        )
        result = pipeline.search_work("Test Book")
        assert result["status"] == "no_match"
        assert result["selected"] is None
        assert len(result["candidates"]) == 1


class TestRunSearchCliAdHoc:
    def _args(self, argv: list[str]) -> Any:
        return create_cli_parser().parse_args(argv)

    @patch("main.cli.commands.search.pipeline.search_work")
    def test_match_emits_ndjson_and_exits_zero(
        self, mock_search: MagicMock, capsys: Any
    ) -> None:
        mock_search.return_value = {
            "entry_id": None,
            "query": {"title": "Test Book", "creator": None},
            "status": "match",
            "selected": {"provider_key": "mdz", "source_id": "bsb1"},
            "candidates": [],
        }
        args = self._args(["--search", "Test Book", "--json"])
        code = run_search_cli(args, {}, logger)
        assert code == EXIT_OK
        out_lines = [
            line for line in capsys.readouterr().out.splitlines() if line.strip()
        ]
        payload = json.loads(out_lines[-1])
        assert payload["status"] == "match"
        assert payload["selected"]["source_id"] == "bsb1"

    @patch("main.cli.commands.search.pipeline.search_work")
    def test_no_match_exits_one(self, mock_search: MagicMock) -> None:
        mock_search.return_value = {
            "entry_id": None,
            "query": {"title": "X", "creator": None},
            "status": "no_candidates",
            "selected": None,
            "candidates": [],
        }
        args = self._args(["--search", "X", "--json"])
        assert run_search_cli(args, {}, logger) == EXIT_FAILURES

    @patch("main.cli.commands.search.pipeline.search_work")
    def test_creator_passed_through(self, mock_search: MagicMock) -> None:
        mock_search.return_value = {
            "entry_id": None,
            "query": {"title": "T", "creator": "C"},
            "status": "match",
            "selected": {},
            "candidates": [],
        }
        args = self._args(["--search", "T", "--creator", "C"])
        run_search_cli(args, {}, logger)
        mock_search.assert_called_once_with("T", "C", None)

    def test_human_output_renders(self, capsys: Any) -> None:
        with patch("main.cli.commands.search.pipeline.search_work") as mock_search:
            mock_search.return_value = {
                "entry_id": "E1",
                "query": {"title": "Test Book", "creator": "Author"},
                "status": "match",
                "selected": {
                    "provider": "MDZ",
                    "title": "Test Book",
                    "date": "1590",
                    "source_id": "bsb1",
                    "scores": {"score": 92.0},
                },
                "candidates": [
                    {
                        "provider": "MDZ",
                        "title": "Test Book",
                        "date": "1590",
                        "source_id": "bsb1",
                        "item_url": "https://example.org/bsb1",
                        "scores": {"score": 92.0},
                    }
                ],
            }
            args = self._args(["--search", "Test Book"])
            assert run_search_cli(args, {}, logger) == EXIT_OK
        out = capsys.readouterr().out
        assert "match" in out
        assert "bsb1" in out


class TestRunSearchCliCsv:
    def _write_csv(self, tmp_path: Path) -> str:
        csv_path = tmp_path / "works.csv"
        csv_path.write_text(
            "entry_id,short_title,main_author\n"
            "E1,First Book,Author One\n"
            "E2,Second Book,\n"
            "E3,Third Book,Author Three\n",
            encoding="utf-8",
        )
        return str(csv_path)

    def _args(self, argv: list[str]) -> Any:
        return create_cli_parser().parse_args(argv)

    def test_missing_csv_is_usage_error(self) -> None:
        args = self._args(["--search-only"])
        assert run_search_cli(args, {}, logger) == EXIT_USAGE

    def test_nonexistent_csv_is_usage_error(self) -> None:
        args = self._args(["no_such_file.csv", "--search-only"])
        assert run_search_cli(args, {}, logger) == EXIT_USAGE

    @patch("main.cli.commands.search.pipeline.search_work")
    def test_all_rows_searched(
        self, mock_search: MagicMock, tmp_path: Path, capsys: Any
    ) -> None:
        mock_search.side_effect = lambda title, creator, entry_id: {
            "entry_id": entry_id,
            "query": {"title": title, "creator": creator},
            "status": "match",
            "selected": {},
            "candidates": [],
        }
        args = self._args([self._write_csv(tmp_path), "--search-only", "--json"])
        assert run_search_cli(args, {}, logger) == EXIT_OK
        assert mock_search.call_count == 3
        # Missing creator cell must arrive as None, not NaN.
        assert mock_search.call_args_list[1].args[1] is None
        out_lines = [
            line for line in capsys.readouterr().out.splitlines() if line.strip()
        ]
        assert len(out_lines) == 3
        assert json.loads(out_lines[0])["entry_id"] == "E1"

    @patch("main.cli.commands.search.pipeline.search_work")
    def test_entry_ids_and_limit_filters(
        self, mock_search: MagicMock, tmp_path: Path
    ) -> None:
        mock_search.return_value = {
            "entry_id": "E1",
            "query": {"title": "First Book", "creator": "Author One"},
            "status": "match",
            "selected": {},
            "candidates": [],
        }
        args = self._args(
            [
                self._write_csv(tmp_path),
                "--search-only",
                "--entry-ids",
                "E1,E3",
                "--limit",
                "1",
            ]
        )
        assert run_search_cli(args, {}, logger) == EXIT_OK
        assert mock_search.call_count == 1
        assert mock_search.call_args.args[0] == "First Book"

    @patch("main.cli.commands.search.pipeline.search_work")
    def test_partial_match_exits_one(
        self, mock_search: MagicMock, tmp_path: Path
    ) -> None:
        results = iter(["match", "no_match", "match"])
        mock_search.side_effect = lambda title, creator, entry_id: {
            "entry_id": entry_id,
            "query": {"title": title, "creator": creator},
            "status": next(results),
            "selected": None,
            "candidates": [],
        }
        args = self._args([self._write_csv(tmp_path), "--search-only", "--json"])
        assert run_search_cli(args, {}, logger) == EXIT_FAILURES


class TestDispatchRouting:
    @patch("main.cli.dispatch.run_search_cli", return_value=0)
    @patch("main.cli.dispatch._apply_runtime_config_overrides")
    @patch("main.cli.dispatch.pipeline")
    def test_search_routes_to_search_handler(
        self,
        mock_pipeline: MagicMock,
        mock_overrides: MagicMock,
        mock_search_cli: MagicMock,
    ) -> None:
        from main.cli.dispatch import run_cli

        mock_overrides.side_effect = lambda args, config, log: config
        provider = ("internet_archive", MagicMock(), MagicMock(), "Internet Archive")
        mock_pipeline.load_enabled_apis.return_value = [provider]
        mock_pipeline.filter_enabled_providers_for_keys.side_effect = lambda p: p

        args = create_cli_parser().parse_args(["--search", "Test Book"])
        assert run_cli(args, {}) == 0
        mock_search_cli.assert_called_once()

    @patch("main.cli.dispatch.run_search_cli", return_value=0)
    @patch("main.cli.dispatch._apply_runtime_config_overrides")
    @patch("main.cli.dispatch.pipeline")
    def test_search_only_routes_to_search_handler(
        self,
        mock_pipeline: MagicMock,
        mock_overrides: MagicMock,
        mock_search_cli: MagicMock,
    ) -> None:
        from main.cli.dispatch import run_cli

        mock_overrides.side_effect = lambda args, config, log: config
        provider = ("internet_archive", MagicMock(), MagicMock(), "Internet Archive")
        mock_pipeline.load_enabled_apis.return_value = [provider]
        mock_pipeline.filter_enabled_providers_for_keys.side_effect = lambda p: p

        args = create_cli_parser().parse_args(["works.csv", "--search-only"])
        assert run_cli(args, {}) == 0
        mock_search_cli.assert_called_once()
