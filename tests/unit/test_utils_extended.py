"""Extended tests for api.utils module — file download and utility functions."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from api.utils import (
    _build_standardized_filename,
    _determine_target_directory,
    _filename_from_content_disposition,
    _infer_extension_from_content_type,
    _should_reject_html_response,
    _validate_file_magic_bytes,
    _validate_html_not_login_page,
    download_iiif_renderings,
    save_json,
)


# ============================================================================
# _infer_extension_from_content_type
# ============================================================================

class TestInferExtensionFromContentType:
    """Tests for MIME type to extension mapping."""

    def test_pdf(self):
        assert _infer_extension_from_content_type("application/pdf") == ".pdf"

    def test_epub(self):
        assert _infer_extension_from_content_type("application/epub+zip") == ".epub"

    def test_jpeg(self):
        assert _infer_extension_from_content_type("image/jpeg") == ".jpg"

    def test_png(self):
        assert _infer_extension_from_content_type("image/png") == ".png"

    def test_json(self):
        assert _infer_extension_from_content_type("application/json") == ".json"

    def test_unknown(self):
        assert _infer_extension_from_content_type("application/unknown") == ""

    def test_case_insensitive(self):
        assert _infer_extension_from_content_type("Application/PDF") == ".pdf"

    def test_with_charset(self):
        assert _infer_extension_from_content_type("application/json; charset=utf-8") == ".json"


# ============================================================================
# _should_reject_html_response
# ============================================================================

class TestShouldRejectHtmlResponse:
    """Tests for HTML response rejection logic."""

    def test_non_html_is_accepted(self):
        reject, reason = _should_reject_html_response("application/pdf", "https://example.org/file.pdf")
        assert reject is False

    def test_html_for_pdf_url_is_rejected(self):
        reject, reason = _should_reject_html_response(
            "text/html", "https://example.org/file.pdf"
        )
        assert reject is True
        assert "PDF" in reason

    def test_html_for_epub_url_is_rejected(self):
        reject, reason = _should_reject_html_response(
            "text/html", "https://example.org/file.epub"
        )
        assert reject is True

    def test_html_for_download_url_is_rejected(self):
        reject, reason = _should_reject_html_response(
            "text/html", "https://example.org/download?id=123"
        )
        assert reject is True

    def test_html_for_regular_url_is_accepted(self):
        reject, reason = _should_reject_html_response(
            "text/html", "https://example.org/about"
        )
        assert reject is False

    def test_annas_archive_login_page(self):
        reject, reason = _should_reject_html_response(
            "text/html",
            "https://annas-archive.li/download",
            content_length=180000,
        )
        assert reject is True


# ============================================================================
# _validate_file_magic_bytes
# ============================================================================

class TestValidateFileMagicBytes:
    """Tests for file content validation."""

    def test_valid_pdf(self, tmp_path):
        f = tmp_path / "test.pdf"
        f.write_bytes(b"%PDF-1.4 test content")
        valid, msg = _validate_file_magic_bytes(str(f), ".pdf")
        assert valid is True

    def test_invalid_pdf_with_html(self, tmp_path):
        f = tmp_path / "test.pdf"
        f.write_bytes(b"<!DOCTYPE html><html><body>Error</body></html>")
        valid, msg = _validate_file_magic_bytes(str(f), ".pdf")
        assert valid is False
        assert "HTML" in msg

    def test_valid_epub(self, tmp_path):
        f = tmp_path / "test.epub"
        f.write_bytes(b"PK\x03\x04 test content")
        valid, msg = _validate_file_magic_bytes(str(f), ".epub")
        assert valid is True

    def test_invalid_epub_with_html(self, tmp_path):
        f = tmp_path / "test.epub"
        f.write_bytes(b"<!DOCTYPE html><html><body>Error</body></html>")
        valid, msg = _validate_file_magic_bytes(str(f), ".epub")
        assert valid is False

    def test_non_pdf_epub_always_valid(self, tmp_path):
        f = tmp_path / "test.jpg"
        f.write_bytes(b"random bytes")
        valid, msg = _validate_file_magic_bytes(str(f), ".jpg")
        assert valid is True

    def test_nonexistent_file(self):
        valid, msg = _validate_file_magic_bytes("/nonexistent/path.pdf", ".pdf")
        assert valid is True  # Should not reject on validation error


# ============================================================================
# _validate_html_not_login_page
# ============================================================================

class TestValidateHtmlNotLoginPage:
    """Tests for Anna's Archive login page detection."""

    def test_non_annas_archive_accepted(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text("<html><title>log in / register</title></html>")
        valid, msg = _validate_html_not_login_page(str(f), "https://example.org", None)
        assert valid is True

    def test_annas_archive_login_detected(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text("<html><title>log in / register</title></html>")
        valid, msg = _validate_html_not_login_page(
            str(f), "https://annas-archive.li/download", "annas_archive"
        )
        assert valid is False

    def test_annas_archive_normal_page_accepted(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text("<html><title>Download Page</title></html>")
        valid, msg = _validate_html_not_login_page(
            str(f), "https://annas-archive.li/download", "annas_archive"
        )
        assert valid is True


# ============================================================================
# _determine_target_directory
# ============================================================================

class TestDetermineTargetDirectory:
    """Tests for target directory determination."""

    def test_no_whitelist_returns_objects(self):
        target, msg, success = _determine_target_directory("/work", ".pdf", None, False)
        assert target == os.path.join("/work", "objects")
        assert success is True

    def test_empty_whitelist_returns_objects(self):
        target, msg, success = _determine_target_directory("/work", ".pdf", [], False)
        assert target == os.path.join("/work", "objects")
        assert success is True

    def test_allowed_extension_returns_objects(self):
        target, msg, success = _determine_target_directory(
            "/work", ".pdf", [".pdf", ".epub"], False
        )
        assert target == os.path.join("/work", "objects")
        assert success is True

    def test_disallowed_ext_without_metadata_save(self):
        target, msg, success = _determine_target_directory(
            "/work", ".html", [".pdf"], False
        )
        assert target is None
        assert success is False

    def test_disallowed_ext_with_metadata_save(self):
        target, msg, success = _determine_target_directory(
            "/work", ".html", [".pdf"], True
        )
        assert "metadata" in target
        assert success is False


# ============================================================================
# _build_standardized_filename
# ============================================================================

class TestBuildStandardizedFilename:
    """Tests for filename standardization."""

    def test_basic_filename(self):
        from api.core.context import reset_counters
        reset_counters()
        name = _build_standardized_filename(".pdf", "test_book", "ia")
        assert name.endswith(".pdf")
        assert "ia" in name
        assert "test_book" in name

    def test_image_filename_includes_sequence(self):
        from api.core.context import reset_counters
        reset_counters()
        name = _build_standardized_filename(".jpg", "test", "mdz")
        assert "image" in name
        assert "001" in name

    def test_truncates_long_stem(self):
        from api.core.context import reset_counters
        reset_counters()
        long_stem = "a" * 100
        name = _build_standardized_filename(".pdf", long_stem, "ia", max_stem_len=50)
        assert len(name.split("_ia")[0]) <= 50


# ============================================================================
# _filename_from_content_disposition
# ============================================================================

class TestFilenameFromContentDisposition:
    """Tests for Content-Disposition header parsing."""

    def test_simple_filename(self):
        result = _filename_from_content_disposition('attachment; filename="test.pdf"')
        assert result == "test.pdf"

    def test_filename_star(self):
        result = _filename_from_content_disposition("attachment; filename*=UTF-8''test%20file.pdf")
        assert result == "test file.pdf"

    def test_none_input(self):
        assert _filename_from_content_disposition(None) is None

    def test_empty_string(self):
        assert _filename_from_content_disposition("") is None

    def test_no_filename_param(self):
        result = _filename_from_content_disposition("attachment")
        assert result is None

    def test_unquoted_filename(self):
        result = _filename_from_content_disposition("attachment; filename=test.pdf")
        assert result == "test.pdf"


# ============================================================================
# save_json
# ============================================================================

class TestSaveJson:
    """Tests for JSON metadata saving."""

    @patch("api.utils.include_metadata", return_value=True)
    @patch("api.utils.get_current_name_stem", return_value="test_stem")
    @patch("api.utils.get_current_provider", return_value="ia")
    def test_saves_json_file(self, mock_prov, mock_stem, mock_meta, tmp_path):
        data = {"key": "value"}
        result = save_json(data, str(tmp_path), "test")
        assert result is not None
        assert os.path.exists(result)
        with open(result) as f:
            saved = json.load(f)
        assert saved["key"] == "value"

    @patch("api.utils.include_metadata", return_value=False)
    def test_skips_when_metadata_disabled(self, mock_meta, tmp_path):
        result = save_json({"key": "value"}, str(tmp_path), "test")
        assert result is None


# ============================================================================
# download_iiif_renderings
# ============================================================================

class TestDownloadIIIFRenderings:
    """Tests for IIIF manifest rendering downloads."""

    @patch("api.utils.get_download_config", return_value={
        "download_manifest_renderings": False,
    })
    def test_disabled_by_config(self, mock_cfg):
        result = download_iiif_renderings({}, "/out")
        assert result == 0

    @patch("api.utils.download_file")
    @patch("api.utils.get_download_config", return_value={
        "download_manifest_renderings": True,
        "rendering_mime_whitelist": ["application/pdf"],
        "max_renderings_per_manifest": 1,
    })
    def test_downloads_pdf_rendering(self, mock_cfg, mock_dl):
        mock_dl.return_value = "/path/to/file.pdf"
        manifest = {
            "rendering": [
                {"@id": "https://example.org/file.pdf", "format": "application/pdf"}
            ]
        }
        result = download_iiif_renderings(manifest, "/out")
        assert result == 1

    @patch("api.utils.download_file")
    @patch("api.utils.get_download_config", return_value={
        "download_manifest_renderings": True,
        "rendering_mime_whitelist": ["application/pdf"],
        "max_renderings_per_manifest": 1,
    })
    def test_skips_non_whitelisted_format(self, mock_cfg, mock_dl):
        manifest = {
            "rendering": [
                {"@id": "https://example.org/file.xml", "format": "text/xml"}
            ]
        }
        result = download_iiif_renderings(manifest, "/out")
        assert result == 0
        mock_dl.assert_not_called()

    @patch("api.utils.download_file")
    @patch("api.utils.get_download_config", return_value={
        "download_manifest_renderings": True,
        "rendering_mime_whitelist": ["application/pdf"],
        "max_renderings_per_manifest": 1,
    })
    def test_respects_max_renderings_limit(self, mock_cfg, mock_dl):
        mock_dl.return_value = "/path/to/file.pdf"
        manifest = {
            "rendering": [
                {"@id": "https://example.org/a.pdf", "format": "application/pdf"},
                {"@id": "https://example.org/b.pdf", "format": "application/pdf"},
            ]
        }
        result = download_iiif_renderings(manifest, "/out")
        assert result == 1  # Only 1 due to limit

    @patch("api.utils.get_download_config", return_value={
        "download_manifest_renderings": True,
        "rendering_mime_whitelist": ["application/pdf"],
        "max_renderings_per_manifest": 1,
    })
    def test_handles_empty_manifest(self, mock_cfg):
        result = download_iiif_renderings({}, "/out")
        assert result == 0

    @patch("api.utils.download_file")
    @patch("api.utils.get_download_config", return_value={
        "download_manifest_renderings": True,
        "rendering_mime_whitelist": ["application/pdf"],
        "max_renderings_per_manifest": 2,
    })
    def test_deduplicates_rendering_urls(self, mock_cfg, mock_dl):
        mock_dl.return_value = "/path/to/file.pdf"
        manifest = {
            "rendering": [
                {"@id": "https://example.org/same.pdf", "format": "application/pdf"},
                {"@id": "https://example.org/same.pdf", "format": "application/pdf"},
            ]
        }
        result = download_iiif_renderings(manifest, "/out")
        assert result == 1  # Deduplicated

    @patch("api.utils.download_file")
    @patch("api.utils.get_download_config", return_value={
        "download_manifest_renderings": True,
        "rendering_mime_whitelist": [],
        "max_renderings_per_manifest": 1,
    })
    def test_allows_pdf_by_url_suffix_when_no_whitelist(self, mock_cfg, mock_dl):
        mock_dl.return_value = "/path"
        manifest = {
            "rendering": [
                {"@id": "https://example.org/file.pdf", "format": ""}
            ]
        }
        result = download_iiif_renderings(manifest, "/out")
        assert result == 1

    @patch("api.utils.download_file")
    @patch("api.utils.get_download_config", return_value={
        "download_manifest_renderings": True,
        "rendering_mime_whitelist": ["application/pdf"],
        "max_renderings_per_manifest": 1,
    })
    def test_rendering_as_dict_not_list(self, mock_cfg, mock_dl):
        mock_dl.return_value = "/path"
        manifest = {
            "rendering": {"@id": "https://example.org/file.pdf", "format": "application/pdf"}
        }
        result = download_iiif_renderings(manifest, "/out")
        assert result == 1
