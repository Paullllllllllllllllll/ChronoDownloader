"""Connector for the Digital Public Library of America (DPLA) API."""

import logging
import os
from typing import List, Union

from .utils import save_json, make_request
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)


API_BASE_URL = "https://api.dp.la/v2/"
API_KEY = os.getenv("DPLA_API_KEY")


def search_dpla(title, creator=None, max_results=3) -> List[SearchResult]:
    """Search the DPLA API for a title and optional creator."""

    if not API_KEY:
        logger.warning("DPLA API key not configured. Skipping search.")
        return []

    params = {
        "q": title,
        "api_key": API_KEY,
        "page_size": max_results,
    }
    if creator:
        params["sourceResource.creator"] = creator

    logger.info("Searching DPLA for: %s", title)
    data = make_request(f"{API_BASE_URL}items", params=params)

    results: List[SearchResult] = []
    if data and data.get("docs"):
        for doc in data["docs"]:
            raw = {
                "title": doc.get("sourceResource", {}).get("title", "N/A"),
                "creator": ", ".join(doc.get("sourceResource", {}).get("creator", [])),
                "id": doc.get("id"),
                "iiif_manifest": doc.get("object"),
            }
            results.append(convert_to_searchresult("DPLA", raw))

    return results


def download_dpla_work(item_data: Union[SearchResult, dict], output_folder):
    """Download metadata and IIIF manifest for a DPLA item."""

    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id")
    else:
        item_id = item_data.get("id")
    if not item_id:
        logger.warning("No DPLA item id found in item data.")
        return False

    params = {"api_key": API_KEY} if API_KEY else None
    item_details = make_request(f"{API_BASE_URL}items/{item_id}", params=params)
    if item_details:
        save_json(item_details, output_folder, f"dpla_{item_id}_metadata")

        manifest_url = item_details.get("object") or (item_data.raw.get("iiif_manifest") if isinstance(item_data, SearchResult) else item_data.get("iiif_manifest"))
        if manifest_url and "manifest" in manifest_url:
            manifest_data = make_request(manifest_url)
            if manifest_data:
                save_json(manifest_data, output_folder, f"dpla_{item_id}_iiif_manifest")

        return True

    return False
