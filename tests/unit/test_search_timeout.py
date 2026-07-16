"""Tests for the per-provider search timeout.

Covers the parallel fan-out and both sequential collection paths, timeout
disabling, per-provider override precedence, and the CLI wiring
(``--search-timeout``, runtime override, and CLI auto-detection).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import api.core.config as core_config
from api.model import SearchResult
from main.cli.overrides import (
    _apply_runtime_config_overrides,
    _looks_like_cli_invocation,
)
from main.cli.parser import create_cli_parser
from main.orchestration import selection

logger = logging.getLogger("test_search_timeout")

# Upper bound (seconds) on wall-clock time for a fan-out that must NOT wait for
# the ~2s slow provider. Generous enough to avoid flakiness on a loaded box.
FAST_LIMIT_S = 1.5


def _download_stub(*_args: Any, **_kwargs: Any) -> bool:
    return True


def _fast_search(
    title: str, creator: str | None = None, max_results: int = 5
) -> list[SearchResult]:
    return [SearchResult(provider="Fast", title=title, source_id="fast1")]


def _slow_search(
    title: str, creator: str | None = None, max_results: int = 5
) -> list[SearchResult]:
    time.sleep(2.0)
    return [SearchResult(provider="Slow", title=title, source_id="slow1")]


def _briefly_slow_search(
    title: str, creator: str | None = None, max_results: int = 5
) -> list[SearchResult]:
    time.sleep(0.3)
    return [SearchResult(provider="Slow", title=title, source_id="slow1")]


def _providers(
    slow_func: Any = _slow_search, slow_first: bool = False
) -> list[selection.ProviderTuple]:
    fast = ("fast", _fast_search, _download_stub, "Fast")
    slow = ("slow", slow_func, _download_stub, "Slow")
    return [slow, fast] if slow_first else [fast, slow]


@pytest.fixture(autouse=True)
def _restore_config_cache() -> Iterator[None]:
    """Snapshot and restore the module-level config cache around each test."""
    saved = core_config._CONFIG_CACHE
    try:
        yield
    finally:
        core_config._CONFIG_CACHE = saved


def _set_config(cfg: dict[str, Any]) -> None:
    core_config._CONFIG_CACHE = cfg


class TestParallelTimeout:
    def test_slow_provider_dropped_fast_kept(self) -> None:
        _set_config({"selection": {"search_timeout_seconds": 0.2}})
        start = time.perf_counter()
        candidates = selection._collect_candidates_parallel(
            _providers(), "Book", None, 0.0, 5, 4
        )
        elapsed = time.perf_counter() - start

        keys = {c.provider_key for c in candidates}
        assert "fast" in keys
        assert "slow" not in keys
        assert elapsed < FAST_LIMIT_S

    def test_disabled_includes_slow(self) -> None:
        _set_config({"selection": {"search_timeout_seconds": 0}})
        candidates = selection._collect_candidates_parallel(
            _providers(slow_func=_briefly_slow_search), "Book", None, 0.0, 5, 4
        )
        keys = {c.provider_key for c in candidates}
        assert "fast" in keys
        assert "slow" in keys

    def test_per_provider_override_beats_global(self) -> None:
        # Global is generous (5s) but the slow provider is capped at 0.2s.
        _set_config(
            {
                "selection": {"search_timeout_seconds": 5.0},
                "provider_settings": {"slow": {"search_timeout_seconds": 0.2}},
            }
        )
        start = time.perf_counter()
        candidates = selection._collect_candidates_parallel(
            _providers(), "Book", None, 0.0, 5, 4
        )
        elapsed = time.perf_counter() - start

        keys = {c.provider_key for c in candidates}
        assert "fast" in keys
        assert "slow" not in keys
        assert elapsed < FAST_LIMIT_S


class TestSequentialExhaustiveTimeout:
    def test_slow_provider_dropped_fast_kept(self) -> None:
        _set_config({"selection": {"search_timeout_seconds": 0.2}})
        start = time.perf_counter()
        candidates = selection._collect_candidates_exhaustive(
            _providers(), "Book", None, 0.0, 5
        )
        elapsed = time.perf_counter() - start

        keys = {c.provider_key for c in candidates}
        assert "fast" in keys
        assert "slow" not in keys
        assert elapsed < FAST_LIMIT_S

    def test_disabled_includes_slow(self) -> None:
        _set_config({"selection": {"search_timeout_seconds": None}})
        candidates = selection._collect_candidates_exhaustive(
            _providers(slow_func=_briefly_slow_search), "Book", None, 0.0, 5
        )
        keys = {c.provider_key for c in candidates}
        assert "slow" in keys


class TestSequentialFirstHitTimeout:
    def test_slow_first_times_out_then_fast_selected(self) -> None:
        _set_config({"selection": {"search_timeout_seconds": 0.2}})
        start = time.perf_counter()
        all_candidates, selected, selected_tuple = (
            selection.collect_candidates_sequential(
                _providers(slow_first=True), "Book", None, 10.0, 0.0, 5
            )
        )
        elapsed = time.perf_counter() - start

        assert selected is not None
        assert selected.provider_key == "fast"
        assert selected_tuple is not None and selected_tuple[0] == "fast"
        assert {c.provider_key for c in all_candidates} == {"fast"}
        assert elapsed < FAST_LIMIT_S


class TestConfigDrivenResolution:
    """Timeout is resolved live from config, with no CLI plumbing involved.

    Interactive mode routes through the same pipeline and ``get_config()``, so a
    config-file value alone must be honored. These tests set only the config
    cache (exactly what an interactive run with a chosen config file yields).
    """

    def test_global_config_value_honored_by_fan_out(self) -> None:
        _set_config({"selection": {"search_timeout_seconds": 0.2}})
        start = time.perf_counter()
        candidates = selection._collect_candidates_parallel(
            _providers(), "Book", None, 0.0, 5, 4
        )
        elapsed = time.perf_counter() - start
        assert {c.provider_key for c in candidates} == {"fast"}
        assert elapsed < FAST_LIMIT_S

    def test_resolver_reads_config_alone(self) -> None:
        _set_config(
            {
                "selection": {"search_timeout_seconds": 12.5},
                "provider_settings": {"slow": {"search_timeout_seconds": 0.5}},
            }
        )
        # Global value.
        assert core_config.get_search_timeout() == 12.5
        assert core_config.get_search_timeout("fast") == 12.5
        # Per-provider override beats the global value.
        assert core_config.get_search_timeout("slow") == 0.5

    def test_zero_and_null_disable(self) -> None:
        _set_config({"selection": {"search_timeout_seconds": 0}})
        assert core_config.get_search_timeout() is None
        _set_config({"selection": {"search_timeout_seconds": None}})
        assert core_config.get_search_timeout() is None

    def test_default_when_absent(self) -> None:
        _set_config({"selection": {}})
        assert (
            core_config.get_search_timeout()
            == core_config.DEFAULT_SEARCH_TIMEOUT_SECONDS
        )


_REPO_ROOT = Path(__file__).resolve().parents[2]

# Shared preamble: a provider list with one search that stalls for 30s and one
# that is instant, a 0.5s timeout in the config cache, and parallel workers.
_SUBPROCESS_PREAMBLE = """
import time
import api.core.config as cc
cc._CONFIG_CACHE = {"selection": {"search_timeout_seconds": 0.5}}
from api.model import SearchResult
from main.orchestration import selection

def fast(title, creator=None, max_results=5):
    return [SearchResult(provider="Fast", title=title, source_id="f")]

def slow(title, creator=None, max_results=5):
    time.sleep(30)
    return [SearchResult(provider="Slow", title=title, source_id="s")]

def dl(*a, **k):
    return True

providers = [
    ("slow", slow, dl, "Slow"),
    ("fast", fast, dl, "Fast"),
]
"""


def _run_subprocess_script(body: str) -> subprocess.CompletedProcess[str]:
    """Run a script body in a fresh interpreter rooted at the repo.

    A 15s subprocess timeout is the assertion: without daemon threads the
    stalled 30s search would pin interpreter exit and blow the timeout.
    """
    script = _SUBPROCESS_PREAMBLE + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )


class TestProcessExitNotBlocked:
    """Regression: a stalled search must not pin the process at exit.

    Reproduces the live defect where non-daemon executor threads were joined
    at interpreter shutdown. With daemon threads the process exits promptly.
    """

    def test_parallel_fan_out_exits(self) -> None:
        proc = _run_subprocess_script(
            """
            cands = selection._collect_candidates_parallel(
                providers, "Book", None, 0.0, 5, 2
            )
            keys = sorted(c.provider_key for c in cands)
            print("SENTINEL_DONE", keys)
            """
        )
        assert proc.returncode == 0, proc.stderr
        assert "SENTINEL_DONE ['fast']" in proc.stdout

    def test_sequential_run_with_timeout_exits(self) -> None:
        proc = _run_subprocess_script(
            """
            cands = selection._collect_candidates_exhaustive(
                providers, "Book", None, 0.0, 5
            )
            keys = sorted(c.provider_key for c in cands)
            print("SENTINEL_DONE", keys)
            """
        )
        assert proc.returncode == 0, proc.stderr
        assert "SENTINEL_DONE ['fast']" in proc.stdout


class TestCliWiring:
    def test_parser_accepts_search_timeout(self) -> None:
        args = create_cli_parser().parse_args(["--search-timeout", "5"])
        assert args.search_timeout == 5.0

    def test_override_written_into_selection(self) -> None:
        args = create_cli_parser().parse_args(["--search-timeout", "5"])
        merged = _apply_runtime_config_overrides(args, {}, logger)
        assert merged["selection"]["search_timeout_seconds"] == 5.0

    def test_detected_as_cli_invocation(self) -> None:
        assert _looks_like_cli_invocation(["--search-timeout", "5"]) is True
