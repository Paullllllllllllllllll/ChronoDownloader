"""Tests for api.download_helpers module — shared download patterns."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

from api.core import download as dl_mod
from api.core.context import reset_counters

# ============================================================================
# download_page_images
# ============================================================================


class TestDownloadPageImages:
    """Tests for page image download loop."""

    @patch("api.iiif._strategies.download_one_from_service")
    @patch("api.iiif._strategies.budget_exhausted", return_value=False)
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_downloads_all_pages(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_page_images

        mock_dl.return_value = True
        result = download_page_images(
            ["https://svc/1", "https://svc/2", "https://svc/3"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is True
        assert mock_dl.call_count == 3

    @patch("api.iiif._strategies.download_one_from_service")
    @patch("api.iiif._strategies.budget_exhausted", return_value=False)
    @patch("api.iiif._strategies.get_max_pages", return_value=2)
    def test_respects_max_pages_from_config(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_page_images

        mock_dl.return_value = True
        result = download_page_images(
            ["https://svc/1", "https://svc/2", "https://svc/3"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is True
        assert mock_dl.call_count == 2

    @patch("api.iiif._strategies.download_one_from_service")
    @patch("api.iiif._strategies.budget_exhausted", return_value=False)
    def test_respects_max_pages_override(
        self, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_page_images

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

    def test_returns_false_for_empty_service_bases(self) -> None:
        from api.iiif import download_page_images

        result = download_page_images([], "/out", "test_provider", "item123")
        assert result is False

    @patch("api.iiif._strategies.download_one_from_service")
    @patch("api.iiif._strategies.budget_exhausted")
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_stops_on_budget_exhausted(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_page_images

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

    @patch("api.iiif._strategies.download_one_from_service")
    @patch("api.iiif._strategies.budget_exhausted", return_value=False)
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_handles_download_failure(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_page_images

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

    @patch("api.iiif._strategies.download_one_from_service")
    @patch("api.iiif._strategies.budget_exhausted", return_value=False)
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_handles_download_exception(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_page_images

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

    @patch("api.iiif._strategies.download_page_images")
    @patch("api.iiif._strategies.extract_image_service_bases")
    @patch("api.iiif._strategies.download_iiif_renderings")
    @patch("api.iiif._strategies.save_json")
    @patch("api.iiif._strategies.make_request")
    def test_successful_download_with_renderings(
        self,
        mock_req: MagicMock,
        mock_save: MagicMock,
        mock_render: MagicMock,
        mock_extract: MagicMock,
        mock_pages: MagicMock,
    ) -> None:
        from api.iiif import download_iiif_manifest_and_images

        mock_req.return_value = {"@context": "v2", "sequences": []}
        mock_render.return_value = 1
        with patch("api.iiif._strategies.prefer_pdf_over_images", return_value=True):
            result = download_iiif_manifest_and_images(
                "https://example.org/manifest.json",
                "/out",
                "gallica",
                "bpt6k123",
            )
        assert result is True
        mock_pages.assert_not_called()

    @patch("api.iiif._strategies.download_page_images")
    @patch("api.iiif._strategies.extract_image_service_bases")
    @patch("api.iiif._strategies.download_iiif_renderings")
    @patch("api.iiif._strategies.save_json")
    @patch("api.iiif._strategies.make_request")
    def test_falls_back_to_images_when_no_renderings(
        self,
        mock_req: MagicMock,
        mock_save: MagicMock,
        mock_render: MagicMock,
        mock_extract: MagicMock,
        mock_pages: MagicMock,
    ) -> None:
        from api.iiif import download_iiif_manifest_and_images

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

    @patch("api.iiif._strategies.make_request")
    def test_returns_false_for_invalid_manifest(self, mock_req: MagicMock) -> None:
        from api.iiif import download_iiif_manifest_and_images

        mock_req.return_value = None
        result = download_iiif_manifest_and_images(
            "https://example.org/manifest.json",
            "/out",
            "gallica",
            "bpt6k123",
        )
        assert result is False

    @patch("api.iiif._strategies.download_page_images")
    @patch("api.iiif._strategies.extract_image_service_bases")
    @patch("api.iiif._strategies.download_iiif_renderings")
    @patch("api.iiif._strategies.save_json")
    @patch("api.iiif._strategies.make_request")
    def test_downloads_images_when_not_preferring_pdf(
        self,
        mock_req: MagicMock,
        mock_save: MagicMock,
        mock_render: MagicMock,
        mock_extract: MagicMock,
        mock_pages: MagicMock,
    ) -> None:
        from api.iiif import download_iiif_manifest_and_images

        mock_req.return_value = {"@context": "v2"}
        mock_render.return_value = 1
        mock_extract.return_value = ["https://svc/1"]
        mock_pages.return_value = True
        with patch("api.iiif._strategies.prefer_pdf_over_images", return_value=False):
            result = download_iiif_manifest_and_images(
                "https://example.org/manifest.json",
                "/out",
                "gallica",
                "bpt6k123",
            )
        assert result is True
        mock_pages.assert_called_once()

    @patch("api.iiif._strategies.download_page_images")
    @patch("api.iiif._strategies.extract_image_service_bases")
    @patch("api.iiif._strategies.download_iiif_renderings")
    @patch("api.iiif._strategies.save_json")
    @patch("api.iiif._strategies.make_request")
    def test_rendering_exception_handled(
        self,
        mock_req: MagicMock,
        mock_save: MagicMock,
        mock_render: MagicMock,
        mock_extract: MagicMock,
        mock_pages: MagicMock,
    ) -> None:
        from api.iiif import download_iiif_manifest_and_images

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

    @patch("api.iiif._strategies.download_file")
    @patch("api.iiif._strategies.prefer_pdf_over_images", return_value=True)
    def test_stops_after_pdf_when_preferred(
        self, mock_pref: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import try_pdf_first_then_images

        mock_dl.return_value = "/path/to/file.pdf"
        result = try_pdf_first_then_images(
            ["https://example.org/file.pdf"],
            "https://example.org/manifest.json",
            "/out",
            "ia",
            "item123",
        )
        assert result is True

    @patch("api.iiif._strategies.download_iiif_manifest_and_images")
    @patch("api.iiif._strategies.download_file")
    @patch("api.iiif._strategies.prefer_pdf_over_images", return_value=False)
    def test_downloads_both_when_not_preferring_pdf(
        self, mock_pref: MagicMock, mock_dl: MagicMock, mock_iiif: MagicMock
    ) -> None:
        from api.iiif import try_pdf_first_then_images

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

    @patch("api.iiif._strategies.download_iiif_manifest_and_images")
    @patch("api.iiif._strategies.download_file")
    @patch("api.iiif._strategies.prefer_pdf_over_images", return_value=True)
    def test_falls_back_to_iiif_when_pdf_fails(
        self, mock_pref: MagicMock, mock_dl: MagicMock, mock_iiif: MagicMock
    ) -> None:
        from api.iiif import try_pdf_first_then_images

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

    @patch("api.iiif._strategies.download_file")
    def test_skips_empty_urls(self, mock_dl: MagicMock) -> None:
        from api.iiif import try_pdf_first_then_images

        mock_dl.return_value = None
        result = try_pdf_first_then_images(
            ["", None],  # type: ignore[list-item]
            None,
            "/out",
            "ia",
            "item123",
        )
        assert result is False
        mock_dl.assert_not_called()

    @patch("api.iiif._strategies.download_file")
    def test_no_manifest_and_no_pdf(self, mock_dl: MagicMock) -> None:
        from api.iiif import try_pdf_first_then_images

        mock_dl.return_value = None
        result = try_pdf_first_then_images(
            ["https://example.org/file.pdf"],
            None,
            "/out",
            "ia",
            "item123",
        )
        assert result is False

    @patch("api.iiif._strategies.download_iiif_manifest_and_images")
    @patch("api.iiif._strategies.download_file")
    @patch("api.iiif._strategies.prefer_pdf_over_images", return_value=True)
    def test_handles_pdf_exception(
        self, mock_pref: MagicMock, mock_dl: MagicMock, mock_iiif: MagicMock
    ) -> None:
        from api.iiif import try_pdf_first_then_images

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


# ============================================================================
# _parse_content_length
# ============================================================================


class TestParseContentLength:
    """Content-Length parsing: malformed values must degrade to None."""

    def test_parses_valid_length(self) -> None:
        from api.core.download import _parse_content_length

        assert _parse_content_length("1024") == 1024
        assert _parse_content_length("0") == 0

    def test_returns_none_for_missing_or_malformed(self) -> None:
        from api.core.download import _parse_content_length

        assert _parse_content_length(None) is None
        assert _parse_content_length("") is None
        assert _parse_content_length("abc") is None

    def test_returns_none_for_negative_length(self) -> None:
        """A negative declared length is malformed and must be treated as
        unknown, not as a size mismatch that discards a complete file."""
        from api.core.download import _parse_content_length

        assert _parse_content_length("-5") is None


# ============================================================================
# _try_skip_existing / download_file — metadata-routed files never "succeed"
# ============================================================================


def _dh_make_session(response: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=response)
    cm.__exit__ = MagicMock(return_value=False)
    session = MagicMock()
    session.get.return_value = cm
    return session


def _dh_make_response(headers: dict[str, str], content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = headers
    resp.iter_content = MagicMock(return_value=iter([content]))
    resp.raise_for_status = MagicMock()
    return resp


class TestExistingFileSkipRouting:
    """A skip-check hit on a file already routed to metadata/ must not be
    reported as a successful download, matching a fresh download of the same
    disallowed extension. Pre-fix, ``_try_skip_existing`` returned only the
    path and any existing file counted as success, including one sitting in
    metadata/.
    """

    def test_existing_metadata_routed_file_returns_none(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        folder = str(tmp_path / "work")
        dl_cfg = {
            "allowed_object_extensions": [".pdf"],
            "save_disallowed_to_metadata": True,
        }
        resp = _dh_make_response(
            {"Content-Type": ""}, b"<xml>not an allowed object type</xml>"
        )
        session = _dh_make_session(resp)

        dl_mod._BUDGET._exhausted = False
        with (
            patch.object(dl_mod, "get_session", return_value=session),
            patch("api.core.download.get_download_config", return_value=dl_cfg),
        ):
            # Fresh download: the .xml extension is not in the allowed list,
            # so it is routed to metadata/ and does not count as success.
            first = dl_mod.download_file(
                "https://example.org/notes.xml", folder, "notes"
            )
            assert first is None
            metadata_dir = os.path.join(folder, "metadata")
            assert os.path.isdir(metadata_dir)
            assert len(os.listdir(metadata_dir)) == 1

            # Simulate a resumed run (counters reset, as work_context() does
            # per work) hitting the same URL: the early skip-existing path
            # must report the same non-success outcome as the fresh download.
            reset_counters()
            second = dl_mod.download_file(
                "https://example.org/notes.xml", folder, "notes"
            )
            assert second is None
            assert len(os.listdir(metadata_dir)) == 1

    def test_existing_allowed_extension_still_returns_path(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """Counterpart: an existing ALLOWED object extension still counts."""
        folder = str(tmp_path / "work")
        dl_cfg = {
            "allowed_object_extensions": [".pdf"],
            "save_disallowed_to_metadata": True,
        }
        payload = b"%PDF-1.4\n" + b"x" * 16
        resp = _dh_make_response({"Content-Type": "application/pdf"}, payload)
        session = _dh_make_session(resp)

        dl_mod._BUDGET._exhausted = False
        with (
            patch.object(dl_mod, "get_session", return_value=session),
            patch("api.core.download.get_download_config", return_value=dl_cfg),
        ):
            first = dl_mod.download_file("https://example.org/book.pdf", folder, "book")
            assert first is not None

            reset_counters()
            second = dl_mod.download_file(
                "https://example.org/book.pdf", folder, "book"
            )
            assert second == first
