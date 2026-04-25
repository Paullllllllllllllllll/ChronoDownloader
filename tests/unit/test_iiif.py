"""Tests for api.iiif module — IIIF manifest parsing and image URL generation."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from api.iiif import (
    download_one_from_service,
    extract_direct_image_urls,
    extract_image_service_bases,
    image_url_candidates,
)
from api.iiif._parsing import _INFO_JSON_CACHE, _fetch_info_json


# ============================================================================
# extract_image_service_bases – IIIF v2
# ============================================================================

class TestExtractImageServiceBasesV2:
    """Tests for IIIF v2 manifest parsing."""

    def test_extracts_service_ids_from_v2(self, sample_iiif_manifest_v2: dict[str, Any]) -> None:
        bases = extract_image_service_bases(sample_iiif_manifest_v2)
        assert len(bases) == 2
        assert bases[0] == "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f1"
        assert bases[1] == "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f2"

    def test_extracts_from_resource_id_fallback(self) -> None:
        """Fallback when service is missing but resource @id contains /full/."""
        manifest = {
            "sequences": [{
                "canvases": [{
                    "images": [{
                        "resource": {
                            "@id": "https://example.org/iiif/img1/full/max/0/default.jpg",
                            "service": {}
                        }
                    }]
                }]
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_skips_canvas_without_images(self) -> None:
        manifest = {
            "sequences": [{
                "canvases": [
                    {"images": []},
                    {
                        "images": [{
                            "resource": {
                                "service": {"@id": "https://example.org/iiif/img1"}
                            }
                        }]
                    },
                ]
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_empty_manifest_returns_empty_list(self) -> None:
        assert extract_image_service_bases({}) == []

    def test_deduplicates_service_bases(self) -> None:
        manifest = {
            "sequences": [{
                "canvases": [
                    {"images": [{"resource": {"service": {"@id": "https://example.org/img"}}}]},
                    {"images": [{"resource": {"service": {"@id": "https://example.org/img"}}}]},
                ]
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/img"]

    def test_uses_id_key_when_at_id_missing(self) -> None:
        manifest = {
            "sequences": [{
                "canvases": [{
                    "images": [{
                        "resource": {
                            "service": {"id": "https://example.org/iiif/img1"}
                        }
                    }]
                }]
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]


# ============================================================================
# extract_image_service_bases – IIIF v3
# ============================================================================

class TestExtractImageServiceBasesV3:
    """Tests for IIIF v3 manifest parsing."""

    def test_extracts_from_v3_manifest(self, sample_iiif_manifest_v3: dict[str, Any]) -> None:
        bases = extract_image_service_bases(sample_iiif_manifest_v3)
        assert bases == ["https://example.org/iiif/img1"]

    def test_v3_body_as_list(self) -> None:
        """v3 where body is a list instead of a dict."""
        manifest = {
            "items": [{
                "items": [{
                    "items": [{
                        "body": [{
                            "service": [{"id": "https://example.org/iiif/img1"}]
                        }]
                    }]
                }]
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_v3_service_as_dict(self) -> None:
        """v3 where service is a dict instead of a list."""
        manifest = {
            "items": [{
                "items": [{
                    "items": [{
                        "body": {
                            "service": {"@id": "https://example.org/iiif/img1"}
                        }
                    }]
                }]
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_v3_fallback_from_body_id(self) -> None:
        """v3 fallback from body.id when service is missing."""
        manifest = {
            "items": [{
                "items": [{
                    "items": [{
                        "body": {
                            "id": "https://example.org/iiif/img1/full/max/0/default.jpg"
                        }
                    }]
                }]
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_v3_skips_empty_annotation_pages(self) -> None:
        manifest: dict[str, Any] = {
            "items": [{
                "items": []
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == []

    def test_v3_skips_empty_annotations(self) -> None:
        manifest: dict[str, Any] = {
            "items": [{
                "items": [{
                    "items": []
                }]
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == []


# ============================================================================
# extract_image_service_bases – Mixed v2+v3
# ============================================================================

class TestExtractImageServiceBasesMixed:
    """Tests for manifests with both v2 and v3 structures."""

    def test_both_v2_and_v3_deduped(self) -> None:
        manifest = {
            "sequences": [{
                "canvases": [{
                    "images": [{
                        "resource": {"service": {"@id": "https://example.org/img1"}}
                    }]
                }]
            }],
            "items": [{
                "items": [{
                    "items": [{
                        "body": {"service": [{"id": "https://example.org/img1"}]}
                    }]
                }]
            }]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/img1"]


# ============================================================================
# extract_direct_image_urls
# ============================================================================

class TestExtractDirectImageUrls:
    """Tests for extracting direct image URLs."""

    def test_extracts_v2_image_urls(self) -> None:
        manifest = {
            "sequences": [{
                "canvases": [
                    {"images": [{"resource": {"@id": "https://example.org/page1.jpg"}}]},
                    {"images": [{"resource": {"@id": "https://example.org/page2.jpg"}}]},
                ]
            }]
        }
        urls = extract_direct_image_urls(manifest)
        assert urls == [
            "https://example.org/page1.jpg",
            "https://example.org/page2.jpg",
        ]

    def test_extracts_v3_image_urls(self) -> None:
        manifest = {
            "items": [{
                "items": [{
                    "items": [{
                        "body": {"id": "https://example.org/page1.jpg"}
                    }]
                }]
            }]
        }
        urls = extract_direct_image_urls(manifest)
        assert urls == ["https://example.org/page1.jpg"]

    def test_deduplicates_urls(self) -> None:
        manifest = {
            "sequences": [{
                "canvases": [
                    {"images": [{"resource": {"@id": "https://example.org/same.jpg"}}]},
                    {"images": [{"resource": {"@id": "https://example.org/same.jpg"}}]},
                ]
            }]
        }
        urls = extract_direct_image_urls(manifest)
        assert urls == ["https://example.org/same.jpg"]

    def test_empty_manifest_returns_empty(self) -> None:
        assert extract_direct_image_urls({}) == []

    def test_skips_canvases_without_images(self) -> None:
        manifest: dict[str, Any] = {"sequences": [{"canvases": [{"images": []}]}]}
        assert extract_direct_image_urls(manifest) == []

    def test_v3_body_as_list(self) -> None:
        manifest = {
            "items": [{
                "items": [{
                    "items": [{
                        "body": [{"id": "https://example.org/page1.jpg"}]
                    }]
                }]
            }]
        }
        urls = extract_direct_image_urls(manifest)
        assert urls == ["https://example.org/page1.jpg"]


# ============================================================================
# image_url_candidates
# ============================================================================

class TestImageUrlCandidates:
    """Tests for URL candidate generation."""

    def test_basic_candidates_without_info(self) -> None:
        candidates = image_url_candidates("https://example.org/iiif/img1")
        assert len(candidates) >= 5
        assert "https://example.org/iiif/img1/full/full/0/default.jpg" in candidates
        assert "https://example.org/iiif/img1/full/max/0/default.jpg" in candidates
        assert "https://example.org/iiif/img1/full/pct:100/0/default.jpg" in candidates
        assert "https://example.org/iiif/img1/full/full/0/native.jpg" in candidates
        assert "https://example.org/iiif/img1/full/full/0/color.jpg" in candidates

    def test_strips_trailing_slash(self) -> None:
        candidates = image_url_candidates("https://example.org/iiif/img1/")
        assert "https://example.org/iiif/img1/full/full/0/default.jpg" in candidates

    def test_with_info_sizes(self) -> None:
        info = {"sizes": [{"width": 1000, "height": 800}, {"width": 2000, "height": 1600}]}
        candidates = image_url_candidates("https://example.org/iiif/img1", info=info)
        assert "https://example.org/iiif/img1/full/2000,/0/default.jpg" in candidates
        assert "https://example.org/iiif/img1/full/2000,/0/native.jpg" in candidates

    def test_with_info_max_width(self) -> None:
        info = {"maxWidth": 3000}
        candidates = image_url_candidates("https://example.org/iiif/img1", info=info)
        assert "https://example.org/iiif/img1/full/3000,/0/default.jpg" in candidates

    def test_with_info_no_sizes_adds_fallback_widths(self) -> None:
        info = {"profile": "level1"}
        candidates = image_url_candidates("https://example.org/iiif/img1", info=info)
        assert "https://example.org/iiif/img1/full/2000,/0/default.jpg" in candidates
        assert "https://example.org/iiif/img1/full/1000,/0/default.jpg" in candidates

    def test_with_png_support(self) -> None:
        info = {"formats": ["png", "jpg"]}
        candidates = image_url_candidates("https://example.org/iiif/img1", info=info)
        png_candidates = [c for c in candidates if c.endswith(".png")]
        jpg_candidates = [c for c in candidates if c.endswith(".jpg")]
        assert len(png_candidates) > 0
        assert len(jpg_candidates) > 0
        # PNGs should come first
        first_png_idx = candidates.index(png_candidates[0])
        first_jpg_idx = candidates.index(jpg_candidates[0])
        assert first_png_idx < first_jpg_idx

    def test_deduplicates_candidates(self) -> None:
        candidates = image_url_candidates("https://example.org/iiif/img1")
        assert len(candidates) == len(set(candidates))

    def test_empty_info_dict(self) -> None:
        candidates = image_url_candidates("https://example.org/iiif/img1", info={})
        # Empty dict still gets the base candidates at minimum
        assert "https://example.org/iiif/img1/full/full/0/default.jpg" in candidates
        assert len(candidates) >= 5


# ============================================================================
# _fetch_info_json
# ============================================================================

class TestFetchInfoJson:
    """Tests for info.json fetching and caching."""

    def setup_method(self) -> None:
        _INFO_JSON_CACHE.clear()

    def teardown_method(self) -> None:
        _INFO_JSON_CACHE.clear()

    @patch("api.iiif._parsing.make_request")
    def test_fetches_and_caches_info(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"width": 1000, "height": 800}
        result = _fetch_info_json("https://example.org/iiif/img1")
        assert result == {"width": 1000, "height": 800}
        assert "https://example.org/iiif/img1" in _INFO_JSON_CACHE
        mock_req.assert_called_once_with("https://example.org/iiif/img1/info.json")

    @patch("api.iiif._parsing.make_request")
    def test_returns_cached_result(self, mock_req: MagicMock) -> None:
        _INFO_JSON_CACHE["https://example.org/iiif/img1"] = {"width": 500}
        result = _fetch_info_json("https://example.org/iiif/img1")
        assert result == {"width": 500}
        mock_req.assert_not_called()

    @patch("api.iiif._parsing.make_request")
    def test_returns_none_on_failure(self, mock_req: MagicMock) -> None:
        mock_req.return_value = None
        result = _fetch_info_json("https://example.org/iiif/img1")
        assert result is None

    @patch("api.iiif._parsing.make_request")
    def test_strips_trailing_slash(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"width": 1000}
        _fetch_info_json("https://example.org/iiif/img1/")
        mock_req.assert_called_once_with("https://example.org/iiif/img1/info.json")


# ============================================================================
# download_one_from_service
# ============================================================================

class TestDownloadOneFromService:
    """Tests for single-image download from IIIF service."""

    def setup_method(self) -> None:
        _INFO_JSON_CACHE.clear()

    def teardown_method(self) -> None:
        _INFO_JSON_CACHE.clear()

    @patch("api.iiif._parsing.download_file")
    def test_succeeds_on_first_candidate(self, mock_dl: MagicMock) -> None:
        mock_dl.return_value = "/path/to/file.jpg"
        result = download_one_from_service("https://example.org/iiif/img1", "/out", "page_001.jpg")
        assert result is True
        mock_dl.assert_called_once()

    @patch("api.iiif._parsing._fetch_info_json")
    @patch("api.iiif._parsing.download_file")
    def test_falls_back_to_info_json(self, mock_dl: MagicMock, mock_info: MagicMock) -> None:
        # All default candidates fail, then info.json candidates succeed
        default_count = len(image_url_candidates("https://example.org/iiif/img1"))
        call_count = [0]

        def side_effect(*args: Any, **kwargs: Any) -> str | None:
            call_count[0] += 1
            if call_count[0] <= default_count:
                return None
            return "/path/to/file.jpg"

        mock_dl.side_effect = side_effect
        mock_info.return_value = {"sizes": [{"width": 2000}]}
        result = download_one_from_service("https://example.org/iiif/img1", "/out", "page_001.jpg")
        assert result is True
        mock_info.assert_called_once()

    @patch("api.iiif._parsing._fetch_info_json")
    @patch("api.iiif._parsing.download_file")
    def test_returns_false_when_all_fail(self, mock_dl: MagicMock, mock_info: MagicMock) -> None:
        mock_dl.return_value = None
        mock_info.return_value = {"sizes": [{"width": 1000}]}
        result = download_one_from_service("https://example.org/iiif/img1", "/out", "page_001.jpg")
        assert result is False

    @patch("api.iiif._parsing._fetch_info_json")
    @patch("api.iiif._parsing.download_file")
    def test_handles_info_json_failure(self, mock_dl: MagicMock, mock_info: MagicMock) -> None:
        mock_dl.return_value = None
        mock_info.return_value = None
        result = download_one_from_service("https://example.org/iiif/img1", "/out", "page_001.jpg")
        assert result is False

    @patch("api.iiif._parsing._fetch_info_json")
    @patch("api.iiif._parsing.download_file")
    def test_handles_info_json_exception(self, mock_dl: MagicMock, mock_info: MagicMock) -> None:
        mock_dl.return_value = None
        mock_info.side_effect = Exception("network error")
        result = download_one_from_service("https://example.org/iiif/img1", "/out", "page_001.jpg")
        assert result is False
