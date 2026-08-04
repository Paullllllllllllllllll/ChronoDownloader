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
from api.core.budget import DownloadBudget
from api.core.context import peek_counter
from api.core.naming import get_provider_slug, to_snake_case
from api.core.network import CONNECT_FAILURE_MAX_ATTEMPTS


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
    with (
        patch.object(dl_mod, "get_session", return_value=session),
        patch("api.core.download.time.sleep"),
    ):
        result = dl_mod.download_file("https://example.org/book.pdf", folder, "book")

    assert result is None
    # No file (and crucially no leftover .part) at the final location.
    assert _objects_files(folder) == []


def test_midstream_connection_error_is_retried(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """A drop after raise_for_status must consume an attempt, not the run.

    The mid-stream handler used to return None straight out of
    ``_process_response``, which left ``download_file``'s retry loop
    immediately -- so the configured ``max_attempts`` never applied to the
    normal failure mode of a large PDF.
    """
    payload = b"%PDF-1.4\n" + b"x" * 512

    def broken_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield payload[:64]
        raise requests.exceptions.ConnectionError("connection dropped mid-stream")

    def good_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield payload

    def _as_cm(response: MagicMock) -> MagicMock:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=response)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    session = MagicMock()
    session.get.side_effect = [
        _as_cm(_make_response({"Content-Type": "application/pdf"}, broken_iter)),
        _as_cm(_make_response({"Content-Type": "application/pdf"}, good_iter)),
    ]
    folder = str(tmp_path / "work")

    dl_mod._BUDGET._exhausted = False
    with (
        patch.object(dl_mod, "get_session", return_value=session),
        patch("api.core.download.time.sleep"),
    ):
        result = dl_mod.download_file("https://example.org/book.pdf", folder, "book")

    assert result is not None
    assert session.get.call_count == 2
    # No "_2" suffix: the failed attempt gave its sequence number back, so a
    # retried page cannot leave a gap in the image numbering.
    assert _objects_files(folder) == ["book_unknown.pdf"]


def test_every_discard_gives_the_page_number_back(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """A discard on any path must not spend a page's sequence number.

    Only the mid-stream retry handed the number back, so every other
    discard -- a short read, a validation failure, a budget refusal --
    permanently burned one. The IIIF service loop tries several URLs for the
    SAME page, so one failed candidate pushed the page that did download to
    image_002 and left a phantom gap at image_001.
    """
    payload = b"\xff\xd8\xff\xe0" + b"x" * 512

    def short_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield payload[:16]

    def good_iter(chunk_size: int = 8192) -> Iterator[bytes]:
        yield payload

    def _as_cm(response: MagicMock) -> MagicMock:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=response)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    # First candidate declares more bytes than it delivers, so the download is
    # discarded as incomplete; the second candidate for the same page works.
    session = MagicMock()
    session.get.side_effect = [
        _as_cm(
            _make_response(
                {"Content-Type": "image/jpeg", "Content-Length": str(len(payload))},
                short_iter,
            )
        ),
        _as_cm(_make_response({"Content-Type": "image/jpeg"}, good_iter)),
    ]
    folder = str(tmp_path / "work")

    dl_mod._BUDGET._exhausted = False
    with patch.object(dl_mod, "get_session", return_value=session):
        assert dl_mod.download_file("https://example.org/a.jpg", folder, "page") is None
        assert (
            dl_mod.download_file("https://example.org/b.jpg", folder, "page")
            is not None
        )

    assert _objects_files(folder) == ["page_unknown_image_001.jpg"]


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
    mock_cb.record_failure.assert_not_called()


def test_blocked_status_records_failure_on_breaker(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """A 403 during download_file must record failure on the breaker.

    A blanket rejection of the client says nothing good about the provider:
    counting it as a success meant a provider that 403s every request never
    tripped its own breaker and was re-dialled for every remaining work.
    """
    resp = MagicMock()
    resp.status_code = 403
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
        result = dl_mod.download_file("https://example.org/blocked.pdf", folder, "book")

    assert result is None
    mock_cb.record_failure.assert_called_once()
    mock_cb.record_success.assert_not_called()


def test_download_fast_fails_on_connect_level_death(
    tmp_path: Any, mock_config: dict[str, Any]
) -> None:
    """A host that never accepts a connection gets the short retry budget."""
    session = MagicMock()
    session.get.side_effect = requests.exceptions.ConnectTimeout(
        "Connection to dead.example timed out. (connect timeout=40)"
    )

    mock_cb = MagicMock()
    mock_cb.allow_request.return_value = True

    folder = str(tmp_path / "work")
    dl_mod._BUDGET._exhausted = False
    with (
        patch.object(dl_mod, "get_session", return_value=session),
        patch.object(dl_mod, "get_circuit_breaker", return_value=mock_cb),
        patch("api.core.download.time.sleep"),
    ):
        result = dl_mod.download_file("https://dead.example/book.pdf", folder, "book")

    assert result is None
    assert session.get.call_count == CONNECT_FAILURE_MAX_ATTEMPTS


class TestSaveJsonBudget:
    """``save_json`` must be bound by the metadata budget like any download.

    It previously wrote first and then called ``record_download``, which only
    increments counters: it consults no limit and never trips the ``stop``
    policy. Every manifest -- megabytes on a large work -- was therefore
    exempt from both ``download_limits.total.metadata_gb`` and the per-work
    ``metadata_mb`` cap.
    """

    def test_writes_within_budget(self, temp_output_dir: str) -> None:
        budget = DownloadBudget()
        with (
            patch.object(dl_mod, "_BUDGET", budget),
            patch("api.core.download.include_metadata", return_value=True),
            patch("api.core.budget.get_download_limits", return_value={}),
        ):
            path = dl_mod.save_json({"a": 1}, temp_output_dir, "meta")

        assert path is not None
        assert os.path.exists(path)
        assert budget.total_metadata_bytes > 0

    def test_refuses_and_discards_beyond_the_ceiling(
        self, temp_output_dir: str
    ) -> None:
        budget = DownloadBudget()
        limits = {
            "total": {"metadata_gb": 1e-9},  # about 1 byte
            "on_exceed": "stop",
        }
        with (
            patch.object(dl_mod, "_BUDGET", budget),
            patch("api.core.download.include_metadata", return_value=True),
            patch("api.core.budget.get_download_limits", return_value=limits),
        ):
            path = dl_mod.save_json({"payload": "x" * 5000}, temp_output_dir, "meta")

        assert path is None
        meta_dir = os.path.join(temp_output_dir, "metadata")
        leftovers = os.listdir(meta_dir) if os.path.isdir(meta_dir) else []
        assert leftovers == [], f"refused metadata left on disk: {leftovers}"
        assert budget.exhausted() is True

    def test_skips_once_the_budget_is_exhausted(self, temp_output_dir: str) -> None:
        budget = DownloadBudget()
        budget._exhausted = True
        with (
            patch.object(dl_mod, "_BUDGET", budget),
            patch("api.core.download.include_metadata", return_value=True),
            patch("api.core.budget.get_download_limits", return_value={}),
        ):
            assert dl_mod.save_json({"a": 1}, temp_output_dir, "meta") is None


class TestSaveJsonCounterReservations:
    """A save that writes nothing must hand its sequence number back.

    ``save_json`` reserves the number before it knows whether the file will be
    written. Spending it on a refusal leaves a permanent gap in the metadata
    numbering (the next save lands at ``_2`` with nothing at ``_1``), and a run
    refused on every item marched the counter on regardless.
    """

    @staticmethod
    def _key() -> tuple[str, str, str]:
        """Mirror the key save_json builds for filename ``meta``."""
        return (
            to_snake_case("meta"),
            get_provider_slug(None, None) or "unknown",
            "metadata",
        )

    def test_budget_exhausted_skip_returns_the_number(
        self, temp_output_dir: str
    ) -> None:
        budget = DownloadBudget()
        budget._exhausted = True
        with (
            patch.object(dl_mod, "_BUDGET", budget),
            patch("api.core.download.include_metadata", return_value=True),
            patch("api.core.budget.get_download_limits", return_value={}),
        ):
            assert dl_mod.save_json({"a": 1}, temp_output_dir, "meta") is None

        assert peek_counter(self._key()) == 1

    def test_metadata_refusal_returns_the_number(self, temp_output_dir: str) -> None:
        budget = DownloadBudget()
        limits = {"total": {"metadata_gb": 1e-9}, "on_exceed": "stop"}
        with (
            patch.object(dl_mod, "_BUDGET", budget),
            patch("api.core.download.include_metadata", return_value=True),
            patch("api.core.budget.get_download_limits", return_value=limits),
        ):
            assert (
                dl_mod.save_json({"payload": "x" * 5000}, temp_output_dir, "meta")
                is None
            )

        assert peek_counter(self._key()) == 1

    def test_serialization_failure_returns_the_number(
        self, temp_output_dir: str
    ) -> None:
        budget = DownloadBudget()
        with (
            patch.object(dl_mod, "_BUDGET", budget),
            patch("api.core.download.include_metadata", return_value=True),
            patch("api.core.budget.get_download_limits", return_value={}),
        ):
            assert dl_mod.save_json({"bad": object()}, temp_output_dir, "meta") is None

        assert peek_counter(self._key()) == 1

    def test_a_later_save_still_numbers_from_one(self, temp_output_dir: str) -> None:
        """The user-visible consequence: no hole in front of the first file."""
        exhausted = DownloadBudget()
        exhausted._exhausted = True
        with (
            patch.object(dl_mod, "_BUDGET", exhausted),
            patch("api.core.download.include_metadata", return_value=True),
            patch("api.core.budget.get_download_limits", return_value={}),
        ):
            assert dl_mod.save_json({"a": 1}, temp_output_dir, "meta") is None

        with (
            patch.object(dl_mod, "_BUDGET", DownloadBudget()),
            patch("api.core.download.include_metadata", return_value=True),
            patch("api.core.budget.get_download_limits", return_value={}),
        ):
            path = dl_mod.save_json({"a": 1}, temp_output_dir, "meta")

        assert path is not None
        assert not os.path.basename(path).endswith("_2.json")

    def test_written_files_keep_their_numbers(self, temp_output_dir: str) -> None:
        """Only refusals are refunded; consecutive saves still count upwards."""
        with (
            patch.object(dl_mod, "_BUDGET", DownloadBudget()),
            patch("api.core.download.include_metadata", return_value=True),
            patch("api.core.budget.get_download_limits", return_value={}),
        ):
            first = dl_mod.save_json({"a": 1}, temp_output_dir, "meta")
            second = dl_mod.save_json({"a": 2}, temp_output_dir, "meta")

        assert first is not None and second is not None
        assert first != second
        assert os.path.basename(second).endswith("_2.json")


class TestDownloadFileTerminalFailures:
    """Terminal transport failures in ``download_file`` and their bookkeeping."""

    @staticmethod
    def _run(
        exc: Exception,
        folder: str,
        net: dict[str, Any] | None = None,
        url: str = "https://bad.example/book.pdf",
    ) -> tuple[MagicMock, MagicMock]:
        session = MagicMock()
        session.get.side_effect = exc

        mock_cb = MagicMock()
        mock_cb.allow_request.return_value = True

        config: dict[str, Any] = {"max_attempts": 4, "base_backoff_s": 0.0}
        config.update(net or {})

        dl_mod._BUDGET._exhausted = False
        with (
            patch.object(dl_mod, "get_session", return_value=session),
            patch.object(dl_mod, "get_circuit_breaker", return_value=mock_cb),
            patch.object(dl_mod, "get_network_config", return_value=config),
            patch("api.core.download.time.sleep"),
        ):
            assert dl_mod.download_file(url, folder, "book") is None

        return session, mock_cb

    def test_ssl_error_feeds_the_breaker(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """Mirrors make_request: an unusable handshake is a provider outage."""
        session, cb = self._run(
            requests.exceptions.SSLError("certificate verify failed"),
            str(tmp_path / "work"),
        )
        assert session.get.call_count == 1
        cb.record_failure.assert_called_once()

    def test_ssl_policy_still_retries_insecurely_once(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        session, cb = self._run(
            requests.exceptions.SSLError("certificate verify failed"),
            str(tmp_path / "work"),
            net={"ssl_error_policy": "retry_insecure_once"},
        )
        assert session.get.call_count == 2
        cb.record_failure.assert_called_once()

    def test_credentialed_url_suppresses_the_insecure_retry(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """A keyed download URL must not be replayed over an unverified link."""
        session, cb = self._run(
            requests.exceptions.SSLError("certificate verify failed"),
            str(tmp_path / "work"),
            net={"ssl_error_policy": "retry_insecure_once"},
            url="https://bad.example/fast_download?md5=abc&key=s3cr3t",
        )
        assert session.get.call_count == 1
        cb.record_failure.assert_called_once()

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.MissingSchema("no schema"),
            requests.exceptions.InvalidSchema("no adapter"),
            requests.exceptions.InvalidURL("invalid label"),
            requests.exceptions.URLRequired("no url"),
        ],
    )
    def test_invalid_url_is_not_retried_or_charged_to_the_breaker(
        self, exc: Exception, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """Bad provider metadata must not mark the provider itself unhealthy."""
        session, cb = self._run(exc, str(tmp_path / "work"))
        assert session.get.call_count == 1
        cb.record_failure.assert_not_called()
