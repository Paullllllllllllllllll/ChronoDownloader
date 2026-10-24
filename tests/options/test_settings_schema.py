"""Typed reads of the settings schema."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from chronodownloader.options.settings_loader import SettingsError
from chronodownloader.options.settings_schema import Reader


def _reader() -> Reader:
    return Reader("settings.yaml", Path("."))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_number_must_be_finite(value: float) -> None:
    with pytest.raises(SettingsError, match="network.timeout_s: must be a finite"):
        _reader().number({"timeout_s": value}, "timeout_s", 30.0, "network.")


@pytest.mark.parametrize("text", [".nan", ".inf", "-.inf"])
def test_yaml_infinities_and_nan_are_refused(text: str) -> None:
    raw = yaml.safe_load(f"timeout_s: {text}\n")
    with pytest.raises(SettingsError):
        _reader().number(raw, "timeout_s", 30.0, "")


def test_a_finite_number_is_read_as_float() -> None:
    assert _reader().number({"timeout_s": 45}, "timeout_s", 30.0, "") == 45.0
