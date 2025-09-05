"""Connector for the Polona.pl API."""

import logging
from typing import List, Union

from .utils import save_json, make_request
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)


SEARCH_API_URL = "https://polona.pl/api/search"
DETAIL_API_URL = "https://polona.pl/api/items/{item_id}"
IIIF_MANIFEST_URL = "https://polona.pl/iiif/item/{item_id}/manifest.json"


def search_polona(title, creator=None, max_results=3) -> List[SearchResult]:
    """Search Polona for items matching the title/creator."""

    query = title
    if creator:
        query += f" {creator}"

    params = {
        "query": query,
        "format": "json",
        "limit": max_results,
    }

    logger.info("Searching Polona for: %s", title)
    data = make_request(SEARCH_API_URL, params=params)

    results: List[SearchResult] = []
    if data and data.get("items"):
        for item in data["items"]:
            raw = {
                "title": item.get("title", "N/A"),
                "creator": item.get("creator", "N/A"),
                "id": item.get("uid"),
            }
            results.append(convert_to_searchresult("Polona", raw))

    return results


def download_polona_work(item_data: Union[SearchResult, dict], output_folder):
    """Download metadata and IIIF manifest for a Polona item."""

    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id")
    else:
        item_id = item_data.get("id")
    if not item_id:
        logger.warning("No Polona item id provided.")
        return False

    detail_url = DETAIL_API_URL.format(item_id=item_id)
    metadata = make_request(detail_url)
    if metadata:
        save_json(metadata, output_folder, f"polona_{item_id}_metadata")

    manifest_url = IIIF_MANIFEST_URL.format(item_id=item_id)
    manifest = make_request(manifest_url)
    if manifest:
        save_json(manifest, output_folder, f"polona_{item_id}_manifest")
        return True

    return False
