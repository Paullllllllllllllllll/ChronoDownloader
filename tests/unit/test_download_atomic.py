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

import pytest
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


def test_write_failure_refunds_all_booked_bytes(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """An OSError from the file write refunds every budget-booked chunk.

    Each chunk is booked via ``add_bytes`` before it is written; when the
    write itself fails, the just-booked chunk must be included in the refund
    (pre-fix, ``bytes_written`` was incremented only after a successful
    write, leaking the failing chunk into the budget).
    """
    payload = b"%PDF-1.4\n" + b"x" * 1024

    def good_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield payload

    resp = _make_response({"Content-Type": "application/pdf"}, good_iter)
    session = _make_session(resp)
    folder = str(tmp_path / "work")

    failing_file = MagicMock()
    failing_file.write.side_effect = OSError("disk full")
    open_cm = MagicMock()
    open_cm.__enter__ = MagicMock(return_value=failing_file)
    open_cm.__exit__ = MagicMock(return_value=False)

    dl_mod._BUDGET._exhausted = False
    before = dl_mod._BUDGET.total_pdfs_bytes
    with (
        patch.object(dl_mod, "get_session", return_value=session),
        patch("api.core.download.open", return_value=open_cm, create=True),
    ):
        result = dl_mod.download_file("https://example.org/book.pdf", folder, "book")

    assert result is None
    assert dl_mod._BUDGET.total_pdfs_bytes == before
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


def test_transient_connection_error_retried(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """Connection errors on the initial GET are retried up to max_attempts.

    Pre-fix, the try/except wrapped the whole retry loop, so the first
    ConnectionError aborted the download and the configured max_attempts
    never applied to the download path.
    """
    payload = b"%PDF-1.4\n" + b"x" * 1024

    def good_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield payload

    resp = _make_response(
        {"Content-Type": "application/pdf", "Content-Length": str(len(payload))},
        good_iter,
    )
    good_cm = MagicMock()
    good_cm.__enter__ = MagicMock(return_value=resp)
    good_cm.__exit__ = MagicMock(return_value=False)

    session = MagicMock()
    session.get.side_effect = [
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.Timeout("timed out"),
        good_cm,
    ]

    folder = str(tmp_path / "work")
    dl_mod._BUDGET._exhausted = False
    with (
        patch.object(dl_mod, "get_session", return_value=session),
        patch("api.core.download.time.sleep"),
    ):
        result = dl_mod.download_file("https://example.org/book.pdf", folder, "book")

    assert result is not None
    assert session.get.call_count == 3
    files = _objects_files(folder)
    assert len(files) == 1


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


def test_keyboard_interrupt_midstream_leaves_no_file(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """Ctrl-C mid-stream discards the .part file and refunds booked bytes.

    KeyboardInterrupt derives from BaseException, bypassing the
    RequestException/OSError handler above it; the ``except BaseException``
    clause must still call ``_discard_partial()`` before re-raising, or the
    ``.part`` file survives inside ``objects/`` and a later
    ``skip_if_has_objects`` resume run skips the work forever on the strength
    of a partial file holding no usable content.
    """

    def interrupted_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield b"%PDF-1.4 partial data before the interrupt"
        raise KeyboardInterrupt()

    resp = _make_response({"Content-Type": "application/pdf"}, interrupted_iter)
    session = _make_session(resp)
    folder = str(tmp_path / "work")

    dl_mod._BUDGET._exhausted = False
    before = dl_mod._BUDGET.total_pdfs_bytes
    with (
        patch.object(dl_mod, "get_session", return_value=session),
        pytest.raises(KeyboardInterrupt),
    ):
        dl_mod.download_file("https://example.org/book.pdf", folder, "book")

    # No leftover .part file and no promoted object file at the final path.
    assert _objects_files(folder) == []
    # The bytes booked into the budget before the interrupt are refunded.
    assert dl_mod._BUDGET.total_pdfs_bytes == before


def test_non_retryable_status_records_success_on_breaker(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """A 404 during download_file must record success on the breaker.

    Mirrors the make_request fix: the server answered, so the transport is
    healthy. Without recording success here, a half-open probe spent on this
    dead URL would leave the breaker stuck HALF_OPEN and throttle a working
    provider to one request per cooldown.
    """
    resp = MagicMock()
    resp.status_code = 404
    resp.headers = {}

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)
    session = MagicMock()
    session.get.return_value = cm

    mock_cb = MagicMock()
    mock_cb.allow_request.return_value = True

    folder = str(tmp_path / "work")
    dl_mod._BUDGET._exhausted = False
    with (
        patch.object(dl_mod, "get_session", return_value=session),
        patch.object(dl_mod, "get_circuit_breaker", return_value=mock_cb),
    ):
        result = dl_mod.download_file("https://example.org/missing.pdf", folder, "book")

    assert result is None
    mock_cb.record_success.assert_called_once()
