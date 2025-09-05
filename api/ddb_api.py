"""Connector for the Deutsche Digitale Bibliothek (DDB) API."""

import logging
import os
from typing import List, Union

from .utils import save_json, make_request
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)


API_BASE_URL = "https://api.deutsche-digitale-bibliothek.de/"
API_KEY = os.getenv("DDB_API_KEY")


def search_ddb(title, creator=None, max_results=3) -> List[SearchResult]:
    """Search the DDB API for a title and optional creator."""

    if not API_KEY:
        logger.warning("DDB API key not configured. Skipping DDB search.")
        return []

    query_parts = [f'"{title}"']
    if creator:
        query_parts.append(f'AND creator:"{creator}"')
    query = " ".join(query_parts)

    params = {
        "oauth_consumer_key": API_KEY,
        "query": query,
        "rows": max_results,
    }

    logger.info("Searching DDB for: %s", title)
    data = make_request(f"{API_BASE_URL}search", params=params)

    results: List[SearchResult] = []
    if data and data.get("results"):
        for item in data["results"]:
            raw = {
                "title": item.get("title", "N/A"),
                "creator": ", ".join(item.get("creator", [])),
                "id": item.get("id") or item.get("objectID"),
                "iiif_manifest": item.get("iiifManifest"),
            }
            results.append(convert_to_searchresult("DDB", raw))

    return results


def download_ddb_work(item_data: Union[SearchResult, dict], output_folder):
    """Download metadata and IIIF manifest for a DDB item."""

    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id")
    else:
        item_id = item_data.get("id")
    if not item_id:
        logger.warning("No DDB item id found in item data.")
        return False

    params = {"oauth_consumer_key": API_KEY} if API_KEY else None
    item_meta = make_request(f"{API_BASE_URL}items/{item_id}", params=params)

    if item_meta:
        save_json(item_meta, output_folder, f"ddb_{item_id}_metadata")

        manifest_url = item_meta.get("iiifManifest") or (
            item_data.raw.get("iiif_manifest") if isinstance(item_data, SearchResult) else item_data.get("iiif_manifest")
        )
        if manifest_url:
            manifest_data = make_request(manifest_url)
            if manifest_data:
                save_json(manifest_data, output_folder, f"ddb_{item_id}_iiif_manifest")

        return True

    return False
