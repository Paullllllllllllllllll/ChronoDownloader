"""Resolution of the option tables into specs, and the equivalent command."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from chronodownloader.cli.parser import build_parser
from chronodownloader.options.options import Source
from chronodownloader.spec import (
    DOWNLOAD_OPTIONS,
    TABLES,
    Resolution,
    SpecError,
    effective_spec,
    resolve_download,
    resolve_drop,
    resolve_fetch,
    resolve_inspect,
    resolve_providers,
    resolve_search,
    resolve_status,
    resolve_ui,
    resolve_verify,
    to_argv,
)

RESOLVERS: Mapping[str, Callable[..., Resolution[Any]]] = {
    "download": resolve_download,
    "fetch": resolve_fetch,
    "search": resolve_search,
    "inspect": resolve_inspect,
    "verify": resolve_verify,
    "status": resolve_status,
    "drop": resolve_drop,
    "providers": resolve_providers,
    "ui": resolve_ui,
}


def _flags(argv: list[str]) -> dict[str, Any]:
    """Parse a full command line with the real parser."""
    namespace = build_parser().parse_args(argv)
    return TABLES[argv[0]].values(namespace)


def _resolve(argv: list[str], defaults: Mapping[str, Any] | None = None) -> Any:
    return RESOLVERS[argv[0]](_flags(argv), defaults)


def test_providers_keep_the_given_order() -> None:
    argv = ["download", "--input", "w.csv", "--providers", "polona,mdz"]
    resolution = _resolve(argv)
    assert resolution.spec.matching.providers == ("polona", "mdz")
    view = effective_spec(resolution.spec, resolution.sources)
    assert view["matching.providers"] == {"value": ["polona", "mdz"], "source": "flag"}


def test_repeated_providers_accumulate_in_order() -> None:
    argv = ["search", "--title", "x", "--providers", "polona", "--providers", "mdz"]
    assert _resolve(argv).spec.matching.providers == ("polona", "mdz")


def test_only_is_ordered_by_status_and_defaults_to_the_open_works() -> None:
    default = _resolve(["download", "--input", "w.csv"]).spec.selection.only
    assert default == ("new", "partial", "failed", "deferred", "blocked")
    given = _resolve(["download", "--input", "w.csv", "--only", "failed,new"])
    assert given.spec.selection.only == ("new", "failed")


def test_the_defaults_block_is_checked_against_the_download_table() -> None:
    defaults = DOWNLOAD_OPTIONS.check_defaults(
        {"output": "store", "providers": ["wellcome", "mdz"], "format": "images"}
    )
    download = _resolve(["download", "--input", "w.csv"], defaults)
    assert download.spec.output == Path("store")
    assert download.spec.files.format == "images"
    assert download.sources["matching.providers"] is Source.SETTINGS
    search = _resolve(["search", "--title", "x"], defaults)
    assert search.spec.output is None
    assert search.spec.matching.providers == ("wellcome", "mdz")
    inspect = _resolve(["inspect", "--provider", "mdz", "--id", "b1"], defaults)
    assert inspect.spec.providers == ("wellcome", "mdz")
    assert inspect.spec.output == Path("store")


SPEC_ERRORS = [
    pytest.param(["download"], id="download-no-input"),
    pytest.param(["download", "--input", "w.csv", "--only", ""], id="empty-only"),
    pytest.param(["fetch", "--url", "u"], id="fetch-no-entry-id"),
    pytest.param(["fetch", "--entry-id", "E", "--provider", "mdz"], id="fetch-no-id"),
    pytest.param(
        ["fetch", "--entry-id", "E", "--url", "u", "--id", "b1"], id="fetch-url-and-id"
    ),
    pytest.param(["search"], id="search-no-query"),
    pytest.param(["search", "--input", "w.csv", "--year", "1651"], id="year-no-title"),
    pytest.param(["search", "--title", "x", "--limit", "3"], id="limit-no-input"),
    pytest.param(["inspect"], id="inspect-nothing"),
    pytest.param(
        ["inspect", "--entry-ids", "E", "--provider", "mdz", "--id", "b"], id="both"
    ),
    pytest.param(["inspect", "--entry-ids", "E"], id="inspect-no-output"),
    pytest.param(["verify", "--first", "3"], id="first-without-pages"),
    pytest.param(["drop", "--all"], id="drop-no-output"),
    pytest.param(["drop", "--output", "o"], id="drop-no-selection"),
]


@pytest.mark.parametrize("argv", SPEC_ERRORS)
def test_invalid_combinations_raise_spec_errors(argv: list[str]) -> None:
    with pytest.raises(SpecError):
        _resolve(argv)


ROUND_TRIPS = [
    ["download", "--input", "w.csv"],
    [
        "download",
        "--input",
        "w.csv",
        "--entry-ids",
        "E2,E1",
        "--limit",
        "3",
        "--only",
        "failed",
        "--research",
        "--matching",
        "fuzzy",
        "--providers",
        "polona,mdz",
        "--search-timeout",
        "0",
        "--format",
        "both",
        "--max-pages",
        "20",
        "--no-include-metadata",
        "--force",
        "--total-pdfs-gb",
        "2.5",
        "--on-exceed",
        "skip",
        "--output",
        "out",
        "--concurrency",
        "1",
        "--dry-run",
        "--json",
    ],
    ["fetch", "--entry-id", "E1", "--provider", "mdz", "--id", "b1", "--reason", "r"],
    ["search", "--title=-x", "--creator", "A", "--year", "1651", "--entry-id", "E"],
    ["search", "--input", "w.csv", "--output", "out", "--max-candidates", "2"],
    ["inspect", "--entry-ids", "E1", "--output", "out", "--first", "4"],
    ["verify", "--pages", "--first", "3", "--dry-run"],
    ["status", "--input", "w.csv", "--only", "needs_review", "--json"],
    ["drop", "--output", "out", "--only", "failed,no_match", "--dry-run"],
    ["providers", "--json"],
    ["ui"],
]

ROUND_TRIP_DEFAULTS = DOWNLOAD_OPTIONS.check_defaults(
    {"matching": "hybrid", "max_pages": 7, "include_metadata": False}
)


@pytest.mark.parametrize("argv", ROUND_TRIPS, ids=[" ".join(a) for a in ROUND_TRIPS])
@pytest.mark.parametrize("defaults", [None, ROUND_TRIP_DEFAULTS], ids=["plain", "set"])
def test_to_argv_reproduces_the_spec(
    argv: list[str], defaults: Mapping[str, Any] | None
) -> None:
    spec = _resolve(argv, defaults).spec
    words = to_argv(spec, defaults)
    assert _resolve([argv[0], *words]).spec == spec
    assert _resolve([argv[0], *words], defaults).spec == spec


def test_to_argv_refuses_a_default_without_spelling() -> None:
    spec = _resolve(["inspect", "--provider", "mdz", "--id", "b1"]).spec
    defaults = DOWNLOAD_OPTIONS.check_defaults({"output": "store"})
    with pytest.raises(SpecError):
        to_argv(spec, defaults)
