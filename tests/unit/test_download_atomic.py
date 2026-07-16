"""Regression tests for atomic download behavior (audit B1).

These exercise the production ``download_file`` streaming path with a mocked
HTTP session (no live network). On the pre-fix code a mid-stream abort or a
short read left a partial file at the FINAL path, which a later resume run
treated as a complete download. The atomic ``.part`` -> ``os.replace`` path
must leave nothing behind on failure.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import requests

from api.core import download as dl_mod


def _make_session(response: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=response)
    cm.__exit__ = MagicMock(return_value=False)
    session = MagicMock()
    session.get.return_value = cm
    return session


def _make_response(headers: dict[str, str], iter_content: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = headers
    resp.iter_content = iter_content
    resp.raise_for_status = MagicMock()
    return resp


def _objects_files(folder: str) -> list[str]:
    objects = os.path.join(folder, "objects")
    if not os.path.isdir(objects):
        return []
    return sorted(os.listdir(objects))


def test_midstream_abort_leaves_no_file(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """A connection drop mid-stream leaves neither final nor .part file."""

    def broken_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield b"%PDF-1.4 partial data that will never complete"
        raise requests.exceptions.ConnectionError("connection dropped mid-stream")

    resp = _make_response({"Content-Type": "application/pdf"}, broken_iter)
    session = _make_session(resp)
    folder = str(tmp_path / "work")

    dl_mod._BUDGET._exhausted = False
    with patch.object(dl_mod, "get_session", return_value=session):
        result = dl_mod.download_file("https://example.org/book.pdf", folder, "book")

    assert result is None
    # No file (and crucially no leftover .part) at the final location.
    assert _objects_files(folder) == []


def test_content_length_short_read_discarded(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """A stream shorter than the declared Content-Length is discarded."""

    def short_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield b"%PDF-1.4 only-part-of-the-file"

    resp = _make_response(
        {"Content-Type": "application/pdf", "Content-Length": "100000"},
        short_iter,
    )
    session = _make_session(resp)
    folder = str(tmp_path / "work")

    dl_mod._BUDGET._exhausted = False
    with patch.object(dl_mod, "get_session", return_value=session):
        result = dl_mod.download_file("https://example.org/book.pdf", folder, "book")

    assert result is None
    assert _objects_files(folder) == []


def test_content_encoded_stream_not_discarded_on_length_mismatch(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """A gzip-encoded response must not be discarded as 'incomplete'.

    ``iter_content`` yields DECODED bytes while Content-Length counts the
    encoded wire bytes, so the byte counts legitimately differ; the
    completeness check applies only to identity-encoded responses.
    """
    payload = b"%PDF-1.4\n" + b"x" * 1024  # decoded size 1033 != wire size 500

    def good_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield payload

    resp = _make_response(
        {
            "Content-Type": "application/pdf",
            "Content-Length": "500",
            "Content-Encoding": "gzip",
        },
        good_iter,
    )
    session = _make_session(resp)
    folder = str(tmp_path / "work")

    dl_mod._BUDGET._exhausted = False
    with patch.object(dl_mod, "get_session", return_value=session):
        result = dl_mod.download_file("https://example.org/book.pdf", folder, "book")

    assert result is not None
    with open(result, "rb") as fh:
        assert fh.read() == payload


def test_complete_download_promoted_atomically(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """A complete, valid PDF stream lands at the final path (no .part)."""
    payload = b"%PDF-1.4\n" + b"x" * 1024

    def good_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield payload

    resp = _make_response(
        {"Content-Type": "application/pdf", "Content-Length": str(len(payload))},
        good_iter,
    )
    session = _make_session(resp)
    folder = str(tmp_path / "work")

    dl_mod._BUDGET._exhausted = False
    with patch.object(dl_mod, "get_session", return_value=session):
        result = dl_mod.download_file("https://example.org/book.pdf", folder, "book")

    assert result is not None
    files = _objects_files(folder)
    assert len(files) == 1
    assert not files[0].endswith(".part")
    with open(result, "rb") as fh:
        assert fh.read() == payload
