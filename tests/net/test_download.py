"""Tests for Network.download: streaming to .part, checks and atomic rename.

Re-expresses v1.30.3 ``tests/unit/test_download_atomic.py`` (mid-stream
abort, mid-stream retry, short read, content encoding, transient retries,
atomic promotion, interrupt, breaker side of 404 and 403, connect-level
death) against the new API.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest
import requests

from chronodownloader.net import Cause, RequestFailed
from chronodownloader.net.classify import CONNECT_FAILURE_MAX_ATTEMPTS
from chronodownloader.settings import AnnasQuota

from .conftest import NetworkFactory
from .support import (
    SLOT,
    FakeCoordinator,
    Reply,
    ScriptedServer,
    ScriptedSession,
    response,
    settings_with,
)

PDF = b"%PDF-1.4\n" + b"x" * 200_000
PDF_TYPE = {"Content-Type": "application/pdf"}


def _files(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


def test_a_complete_download_is_renamed_from_its_part_file(
    tmp_path: Path,
    server: ScriptedServer,
    coordinator: FakeCoordinator,
    make_network: NetworkFactory,
) -> None:
    url = server.script(
        "/book.pdf",
        Reply(
            200,
            PDF,
            {**PDF_TYPE, "Content-Disposition": 'attachment; filename="b.pdf"'},
        ),
    )
    part = tmp_path / "staging" / "book.pdf.part"
    outcome = make_network(settings_with()).download("mdz", url, part, slot=SLOT)
    assert outcome.path == tmp_path / "staging" / "book.pdf"
    assert outcome.path.read_bytes() == PDF
    assert (outcome.size, outcome.extension, outcome.filename) == (
        len(PDF),
        ".pdf",
        "b.pdf",
    )
    assert outcome.insecure is False
    assert _files(tmp_path / "staging") == ["book.pdf"]
    assert coordinator.successes == ["mdz"]
    assert coordinator.reservations[0].slot == SLOT


def test_a_midstream_drop_is_retried_and_leaves_no_part_file(
    tmp_path: Path,
    server: ScriptedServer,
    coordinator: FakeCoordinator,
    make_network: NetworkFactory,
) -> None:
    url = server.script(
        "/book.pdf",
        Reply(200, PDF, PDF_TYPE, send_bytes=1000),
        Reply(200, PDF, PDF_TYPE),
    )
    part = tmp_path / "book.pdf.part"
    outcome = make_network(settings_with()).download("mdz", url, part, slot=SLOT)
    assert outcome.path.read_bytes() == PDF
    assert server.hits("/book.pdf") == 2
    assert len(coordinator.reservations) == 2
    assert _files(tmp_path) == ["book.pdf"]


def test_a_midstream_drop_on_every_attempt_leaves_nothing(
    tmp_path: Path,
    server: ScriptedServer,
    coordinator: FakeCoordinator,
    make_network: NetworkFactory,
) -> None:
    url = server.script("/book.pdf", Reply(200, PDF, PDF_TYPE, send_bytes=1000))
    with pytest.raises(RequestFailed) as excinfo:
        make_network(settings_with(max_attempts=2)).download(
            "mdz", url, tmp_path / "book.pdf.part", slot=SLOT
        )
    assert excinfo.value.cause is Cause.TRANSIENT
    assert server.hits("/book.pdf") == 2
    assert _files(tmp_path) == []
    assert coordinator.failures == [("mdz", "transient")] * 2


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.SSLError("certificate verify failed"),
        requests.exceptions.ConnectionError("Connection refused"),
    ],
)
def test_an_error_while_reading_the_body_keeps_the_quota_unit(
    error: requests.exceptions.RequestException,
    tmp_path: Path,
    coordinator: FakeCoordinator,
    make_network: NetworkFactory,
) -> None:
    session = ScriptedSession(response(200, [b"%PDF-1.4 part"], PDF_TYPE, error=error))
    settings = settings_with(
        "annas_archive", quota=AnnasQuota(daily_limit=5), max_attempts=1
    )
    with pytest.raises(RequestFailed):
        make_network(settings, session_factory=lambda: session).download(
            "annas_archive",
            "https://annas.example/fast.pdf",
            tmp_path / "fast.pdf.part",
            slot=SLOT,
            quota=True,
        )
    assert coordinator.quota_used["annas_archive"] == 1
    assert coordinator.released == []
    assert _files(tmp_path) == []


def test_a_short_body_is_discarded_as_incomplete(
    tmp_path: Path, make_network: NetworkFactory
) -> None:
    session = ScriptedSession(
        response(200, [b"%PDF-1.4 only part"], {**PDF_TYPE, "Content-Length": "100000"})
    )
    with pytest.raises(RequestFailed) as excinfo:
        make_network(settings_with(), session_factory=lambda: session).download(
            "mdz", "https://example.org/book.pdf", tmp_path / "book.pdf.part", slot=SLOT
        )
    assert excinfo.value.cause is Cause.INCOMPLETE
    assert len(session.calls) == 1
    assert _files(tmp_path) == []


def test_an_empty_body_is_discarded(
    tmp_path: Path, make_network: NetworkFactory
) -> None:
    session = ScriptedSession(response(200, [], PDF_TYPE))
    with pytest.raises(RequestFailed) as excinfo:
        make_network(settings_with(), session_factory=lambda: session).download(
            "mdz", "https://example.org/book.pdf", tmp_path / "book.pdf.part", slot=SLOT
        )
    assert excinfo.value.cause is Cause.INCOMPLETE
    assert _files(tmp_path) == []


def test_a_content_encoded_body_is_not_checked_against_its_length(
    tmp_path: Path, server: ScriptedServer, make_network: NetworkFactory
) -> None:
    packed = gzip.compress(PDF)
    url = server.script(
        "/book.pdf", Reply(200, packed, {**PDF_TYPE, "Content-Encoding": "gzip"})
    )
    outcome = make_network(settings_with()).download(
        "mdz", url, tmp_path / "book.pdf.part", slot=SLOT
    )
    assert outcome.path.read_bytes() == PDF
    assert outcome.size == len(PDF) != len(packed)


def test_html_where_a_pdf_was_expected_is_invalid(
    tmp_path: Path,
    server: ScriptedServer,
    coordinator: FakeCoordinator,
    make_network: NetworkFactory,
) -> None:
    page = b"<!DOCTYPE html><html><body>Error</body></html>"
    url = server.script("/book.pdf", Reply(200, page, {"Content-Type": "text/html"}))
    with pytest.raises(RequestFailed) as excinfo:
        make_network(settings_with()).download(
            "mdz", url, tmp_path / "book.pdf.part", slot=SLOT
        )
    assert excinfo.value.cause is Cause.INVALID_PAYLOAD
    assert "HTML" in excinfo.value.detail
    assert server.hits("/book.pdf") == 1
    assert _files(tmp_path) == []
    assert coordinator.successes == ["mdz"]


def test_a_challenge_page_is_blocked(
    tmp_path: Path,
    server: ScriptedServer,
    coordinator: FakeCoordinator,
    make_network: NetworkFactory,
) -> None:
    page = b"<html><head><title>Just a moment...</title></head></html>"
    url = server.script("/p.jpg", Reply(200, page, {"Content-Type": "text/html"}))
    with pytest.raises(RequestFailed) as excinfo:
        make_network(settings_with()).download(
            "mdz", url, tmp_path / "p.jpg.part", slot=SLOT
        )
    assert excinfo.value.cause is Cause.BLOCKED
    assert coordinator.failures == [("mdz", "blocked")]
    assert _files(tmp_path) == []


def test_a_generic_type_is_resolved_from_the_bytes(
    tmp_path: Path, server: ScriptedServer, make_network: NetworkFactory
) -> None:
    url = server.script(
        "/cgi/data.fcgi", Reply(200, PDF, {"Content-Type": "application/octet-stream"})
    )
    outcome = make_network(settings_with()).download(
        "mdz", url, tmp_path / "object.part", slot=SLOT
    )
    assert outcome.extension == ".pdf"
    assert outcome.path == tmp_path / "object"


def test_transient_errors_before_the_body_are_retried(
    tmp_path: Path, make_network: NetworkFactory
) -> None:
    session = ScriptedSession(
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.Timeout("timed out"),
        response(200, [PDF], {**PDF_TYPE, "Content-Length": str(len(PDF))}),
    )
    outcome = make_network(settings_with(), session_factory=lambda: session).download(
        "mdz", "https://example.org/book.pdf", tmp_path / "book.pdf.part", slot=SLOT
    )
    assert outcome.path.read_bytes() == PDF
    assert len(session.calls) == 3
    assert all(call["stream"] is True for call in session.calls)


def test_an_interrupt_midstream_leaves_no_file(
    tmp_path: Path, make_network: NetworkFactory
) -> None:
    session = ScriptedSession(
        response(200, [b"%PDF-1.4 partial data"], PDF_TYPE, error=KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        make_network(settings_with(), session_factory=lambda: session).download(
            "mdz", "https://example.org/book.pdf", tmp_path / "book.pdf.part", slot=SLOT
        )
    assert _files(tmp_path) == []


def test_a_404_records_success_and_a_403_failure(
    tmp_path: Path,
    server: ScriptedServer,
    coordinator: FakeCoordinator,
    make_network: NetworkFactory,
) -> None:
    client = make_network(settings_with())
    missing = server.script("/missing.pdf", Reply(404))
    blocked = server.script("/blocked.pdf", Reply(403))
    with pytest.raises(RequestFailed) as gone:
        client.download("mdz", missing, tmp_path / "a.pdf.part", slot=SLOT)
    assert gone.value.cause is Cause.NOT_FOUND
    assert coordinator.successes == ["mdz"]
    with pytest.raises(RequestFailed) as refused:
        client.download("mdz", blocked, tmp_path / "b.pdf.part", slot=SLOT)
    assert refused.value.cause is Cause.BLOCKED
    assert coordinator.failures == [("mdz", "blocked")]
    assert _files(tmp_path) == []


def test_a_dead_host_gets_the_short_budget(
    tmp_path: Path, make_network: NetworkFactory
) -> None:
    session = ScriptedSession(
        requests.exceptions.ConnectTimeout("Connection to dead.example timed out.")
    )
    with pytest.raises(RequestFailed):
        make_network(
            settings_with(max_attempts=8), session_factory=lambda: session
        ).download(
            "mdz",
            "https://dead.example/book.pdf",
            tmp_path / "book.pdf.part",
            slot=SLOT,
        )
    assert len(session.calls) == CONNECT_FAILURE_MAX_ATTEMPTS


def test_a_credentialed_url_is_not_retried_unverified(
    tmp_path: Path, make_network: NetworkFactory
) -> None:
    session = ScriptedSession(requests.exceptions.SSLError("certificate verify failed"))
    with pytest.raises(RequestFailed) as excinfo:
        make_network(
            settings_with(ssl_error_policy="retry_insecure_once"),
            session_factory=lambda: session,
        ).download(
            "mdz",
            "https://bad.example/fast_download?md5=abc&key=s3cr3t",
            tmp_path / "book.pdf.part",
            slot=SLOT,
        )
    assert excinfo.value.cause is Cause.SSL
    assert len(session.calls) == 1


def test_the_target_must_be_a_part_file(
    tmp_path: Path, make_network: NetworkFactory
) -> None:
    with pytest.raises(ValueError, match=r"\.part"):
        make_network(settings_with()).download(
            "mdz", "https://example.org/b.pdf", tmp_path / "b.pdf", slot=SLOT
        )
