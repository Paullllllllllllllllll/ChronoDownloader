"""Tests for net/atomic.py.

Ports ``TestAtomicWrites`` of v1.30.3 ``tests/unit/test_state_and_atomicity.py``
(the works-CSV case is gone with the CSV rewrite).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from unittest.mock import patch

from chronodownloader.net import atomic


def test_a_write_replaces_the_file_and_leaves_no_temporary(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    atomic.atomic_write_text(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_a_failed_write_leaves_the_original(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"ok": true}', encoding="utf-8")

    class Unserializable:
        pass

    with contextlib.suppress(TypeError):
        atomic.atomic_write_json(target, {"bad": Unserializable()})
    assert target.read_text(encoding="utf-8") == '{"ok": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_a_transient_permission_error_is_retried(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src: str, dst: str) -> None:
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError("file in use")
        real_replace(src, dst)

    with (
        patch.object(atomic.os, "replace", side_effect=flaky_replace),
        patch.object(atomic.time, "sleep", return_value=None),
    ):
        atomic.atomic_write_text(target, "new content")

    assert calls["n"] == 4
    assert target.read_text(encoding="utf-8") == "new content"
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]
