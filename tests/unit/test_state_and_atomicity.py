"""Regression tests for atomic persistence and state-file relocation.

B5: state file and works CSV were rewritten in place (a crash mid-write
    corrupted them); a corrupt state file was silently discarded, resetting
    quota counters. Now writes are atomic (temp + os.replace), the corrupt
    file is preserved, and the works CSV is backed up once per run.
B7: a non-PDF, non-HTML body was accepted as a valid ``.pdf``.
Decision 4: the state file lives in a user-level directory with a config
    override and one-time legacy CWD adoption.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd


class TestAtomicWrites:
    def test_atomic_write_text_replaces_file(self, tmp_path: Path) -> None:
        from api.core.atomic import atomic_write_text

        target = tmp_path / "out.txt"
        target.write_text("old", encoding="utf-8")
        atomic_write_text(str(target), "new content")
        assert target.read_text(encoding="utf-8") == "new content"
        # No temp litter left behind
        assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]

    def test_atomic_write_failure_leaves_original(self, tmp_path: Path) -> None:
        from api.core.atomic import atomic_write_json

        target = tmp_path / "state.json"
        target.write_text('{"ok": true}', encoding="utf-8")

        class Unserializable:
            pass

        import contextlib

        with contextlib.suppress(TypeError):
            atomic_write_json(str(target), {"bad": Unserializable()})
        # Original content is untouched by the failed write.
        assert target.read_text(encoding="utf-8") == '{"ok": true}'

    def test_atomic_replace_retries_on_permission_error(self, tmp_path: Path) -> None:
        """A transient PermissionError (Windows AV/viewer) must not drop a save."""
        from api.core import atomic

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
            patch("api.core.atomic.os.replace", side_effect=flaky_replace),
            patch("api.core.atomic.time.sleep", return_value=None),
        ):
            atomic.atomic_write_text(str(target), "new content")

        assert calls["n"] == 4
        assert target.read_text(encoding="utf-8") == "new content"
        assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]

    def test_works_csv_save_is_atomic(self, tmp_path: Path) -> None:
        """mark_failed must go through the atomic write helper."""
        from main.data import works_csv

        csv_path = str(tmp_path / "works.csv")
        pd.DataFrame(
            {"entry_id": ["E1"], "short_title": ["T"], "retrievable": [pd.NA]}
        ).to_csv(csv_path, index=False)

        calls: list[str] = []
        from api.core.atomic import atomic_write_text as real_write

        def spy(path: str, data: str, **kw: Any) -> None:
            calls.append(path)
            real_write(path, data, **kw)

        with patch.object(works_csv, "atomic_write_text", side_effect=spy):
            assert works_csv.mark_failed(csv_path, "E1") is True

        assert calls == [csv_path]


class TestCorruptStatePreserved:
    def test_corrupt_state_file_copied_not_discarded(self, tmp_path: Path) -> None:
        from main.state.store import StateManager

        StateManager._instance = None
        try:
            state_file = tmp_path / "state.json"
            state_file.write_text("{ not valid json", encoding="utf-8")

            StateManager(state_file=str(state_file))

            corrupt_copy = state_file.with_suffix(".corrupt")
            assert corrupt_copy.exists()
            assert corrupt_copy.read_text(encoding="utf-8") == "{ not valid json"
        finally:
            StateManager._instance = None


class TestStateFileLocation:
    def test_state_dir_config_override(
        self, tmp_path: Path, mock_config: dict[str, Any]
    ) -> None:
        from main.state import store

        override_dir = tmp_path / "custom_state"
        mock_config["deferred"] = {"state_dir": str(override_dir)}
        with patch.object(store, "DEFAULT_STATE_FILE", ".downloader_state.json"):
            resolved = store.resolve_state_file_path()
        assert resolved == override_dir / ".downloader_state.json"

    def test_legacy_cwd_file_adopted_once(
        self, tmp_path: Path, mock_config: dict[str, Any], monkeypatch: Any
    ) -> None:
        from main.state import store

        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        legacy = cwd / ".downloader_state.json"
        legacy.write_text('{"quotas": {"p": {}}}', encoding="utf-8")

        user_dir = tmp_path / "userhome_state"
        mock_config["deferred"] = {"state_dir": str(user_dir)}
        with patch.object(store, "DEFAULT_STATE_FILE", ".downloader_state.json"):
            resolved = store.resolve_state_file_path()

        assert resolved == user_dir / ".downloader_state.json"
        assert resolved.exists()
        assert '"quotas"' in resolved.read_text(encoding="utf-8")


class TestMagicByteStrictness:
    """B7: .pdf/.epub require correct magic bytes, full stop."""

    def test_garbage_pdf_rejected(self, tmp_path: Path) -> None:
        from api.core.download import _validate_file_magic_bytes

        f = tmp_path / "garbage.pdf"
        f.write_bytes(b"\x00\x01\x02 random non-pdf non-html bytes")
        valid, msg = _validate_file_magic_bytes(str(f), ".pdf")
        assert valid is False
        assert "magic" in msg.lower() or "HTML" in msg

    def test_garbage_epub_rejected(self, tmp_path: Path) -> None:
        from api.core.download import _validate_file_magic_bytes

        f = tmp_path / "garbage.epub"
        f.write_bytes(b"definitely not a zip container")
        valid, _ = _validate_file_magic_bytes(str(f), ".epub")
        assert valid is False

    def test_content_type_preferred_over_url_suffix(
        self, tmp_path: Path, mock_config: dict[str, Any]
    ) -> None:
        """A .pdf URL serving image/jpeg is stored as .jpg, not .pdf."""
        from collections.abc import Iterator
        from unittest.mock import MagicMock

        from api.core import download as dl_mod

        payload = b"\xff\xd8\xff\xe0" + b"j" * 256  # JPEG magic

        def body(chunk_size: int = 8192) -> Iterator[bytes]:
            yield payload

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(payload)),
        }
        resp.iter_content = body
        resp.raise_for_status = MagicMock()
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=resp)
        cm.__exit__ = MagicMock(return_value=False)
        session = MagicMock()
        session.get.return_value = cm

        dl_mod._BUDGET._exhausted = False
        with patch.object(dl_mod, "get_session", return_value=session):
            result = dl_mod.download_file(
                "https://example.org/really-a-jpeg.pdf",
                str(tmp_path / "work"),
                "item",
            )

        assert result is not None
        assert result.endswith(".jpg")
        assert os.path.exists(result)
