"""Tests for net/payload.py.

Ports the helper tests of v1.30.3 ``tests/unit/test_utils.py``,
``test_utils_extended.py`` and ``test_download_helpers.py`` (content type to
extension, magic bytes, Content-Disposition, Content-Length, sniffing) to the
pure functions; the assertions are unchanged.
"""

from __future__ import annotations

import pytest

from chronodownloader.net.payload import (
    content_disposition_filename,
    extension_for_content_type,
    magic_mismatch,
    parse_content_length,
    sniff_extension,
)


class TestExtensionForContentType:
    @pytest.mark.parametrize(
        ("content_type", "extension"),
        [
            ("application/pdf", ".pdf"),
            ("application/epub+zip", ".epub"),
            ("image/jpeg", ".jpg"),
            ("image/png", ".png"),
            ("application/json", ".json"),
            ("application/json; charset=utf-8", ".json"),
            ("Application/PDF", ".pdf"),
            ("APPLICATION/PDF", ".pdf"),
        ],
    )
    def test_known_types(self, content_type: str, extension: str) -> None:
        assert extension_for_content_type(content_type) == extension

    def test_unknown_and_generic_types_give_none(self) -> None:
        assert extension_for_content_type("application/unknown") == ""
        assert extension_for_content_type("application/octet-stream") == ""


class TestMagicMismatch:
    def test_valid_pdf(self) -> None:
        assert magic_mismatch(".pdf", b"%PDF-1.4 test content") == ""

    def test_html_posing_as_pdf(self) -> None:
        reason = magic_mismatch(".pdf", b"<!DOCTYPE html><html>Error page</html>")
        assert "HTML" in reason

    def test_valid_epub(self) -> None:
        assert magic_mismatch(".epub", b"PK\x03\x04 test content") == ""

    def test_html_posing_as_epub(self) -> None:
        body = b"<!DOCTYPE html><html><body>Error</body></html>"
        assert magic_mismatch(".epub", body) != ""

    def test_a_non_html_body_without_the_magic_is_rejected(self) -> None:
        assert "magic bytes" in magic_mismatch(".pdf", b"\x00\x01binary")

    def test_other_extensions_are_not_checked(self) -> None:
        assert magic_mismatch(".jpg", b"random bytes") == ""
        assert magic_mismatch(".txt", b"text content") == ""


class TestContentDispositionFilename:
    def test_quoted_filename(self) -> None:
        value = 'attachment; filename="test.pdf"'
        assert content_disposition_filename(value) == "test.pdf"

    def test_unquoted_filename(self) -> None:
        value = "attachment; filename=test.pdf"
        assert content_disposition_filename(value) == "test.pdf"

    def test_rfc_5987_filename(self) -> None:
        value = "attachment; filename*=UTF-8''test%20file.pdf"
        assert content_disposition_filename(value) == "test file.pdf"

    def test_missing_header_or_parameter(self) -> None:
        assert content_disposition_filename(None) is None
        assert content_disposition_filename("") is None
        assert content_disposition_filename("attachment") is None


class TestParseContentLength:
    def test_valid_length(self) -> None:
        assert parse_content_length("1024") == 1024
        assert parse_content_length("0") == 0

    def test_missing_malformed_or_negative(self) -> None:
        assert parse_content_length(None) is None
        assert parse_content_length("") is None
        assert parse_content_length("abc") is None
        assert parse_content_length("-5") is None


class TestSniffExtension:
    def test_documents_and_images(self) -> None:
        assert sniff_extension(b"%PDF-1.7\n%\xe2\xe3") == ".pdf"
        assert sniff_extension(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == ".png"
        assert sniff_extension(b"\xff\xd8\xff\xe0JFIF") == ".jpg"
        assert sniff_extension(b"II*\x00\x08\x00") == ".tif"
        assert sniff_extension(b"MM\x00*\x00\x00") == ".tif"
        assert sniff_extension(b"\x00\x00\x00\x0cjP  \r\n\x87\n") == ".jp2"
        assert sniff_extension(b"\xff\x4f\xff\x51\x00") == ".jp2"

    def test_epub_is_told_apart_from_a_plain_zip(self) -> None:
        epub = b"PK\x03\x04" + b"\x00" * 26 + b"mimetypeapplication/epub+zip"
        assert sniff_extension(epub) == ".epub"
        assert sniff_extension(b"PK\x03\x04\x14\x00\x00\x00" + b"\x00" * 24) == ".zip"

    def test_unknown_and_empty_payloads(self) -> None:
        assert sniff_extension(b"") == ""
        assert sniff_extension(b"not a recognizable payload at all") == ""
