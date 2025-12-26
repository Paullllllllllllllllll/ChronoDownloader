"""Unit tests for api.utils module."""
from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


class TestInferExtensionFromContentType:
    """Tests for _infer_extension_from_content_type function."""
    
    def test_pdf_content_type(self):
        """Test PDF content type."""
        from api.utils import _infer_extension_from_content_type
        
        assert _infer_extension_from_content_type("application/pdf") == ".pdf"
    
    def test_epub_content_type(self):
        """Test EPUB content type."""
        from api.utils import _infer_extension_from_content_type
        
        assert _infer_extension_from_content_type("application/epub+zip") == ".epub"
    
    def test_jpeg_content_type(self):
        """Test JPEG content type."""
        from api.utils import _infer_extension_from_content_type
        
        assert _infer_extension_from_content_type("image/jpeg") == ".jpg"
    
    def test_unknown_content_type(self):
        """Test unknown content type."""
        from api.utils import _infer_extension_from_content_type
        
        assert _infer_extension_from_content_type("application/unknown") == ""
    
    def test_case_insensitive(self):
        """Test case insensitivity."""
        from api.utils import _infer_extension_from_content_type
        
        assert _infer_extension_from_content_type("APPLICATION/PDF") == ".pdf"


class TestShouldRejectHtmlResponse:
    """Tests for _should_reject_html_response function."""
    
    def test_accepts_non_html(self):
        """Test that non-HTML responses are accepted."""
        from api.utils import _should_reject_html_response
        
        should_reject, reason = _should_reject_html_response(
            "application/pdf", "https://example.com/file.pdf"
        )
        
        assert should_reject is False
    
    def test_rejects_html_for_pdf_url(self):
        """Test that HTML is rejected for PDF URLs."""
        from api.utils import _should_reject_html_response
        
        should_reject, reason = _should_reject_html_response(
            "text/html", "https://example.com/file.pdf"
        )
        
        assert should_reject is True
        assert "HTML" in reason or "PDF" in reason
    
    def test_rejects_html_for_download_url(self):
        """Test that HTML is rejected for download URLs."""
        from api.utils import _should_reject_html_response
        
        should_reject, reason = _should_reject_html_response(
            "text/html", "https://example.com/download?id=123"
        )
        
        assert should_reject is True


class TestValidateFileMagicBytes:
    """Tests for _validate_file_magic_bytes function."""
    
    def test_valid_pdf(self, temp_dir: str):
        """Test validation of valid PDF."""
        from api.utils import _validate_file_magic_bytes
        
        pdf_path = os.path.join(temp_dir, "test.pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 test content")
        
        is_valid, error = _validate_file_magic_bytes(pdf_path, ".pdf")
        
        assert is_valid is True
        assert error == ""
    
    def test_invalid_pdf_with_html(self, temp_dir: str):
        """Test validation of HTML masquerading as PDF."""
        from api.utils import _validate_file_magic_bytes
        
        fake_pdf_path = os.path.join(temp_dir, "fake.pdf")
        with open(fake_pdf_path, "wb") as f:
            f.write(b"<!DOCTYPE html><html>Error page</html>")
        
        is_valid, error = _validate_file_magic_bytes(fake_pdf_path, ".pdf")
        
        assert is_valid is False
        assert "HTML" in error
    
    def test_skips_non_pdf_epub(self, temp_dir: str):
        """Test that non-PDF/EPUB files are not validated."""
        from api.utils import _validate_file_magic_bytes
        
        txt_path = os.path.join(temp_dir, "test.txt")
        with open(txt_path, "w") as f:
            f.write("text content")
        
        is_valid, error = _validate_file_magic_bytes(txt_path, ".txt")
        
        assert is_valid is True


class TestDetermineTargetDirectory:
    """Tests for _determine_target_directory function."""
    
    def test_default_to_objects(self):
        """Test that default target is objects directory."""
        import os
        from api.utils import _determine_target_directory
        
        target, msg, success = _determine_target_directory(
            "/base/path", ".pdf", None, False
        )
        
        # Use os.path.join for cross-platform compatibility
        expected = os.path.join("/base/path", "objects")
        assert target == expected
        assert success is True
    
    def test_filters_disallowed_extensions(self):
        """Test filtering of disallowed extensions."""
        from api.utils import _determine_target_directory
        
        target, msg, success = _determine_target_directory(
            "/base/path", ".xyz", [".pdf", ".epub"], False
        )
        
        assert target is None
        assert success is False
    
    def test_saves_disallowed_to_metadata(self):
        """Test saving disallowed files to metadata."""
        import os
        from api.utils import _determine_target_directory
        
        target, msg, success = _determine_target_directory(
            "/base/path", ".xyz", [".pdf"], True
        )
        
        expected = os.path.join("/base/path", "metadata")
        assert target == expected
        assert success is False


class TestBuildStandardizedFilename:
    """Tests for _build_standardized_filename function."""
    
    def test_basic_filename(self):
        """Test building basic filename."""
        from api.utils import _build_standardized_filename
        from api.core.context import reset_counters
        
        reset_counters()
        
        filename = _build_standardized_filename(".pdf", "my_work", "ia")
        
        assert filename.endswith(".pdf")
        assert "my_work" in filename
        assert "ia" in filename
    
    def test_increments_sequence(self):
        """Test that sequence number increments."""
        from api.utils import _build_standardized_filename
        from api.core.context import reset_counters
        
        reset_counters()
        
        fn1 = _build_standardized_filename(".pdf", "work", "ia")
        fn2 = _build_standardized_filename(".pdf", "work", "ia")
        
        # Second file should have different name (with sequence number)
        assert fn1 != fn2
    
    def test_image_naming(self):
        """Test image file naming."""
        from api.utils import _build_standardized_filename
        from api.core.context import reset_counters
        
        reset_counters()
        
        filename = _build_standardized_filename(".jpg", "work", "ia")
        
        assert "image" in filename
        assert filename.endswith(".jpg")


class TestSaveJson:
    """Tests for save_json function."""
    
    def test_saves_json_file(self, temp_dir: str):
        """Test saving JSON data to file."""
        from api.utils import save_json
        from api.core.context import set_current_name_stem, set_current_provider, reset_counters
        
        reset_counters()
        set_current_name_stem("test_work")
        set_current_provider("internet_archive")
        
        with patch("api.utils.include_metadata", return_value=True):
            result = save_json({"key": "value"}, temp_dir, "metadata")
        
        assert result is not None
        assert os.path.exists(result)
        assert result.endswith(".json")
    
    def test_respects_include_metadata_false(self, temp_dir: str):
        """Test that save is skipped when include_metadata is False."""
        from api.utils import save_json
        
        with patch("api.utils.include_metadata", return_value=False):
            result = save_json({"key": "value"}, temp_dir, "metadata")
        
        assert result is None
    
    def test_creates_metadata_directory(self, temp_dir: str):
        """Test that metadata directory is created."""
        from api.utils import save_json
        from api.core.context import set_current_name_stem, reset_counters
        
        reset_counters()
        set_current_name_stem("test_work")
        
        output_dir = os.path.join(temp_dir, "new_work")
        
        with patch("api.utils.include_metadata", return_value=True):
            result = save_json({"data": 123}, output_dir, "test")
        
        assert os.path.exists(os.path.join(output_dir, "metadata"))


class TestFilenameFromContentDisposition:
    """Tests for _filename_from_content_disposition function."""
    
    def test_simple_filename(self):
        """Test parsing simple filename."""
        from api.utils import _filename_from_content_disposition
        
        result = _filename_from_content_disposition('attachment; filename="test.pdf"')
        
        assert result == "test.pdf"
    
    def test_filename_star(self):
        """Test parsing filename* (RFC 5987)."""
        from api.utils import _filename_from_content_disposition
        
        result = _filename_from_content_disposition(
            "attachment; filename*=UTF-8''test%20file.pdf"
        )
        
        assert result == "test file.pdf"
    
    def test_none_value(self):
        """Test with None value."""
        from api.utils import _filename_from_content_disposition
        
        result = _filename_from_content_disposition(None)
        
        assert result is None
    
    def test_no_filename(self):
        """Test with no filename parameter."""
        from api.utils import _filename_from_content_disposition
        
        result = _filename_from_content_disposition("attachment")
        
        assert result is None


class TestDownloadIiifRenderings:
    """Tests for download_iiif_renderings function."""
    
    def test_skips_when_disabled(self, temp_dir: str):
        """Test that download is skipped when disabled."""
        from api.utils import download_iiif_renderings
        
        manifest = {
            "rendering": [{"@id": "https://example.com/pdf", "format": "application/pdf"}]
        }
        
        with patch("api.utils.get_download_config", return_value={"download_manifest_renderings": False}):
            count = download_iiif_renderings(manifest, temp_dir)
        
        assert count == 0
    
    def test_respects_whitelist(self, temp_dir: str):
        """Test that only whitelisted formats are downloaded."""
        from api.utils import download_iiif_renderings
        
        manifest = {
            "rendering": [
                {"@id": "https://example.com/pdf", "format": "application/pdf"},
                {"@id": "https://example.com/html", "format": "text/html"}
            ]
        }
        
        config = {
            "download_manifest_renderings": True,
            "rendering_mime_whitelist": ["application/pdf"],
            "max_renderings_per_manifest": 10
        }
        
        with patch("api.utils.get_download_config", return_value=config):
            with patch("api.utils.download_file", return_value="/path/to/file") as mock_dl:
                count = download_iiif_renderings(manifest, temp_dir)
        
        # Should only attempt PDF download
        assert mock_dl.call_count == 1
    
    def test_respects_max_limit(self, temp_dir: str):
        """Test that max renderings limit is respected."""
        from api.utils import download_iiif_renderings
        
        manifest = {
            "rendering": [
                {"@id": f"https://example.com/pdf{i}", "format": "application/pdf"}
                for i in range(5)
            ]
        }
        
        config = {
            "download_manifest_renderings": True,
            "rendering_mime_whitelist": ["application/pdf"],
            "max_renderings_per_manifest": 2
        }
        
        with patch("api.utils.get_download_config", return_value=config):
            with patch("api.utils.download_file", return_value="/path/to/file") as mock_dl:
                count = download_iiif_renderings(manifest, temp_dir)
        
        assert mock_dl.call_count <= 2
