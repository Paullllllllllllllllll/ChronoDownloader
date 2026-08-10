"""Connector for the Polona.pl API.

Polona is an Angular single-page application: ``https://polona.pl/search/``
returns a content-free HTML shell, so the former link-scraping search could
never yield a hit. The SPA talks to a documented Spring gateway at
``https://polona.pl/api`` (see ``/api/search-service/api-docs``); this module
uses that gateway directly.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.network import make_request
from ..iiif import download_iiif_manifest_and_images
from ..model import SearchResult, convert_to_searchresult, resolve_item_id

logger = logging.getLogger(__name__)

API_BASE_URL = "https://polona.pl/api"
SEARCH_URL = f"{API_BASE_URL}/search-service/search/simple"
# The old Cantaloupe-style URL (https://polona.pl/iiif/item/<id>/manifest.json)
# answers "404 No route for path"; manifests are served by the search service.
IIIF_MANIFEST_URL = (
    f"{API_BASE_URL}/search-service/search/iiif/{{item_id}}/manifest.json"
)
ITEM_URL = "https://polona.pl/preview/{item_id}"


def _first_value(fields: Any, name: str) -> str | None:
    """Return the first non-empty entry of a Polona metadata field.

    Polona wraps every metadata field as ``{"name": ..., "values": [...],
    "labels": {...}, "type": ...}``.

    Args:
        fields: A ``basicFields``/``expandedFields``/``hiddenFields`` mapping.
        name: Field name to read (e.g. ``"title"``).

    Returns:
        The first non-empty value as a string, or ``None``.
    """
    if not isinstance(fields, dict):
        return None
    field = fields.get(name)
    if not isinstance(field, dict):
        return None
    values = field.get("values")
    if isinstance(values, list):
        for value in values:
            if value:
                return str(value)
    return None


def _thumbnail_url(attributes: Any) -> str | None:
    """Resolve the gateway-relative thumbnail path of a search hit.

    Args:
        attributes: The ``attributes`` mapping of a Polona hit.

    Returns:
        An absolute thumbnail URL, or ``None`` when the hit carries none.
    """
    if not isinstance(attributes, dict):
        return None
    thumbnail = attributes.get("thumbnail")
    if not isinstance(thumbnail, dict):
        return None
    value = thumbnail.get("stringValue")
    if not value:
        return None
    value = str(value)
    return f"{API_BASE_URL}{value}" if value.startswith("/") else value


def search_polona(
    title: str, creator: str | None = None, max_results: int = 3
) -> list[SearchResult]:
    """Search Polona through the public search-service gateway.

    Args:
        title: Title terms to search for.
        creator: Optional creator terms, appended to the query.
        max_results: Maximum number of results to return.

    Returns:
        List of SearchResult objects whose ``source_id`` is the Polona
        ``objectId`` (a UUID) that the download path needs.
    """

    query = title if not creator else f"{title} {creator}"
    logger.info("Searching Polona for: %s", title)
    # All four parameters are mandatory; omitting ``sort`` yields HTTP 400.
    data = make_request(
        SEARCH_URL,
        params={
            "query": query,
            "page": 0,
            "pageSize": max(1, max_results),
            "sort": "RELEVANCE",
        },
    )
    if not isinstance(data, dict):
        logger.warning("Polona: search returned no usable JSON for %r", query)
        return []

    results: list[SearchResult] = []
    for hit in data.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        object_id = hit.get("objectId")
        if not object_id:
            continue

        basic = hit.get("basicFields")
        raw: dict[str, Any] = {
            "title": _first_value(basic, "title") or "",
            "creator": _first_value(basic, "creatorForSearch"),
            "date": (
                _first_value(basic, "dateDescriptive")
                or _first_value(hit.get("hiddenFields"), "date")
            ),
            "id": str(object_id),
            "item_url": ITEM_URL.format(item_id=object_id),
            "thumbnail": _thumbnail_url(hit.get("attributes")),
        }
        results.append(convert_to_searchresult("Polona", raw))
        if len(results) >= max_results:
            break

    return results


def download_polona_work(
    item_data: SearchResult | dict[str, Any], output_folder: str
) -> bool:
    """Download IIIF manifest and page images for a Polona item.

    Polona exposes a stable IIIF manifest per item; we parse v2/v3 and download
    full-size images.
    """

    item_id = resolve_item_id(item_data)
    if not item_id:
        logger.warning("No Polona item id provided.")
        return False

    manifest_url = IIIF_MANIFEST_URL.format(item_id=item_id)
    return download_iiif_manifest_and_images(
        manifest_url, output_folder, "polona", item_id
    )
