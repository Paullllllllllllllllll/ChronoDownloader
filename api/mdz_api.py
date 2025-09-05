"""Connector for the Münchener DigitalisierungsZentrum (MDZ) API."""

import logging
from typing import List, Union

from .utils import save_json, make_request
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)


# MDZ switched Solr endpoints; try the current generic select first, then fallback to the old core path
SEARCH_PRIMARY_URL = "https://api.digitale-sammlungen.de/solr/select"
SEARCH_FALLBACK_URL = "https://api.digitale-sammlungen.de/solr/mdzsearch/select"
IIIF_MANIFEST_URL = "https://api.digitale-sammlungen.de/iiif/presentation/v2/{object_id}/manifest"


def search_mdz(title, creator=None, max_results=3) -> List[SearchResult]:
    """Search MDZ using its Solr search interface."""

    query = f'title:"{title}"'
    if creator:
        query += f' AND creator:"{creator}"'

    params = {
        "q": query,
        "rows": max_results,
        "wt": "json",
    }

    logger.info("Searching MDZ for: %s", title)
    data = make_request(SEARCH_PRIMARY_URL, params=params)
    if not data or not data.get("response"):
        # Try legacy core URL
        data = make_request(SEARCH_FALLBACK_URL, params=params)

    results: List[SearchResult] = []
    if data and data.get("response") and data["response"].get("docs"):
        for doc in data["response"]["docs"]:
            raw = {
                "title": doc.get("title") or doc.get("title_t", "N/A"),
                "creator": ", ".join(doc.get("creator", [])) if isinstance(doc.get("creator"), list) else (doc.get("creator") or "N/A"),
                # MDZ docs may use different fields for identifiers
                "id": doc.get("id") or doc.get("pi") or doc.get("recordId"),
            }
            results.append(convert_to_searchresult("MDZ", raw))

    return results


def download_mdz_work(item_data: Union[SearchResult, dict], output_folder):
    """Download the IIIF manifest for a MDZ item."""

    if isinstance(item_data, SearchResult):
        object_id = item_data.source_id or item_data.raw.get("id")
    else:
        object_id = item_data.get("id")
    if not object_id:
        logger.warning("No MDZ object id found in item data.")
        return False

    manifest_url = IIIF_MANIFEST_URL.format(object_id=object_id)
    logger.info("Fetching MDZ IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)

    if manifest:
        save_json(manifest, output_folder, f"mdz_{object_id}_manifest")
        return True

    return False
