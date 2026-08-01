"""Unit tests for the Wellcome Collection connector.

Wellcome's catalogue API used to expose digitised content as ``iiif-image``
locations pointing at an ``info.json``, from which the Image API service base
was read directly. It now serves ``iiif-presentation`` manifest URLs instead,
and live responses carry no ``iiif-image`` entry at all. Filtering on the old
type discarded every hit, so ``search_wellcome`` returned an empty list for
every query while the API itself answered 200 with thousands of matches.

The fixtures below are trimmed but verbatim snippets of a live response
recorded on 01.08.2026; the tests themselves never touch the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from api.providers.wellcome import (
    _extract_image_services,
    _extract_manifest_urls,
    _max_images,
    download_wellcome_work,
    search_wellcome,
)

MANIFEST_URL = "https://iiif.wellcomecollection.org/presentation/v2/b21528391"

# Current shape: an iiif-presentation location, alongside physical holdings.
PRESENTATION_WORK: dict[str, Any] = {
    "id": "asd4849u",
    "title": "Cookery book ; and, General axioms for plain cookery",
    "thumbnail": {"url": "https://iiif.wellcomecollection.org/thumb/b21528391"},
    "items": [
        {
            "locations": [
                {"locationType": {"id": "closed-stores"}, "label": "Closed stores"}
            ]
        },
        {
            "locations": [
                {"locationType": {"id": "iiif-presentation"}, "url": MANIFEST_URL}
            ]
        },
    ],
}

# Legacy shape, kept working for any endpoint still serving it.
IMAGE_WORK: dict[str, Any] = {
    "id": "legacy01",
    "title": "A legacy image work",
    "items": [
        {
            "locations": [
                {
                    "locationType": {"id": "iiif-image"},
                    "url": "https://iiif.wellcomecollection.org/image/b111_0001.jp2/info.json",
                }
            ]
        }
    ],
}

# Physical-only holding: no digitised content of any kind.
PHYSICAL_ONLY_WORK: dict[str, Any] = {
    "id": "physical1",
    "title": "Only on the shelf",
    "items": [{"locations": [{"locationType": {"id": "open-shelves"}}]}],
}


class TestLocationExtraction:
    """Both location shapes must be recognised."""

    def test_presentation_manifest_is_extracted(self) -> None:
        assert _extract_manifest_urls(PRESENTATION_WORK) == [MANIFEST_URL]

    def test_image_service_base_strips_info_json(self) -> None:
        assert _extract_image_services(IMAGE_WORK) == [
            "https://iiif.wellcomecollection.org/image/b111_0001.jp2"
        ]

    def test_presentation_work_has_no_image_services(self) -> None:
        """The regression itself: the old filter sees nothing here."""
        assert _extract_image_services(PRESENTATION_WORK) == []

    def test_physical_only_work_yields_neither(self) -> None:
        assert _extract_manifest_urls(PHYSICAL_ONLY_WORK) == []
        assert _extract_image_services(PHYSICAL_ONLY_WORK) == []

    def test_duplicate_manifest_urls_are_collapsed(self) -> None:
        work = {
            "items": [
                {
                    "locations": [
                        {
                            "locationType": {"id": "iiif-presentation"},
                            "url": MANIFEST_URL,
                        }
                    ]
                },
                {
                    "locations": [
                        {
                            "locationType": {"id": "iiif-presentation"},
                            "url": MANIFEST_URL,
                        }
                    ]
                },
            ]
        }
        assert _extract_manifest_urls(work) == [MANIFEST_URL]


class TestSearchWellcome:
    """Search must keep presentation-only works instead of discarding them."""

    def test_presentation_only_work_is_returned(self) -> None:
        response = {"results": [PRESENTATION_WORK]}
        with patch("api.providers.wellcome.make_request", return_value=response):
            results = search_wellcome("cookery")

        assert len(results) == 1
        assert results[0].source_id == "asd4849u"
        assert results[0].iiif_manifest == MANIFEST_URL

    def test_legacy_image_work_still_carries_its_services(self) -> None:
        response = {"results": [IMAGE_WORK]}
        with patch("api.providers.wellcome.make_request", return_value=response):
            results = search_wellcome("cookery")

        assert len(results) == 1
        assert results[0].raw["image_services"]
        assert results[0].raw["iiif_manifest"] is None

    def test_works_without_digitised_content_are_skipped(self) -> None:
        response = {"results": [PHYSICAL_ONLY_WORK]}
        with patch("api.providers.wellcome.make_request", return_value=response):
            assert search_wellcome("cookery") == []

    def test_max_results_is_respected(self) -> None:
        response = {"results": [PRESENTATION_WORK, IMAGE_WORK, PRESENTATION_WORK]}
        with patch("api.providers.wellcome.make_request", return_value=response):
            assert len(search_wellcome("cookery", max_results=2)) == 2

    def test_non_dict_response_returns_no_results(self) -> None:
        with patch("api.providers.wellcome.make_request", return_value=None):
            assert search_wellcome("cookery") == []

    def test_contributors_and_production_are_requested(self) -> None:
        """Both ride along on the same request, so they cost nothing."""
        with patch(
            "api.providers.wellcome.make_request", return_value={"results": []}
        ) as mock_req:
            search_wellcome("cookery")

        include = mock_req.call_args.kwargs["params"]["include"]
        assert "contributors" in include
        assert "production" in include
        assert "items" in include

    def test_creator_and_date_reach_the_result(self) -> None:
        """Every Wellcome hit used to arrive with no author and no year.

        creator was hard-coded None and production was never requested, so
        Wellcome candidates forfeited the whole creator ranking bonus and
        left the year column of the run index empty.
        """
        work = dict(PRESENTATION_WORK)
        work["contributors"] = [
            {"agent": {"label": "Briggs, Emily.", "type": "Person"}, "primary": True}
        ]
        work["production"] = [
            {
                "label": "London : School Board for London, [1890].",
                "dates": [{"label": "[1890]", "type": "Period"}],
            }
        ]
        with patch(
            "api.providers.wellcome.make_request", return_value={"results": [work]}
        ):
            result = search_wellcome("cookery")[0]

        assert result.creators == ["Briggs, Emily."]
        assert result.date == "[1890]"

    def test_a_work_naming_nobody_stays_empty(self) -> None:
        work = dict(PRESENTATION_WORK)
        work["contributors"] = [{"agent": {}}, {"not": "a contributor"}]
        work["production"] = [{"label": "London", "dates": []}]
        with patch(
            "api.providers.wellcome.make_request", return_value={"results": [work]}
        ):
            result = search_wellcome("cookery")[0]

        assert result.creators == []
        assert result.date is None


class TestDownloadWellcomeWork:
    """A manifest-bearing result downloads through the shared IIIF strategy."""

    def test_manifest_is_routed_to_the_shared_strategy(self) -> None:
        response = {"results": [PRESENTATION_WORK]}
        with patch("api.providers.wellcome.make_request", return_value=response):
            result = search_wellcome("cookery")[0]

        with patch(
            "api.providers.wellcome.download_iiif_manifest_and_images",
            return_value=True,
        ) as mock_dl:
            assert download_wellcome_work(result, "/tmp/out") is True

        mock_dl.assert_called_once_with(
            MANIFEST_URL, "/tmp/out", "wellcome", "asd4849u"
        )

    def test_refetch_recovers_a_manifest_for_a_bare_id(self) -> None:
        with (
            patch(
                "api.providers.wellcome.make_request", return_value=PRESENTATION_WORK
            ),
            patch(
                "api.providers.wellcome.download_iiif_manifest_and_images",
                return_value=True,
            ) as mock_dl,
        ):
            assert download_wellcome_work({"id": "asd4849u"}, "/tmp/out") is True

        assert mock_dl.call_args[0][0] == MANIFEST_URL

    def test_no_digitised_content_fails_cleanly(self) -> None:
        with patch(
            "api.providers.wellcome.make_request", return_value=PHYSICAL_ONLY_WORK
        ):
            assert download_wellcome_work({"id": "physical1"}, "/tmp/out") is False


class TestMaxImages:
    """A JSON-authored cap must apply whatever numeric shape it takes."""

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [(50, 50), ("50", 50), (50.0, 50), (0, 0)],
    )
    def test_numeric_shapes_are_coerced(self, configured: Any, expected: int) -> None:
        with patch(
            "api.providers.wellcome.get_provider_setting", return_value=configured
        ):
            assert _max_images() == expected

    def test_unparsable_value_falls_back_instead_of_raising(self) -> None:
        with patch(
            "api.providers.wellcome.get_provider_setting", return_value="not-a-number"
        ):
            assert _max_images() == 0
