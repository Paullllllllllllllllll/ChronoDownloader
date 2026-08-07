"""Tests for api.download_helpers module — shared download patterns."""

from __future__ import annotations

import json
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
        # Filename shape is part of the connector contract: every provider
        # consolidated onto this helper must keep producing
        # "{provider_key}_{item_id}_p{index:05d}.jpg" in the work folder.
        assert [c.args[1] for c in mock_dl.call_args_list] == ["/out"] * 3
        assert [c.args[2] for c in mock_dl.call_args_list] == [
            "test_provider_item123_p00001.jpg",
            "test_provider_item123_p00002.jpg",
            "test_provider_item123_p00003.jpg",
        ]

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
    @patch("api.iiif._strategies.budget_exhausted")
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_stops_mid_loop_when_budget_runs_out(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        """The budget guard must abandon the remaining pages, not only refuse
        to start: pages already written still count as a success."""
        from api.iiif import download_page_images

        mock_budget.side_effect = [False, True]
        mock_dl.return_value = True
        result = download_page_images(
            ["https://svc/1", "https://svc/2", "https://svc/3"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is True
        assert mock_dl.call_count == 1

    @patch("api.iiif._strategies.download_one_from_service")
    @patch("api.iiif._strategies.budget_exhausted")
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_stops_when_budget_runs_out_during_a_page(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        """A failed page that turns out to be a budget refusal ends the loop
        rather than burning the remaining pages on doomed requests."""
        from api.iiif import download_page_images

        # Iteration 1: guard passes, download fails, post-failure guard trips.
        mock_budget.side_effect = [False, True]
        mock_dl.return_value = False
        result = download_page_images(
            ["https://svc/1", "https://svc/2", "https://svc/3"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is False
        assert mock_dl.call_count == 1

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

    @patch("api.iiif._strategies.download_one_from_service")
    @patch("api.iiif._strategies.budget_exhausted", return_value=False)
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_exception_on_one_page_does_not_abort_the_work(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        """Per-page recovery: one blown-up canvas must not cost the rest."""
        from api.iiif import download_page_images

        mock_dl.side_effect = [Exception("network error"), True, True]
        result = download_page_images(
            ["https://svc/1", "https://svc/2", "https://svc/3"],
            "/out",
            "test_provider",
            "item123",
        )
        assert result is True
        assert mock_dl.call_count == 3


# ============================================================================
# download_direct_image_urls
# ============================================================================


class TestDownloadDirectImageUrls:
    """Tests for the direct whole-image URL loop (no IIIF Image API service)."""

    @patch("api.iiif._strategies.download_file")
    @patch("api.iiif._strategies.budget_exhausted", return_value=False)
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_downloads_all_urls_with_page_numbered_names(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_direct_image_urls

        mock_dl.return_value = "/out/f"
        result = download_direct_image_urls(
            ["https://img/1", "https://img/2"],
            "/out",
            "europeana",
            "item123",
        )
        assert result is True
        assert [c.args[2] for c in mock_dl.call_args_list] == [
            "europeana_item123_p00001",
            "europeana_item123_p00002",
        ]

    @patch("api.iiif._strategies.download_file")
    @patch("api.iiif._strategies.budget_exhausted", return_value=False)
    @patch("api.iiif._strategies.get_max_pages", return_value=1)
    def test_respects_max_pages_from_config(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_direct_image_urls

        mock_dl.return_value = "/out/f"
        result = download_direct_image_urls(
            ["https://img/1", "https://img/2"],
            "/out",
            "europeana",
            "item123",
        )
        assert result is True
        assert mock_dl.call_count == 1
        mock_max.assert_called_once_with("europeana")

    def test_returns_false_for_empty_url_list(self) -> None:
        from api.iiif import download_direct_image_urls

        assert download_direct_image_urls([], "/out", "europeana", "item123") is False

    @patch("api.iiif._strategies.download_file")
    @patch("api.iiif._strategies.budget_exhausted")
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_stops_mid_loop_when_budget_runs_out(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_direct_image_urls

        mock_budget.side_effect = [False, True]
        mock_dl.return_value = "/out/f"
        result = download_direct_image_urls(
            ["https://img/1", "https://img/2", "https://img/3"],
            "/out",
            "europeana",
            "item123",
        )
        assert result is True
        assert mock_dl.call_count == 1

    @patch("api.iiif._strategies.download_file")
    @patch("api.iiif._strategies.budget_exhausted", return_value=False)
    @patch("api.iiif._strategies.get_max_pages", return_value=None)
    def test_exception_on_one_url_does_not_abort_the_work(
        self, mock_max: MagicMock, mock_budget: MagicMock, mock_dl: MagicMock
    ) -> None:
        from api.iiif import download_direct_image_urls

        mock_dl.side_effect = [Exception("boom"), "/out/f"]
        result = download_direct_image_urls(
            ["https://img/1", "https://img/2"],
            "/out",
            "europeana",
            "item123",
        )
        assert result is True
        assert mock_dl.call_count == 2


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


# ============================================================================
# sniff_extension — payload type from the leading bytes
# ============================================================================


class TestSniffExtension:
    """Magic-byte recognition for every payload type the tool retrieves."""

    def test_recognizes_documents_and_images(self) -> None:
        from api.core.download import sniff_extension

        assert sniff_extension(b"%PDF-1.7\n%\xe2\xe3") == ".pdf"
        assert sniff_extension(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == ".png"
        assert sniff_extension(b"\xff\xd8\xff\xe0JFIF") == ".jpg"
        assert sniff_extension(b"II*\x00\x08\x00") == ".tif"
        assert sniff_extension(b"MM\x00*\x00\x00") == ".tif"
        assert sniff_extension(b"\x00\x00\x00\x0cjP  \r\n\x87\n") == ".jp2"
        assert sniff_extension(b"\xff\x4f\xff\x51\x00") == ".jp2"

    def test_distinguishes_epub_from_plain_zip(self) -> None:
        """Both are zips; only the EPUB names its media type in the first entry."""
        from api.core.download import sniff_extension

        epub = (
            b"PK\x03\x04\x14\x00\x00\x00\x00\x00"
            + b"\x00" * 16
            + b"mimetypeapplication/epub+zip"
        )
        assert sniff_extension(epub) == ".epub"
        assert sniff_extension(b"PK\x03\x04\x14\x00\x00\x00" + b"\x00" * 24) == ".zip"

    def test_returns_empty_for_unknown_and_empty_payloads(self) -> None:
        from api.core.download import sniff_extension

        assert sniff_extension(b"") == ""
        assert sniff_extension(b"not a recognizable payload at all") == ""


# ============================================================================
# Extension resolution and placement for endpoint-shaped URLs
# ============================================================================

_PLACEMENT_DL_CFG = {
    "allowed_object_extensions": [".pdf", ".epub", ".zip", ".jpg", ".png"],
    "save_disallowed_to_metadata": True,
}

# UB Heidelberg serves a whole-document PDF from a CGI endpoint; the shape,
# not the host, is what matters here -- any provider's script endpoint behaves
# the same way.
_ENDPOINT_URL = "https://example.org/diglitData/pdf/cpg234.fcgi"

_PDF_PAYLOAD = b"%PDF-1.7\n" + b"x" * 64


def _dh_download(
    url: str, folder: str, headers: dict[str, str], payload: bytes
) -> tuple[str | None, MagicMock]:
    """Run one mocked download and return its result plus the session mock."""
    session = _dh_make_session(_dh_make_response(headers, payload))
    dl_mod._BUDGET._exhausted = False
    with (
        patch.object(dl_mod, "get_session", return_value=session),
        patch("api.core.download.get_download_config", return_value=_PLACEMENT_DL_CFG),
    ):
        return dl_mod.download_file(url, folder, "rendering_01"), session


class TestPayloadTypeResolution:
    """A document served from an endpoint URL must be saved under the payload's
    own extension and filed with the work's objects, not as an unrecognized byte
    stream in metadata/. Pre-fix the extension came from the URL path, so UB
    Heidelberg's whole-document PDF landed in metadata/ as a ``.fcgi`` file and
    every extension-based tool downstream misread it.
    """

    def test_content_type_header_decides_extension(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        folder = str(tmp_path / "work")
        saved, _ = _dh_download(
            _ENDPOINT_URL, folder, {"Content-Type": "application/pdf"}, _PDF_PAYLOAD
        )
        assert saved is not None
        assert saved.endswith(".pdf")
        assert os.path.dirname(saved) == os.path.join(folder, "objects")

    def test_magic_bytes_resolve_a_generic_content_type(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        folder = str(tmp_path / "work")
        saved, _ = _dh_download(
            _ENDPOINT_URL,
            folder,
            {"Content-Type": "application/octet-stream"},
            _PDF_PAYLOAD,
        )
        assert saved is not None
        assert saved.endswith(".pdf")
        assert os.path.dirname(saved) == os.path.join(folder, "objects")
        assert not os.path.isdir(os.path.join(folder, "metadata"))

    def test_magic_bytes_resolve_a_missing_content_type(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        folder = str(tmp_path / "work")
        epub = (
            b"PK\x03\x04\x14\x00\x00\x00\x00\x00"
            + b"\x00" * 16
            + b"mimetypeapplication/epub+zip"
            + b"y" * 32
        )
        saved, _ = _dh_download(_ENDPOINT_URL, folder, {}, epub)
        assert saved is not None
        assert saved.endswith(".epub")
        assert os.path.dirname(saved) == os.path.join(folder, "objects")

    def test_declared_content_type_wins_over_the_payload_guess(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """Sniffing is a fallback, not an override: a declared type is honored
        (and the existing magic-byte validator still guards it)."""
        folder = str(tmp_path / "work")
        saved, _ = _dh_download(
            _ENDPOINT_URL, folder, {"Content-Type": "image/png"}, _PDF_PAYLOAD
        )
        assert saved is not None
        assert saved.endswith(".png")

    def test_url_suffix_still_wins_when_it_names_a_format(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """An ``application/octet-stream`` PDF at a ``.pdf`` URL keeps the URL's
        extension; only endpoint-shaped suffixes are second-guessed."""
        folder = str(tmp_path / "work")
        saved, _ = _dh_download(
            "https://example.org/books/cpg234.pdf",
            folder,
            {"Content-Type": "application/octet-stream"},
            _PDF_PAYLOAD,
        )
        assert saved is not None
        assert saved.endswith(".pdf")

    def test_unresolvable_payload_keeps_the_previous_behavior(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """When neither the header nor the bytes settle the type, the URL suffix
        stands and the file is filed under metadata/ as before."""
        folder = str(tmp_path / "work")
        saved, _ = _dh_download(
            _ENDPOINT_URL,
            folder,
            {"Content-Type": "application/octet-stream"},
            b"\x01\x02\x03 nothing recognizable " + b"z" * 32,
        )
        assert saved is None
        metadata_dir = os.path.join(folder, "metadata")
        assert os.listdir(metadata_dir) == ["rendering_01_unknown.fcgi"]

    def test_rerun_does_not_refetch_a_resolved_file(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """Resume: the skip check must find the file under the extension a
        previous run resolved, not only under the endpoint suffix."""
        folder = str(tmp_path / "work")
        first, _ = _dh_download(
            _ENDPOINT_URL,
            folder,
            {"Content-Type": "application/octet-stream"},
            _PDF_PAYLOAD,
        )
        assert first is not None

        reset_counters()
        second, session = _dh_download(
            _ENDPOINT_URL,
            folder,
            {"Content-Type": "application/octet-stream"},
            _PDF_PAYLOAD,
        )
        assert second == first
        session.get.assert_not_called()
        assert len(os.listdir(os.path.join(folder, "objects"))) == 1

    def test_legacy_endpoint_suffixed_file_is_still_recognized(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        """Backward compatibility: a file saved by an older version under the
        endpoint suffix in metadata/ must not be downloaded again."""
        folder = str(tmp_path / "work")
        metadata_dir = os.path.join(folder, "metadata")
        os.makedirs(metadata_dir, exist_ok=True)
        legacy = os.path.join(metadata_dir, "rendering_01_unknown.fcgi")
        with open(legacy, "wb") as f:
            f.write(_PDF_PAYLOAD)

        saved, session = _dh_download(
            _ENDPOINT_URL,
            folder,
            {"Content-Type": "application/octet-stream"},
            _PDF_PAYLOAD,
        )
        # Routed to metadata/, so not a successful object download -- exactly as
        # the pre-existing disallowed-extension contract requires.
        assert saved is None
        session.get.assert_not_called()
        assert os.path.exists(legacy)


class TestRenderingPlacement:
    """End to end for the reported defect: a manifest rendering served by a CGI
    endpoint is a whole-document derivative, so it belongs with the work's
    objects under its payload extension -- not in metadata/ as a ``.fcgi``.
    """

    def test_endpoint_rendering_lands_in_objects_as_a_pdf(
        self, tmp_path: Any, mock_config: dict[str, Any]
    ) -> None:
        from api.iiif._renderings import download_iiif_renderings

        folder = str(tmp_path / "work")
        os.makedirs(folder, exist_ok=True)
        manifest = {
            "rendering": {
                "@id": _ENDPOINT_URL,
                "format": "application/pdf",
                "label": "Download as PDF",
            }
        }
        session = _dh_make_session(
            _dh_make_response(
                {"Content-Type": "application/octet-stream"}, _PDF_PAYLOAD
            )
        )

        dl_mod._BUDGET._exhausted = False
        with (
            patch.object(dl_mod, "get_session", return_value=session),
            patch(
                "api.core.download.get_download_config",
                return_value=_PLACEMENT_DL_CFG,
            ),
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 1},
            ),
        ):
            assert download_iiif_renderings(manifest, folder) == 1

        objects = os.listdir(os.path.join(folder, "objects"))
        assert len(objects) == 1
        assert objects[0].endswith(".pdf")
        assert not os.path.isdir(os.path.join(folder, "metadata"))

        with open(os.path.join(folder, "work.json"), encoding="utf-8") as f:
            recorded = json.load(f)["renderings"]
        assert recorded[0]["url"] == _ENDPOINT_URL
        assert recorded[0]["resolved_media_type"] == "application/pdf"
        assert recorded[0]["saved_as"] == f"objects/{objects[0]}"
