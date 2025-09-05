import logging
import os
from typing import List, Union

from .utils import save_json, make_request
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.europeana.eu/record/v2/search.json"
API_KEY = os.getenv("EUROPEANA_API_KEY")

def search_europeana(title, creator=None, max_results=3) -> List[SearchResult]:
    if not API_KEY:
        logger.warning("Europeana API key not configured. Skipping search.")
        return []
    query_parts = [f'title:"{title}"']
    if creator:
        query_parts.append(f'AND who:"{creator}"')
    query_parts.append('AND proxy_dc_type:"TEXT"')
    query = " ".join(query_parts)
    params = {
        "wskey": API_KEY,
        "query": query,
        "rows": str(max_results),
    }
    logger.info("Searching Europeana for: %s", title)
    data = make_request(API_BASE_URL, params=params)
    results: List[SearchResult] = []
    if data and data.get("success") and data.get("items"):
        for item in data["items"]:
            item_title = item.get("title", ["N/A"])
            if isinstance(item_title, list):
                item_title = item_title[0]
            item_creator = "N/A"
            if item.get("dcCreator"):
                item_creator = item["dcCreator"][0]
            iiif_manifest = None
            if item.get("edmAggregatedCHO") and item["edmAggregatedCHO"].get("hasView"):
                views = item["edmAggregatedCHO"]["hasView"]
                if not isinstance(views, list):
                    views = [views]
                for view in views:
                    if isinstance(view, str) and "iiif" in view and "manifest" in view:
                        iiif_manifest = view
                        break
                    elif isinstance(view, dict) and view.get("@id") and "iiif" in view["@id"] and "manifest" in view["@id"]:
                        iiif_manifest = view["@id"]
                        break
            if not iiif_manifest and "iiif" in item.get("object", "") and "manifest" in item.get("object", ""):
                iiif_manifest = item.get("object")
            raw = {
                "title": item_title,
                "creator": item_creator,
                "id": item.get("id"),
                "europeana_url": item.get("guid"),
                "provider": item.get("dataProvider", ["N/A"])[0] if item.get("dataProvider") else "N/A",
                "iiif_manifest": iiif_manifest,
            }
            results.append(convert_to_searchresult("Europeana", raw))
    elif data and not data.get("success"):
        logger.error("Europeana API error: %s", data.get("error"))
    return results

def download_europeana_work(item_data: Union[SearchResult, dict], output_folder):
    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.title or "unknown_item"
        # save the raw provider result if available
        if item_data.raw:
            save_json(item_data.raw, output_folder, f"europeana_{item_id}_search_meta")
        iiif_manifest_url = item_data.iiif_manifest or item_data.raw.get("iiif_manifest")
    else:
        item_id = item_data.get("id", item_data.get("title", "unknown_item"))
        save_json(item_data, output_folder, f"europeana_{item_id}_search_meta")
        iiif_manifest_url = item_data.get("iiif_manifest")
    if iiif_manifest_url:
        logger.info("Fetching Europeana IIIF manifest: %s", iiif_manifest_url)
        manifest_data = make_request(iiif_manifest_url)
        if manifest_data:
            save_json(manifest_data, output_folder, f"europeana_{item_id}_iiif_manifest")
        else:
            logger.warning("Failed to fetch IIIF manifest from %s", iiif_manifest_url)
    else:
        logger.info("No direct IIIF manifest URL found in Europeana search result for %s.", item_id)
    shown_by = None
    if isinstance(item_data, SearchResult):
        shown_by = item_data.raw.get("edmIsShownBy")
    else:
        shown_by = item_data.get("edmIsShownBy")
    if shown_by:
        v = shown_by[0] if isinstance(shown_by, list) else shown_by
        logger.debug("Item image (provider link): %s", v)
    return True
