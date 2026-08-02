"""Tests for api.iiif module — IIIF manifest parsing and image URL generation."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from api.iiif import (
    download_one_from_service,
    extract_direct_image_urls,
    extract_image_service_bases,
    image_url_candidates,
)
from api.iiif._parsing import (
    _INFO_JSON_CACHE,
    _fetch_info_json,
    extract_page_sources,
)
from api.iiif._renderings import (
    _MAX_RENDERING_ATTEMPTS,
    download_iiif_renderings,
    select_renderings,
)

# ============================================================================
# extract_image_service_bases – IIIF v2
# ============================================================================


class TestExtractImageServiceBasesV2:
    """Tests for IIIF v2 manifest parsing."""

    def test_extracts_service_ids_from_v2(
        self, sample_iiif_manifest_v2: dict[str, Any]
    ) -> None:
        bases = extract_image_service_bases(sample_iiif_manifest_v2)
        assert len(bases) == 2
        assert bases[0] == "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f1"
        assert bases[1] == "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f2"

    def test_extracts_from_resource_id_fallback(self) -> None:
        """Fallback when service is missing but resource @id contains /full/."""
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "@id": "https://example.org/iiif/img1/full/max/0/default.jpg",
                                        "service": {},
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_extracts_when_v2_service_is_a_list(self) -> None:
        """IIIF v2 permits resource.service to be an array; the extractor must
        normalize it to the first entry instead of raising AttributeError
        (which was swallowed, silently yielding zero page images)."""
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": [
                                            {"@id": "https://example.org/iiif/img1"},
                                            {"@id": "https://example.org/iiif/other"},
                                        ]
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_extracts_from_v2_oa_choice_resource(self) -> None:
        """IIIF v2 ``oa:Choice`` resources wrap the real image in ``default``
        (plus alternate ``item`` entries). The extractor must descend into the
        default alternative rather than yielding nothing."""
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "@type": "oa:Choice",
                                        "default": {
                                            "service": {
                                                "@id": "https://example.org/iiif/img1"
                                            }
                                        },
                                        "item": [
                                            {
                                                "service": {
                                                    "@id": "https://example.org/iiif/alt"
                                                }
                                            }
                                        ],
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_skips_canvas_without_images(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {"images": []},
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {
                                            "@id": "https://example.org/iiif/img1"
                                        }
                                    }
                                }
                            ]
                        },
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_empty_manifest_returns_empty_list(self) -> None:
        assert extract_image_service_bases({}) == []

    def test_deduplicates_service_bases(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {"@id": "https://example.org/img"}
                                    }
                                }
                            ]
                        },
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {"@id": "https://example.org/img"}
                                    }
                                }
                            ]
                        },
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/img"]

    def test_uses_id_key_when_at_id_missing(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {
                                            "id": "https://example.org/iiif/img1"
                                        }
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]


# ============================================================================
# extract_image_service_bases – IIIF v3
# ============================================================================


class TestExtractImageServiceBasesV3:
    """Tests for IIIF v3 manifest parsing."""

    def test_extracts_from_v3_manifest(
        self, sample_iiif_manifest_v3: dict[str, Any]
    ) -> None:
        bases = extract_image_service_bases(sample_iiif_manifest_v3)
        assert bases == ["https://example.org/iiif/img1"]

    def test_v3_body_as_list(self) -> None:
        """v3 where body is a list instead of a dict."""
        manifest = {
            "items": [
                {
                    "items": [
                        {
                            "items": [
                                {
                                    "body": [
                                        {
                                            "service": [
                                                {"id": "https://example.org/iiif/img1"}
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_v3_service_as_dict(self) -> None:
        """v3 where service is a dict instead of a list."""
        manifest = {
            "items": [
                {
                    "items": [
                        {
                            "items": [
                                {
                                    "body": {
                                        "service": {
                                            "@id": "https://example.org/iiif/img1"
                                        }
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_extracts_from_v3_choice_body(self) -> None:
        """A v3 ``Choice`` body nests the usable image annotations under
        ``items``; the extractor must descend into the first entry instead of
        dropping the page."""
        manifest = {
            "items": [
                {
                    "items": [
                        {
                            "items": [
                                {
                                    "body": {
                                        "type": "Choice",
                                        "items": [
                                            {
                                                "type": "Image",
                                                "service": [
                                                    {
                                                        "id": "https://example.org/iiif/img1"
                                                    }
                                                ],
                                            },
                                            {
                                                "type": "Image",
                                                "service": [
                                                    {
                                                        "id": "https://example.org/iiif/alt"
                                                    }
                                                ],
                                            },
                                        ],
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_v3_fallback_from_body_id(self) -> None:
        """v3 fallback from body.id when service is missing."""
        manifest = {
            "items": [
                {
                    "items": [
                        {
                            "items": [
                                {
                                    "body": {
                                        "id": "https://example.org/iiif/img1/full/max/0/default.jpg"
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        bases = extract_image_service_bases(manifest)
        assert bases == ["https://example.org/iiif/img1"]

    def test_v3_skips_empty_annotation_pages(self) -> None:
        manifest: dict[str, Any] = {"items": [{"items": []}]}
        bases = extract_image_service_bases(manifest)
        assert bases == []

    def test_v3_skips_empty_annotations(self) -> None:
        manifest: dict[str, Any] = {"items": [{"items": [{"items": []}]}]}
        bases = extract_image_service_bases(manifest)
        assert bases == []


# ============================================================================
# extract_image_service_bases – Mixed v2+v3
# ============================================================================


class TestExtractImageServiceBasesMixed:
    """Tests for manifests with both v2 and v3 structures."""

    def test_both_v2_and_v3_deduped(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {"@id": "https://example.org/img1"}
                                    }
                                }
                            ]
                        }
                    ]
                }
            ],
            "items": [
                {
                    "items": [
                        {
                            "items": [
                                {
                                    "body": {
                                        "service": [{"id": "https://example.org/img1"}]
                                    }
                                }
                            ]
                        }
                    ]
                }
            ],
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
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {"resource": {"@id": "https://example.org/page1.jpg"}}
                            ]
                        },
                        {
                            "images": [
                                {"resource": {"@id": "https://example.org/page2.jpg"}}
                            ]
                        },
                    ]
                }
            ]
        }
        urls = extract_direct_image_urls(manifest)
        assert urls == [
            "https://example.org/page1.jpg",
            "https://example.org/page2.jpg",
        ]

    def test_extracts_v3_image_urls(self) -> None:
        manifest = {
            "items": [
                {
                    "items": [
                        {"items": [{"body": {"id": "https://example.org/page1.jpg"}}]}
                    ]
                }
            ]
        }
        urls = extract_direct_image_urls(manifest)
        assert urls == ["https://example.org/page1.jpg"]

    def test_deduplicates_urls(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {"resource": {"@id": "https://example.org/same.jpg"}}
                            ]
                        },
                        {
                            "images": [
                                {"resource": {"@id": "https://example.org/same.jpg"}}
                            ]
                        },
                    ]
                }
            ]
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
            "items": [
                {
                    "items": [
                        {"items": [{"body": [{"id": "https://example.org/page1.jpg"}]}]}
                    ]
                }
            ]
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
        info = {
            "sizes": [{"width": 1000, "height": 800}, {"width": 2000, "height": 1600}]
        }
        candidates = image_url_candidates("https://example.org/iiif/img1", info=info)
        assert "https://example.org/iiif/img1/full/2000,/0/default.jpg" in candidates
        assert "https://example.org/iiif/img1/full/2000,/0/native.jpg" in candidates

    def test_with_info_max_width(self) -> None:
        info = {"maxWidth": 3000}
        candidates = image_url_candidates("https://example.org/iiif/img1", info=info)
        assert "https://example.org/iiif/img1/full/3000,/0/default.jpg" in candidates

    def test_max_width_caps_the_advertised_sizes(self) -> None:
        """maxWidth is a ceiling, not a capability.

        The Image API says clients must not expect a request wider than
        maxWidth to be supported, so a server advertising sizes up to 4000
        with maxWidth 1000 must be asked for 1000, not 4000.
        """
        info = {"sizes": [{"width": 4000}], "maxWidth": 1000}
        candidates = image_url_candidates("https://example.org/iiif/img1", info=info)
        assert "https://example.org/iiif/img1/full/1000,/0/default.jpg" in candidates
        assert (
            "https://example.org/iiif/img1/full/4000,/0/default.jpg" not in candidates
        )

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
        result = download_one_from_service(
            "https://example.org/iiif/img1", "/out", "page_001.jpg"
        )
        assert result is True
        mock_dl.assert_called_once()

    @patch("api.iiif._parsing._fetch_info_json")
    @patch("api.iiif._parsing.download_file")
    def test_falls_back_to_info_json(
        self, mock_dl: MagicMock, mock_info: MagicMock
    ) -> None:
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
        result = download_one_from_service(
            "https://example.org/iiif/img1", "/out", "page_001.jpg"
        )
        assert result is True
        mock_info.assert_called_once()

    @patch("api.iiif._parsing._fetch_info_json")
    @patch("api.iiif._parsing.download_file")
    def test_returns_false_when_all_fail(
        self, mock_dl: MagicMock, mock_info: MagicMock
    ) -> None:
        mock_dl.return_value = None
        mock_info.return_value = {"sizes": [{"width": 1000}]}
        result = download_one_from_service(
            "https://example.org/iiif/img1", "/out", "page_001.jpg"
        )
        assert result is False

    @patch("api.iiif._parsing._fetch_info_json")
    @patch("api.iiif._parsing.download_file")
    def test_handles_info_json_failure(
        self, mock_dl: MagicMock, mock_info: MagicMock
    ) -> None:
        mock_dl.return_value = None
        mock_info.return_value = None
        result = download_one_from_service(
            "https://example.org/iiif/img1", "/out", "page_001.jpg"
        )
        assert result is False

    @patch("api.iiif._parsing._fetch_info_json")
    @patch("api.iiif._parsing.download_file")
    def test_handles_info_json_exception(
        self, mock_dl: MagicMock, mock_info: MagicMock
    ) -> None:
        mock_dl.return_value = None
        mock_info.side_effect = Exception("network error")
        result = download_one_from_service(
            "https://example.org/iiif/img1", "/out", "page_001.jpg"
        )
        assert result is False


# ============================================================================
# download_iiif_renderings
# ============================================================================


class TestRenderingSelection:
    """The whitelist must bind, and the limit must count files obtained.

    The URL-suffix check was applied unconditionally, so a rendering whose
    URL ended in .pdf or .epub bypassed the whitelist however it was
    narrowed. The limit truncated the candidate list before any download was
    attempted, so a dead first rendering hid the working ones behind it and
    the caller fell back to fetching every page image.
    """

    @staticmethod
    def _manifest(*renderings: dict[str, Any]) -> dict[str, Any]:
        return {"rendering": list(renderings)}

    def test_declared_format_outranks_the_url_suffix(self) -> None:
        manifest = self._manifest(
            {"@id": "https://example.org/book.pdf", "format": "application/pdf"},
            {"@id": "https://example.org/book.epub", "format": "application/epub+zip"},
        )
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={
                    "rendering_mime_whitelist": ["application/epub+zip"],
                    "max_renderings_per_manifest": 2,
                },
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 1

        urls = [call.args[0] for call in mock_dl.call_args_list]
        assert urls == ["https://example.org/book.epub"]

    def test_url_suffix_still_rescues_an_undeclared_format(self) -> None:
        # A bare IIIF v3 resource type settles nothing, so the suffix decides.
        manifest = self._manifest(
            {"@id": "https://example.org/book.pdf"},
            {"id": "https://example.org/other.pdf", "type": "Text"},
        )
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 1},
            ),
            patch("api.iiif._renderings.download_file", return_value="/out/f"),
        ):
            assert download_iiif_renderings(manifest, "/out") == 1

    def test_a_dead_rendering_does_not_consume_the_limit(self) -> None:
        manifest = self._manifest(
            {"@id": "https://example.org/dead.pdf", "format": "application/pdf"},
            {"@id": "https://example.org/live.pdf", "format": "application/pdf"},
        )
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 1},
            ),
            patch(
                "api.iiif._renderings.download_file",
                side_effect=[None, "/out/live.pdf"],
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 1

        assert [call.args[0] for call in mock_dl.call_args_list] == [
            "https://example.org/dead.pdf",
            "https://example.org/live.pdf",
        ]

    def test_attempts_stay_bounded_when_every_rendering_is_dead(self) -> None:
        manifest = self._manifest(
            *(
                {"@id": f"https://example.org/{i}.pdf", "format": "application/pdf"}
                for i in range(40)
            )
        )
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 1},
            ),
            patch("api.iiif._renderings.download_file", return_value=None) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 0

        assert mock_dl.call_count == _MAX_RENDERING_ATTEMPTS

    def test_limit_still_caps_successful_downloads(self) -> None:
        manifest = self._manifest(
            *(
                {"@id": f"https://example.org/{i}.pdf", "format": "application/pdf"}
                for i in range(5)
            )
        )
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 2},
            ),
            patch("api.iiif._renderings.download_file", return_value="/out/f") as m,
        ):
            assert download_iiif_renderings(manifest, "/out") == 2

        assert m.call_count == 2


class TestSelectRenderingsIsTheSharedSelection:
    """``select_renderings`` is the single source of truth for candidates.

    The download path and the manifest preview both consume it, so the
    preview cannot promise a file the download would skip, nor stay silent
    about one it does fetch.
    """

    def test_selection_matches_what_the_download_attempts(self) -> None:
        manifest = {
            "rendering": [
                {"@id": "https://example.org/viewer", "format": "text/html"},
                {
                    "@id": "https://example.org/book.epub",
                    "format": "application/epub+zip",
                },
                {"@id": "https://example.org/plain.pdf"},
            ]
        }
        cfg = {
            "rendering_mime_whitelist": ["application/pdf", "application/epub+zip"],
            "max_renderings_per_manifest": 5,
        }
        with (
            patch("api.iiif._renderings.get_download_config", return_value=cfg),
            patch("api.iiif._renderings.download_file", return_value="/out/f") as m,
        ):
            assert download_iiif_renderings(manifest, "/out") == 2
            selected = select_renderings(manifest)

        attempted = [call.args[0] for call in m.call_args_list]
        assert attempted == [r["url"] for r in selected]
        # The format-less .pdf ranks with the PDF whitelist entry, so it comes
        # before the EPUB even though the manifest lists it last.
        assert attempted == [
            "https://example.org/plain.pdf",
            "https://example.org/book.epub",
        ]

    def test_selection_reads_the_whitelist_from_config_by_default(self) -> None:
        manifest = {
            "rendering": [
                {
                    "@id": "https://example.org/book.epub",
                    "format": "application/epub+zip",
                }
            ]
        }
        with patch(
            "api.iiif._renderings.get_download_config",
            return_value={"rendering_mime_whitelist": ["application/pdf"]},
        ):
            assert select_renderings(manifest) == []


class TestSequenceLevelRenderings:
    """IIIF v2 also carries whole-work renderings on the sequence.

    Wellcome and the DFG viewer hang the volume PDF off sequences[n].rendering
    rather than the manifest, and a manifest-only scan skipped it and fell back
    to downloading every page image.
    """

    def test_sequence_level_pdf_is_collected(self) -> None:
        manifest = {
            "@context": "http://iiif.io/api/presentation/2/context.json",
            "sequences": [
                {
                    "@type": "sc:Sequence",
                    "rendering": {
                        "@id": "https://example.org/volume.pdf",
                        "format": "application/pdf",
                        "label": "Download as PDF",
                    },
                    "canvases": [],
                }
            ],
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 1},
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 1

        assert mock_dl.call_args_list[0].args[0] == "https://example.org/volume.pdf"

    def test_manifest_level_renderings_come_first(self) -> None:
        manifest = {
            "rendering": [
                {"@id": "https://example.org/top.pdf", "format": "application/pdf"}
            ],
            "sequences": [
                {
                    "rendering": [
                        {
                            "@id": "https://example.org/seq.pdf",
                            "format": "application/pdf",
                        }
                    ]
                }
            ],
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 2},
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 2

        assert [c.args[0] for c in mock_dl.call_args_list] == [
            "https://example.org/top.pdf",
            "https://example.org/seq.pdf",
        ]

    def test_duplicate_rendering_across_levels_is_downloaded_once(self) -> None:
        shared = {"@id": "https://example.org/vol.pdf", "format": "application/pdf"}
        manifest = {"rendering": [shared], "sequences": [{"rendering": [shared]}]}
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 5},
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 1

        assert mock_dl.call_count == 1

    def test_non_list_sequences_are_ignored(self) -> None:
        manifest = {"sequences": "not-a-list"}
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 1},
            ),
            patch("api.iiif._renderings.download_file", return_value="/out/f"),
        ):
            assert download_iiif_renderings(manifest, "/out") == 0


# ============================================================================
# Multi-sequence and non-list sequence containers
# ============================================================================


class TestMultiSequenceManifests:
    """Every v2 sequence carries pages, and ``sequences`` is not always a list.

    The walker read ``sequences[0]`` only, so a multi-sequence manifest lost
    every page beyond the first sequence, and a dict-valued ``sequences``
    raised ``KeyError(0)`` into the blanket guard and yielded no pages at all.
    """

    @staticmethod
    def _canvas(service_id: str) -> dict[str, Any]:
        return {"images": [{"resource": {"service": {"@id": service_id}}}]}

    def test_pages_from_every_sequence_are_kept(self) -> None:
        manifest = {
            "sequences": [
                {"canvases": [self._canvas("https://example.org/iiif/v1p1")]},
                {
                    "canvases": [
                        self._canvas("https://example.org/iiif/v2p1"),
                        self._canvas("https://example.org/iiif/v2p2"),
                    ]
                },
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/v1p1",
            "https://example.org/iiif/v2p1",
            "https://example.org/iiif/v2p2",
        ]

    def test_dict_valued_sequences_still_yield_pages(self) -> None:
        manifest = {
            "sequences": {"canvases": [self._canvas("https://example.org/iiif/img1")]}
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_non_dict_sequence_entries_are_skipped(self) -> None:
        manifest = {
            "sequences": [
                "not-a-sequence",
                {"canvases": [self._canvas("https://example.org/iiif/img1")]},
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_non_list_canvases_are_skipped(self) -> None:
        manifest = {
            "sequences": [
                {"canvases": "broken"},
                {"canvases": [self._canvas("https://example.org/iiif/img1")]},
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_direct_urls_span_every_sequence(self) -> None:
        manifest = {
            "sequences": [
                {"canvases": [{"images": [{"resource": {"@id": "https://e/p1.jpg"}}]}]},
                {"canvases": [{"images": [{"resource": {"@id": "https://e/p2.jpg"}}]}]},
            ]
        }
        assert extract_direct_image_urls(manifest) == [
            "https://e/p1.jpg",
            "https://e/p2.jpg",
        ]


# ============================================================================
# Service arrays whose leading entry is unusable
# ============================================================================


class TestServiceArrayFallback:
    """A service array may lead with an entry that carries no identifier.

    Auth and search services sit alongside the image service in real
    manifests; taking ``service[0]`` blindly dropped the page whenever the
    image service was not listed first.
    """

    def test_v2_skips_a_service_entry_without_an_id(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": [
                                            {"profile": "http://iiif.io/api/auth/1/"},
                                            {"@id": "https://example.org/iiif/img1"},
                                        ]
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_v3_skips_a_service_entry_without_an_id(self) -> None:
        manifest = {
            "items": [
                {
                    "items": [
                        {
                            "items": [
                                {
                                    "body": {
                                        "service": [
                                            {"type": "AuthCookieService1"},
                                            {"id": "https://example.org/iiif/img1"},
                                        ]
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_unusable_service_array_falls_back_to_the_resource_id(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": ["junk", {"label": "no id"}],
                                        "@id": (
                                            "https://example.org/iiif/img1"
                                            "/full/max/0/default.jpg"
                                        ),
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]


# ============================================================================
# extract_page_sources
# ============================================================================


class TestExtractPageSources:
    """Mixed manifests must keep every page.

    Choosing between service-backed and direct extraction once per manifest
    dropped the direct-only canvases of a mixed manifest without a trace, so
    the expected page count was understated and the gaps went unrecorded.
    """

    def test_v2_mixed_canvases_keep_every_page(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {
                                            "@id": "https://example.org/iiif/img1"
                                        }
                                    }
                                }
                            ]
                        },
                        {
                            "images": [
                                {"resource": {"@id": "https://example.org/p2.jpg"}}
                            ]
                        },
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {
                                            "@id": "https://example.org/iiif/img3"
                                        }
                                    }
                                }
                            ]
                        },
                    ]
                }
            ]
        }
        assert extract_page_sources(manifest) == [
            ("service", "https://example.org/iiif/img1"),
            ("direct", "https://example.org/p2.jpg"),
            ("service", "https://example.org/iiif/img3"),
        ]
        # The single-kind extractor still sees only its own half.
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1",
            "https://example.org/iiif/img3",
        ]

    def test_v3_mixed_canvases_keep_every_page(self) -> None:
        manifest = {
            "items": [
                {
                    "items": [
                        {
                            "items": [
                                {
                                    "body": {
                                        "service": [
                                            {"id": "https://example.org/iiif/img1"}
                                        ]
                                    }
                                }
                            ]
                        }
                    ]
                },
                {
                    "items": [
                        {"items": [{"body": {"id": "https://example.org/p2.jpg"}}]}
                    ]
                },
            ]
        }
        assert extract_page_sources(manifest) == [
            ("service", "https://example.org/iiif/img1"),
            ("direct", "https://example.org/p2.jpg"),
        ]

    def test_service_wins_when_a_canvas_offers_both(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "@id": "https://example.org/p1.jpg",
                                        "service": {
                                            "@id": "https://example.org/iiif/img1"
                                        },
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        assert extract_page_sources(manifest) == [
            ("service", "https://example.org/iiif/img1")
        ]

    def test_canvas_with_neither_source_is_skipped(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {"images": [{"resource": {"label": "no id at all"}}]},
                        {
                            "images": [
                                {"resource": {"@id": "https://example.org/p2.jpg"}}
                            ]
                        },
                    ]
                }
            ]
        }
        assert extract_page_sources(manifest) == [
            ("direct", "https://example.org/p2.jpg")
        ]

    def test_duplicate_urls_are_dropped(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {"resource": {"@id": "https://example.org/p.jpg"}}
                            ]
                        },
                        {
                            "images": [
                                {"resource": {"@id": "https://example.org/p.jpg"}}
                            ]
                        },
                    ]
                }
            ]
        }
        assert extract_page_sources(manifest) == [
            ("direct", "https://example.org/p.jpg")
        ]

    def test_empty_manifest_returns_empty(self) -> None:
        assert extract_page_sources({}) == []


# ============================================================================
# Mixed manifests in download_iiif_manifest_and_images
# ============================================================================


class TestMixedManifestStrategy:
    """The provider-facing strategy must not drop the direct-only canvases."""

    @staticmethod
    def _mixed_manifest() -> dict[str, Any]:
        return {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {
                                            "@id": "https://example.org/iiif/img1"
                                        }
                                    }
                                }
                            ]
                        },
                        {
                            "images": [
                                {"resource": {"@id": "https://example.org/p2.jpg"}}
                            ]
                        },
                    ]
                }
            ]
        }

    def test_both_kinds_of_canvas_are_downloaded(self) -> None:
        from api.iiif import download_iiif_manifest_and_images

        with (
            patch(
                "api.iiif._strategies.make_request",
                return_value=self._mixed_manifest(),
            ),
            patch("api.iiif._strategies.save_json"),
            patch("api.iiif._strategies.download_iiif_renderings", return_value=0),
            patch("api.iiif._strategies.get_max_pages", return_value=0),
            patch("api.iiif._strategies.budget_exhausted", return_value=False),
            patch(
                "api.iiif._strategies.download_one_from_service", return_value=True
            ) as mock_svc,
            patch(
                "api.iiif._strategies.download_file", return_value="/out/p2.jpg"
            ) as mock_file,
        ):
            assert (
                download_iiif_manifest_and_images(
                    "https://example.org/manifest.json", "/out", "gallica", "item1"
                )
                is True
            )

        assert mock_svc.call_args_list[0].args[0] == "https://example.org/iiif/img1"
        assert mock_svc.call_args_list[0].args[2] == "gallica_item1_p00001.jpg"
        assert mock_file.call_args_list[0].args[0] == "https://example.org/p2.jpg"
        assert mock_file.call_args_list[0].args[2] == "gallica_item1_p00002"

    def test_homogeneous_manifest_still_uses_the_page_image_helper(self) -> None:
        from api.iiif import download_iiif_manifest_and_images

        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {
                                            "@id": "https://example.org/iiif/img1"
                                        }
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        with (
            patch("api.iiif._strategies.make_request", return_value=manifest),
            patch("api.iiif._strategies.save_json"),
            patch("api.iiif._strategies.download_iiif_renderings", return_value=0),
            patch(
                "api.iiif._strategies.download_page_images", return_value=True
            ) as mock_pages,
        ):
            assert (
                download_iiif_manifest_and_images(
                    "https://example.org/manifest.json", "/out", "gallica", "item1"
                )
                is True
            )

        mock_pages.assert_called_once()
        assert mock_pages.call_args.args[0] == ["https://example.org/iiif/img1"]


# ============================================================================
# Rendering selection order and malformed entries
# ============================================================================


class TestRenderingPriority:
    """The MIME whitelist is an ordered preference, not a set.

    Candidates were tried in document order, so with the shipped whitelist and
    the default limit of one file a manifest that listed the EPUB before the
    whole-work PDF handed back the EPUB.
    """

    def test_whitelist_order_beats_document_order(self) -> None:
        manifest = {
            "rendering": [
                {
                    "@id": "https://example.org/book.epub",
                    "format": "application/epub+zip",
                }
            ],
            "sequences": [
                {
                    "rendering": [
                        {
                            "@id": "https://example.org/volume.pdf",
                            "format": "application/pdf",
                        }
                    ]
                }
            ],
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 1},
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 1

        assert mock_dl.call_args_list[0].args[0] == "https://example.org/volume.pdf"

    def test_undeclared_formats_rank_by_url_suffix(self) -> None:
        manifest = {
            "rendering": [
                {"@id": "https://example.org/book.epub"},
                {"@id": "https://example.org/book.pdf"},
            ]
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 2},
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 2

        assert [c.args[0] for c in mock_dl.call_args_list] == [
            "https://example.org/book.pdf",
            "https://example.org/book.epub",
        ]

    def test_equal_rank_keeps_manifest_order(self) -> None:
        manifest = {
            "rendering": [
                {"@id": "https://example.org/a.pdf", "format": "application/pdf"},
                {"@id": "https://example.org/b.pdf", "format": "application/pdf"},
            ]
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 2},
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 2

        assert [c.args[0] for c in mock_dl.call_args_list] == [
            "https://example.org/a.pdf",
            "https://example.org/b.pdf",
        ]

    def test_reordered_whitelist_reorders_the_candidates(self) -> None:
        manifest = {
            "rendering": [
                {"@id": "https://example.org/book.pdf", "format": "application/pdf"},
                {
                    "@id": "https://example.org/book.epub",
                    "format": "application/epub+zip",
                },
            ]
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={
                    "rendering_mime_whitelist": [
                        "application/epub+zip",
                        "application/pdf",
                    ],
                    "max_renderings_per_manifest": 1,
                },
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 1

        assert mock_dl.call_args_list[0].args[0] == "https://example.org/book.epub"


class _ExplodingRendering(dict[str, Any]):
    """A rendering entry whose every lookup fails, as a malformed one would."""

    def get(self, key: Any, default: Any = None) -> Any:
        raise RuntimeError("malformed rendering entry")


class TestMalformedRenderingEntries:
    """One bad entry must cost its own rendering, not the whole manifest.

    A list-valued ``format`` (which real manifests emit) raised
    ``AttributeError`` out of the unguarded loop, so every rendering of that
    manifest was lost and the caller fell back to page images.
    """

    def test_list_valued_format_is_normalized(self) -> None:
        manifest = {
            "rendering": [
                {
                    "@id": "https://example.org/book.epub",
                    "format": ["application/epub+zip"],
                },
                {"@id": "https://example.org/book.pdf", "format": ["application/pdf"]},
            ]
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 2},
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 2

        assert [c.args[0] for c in mock_dl.call_args_list] == [
            "https://example.org/book.pdf",
            "https://example.org/book.epub",
        ]

    def test_list_valued_format_still_binds_the_whitelist(self) -> None:
        manifest = {
            "rendering": [
                {"@id": "https://example.org/book.pdf", "format": ["application/pdf"]}
            ]
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={
                    "rendering_mime_whitelist": ["application/epub+zip"],
                    "max_renderings_per_manifest": 2,
                },
            ),
            patch("api.iiif._renderings.download_file", return_value="/out/f"),
        ):
            assert download_iiif_renderings(manifest, "/out") == 0

    def test_non_string_format_falls_back_to_the_url_suffix(self) -> None:
        manifest = {
            "rendering": [{"@id": "https://example.org/book.pdf", "format": {"a": 1}}]
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 1},
            ),
            patch("api.iiif._renderings.download_file", return_value="/out/f"),
        ):
            assert download_iiif_renderings(manifest, "/out") == 1

    def test_one_unreadable_entry_does_not_abort_the_rest(self) -> None:
        manifest = {
            "rendering": [
                _ExplodingRendering(),
                {"@id": "https://example.org/book.pdf", "format": "application/pdf"},
            ]
        }
        with (
            patch(
                "api.iiif._renderings.get_download_config",
                return_value={"max_renderings_per_manifest": 2},
            ),
            patch(
                "api.iiif._renderings.download_file", return_value="/out/f"
            ) as mock_dl,
        ):
            assert download_iiif_renderings(manifest, "/out") == 1

        assert mock_dl.call_args_list[0].args[0] == "https://example.org/book.pdf"
