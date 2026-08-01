"""Regression guard for the unified configuration defaults.

Every knob below has exactly one authoritative default, declared as a constant
in ``api.core.config`` (or, for provider-specific caps, in the provider
module). Two things used to drift apart and are pinned here:

1. The value a consumer falls back to when the key is absent from the loaded
   configuration must equal that constant -- no module may re-spell it as a
   literal, and no unreachable second fallback may survive.
2. The tracked templates must either match the constant or carry an explicit
   ``_..._note`` key explaining why they deliberately differ.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from api.core.budget import DownloadBudget
from api.core.config import (
    DEFAULT_KEEP_NON_SELECTED_METADATA,
    DEFAULT_MAX_PARALLEL_DOWNLOADS,
    DEFAULT_MAX_PARALLEL_SEARCHES,
    DEFAULT_MAX_RENDERINGS_PER_MANIFEST,
    DEFAULT_MIN_TITLE_SCORE,
    DEFAULT_ON_EXCEED,
    DEFAULT_RESUME_MODE,
    DEFAULT_SEARCH_TIMEOUT_SECONDS,
    DEFAULT_YEAR_TOLERANCE,
    get_download_config,
    get_min_title_score,
    get_resume_mode,
    get_search_timeout,
    get_year_tolerance,
    resolve_max_parallel_downloads,
)
from api.providers.google_books import DEFAULT_MAX_FILES, _gb_max_files

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestDocumentedDefaultValues:
    """The constants themselves, pinned to the values the README documents."""

    def test_constant_values(self) -> None:
        assert DEFAULT_MIN_TITLE_SCORE == 85.0
        assert DEFAULT_MAX_RENDERINGS_PER_MANIFEST == 1
        assert DEFAULT_RESUME_MODE == "skip_completed"
        assert DEFAULT_KEEP_NON_SELECTED_METADATA is False
        assert DEFAULT_ON_EXCEED == "stop"
        assert DEFAULT_MAX_PARALLEL_DOWNLOADS == 1
        assert DEFAULT_MAX_PARALLEL_SEARCHES == 1
        assert DEFAULT_SEARCH_TIMEOUT_SECONDS == 60.0
        assert DEFAULT_YEAR_TOLERANCE == 15
        assert DEFAULT_MAX_FILES == 2


class TestConfigAbsentFallbacks:
    """With no configuration at all, every knob resolves to its constant."""

    def test_min_title_score(self) -> None:
        with patch("api.core.config.get_config", return_value={}):
            assert get_min_title_score() == DEFAULT_MIN_TITLE_SCORE
            assert get_min_title_score("mdz") == DEFAULT_MIN_TITLE_SCORE

    def test_min_title_score_has_a_single_fallback(self) -> None:
        """The signature default is the constant, not a second, looser value.

        ``get_min_title_score`` used to declare ``default=50.0`` while every
        production caller passed 85, leaving a fallback that could never fire
        but contradicted the documented default.
        """
        param = inspect.signature(get_min_title_score).parameters["default"]
        assert param.default == DEFAULT_MIN_TITLE_SCORE

    def test_max_renderings_per_manifest(self) -> None:
        with patch("api.core.config.get_config", return_value={}):
            dl = get_download_config()
            assert dl["max_renderings_per_manifest"] == (
                DEFAULT_MAX_RENDERINGS_PER_MANIFEST
            )

    def test_resume_mode(self) -> None:
        with patch("api.core.config.get_config", return_value={}):
            assert get_resume_mode() == DEFAULT_RESUME_MODE

    def test_search_timeout(self) -> None:
        with patch("api.core.config.get_config", return_value={}):
            assert get_search_timeout() == DEFAULT_SEARCH_TIMEOUT_SECONDS

    def test_year_tolerance(self) -> None:
        with patch("api.core.config.get_config", return_value={}):
            assert get_year_tolerance() == DEFAULT_YEAR_TOLERANCE

    def test_on_exceed(self) -> None:
        with patch("api.core.config.get_config", return_value={}):
            assert DownloadBudget()._policy() == DEFAULT_ON_EXCEED

    def test_on_exceed_in_resolved_limits(self) -> None:
        """resolve_limits must not carry its own divergent fallback."""
        with patch("api.core.config.get_config", return_value={}):
            limits = DownloadBudget().resolve_limits("pdfs", "work_1")
            assert limits.policy == DEFAULT_ON_EXCEED

    def test_google_books_max_files(self) -> None:
        with patch("api.core.config.get_config", return_value={}):
            assert _gb_max_files() == DEFAULT_MAX_FILES

    def test_selection_defaults(self) -> None:
        from main.orchestration.pipeline import _get_selection_config

        with patch("main.orchestration.pipeline.get_config", return_value={}):
            sel = _get_selection_config()

        assert sel["min_title_score"] == DEFAULT_MIN_TITLE_SCORE
        assert sel["keep_non_selected_metadata"] is DEFAULT_KEEP_NON_SELECTED_METADATA
        assert sel["max_parallel_searches"] == DEFAULT_MAX_PARALLEL_SEARCHES
        assert sel["search_timeout_seconds"] == DEFAULT_SEARCH_TIMEOUT_SECONDS
        assert sel["year_tolerance"] == DEFAULT_YEAR_TOLERANCE

    def test_max_parallel_searches(self) -> None:
        from main.orchestration.selection import _get_max_parallel_searches

        with patch("main.orchestration.selection.get_config", return_value={}):
            assert _get_max_parallel_searches() == DEFAULT_MAX_PARALLEL_SEARCHES

    def test_max_parallel_downloads(self) -> None:
        from main.orchestration.scheduler import get_parallel_download_config

        with patch("main.orchestration.scheduler.get_config", return_value={}):
            dl = get_parallel_download_config()

        assert dl["max_parallel_downloads"] == DEFAULT_MAX_PARALLEL_DOWNLOADS


class TestResolveMaxParallelDownloads:
    """One helper backs every worker-count read path.

    ``_run_parallel`` used to coerce a falsy value to 4 while the batch entry
    point coerced the same value to 1, so a single configuration could yield
    two different worker counts.
    """

    def test_absent_key(self) -> None:
        assert resolve_max_parallel_downloads({}) == DEFAULT_MAX_PARALLEL_DOWNLOADS

    def test_zero_means_sequential(self) -> None:
        cfg = {"max_parallel_downloads": 0}
        assert resolve_max_parallel_downloads(cfg) == DEFAULT_MAX_PARALLEL_DOWNLOADS

    def test_negative_is_clamped(self) -> None:
        assert resolve_max_parallel_downloads({"max_parallel_downloads": -3}) == 1

    def test_non_numeric_falls_back(self) -> None:
        cfg = {"max_parallel_downloads": "many"}
        assert resolve_max_parallel_downloads(cfg) == DEFAULT_MAX_PARALLEL_DOWNLOADS

    def test_configured_value_is_honored(self) -> None:
        assert resolve_max_parallel_downloads({"max_parallel_downloads": 3}) == 3

    def test_override_wins(self) -> None:
        cfg = {"max_parallel_downloads": 3}
        assert resolve_max_parallel_downloads(cfg, override=5) == 5

    def test_falsy_override_defers_to_config(self) -> None:
        cfg = {"max_parallel_downloads": 3}
        assert resolve_max_parallel_downloads(cfg, override=0) == 3


def _load_template(name: str) -> dict[str, Any]:
    path = REPO_ROOT / name
    if not path.exists():  # pragma: no cover - template is tracked
        pytest.skip(f"{name} not present")
    with open(path, encoding="utf-8") as fh:
        return dict(json.load(fh))


class TestTrackedTemplatesMatchDefaults:
    """config.example.json / config_small.json must not silently re-drift."""

    def test_example_template_matches_defaults(self) -> None:
        cfg = _load_template("config.example.json")
        dl = cfg["download"]
        sel = cfg["selection"]

        assert dl["resume_mode"] == DEFAULT_RESUME_MODE
        assert dl["max_renderings_per_manifest"] == (
            DEFAULT_MAX_RENDERINGS_PER_MANIFEST
        )
        assert cfg["download_limits"]["on_exceed"] == DEFAULT_ON_EXCEED
        assert sel["keep_non_selected_metadata"] is (DEFAULT_KEEP_NON_SELECTED_METADATA)
        assert cfg["provider_settings"]["google_books"]["max_files"] == (
            DEFAULT_MAX_FILES
        )

    def test_example_template_deviations_are_documented(self) -> None:
        """The three intentional deviations each carry an explanatory note."""
        cfg = _load_template("config.example.json")
        dl = cfg["download"]
        sel = cfg["selection"]

        assert sel["min_title_score"] != DEFAULT_MIN_TITLE_SCORE
        assert "_min_title_score_note" in sel
        assert dl["max_parallel_downloads"] != DEFAULT_MAX_PARALLEL_DOWNLOADS
        assert "_max_parallel_downloads_note" in dl
        assert sel["max_parallel_searches"] != DEFAULT_MAX_PARALLEL_SEARCHES
        assert "_max_parallel_searches_note" in sel

    def test_small_template_matches_defaults(self) -> None:
        cfg = _load_template("config_small.json")

        assert cfg["download"]["max_renderings_per_manifest"] == (
            DEFAULT_MAX_RENDERINGS_PER_MANIFEST
        )
        assert cfg["download_limits"]["on_exceed"] == DEFAULT_ON_EXCEED
        assert cfg["selection"]["keep_non_selected_metadata"] is (
            DEFAULT_KEEP_NON_SELECTED_METADATA
        )
