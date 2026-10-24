"""Image API candidate URLs, uncapped and under a size cap.

The uncapped tests are ported from v1.30.3 ``tests/unit/test_iiif.py``
(``TestImageUrlCandidates``) with assertions unchanged; the cap tests are new.
"""

from __future__ import annotations

import re

import pytest

from chronodownloader.iiif import image_url_candidates, info_url

BASE = "https://example.org/iiif/img1"


class TestImageUrlCandidates:
    def test_basic_candidates_without_info(self) -> None:
        candidates = image_url_candidates(BASE)
        assert len(candidates) >= 5
        assert f"{BASE}/full/full/0/default.jpg" in candidates
        assert f"{BASE}/full/max/0/default.jpg" in candidates
        assert f"{BASE}/full/pct:100/0/default.jpg" in candidates
        assert f"{BASE}/full/full/0/native.jpg" in candidates
        assert f"{BASE}/full/full/0/color.jpg" in candidates

    def test_strips_trailing_slash(self) -> None:
        candidates = image_url_candidates(BASE + "/")
        assert f"{BASE}/full/full/0/default.jpg" in candidates

    def test_with_info_sizes(self) -> None:
        info = {
            "sizes": [{"width": 1000, "height": 800}, {"width": 2000, "height": 1600}]
        }
        candidates = image_url_candidates(BASE, info=info)
        assert f"{BASE}/full/2000,/0/default.jpg" in candidates
        assert f"{BASE}/full/2000,/0/native.jpg" in candidates

    def test_with_info_max_width(self) -> None:
        candidates = image_url_candidates(BASE, info={"maxWidth": 3000})
        assert f"{BASE}/full/3000,/0/default.jpg" in candidates

    def test_max_width_caps_the_advertised_sizes(self) -> None:
        info = {"sizes": [{"width": 4000}], "maxWidth": 1000}
        candidates = image_url_candidates(BASE, info=info)
        assert f"{BASE}/full/1000,/0/default.jpg" in candidates
        assert f"{BASE}/full/4000,/0/default.jpg" not in candidates

    def test_with_info_no_sizes_adds_fallback_widths(self) -> None:
        candidates = image_url_candidates(BASE, info={"profile": "level1"})
        assert f"{BASE}/full/2000,/0/default.jpg" in candidates
        assert f"{BASE}/full/1000,/0/default.jpg" in candidates

    def test_with_png_support(self) -> None:
        candidates = image_url_candidates(BASE, info={"formats": ["png", "jpg"]})
        png_candidates = [c for c in candidates if c.endswith(".png")]
        jpg_candidates = [c for c in candidates if c.endswith(".jpg")]
        assert len(png_candidates) > 0
        assert len(jpg_candidates) > 0
        assert candidates.index(png_candidates[0]) < candidates.index(jpg_candidates[0])

    def test_deduplicates_candidates(self) -> None:
        candidates = image_url_candidates(BASE)
        assert len(candidates) == len(set(candidates))

    def test_empty_info_dict(self) -> None:
        candidates = image_url_candidates(BASE, info={})
        assert f"{BASE}/full/full/0/default.jpg" in candidates
        assert len(candidates) >= 5

    def test_malformed_size_entries_are_ignored(self) -> None:
        info = {"sizes": ["junk", {"width": "wide"}, {"width": 800}]}
        assert image_url_candidates(BASE, info=info)[0] == (
            f"{BASE}/full/800,/0/default.jpg"
        )


def _widths(urls: list[str]) -> list[tuple[int, int]]:
    """Return the requested (width, height) bounds of size segments w, / !w,h."""
    bounds = []
    for url in urls:
        size = url.split("/full/")[1].split("/")[0]
        match = re.fullmatch(r"!?(\d*),(\d*)", size)
        if match:
            width, height = match.groups()
            bounds.append((int(width or 0), int(height or 0)))
    return bounds


class TestCappedCandidates:
    """A provider's image cap bounds every requested size."""

    def test_without_info_only_the_confined_size_is_asked(self) -> None:
        assert image_url_candidates(BASE, max_px=1024) == [
            f"{BASE}/full/!1024,1024/0/default.jpg"
        ]

    def test_a_large_page_is_scaled_on_its_longer_side(self) -> None:
        info = {"width": 2000, "height": 3000}
        candidates = image_url_candidates(BASE, info, max_px=1024)
        assert candidates[0] == f"{BASE}/full/682,/0/default.jpg"
        assert 3000 * 682 // 2000 <= 1024

    def test_a_page_within_the_cap_is_asked_whole(self) -> None:
        info = {"width": 800, "height": 1000}
        candidates = image_url_candidates(BASE, info, max_px=1024)
        assert candidates[:2] == [
            f"{BASE}/full/max/0/default.jpg",
            f"{BASE}/full/full/0/default.jpg",
        ]

    def test_without_dimensions_the_largest_fitting_size_is_used(self) -> None:
        info = {
            "sizes": [
                {"width": 500, "height": 700},
                {"width": 1000, "height": 1400},
                {"width": 700, "height": 980},
            ]
        }
        candidates = image_url_candidates(BASE, info, max_px=1024)
        assert candidates[0] == f"{BASE}/full/700,/0/default.jpg"

    @pytest.mark.parametrize(
        "info",
        [
            None,
            {"width": 5000, "height": 4000},
            {"width": 4000, "height": 5000, "maxWidth": 600},
            {"sizes": [{"width": 4000, "height": 5000}]},
            {"formats": ["png"], "width": 3000, "height": 3000},
        ],
    )
    def test_no_candidate_exceeds_the_cap(self, info: dict[str, object] | None) -> None:
        candidates = image_url_candidates(BASE, info, max_px=1024)
        assert all(w <= 1024 and h <= 1024 for w, h in _widths(candidates))
        assert not any(
            "/full/full/" in url or "/full/max/" in url for url in candidates
        )

    def test_a_cap_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_px"):
            image_url_candidates(BASE, max_px=0)


def test_info_url_strips_a_trailing_slash() -> None:
    assert info_url(BASE + "/") == f"{BASE}/info.json"
