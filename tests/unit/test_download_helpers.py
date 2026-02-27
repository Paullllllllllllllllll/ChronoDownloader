"""Tests for api.download_helpers module — shared download patterns."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# download_page_images
# ============================================================================

class TestDownloadPageImages:
    """Tests for page image download loop."""

    @patch("api.download_helpers.download_one_from_service")
    @patch("api.download_helpers.budget_exhausted", return_value=False)
    @patch("api.download_helpers.get_max_pages", return_value=None)
    def test_downloads_all_pages(self, mock_max, mock_budget, mock_dl):
        from api.download_helpers import download_page_images

        mock_dl.return_value = True
        result = download_page_images(
            ["https://svc/1", "https://svc/2", "https://svc/3"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is True
        assert mock_dl.call_count == 3

    @patch("api.download_helpers.download_one_from_service")
    @patch("api.download_helpers.budget_exhausted", return_value=False)
    @patch("api.download_helpers.get_max_pages", return_value=2)
    def test_respects_max_pages_from_config(self, mock_max, mock_budget, mock_dl):
        from api.download_helpers import download_page_images

        mock_dl.return_value = True
        result = download_page_images(
            ["https://svc/1", "https://svc/2", "https://svc/3"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is True
        assert mock_dl.call_count == 2

    @patch("api.download_helpers.download_one_from_service")
    @patch("api.download_helpers.budget_exhausted", return_value=False)
    def test_respects_max_pages_override(self, mock_budget, mock_dl):
        from api.download_helpers import download_page_images

        mock_dl.return_value = True
        result = download_page_images(
            ["https://svc/1", "https://svc/2", "https://svc/3"],
            "/out",
            "test_provider",
            "item123",
            max_pages=1,
        )
        assert result is True
        assert mock_dl.call_count == 1

    def test_returns_false_for_empty_service_bases(self):
        from api.download_helpers import download_page_images

        result = download_page_images([], "/out", "test_provider", "item123")
        assert result is False

    @patch("api.download_helpers.download_one_from_service")
    @patch("api.download_helpers.budget_exhausted")
    @patch("api.download_helpers.get_max_pages", return_value=None)
    def test_stops_on_budget_exhausted(self, mock_max, mock_budget, mock_dl):
        from api.download_helpers import download_page_images

        mock_budget.side_effect = [True]
        mock_dl.return_value = True
        result = download_page_images(
            ["https://svc/1", "https://svc/2"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is False
        mock_dl.assert_not_called()

    @patch("api.download_helpers.download_one_from_service")
    @patch("api.download_helpers.budget_exhausted", return_value=False)
    @patch("api.download_helpers.get_max_pages", return_value=None)
    def test_handles_download_failure(self, mock_max, mock_budget, mock_dl):
        from api.download_helpers import download_page_images

        mock_dl.side_effect = [False, True]
        # Second budget check after first download failure should also return False
        mock_budget.side_effect = [False, False, False]
        result = download_page_images(
            ["https://svc/1", "https://svc/2"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is True

    @patch("api.download_helpers.download_one_from_service")
    @patch("api.download_helpers.budget_exhausted", return_value=False)
    @patch("api.download_helpers.get_max_pages", return_value=None)
    def test_handles_download_exception(self, mock_max, mock_budget, mock_dl):
        from api.download_helpers import download_page_images

        mock_dl.side_effect = Exception("network error")
        result = download_page_images(
            ["https://svc/1"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is False


# ============================================================================
# download_iiif_manifest_and_images
# ============================================================================

class TestDownloadIIIFManifestAndImages:
    """Tests for manifest-level download orchestration."""

    @patch("api.download_helpers.download_page_images")
    @patch("api.download_helpers.extract_image_service_bases")
    @patch("api.download_helpers.download_iiif_renderings")
    @patch("api.download_helpers.save_json")
    @patch("api.download_helpers.make_request")
    def test_successful_download_with_renderings(
        self, mock_req, mock_save, mock_render, mock_extract, mock_pages
    ):
        from api.download_helpers import download_iiif_manifest_and_images

        mock_req.return_value = {"@context": "v2", "sequences": []}
        mock_render.return_value = 1
        with patch("api.download_helpers.prefer_pdf_over_images", return_value=True):
            result = download_iiif_manifest_and_images(
                "https://example.org/manifest.json",
                "/out",
                "gallica",
                "bpt6k123",
            )
        assert result is True
        mock_pages.assert_not_called()

    @patch("api.download_helpers.download_page_images")
    @patch("api.download_helpers.extract_image_service_bases")
    @patch("api.download_helpers.download_iiif_renderings")
    @patch("api.download_helpers.save_json")
    @patch("api.download_helpers.make_request")
    def test_falls_back_to_images_when_no_renderings(
        self, mock_req, mock_save, mock_render, mock_extract, mock_pages
    ):
        from api.download_helpers import download_iiif_manifest_and_images

        mock_req.return_value = {"@context": "v2", "sequences": []}
        mock_render.return_value = 0
        mock_extract.return_value = ["https://svc/1"]
        mock_pages.return_value = True
        result = download_iiif_manifest_and_images(
            "https://example.org/manifest.json",
            "/out",
            "gallica",
            "bpt6k123",
        )
        assert result is True
        mock_pages.assert_called_once()

    @patch("api.download_helpers.make_request")
    def test_returns_false_for_invalid_manifest(self, mock_req):
        from api.download_helpers import download_iiif_manifest_and_images

        mock_req.return_value = None
        result = download_iiif_manifest_and_images(
            "https://example.org/manifest.json",
            "/out",
            "gallica",
            "bpt6k123",
        )
        assert result is False

    @patch("api.download_helpers.download_page_images")
    @patch("api.download_helpers.extract_image_service_bases")
    @patch("api.download_helpers.download_iiif_renderings")
    @patch("api.download_helpers.save_json")
    @patch("api.download_helpers.make_request")
    def test_downloads_images_when_not_preferring_pdf(
        self, mock_req, mock_save, mock_render, mock_extract, mock_pages
    ):
        from api.download_helpers import download_iiif_manifest_and_images

        mock_req.return_value = {"@context": "v2"}
        mock_render.return_value = 1
        mock_extract.return_value = ["https://svc/1"]
        mock_pages.return_value = True
        with patch("api.download_helpers.prefer_pdf_over_images", return_value=False):
            result = download_iiif_manifest_and_images(
                "https://example.org/manifest.json",
                "/out",
                "gallica",
                "bpt6k123",
            )
        assert result is True
        mock_pages.assert_called_once()

    @patch("api.download_helpers.download_page_images")
    @patch("api.download_helpers.extract_image_service_bases")
    @patch("api.download_helpers.download_iiif_renderings")
    @patch("api.download_helpers.save_json")
    @patch("api.download_helpers.make_request")
    def test_rendering_exception_handled(
        self, mock_req, mock_save, mock_render, mock_extract, mock_pages
    ):
        from api.download_helpers import download_iiif_manifest_and_images

        mock_req.return_value = {"@context": "v2"}
        mock_render.side_effect = Exception("rendering error")
        mock_extract.return_value = ["https://svc/1"]
        mock_pages.return_value = True
        result = download_iiif_manifest_and_images(
            "https://example.org/manifest.json",
            "/out",
            "gallica",
            "bpt6k123",
        )
        assert result is True


# ============================================================================
# try_pdf_first_then_images
# ============================================================================

class TestTryPdfFirstThenImages:
    """Tests for PDF-first download with IIIF fallback."""

    @patch("api.download_helpers.download_file")
    @patch("api.download_helpers.prefer_pdf_over_images", return_value=True)
    def test_stops_after_pdf_when_preferred(self, mock_pref, mock_dl):
        from api.download_helpers import try_pdf_first_then_images

        mock_dl.return_value = "/path/to/file.pdf"
        result = try_pdf_first_then_images(
            ["https://example.org/file.pdf"],
            "https://example.org/manifest.json",
            "/out",
            "ia",
            "item123",
        )
        assert result is True

    @patch("api.download_helpers.download_iiif_manifest_and_images")
    @patch("api.download_helpers.download_file")
    @patch("api.download_helpers.prefer_pdf_over_images", return_value=False)
    def test_downloads_both_when_not_preferring_pdf(self, mock_pref, mock_dl, mock_iiif):
        from api.download_helpers import try_pdf_first_then_images

        mock_dl.return_value = "/path/to/file.pdf"
        mock_iiif.return_value = True
        result = try_pdf_first_then_images(
            ["https://example.org/file.pdf"],
            "https://example.org/manifest.json",
            "/out",
            "ia",
            "item123",
        )
        assert result is True
        mock_iiif.assert_called_once()

    @patch("api.download_helpers.download_iiif_manifest_and_images")
    @patch("api.download_helpers.download_file")
    @patch("api.download_helpers.prefer_pdf_over_images", return_value=True)
    def test_falls_back_to_iiif_when_pdf_fails(self, mock_pref, mock_dl, mock_iiif):
        from api.download_helpers import try_pdf_first_then_images

        mock_dl.return_value = None
        mock_iiif.return_value = True
        result = try_pdf_first_then_images(
            ["https://example.org/file.pdf"],
            "https://example.org/manifest.json",
            "/out",
            "ia",
            "item123",
        )
        assert result is True
        mock_iiif.assert_called_once()

    @patch("api.download_helpers.download_file")
    def test_skips_empty_urls(self, mock_dl):
        from api.download_helpers import try_pdf_first_then_images

        mock_dl.return_value = None
        result = try_pdf_first_then_images(
            ["", None],
            None,
            "/out",
            "ia",
            "item123",
        )
        assert result is False
        mock_dl.assert_not_called()

    @patch("api.download_helpers.download_file")
    def test_no_manifest_and_no_pdf(self, mock_dl):
        from api.download_helpers import try_pdf_first_then_images

        mock_dl.return_value = None
        result = try_pdf_first_then_images(
            ["https://example.org/file.pdf"],
            None,
            "/out",
            "ia",
            "item123",
        )
        assert result is False

    @patch("api.download_helpers.download_iiif_manifest_and_images")
    @patch("api.download_helpers.download_file")
    @patch("api.download_helpers.prefer_pdf_over_images", return_value=True)
    def test_handles_pdf_exception(self, mock_pref, mock_dl, mock_iiif):
        from api.download_helpers import try_pdf_first_then_images

        mock_dl.side_effect = Exception("download error")
        mock_iiif.return_value = True
        result = try_pdf_first_then_images(
            ["https://example.org/file.pdf"],
            "https://example.org/manifest.json",
            "/out",
            "ia",
            "item123",
        )
        assert result is True
