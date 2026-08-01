"""Unit tests for the Polona connector's JSON search and manifest path.

Polona serves an Angular single-page application: ``https://polona.pl/search/``
answers HTTP 200 with a 15.8 kB shell that contains no ``/item/`` links at all,
so the connector's former BeautifulSoup link scraper could not return a single
hit. The SPA drives a Spring gateway at ``https://polona.pl/api`` whose
search-service is self-documenting (``/api/search-service/api-docs``); the
connector now queries ``GET /search-service/search/simple`` directly.

The same rewrite retired ``https://polona.pl/iiif/item/{id}/manifest.json``,
which answers ``404 No route for path`` from Cantaloupe. Manifests come from
``/api/search-service/search/iiif/{objectId}/manifest.json``.

Every fixture below is a trimmed but verbatim snippet of a live response
recorded on 01.08.2026; the tests themselves never touch the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from api.providers.polona import (
    API_BASE_URL,
    IIIF_MANIFEST_URL,
    SEARCH_URL,
    download_polona_work,
    search_polona,
)

# ---------------------------------------------------------------------------
# Recorded fixtures
# ---------------------------------------------------------------------------

# Recorded from
# https://polona.pl/api/search-service/search/simple
#     ?query=kucharz%20doskona%C5%82y&page=0&pageSize=3&sort=RELEVANCE
# (HTTP 200, 21,025 bytes, totalElements 17). Each hit is trimmed to the
# fields the connector reads; the field wrapper shape is verbatim.
SEARCH_RESPONSE: dict[str, Any] = {
    "totalElements": 17,
    "number": 0,
    "size": 3,
    "numberOfElements": 3,
    "totalPages": 6,
    "first": True,
    "last": False,
    "hits": [
        {
            "basicFields": {
                "title": {
                    "name": "title",
                    "values": [
                        "Kucharz doskonały : sekrety kuchmistrzowskie "
                        "Wojciecha Wielądki"
                    ],
                    "labels": {"en": "Title", "pl": "Tytuł"},
                    "type": "TEXT",
                },
                "creatorForSearch": {
                    "name": "creatorForSearch",
                    "values": ["Dumanowski, Jarosław (1967- ) Autor"],
                    "labels": {"en": "Creator", "pl": "Autor"},
                    "type": "SEARCH",
                },
                "dateDescriptive": {
                    "name": "dateDescriptive",
                    "values": ["copyright 2018"],
                    "labels": {"en": "Creation date", "pl": "Data powstania"},
                    "type": "TEXT",
                },
            },
            "hiddenFields": {
                "date": {
                    "name": "date",
                    "values": ["2018-01-01"],
                    "labels": {"en": "Date", "pl": "Data"},
                    "type": "TEXT",
                }
            },
            "attributes": {
                "thumbnail": {
                    "name": "thumbnail",
                    "stringArrValues": None,
                    "stringValue": (
                        "/download/digital-content/5b045498-17df-4035-bd56-6ec0181cb5da"
                    ),
                    "longValue": None,
                    "valueType": "STRING",
                }
            },
            "score": 415.56802,
            "objectId": "6e15cb64-c546-4b38-9f3f-2eccf761f57d",
        },
        {
            "basicFields": {
                "title": {
                    "name": "title",
                    "values": ["Kucharz doskonały. T. 2"],
                    "labels": {"en": "Title", "pl": "Tytuł"},
                    "type": "TEXT",
                },
                "creatorForSearch": {
                    "name": "creatorForSearch",
                    "values": ["Szytler, Jan (1763-1850)"],
                    "labels": {"en": "Creator", "pl": "Autor"},
                    "type": "SEARCH",
                },
                "dateDescriptive": {
                    "name": "dateDescriptive",
                    "values": ["1834"],
                    "labels": {"en": "Creation date", "pl": "Data powstania"},
                    "type": "TEXT",
                },
            },
            "hiddenFields": {
                "date": {
                    "name": "date",
                    "values": ["1834-01-01"],
                    "labels": {"en": "Date", "pl": "Data"},
                    "type": "TEXT",
                }
            },
            "attributes": {
                "thumbnail": {
                    "name": "thumbnail",
                    "stringArrValues": None,
                    "stringValue": (
                        "/download/digital-content/da240875-76e9-4b7f-a83b-cb91486cfc22"
                    ),
                    "longValue": None,
                    "valueType": "STRING",
                }
            },
            "score": 413.344,
            "objectId": "7ca593d6-2d0d-497f-98f0-a4b4f5723a28",
        },
        {
            "basicFields": {
                "title": {
                    "name": "title",
                    "values": ["Kucharz doskonały. T. 1"],
                    "labels": {"en": "Title", "pl": "Tytuł"},
                    "type": "TEXT",
                },
                "creatorForSearch": {
                    "name": "creatorForSearch",
                    "values": ["Szytler, Jan (1763-1850)"],
                    "labels": {"en": "Creator", "pl": "Autor"},
                    "type": "SEARCH",
                },
                "dateDescriptive": {
                    "name": "dateDescriptive",
                    "values": ["1834"],
                    "labels": {"en": "Creation date", "pl": "Data powstania"},
                    "type": "TEXT",
                },
            },
            "hiddenFields": {},
            "attributes": {},
            "score": 412.04913,
            "objectId": "2a7cfd9e-273a-47bc-92ee-f7f563413f25",
        },
    ],
    "aggregations": {},
    "timeAggregations": {},
}

# Recorded from the same endpoint for "compendium ferculorum": an article
# record without a thumbnail attribute, next to the 1755 first edition.
SPARSE_RESPONSE: dict[str, Any] = {
    "totalElements": 2,
    "hits": [
        {
            "basicFields": {
                "title": {
                    "name": "title",
                    "values": [
                        "[Compendium ferculorum albo Zebranie potraw - recenzja]"
                    ],
                    "type": "TEXT",
                }
            },
            "hiddenFields": {},
            "attributes": {},
            "objectId": "8232b659-16de-4b6b-8c5d-7ec976975416",
        },
        {
            "basicFields": {
                "title": {
                    "name": "title",
                    "values": ["Compendium ferculorum albo zebranie potraw"],
                    "type": "TEXT",
                },
                "creatorForSearch": {
                    "name": "creatorForSearch",
                    "values": ["Czerniecki, Stanisław Autor"],
                    "type": "SEARCH",
                },
                "dateDescriptive": {
                    "name": "dateDescriptive",
                    "values": ["1755"],
                    "type": "TEXT",
                },
            },
            "hiddenFields": {},
            "attributes": {
                "thumbnail": {
                    "name": "thumbnail",
                    "stringValue": (
                        "/download/digital-content/312b4dc1-d237-449d-b7f7-bda6dd917b11"
                    ),
                    "valueType": "STRING",
                }
            },
            "objectId": "b11c20f5-9ff1-4b16-995e-a8451a8b954c",
        },
    ],
}

EMPTY_RESPONSE: dict[str, Any] = {"totalElements": 0, "hits": []}

CZERNIECKI_ID = "b11c20f5-9ff1-4b16-995e-a8451a8b954c"

# Trimmed IIIF Presentation v3 manifest recorded from
# https://polona.pl/api/search-service/search/iiif/
#     7ca593d6-2d0d-497f-98f0-a4b4f5723a28/manifest.json (HTTP 200, 133 kB).
MANIFEST: dict[str, Any] = {
    "@context": [
        "http://www.w3.org/ns/anno.jsonld",
        "http://iiif.io/api/presentation/3/context.json",
    ],
    "type": "Manifest",
    "id": (
        "https://polona.pl/api/search-service/search/iiif/"
        "7ca593d6-2d0d-497f-98f0-a4b4f5723a28/manifest.json"
    ),
    "label": {"pl": ["Kucharz doskonały. T. 2"]},
    "items": [
        {
            "id": (
                "https://polona.pl/api/search-service/search/iiif/"
                "7ca593d6-2d0d-497f-98f0-a4b4f5723a28/canvas/"
                "ab2b1059-7b95-4078-84d9-dd312a4b5d52"
            ),
            "type": "Canvas",
            "height": 3306,
            "width": 2243,
            "items": [
                {
                    "type": "AnnotationPage",
                    "items": [
                        {
                            "type": "Annotation",
                            "motivation": "painting",
                            "body": {
                                "id": (
                                    "https://polona.pl/iiif/3/"
                                    "ce686a7b-e0c2-4ee0-85b0-903a96eeb6dc"
                                    "/full/max/0/default.jpg"
                                ),
                                "type": "Image",
                                "format": "image/tiff",
                                "height": 3306,
                                "width": 2243,
                                "service": [
                                    {
                                        "id": (
                                            "https://polona.pl/iiif/3/"
                                            "ce686a7b-e0c2-4ee0-85b0-903a96eeb6dc"
                                        ),
                                        "type": "ImageService3",
                                        "profile": "level2",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


class TestSearchRequest:
    """The gateway rejects an incomplete query string with HTTP 400."""

    def test_targets_the_json_gateway_not_the_spa_shell(self) -> None:
        with patch(
            "api.providers.polona.make_request", return_value=SEARCH_RESPONSE
        ) as mock_request:
            search_polona("kucharz doskonały")

        url = mock_request.call_args.args[0]
        assert url == SEARCH_URL
        assert url.startswith(f"{API_BASE_URL}/search-service/")
        assert "/search/?query=" not in url

    def test_sends_every_mandatory_parameter(self) -> None:
        """query, page, pageSize and sort are all required; a missing sort
        turns the whole call into a 400 Bad Request."""
        with patch(
            "api.providers.polona.make_request", return_value=SEARCH_RESPONSE
        ) as mock_request:
            search_polona("kucharz doskonały", max_results=3)

        params = mock_request.call_args.kwargs["params"]
        assert set(params) == {"query", "page", "pageSize", "sort"}
        assert params["query"] == "kucharz doskonały"
        assert params["page"] == 0
        assert params["pageSize"] == 3
        assert params["sort"] == "RELEVANCE"

    def test_creator_is_appended_to_the_query(self) -> None:
        with patch(
            "api.providers.polona.make_request", return_value=SEARCH_RESPONSE
        ) as mock_request:
            search_polona("kucharz doskonały", creator="Szytler")

        assert mock_request.call_args.kwargs["params"]["query"] == (
            "kucharz doskonały Szytler"
        )

    @pytest.mark.parametrize("max_results", [0, -5])
    def test_page_size_never_drops_below_one(self, max_results: int) -> None:
        with patch(
            "api.providers.polona.make_request", return_value=SEARCH_RESPONSE
        ) as mock_request:
            search_polona("kucharz doskonały", max_results=max_results)

        assert mock_request.call_args.kwargs["params"]["pageSize"] == 1


# ---------------------------------------------------------------------------
# search_polona
# ---------------------------------------------------------------------------


class TestSearchPolona:
    """Hits must be unwrapped into the canonical SearchResult shape."""

    def test_source_id_is_the_object_uuid(self) -> None:
        with patch("api.providers.polona.make_request", return_value=SEARCH_RESPONSE):
            results = search_polona("kucharz doskonały", max_results=3)

        assert [r.source_id for r in results] == [
            "6e15cb64-c546-4b38-9f3f-2eccf761f57d",
            "7ca593d6-2d0d-497f-98f0-a4b4f5723a28",
            "2a7cfd9e-273a-47bc-92ee-f7f563413f25",
        ]

    def test_metadata_is_unwrapped_from_the_field_envelope(self) -> None:
        with patch("api.providers.polona.make_request", return_value=SEARCH_RESPONSE):
            results = search_polona("kucharz doskonały", max_results=3)

        assert results[1].title == "Kucharz doskonały. T. 2"
        assert results[1].creators == ["Szytler, Jan (1763-1850)"]
        assert results[1].date == "1834"

    def test_item_url_uses_the_live_preview_route(self) -> None:
        """The SPA routes records to /preview/<id>; /item/<id> is retired."""
        with patch("api.providers.polona.make_request", return_value=SEARCH_RESPONSE):
            results = search_polona("kucharz doskonały", max_results=3)

        for result in results:
            assert result.item_url == (f"https://polona.pl/preview/{result.source_id}")
        assert not any("/item/" in (r.item_url or "") for r in results)

    def test_thumbnail_is_absolutized_against_the_gateway(self) -> None:
        with patch("api.providers.polona.make_request", return_value=SEARCH_RESPONSE):
            results = search_polona("kucharz doskonały", max_results=2)

        assert results[0].thumbnail_url == (
            f"{API_BASE_URL}/download/digital-content/"
            "5b045498-17df-4035-bd56-6ec0181cb5da"
        )

    def test_missing_thumbnail_is_tolerated(self) -> None:
        with patch("api.providers.polona.make_request", return_value=SPARSE_RESPONSE):
            results = search_polona("compendium ferculorum", max_results=2)

        assert results[0].thumbnail_url is None
        assert results[1].thumbnail_url is not None

    def test_missing_creator_and_date_are_tolerated(self) -> None:
        with patch("api.providers.polona.make_request", return_value=SPARSE_RESPONSE):
            results = search_polona("compendium ferculorum", max_results=2)

        assert results[0].creators == []
        assert results[0].date is None
        assert results[1].date == "1755"

    def test_max_results_is_respected(self) -> None:
        with patch("api.providers.polona.make_request", return_value=SEARCH_RESPONSE):
            results = search_polona("kucharz doskonały", max_results=2)

        assert len(results) == 2

    def test_hits_without_an_object_id_are_dropped(self) -> None:
        payload = {"hits": [{"basicFields": {}}, SEARCH_RESPONSE["hits"][1]]}
        with patch("api.providers.polona.make_request", return_value=payload):
            results = search_polona("kucharz doskonały", max_results=5)

        assert [r.source_id for r in results] == [
            "7ca593d6-2d0d-497f-98f0-a4b4f5723a28"
        ]

    def test_empty_result_set_returns_no_results(self) -> None:
        with patch("api.providers.polona.make_request", return_value=EMPTY_RESPONSE):
            assert search_polona("nonexistent work") == []

    def test_html_shell_response_returns_no_results(self) -> None:
        """A str payload is what the dead SPA-scraping path used to receive."""
        with patch(
            "api.providers.polona.make_request", return_value="<html>...</html>"
        ):
            assert search_polona("kucharz doskonały") == []

    def test_failed_request_returns_no_results(self) -> None:
        with patch("api.providers.polona.make_request", return_value=None):
            assert search_polona("kucharz doskonały") == []


# ---------------------------------------------------------------------------
# download_polona_work
# ---------------------------------------------------------------------------


class TestDownloadPolonaWork:
    """Downloads must address the search-service manifest, not Cantaloupe."""

    def test_manifest_url_is_the_search_service_route(self) -> None:
        assert IIIF_MANIFEST_URL.format(item_id=CZERNIECKI_ID) == (
            "https://polona.pl/api/search-service/search/iiif/"
            f"{CZERNIECKI_ID}/manifest.json"
        )

    def test_fetches_the_manifest_returned_by_search(
        self, temp_output_dir: str
    ) -> None:
        with (
            patch(
                "api.providers.polona.make_request", return_value=MANIFEST
            ) as mock_request,
            patch("api.providers.polona.save_json", return_value=None),
            patch("api.providers.polona.download_iiif_renderings", return_value=0),
            patch(
                "api.providers.polona.download_page_images", return_value=True
            ) as mock_images,
        ):
            assert download_polona_work({"id": CZERNIECKI_ID}, temp_output_dir)

        url = mock_request.call_args.args[0]
        assert url.startswith(f"{API_BASE_URL}/search-service/search/iiif/")
        # The Cantaloupe route answers "404 No route for path".
        assert "polona.pl/iiif/item/" not in url
        assert mock_images.call_args.args[0] == [
            "https://polona.pl/iiif/3/ce686a7b-e0c2-4ee0-85b0-903a96eeb6dc"
        ]

    def test_missing_identifier_fails_cleanly(self, temp_output_dir: str) -> None:
        with patch("api.providers.polona.make_request") as mock_request:
            assert download_polona_work({}, temp_output_dir) is False

        mock_request.assert_not_called()

    def test_unreachable_manifest_fails_cleanly(self, temp_output_dir: str) -> None:
        with patch("api.providers.polona.make_request", return_value=None):
            assert download_polona_work({"id": CZERNIECKI_ID}, temp_output_dir) is False
