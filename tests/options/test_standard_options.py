"""The shared option rows take each tool's values and build the same rows.

Ported from ChronoTranscriber v1.0.0 ``tests/common/test_standard_options.py``;
the tests of the removed model, image, batch and render rows and of their
converters are left out, and the table test builds the kept rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chronodownloader.options.options import Kind, OptionTable
from chronodownloader.options.standard_options import (
    concurrency_option,
    dry_run_option,
    force_option,
    json_option,
    positive_int,
    settings_option,
)


def test_run_rows_build_a_valid_table() -> None:
    table = OptionTable(
        [
            force_option("download again"),
            concurrency_option(4),
            dry_run_option("dry_run"),
            json_option("json"),
            settings_option(target="settings"),
        ]
    )
    assert table.names == ("force", "concurrency", "dry_run", "json", "settings")
    assert (table["force"].kind, table["force"].settable) == (Kind.FLAG, False)
    assert table["concurrency"].default == 4
    assert table["settings"].target == "settings"
    assert table.parse(["--settings", "s.yaml", "--concurrency", "2"]) == {
        "settings": Path("s.yaml"),
        "concurrency": 2,
    }


@pytest.mark.parametrize(("convert", "raw", "expected"), [(positive_int, "3", 3)])
def test_converters_accept(convert: object, raw: str, expected: object) -> None:
    assert convert(raw) == expected  # type: ignore[operator]


@pytest.mark.parametrize(("convert", "raw"), [(positive_int, "0")])
def test_converters_reject(convert: object, raw: str) -> None:
    with pytest.raises(ValueError):
        convert(raw)  # type: ignore[operator]
