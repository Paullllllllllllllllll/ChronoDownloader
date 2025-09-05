"""Connector for the Polona.pl API."""

import logging
import urllib.parse
from typing import List, Union

from .utils import save_json, make_request
from .model import SearchResult, convert_to_searchresult
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# The previous JSON API endpoints appear to have changed. As a reliable fallback,
# query the website search page and parse item links, then use the stable IIIF manifest.
SEARCH_PAGE_URL = "https://polona.pl/search/?query={query}"
IIIF_MANIFEST_URL = "https://polona.pl/iiif/item/{item_id}/manifest.json"


def search_polona(title, creator=None, max_results=3) -> List[SearchResult]:
    """Search Polona by parsing the public search page for item links.

    Note: Polona does not expose a stable, documented JSON search for items.
    This parser targets links of the form /item/<id>/ and extracts up to max_results.
    """

    query = title if not creator else f"{title} {creator}"
    url = SEARCH_PAGE_URL.format(query=urllib.parse.quote_plus(query))
    logger.info("Searching Polona for: %s", title)
    html = make_request(url)

    results: List[SearchResult] = []
    if isinstance(html, str):
        soup = BeautifulSoup(html, "html.parser")
        seen = set()
        # Find item links
        for a in soup.select('a[href^="/item/"]'):
            href = a.get("href", "")
            # Expect /item/<numeric or uuid>/
            try:
                path = href.split("?")[0]
                parts = [p for p in path.strip("/").split("/") if p]
                if len(parts) >= 2 and parts[0] == "item":
                    item_id = parts[1]
                    if item_id not in seen:
                        seen.add(item_id)
                        title_text = a.get("title") or a.get_text(strip=True) or "N/A"
                        raw = {
                            "title": title_text,
                            "creator": creator or "N/A",
                            "id": item_id,
                        }
                        results.append(convert_to_searchresult("Polona", raw))
                        if len(results) >= max_results:
                            break
            except Exception:
                continue
    return results


def download_polona_work(item_data: Union[SearchResult, dict], output_folder):
    """Download IIIF manifest for a Polona item.

    Polona's item JSON API is not publicly documented; we prioritize IIIF.
    """

    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id")
    else:
        item_id = item_data.get("id")
    if not item_id:
        logger.warning("No Polona item id provided.")
        return False

    manifest_url = IIIF_MANIFEST_URL.format(item_id=item_id)
    manifest = make_request(manifest_url)
    if manifest:
        save_json(manifest, output_folder, f"polona_{item_id}_manifest")
        return True

    return False
