"""Connector for the Münchener DigitalisierungsZentrum (MDZ) API."""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from ..core.network import make_request
from ..iiif import download_iiif_manifest_and_images
from ..model import SearchResult, convert_to_searchresult, resolve_item_id

logger = logging.getLogger(__name__)

# MDZ API endpoints (Solr endpoints deprecated as of 2024/2025)
# Primary search endpoint is the web API which returns JSON
MDZ_WEB_SEARCH_URL = "https://www.digitale-sammlungen.de/api/search"
IIIF_MANIFEST_URL = (
    "https://api.digitale-sammlungen.de/iiif/presentation/v2/{object_id}/manifest"
)
IIIF_MANIFEST_V3_URL = (
    "https://api.digitale-sammlungen.de/iiif/presentation/v3/{object_id}/manifest"
)


def search_mdz(
    title: str, creator: str | None = None, max_results: int = 3
) -> list[SearchResult]:
    """Search MDZ using the public JSON search endpoint, with HTML/Solr fallbacks.

    Primary endpoint: /api/search (same domain as the website), returns JSON
    with 'docs'.
    We filter for iiifAvailable=true to prioritize digitized items.
    """

    q = title if not creator else f"{title} {creator}"
    logger.info("Searching MDZ for: %s", title)
    params = {
        "query": q,
        "handler": "simple-metadata",  # metadata-only search
        "pageSize": max_results,
        "ocrContext": 1,
    }
    data = make_request(MDZ_WEB_SEARCH_URL, params=params)
    results: list[SearchResult] = []
    if isinstance(data, dict) and data.get("docs"):
        for doc in data["docs"]:
            try:
                if doc.get("iiifAvailable") is False:
                    continue
                obj_id = doc.get("id")
                if not obj_id:
                    continue
                title_html = doc.get("title") or ""
                # A list-valued (highlighted) title would make re.sub raise
                # TypeError, silently dropping the doc; coerce to str first.
                if isinstance(title_html, list):
                    title_html = title_html[0] if title_html else ""
                # Strip simple tags from highlighted title
                title_text = re.sub(r"<[^>]+>", "", str(title_html))
                authors = doc.get("authors") or []
                # Plural key: joining with ", " would be re-split as an
                # inverted personal name (see api.model._as_list).
                creators_list = (
                    [str(a) for a in authors]
                    if isinstance(authors, list)
                    else ([str(authors)] if authors else [])
                )
                raw = {
                    "title": title_text,
                    "creators": creators_list,
                    "date": doc.get("publicationDate"),
                    "id": obj_id,
                    "item_url": f"https://www.digitale-sammlungen.de/view/{obj_id}",
                }
                results.append(convert_to_searchresult("MDZ", raw))
            except Exception as e:
                logger.debug("Skipping malformed MDZ result: %s", e)
                continue
    if results:
        return results

    # Fallback: HTML search parsing
    url = f"https://www.digitale-sammlungen.de/en/search?search={urllib.parse.quote_plus(q)}"
    html = make_request(url)
    seen = set()
    if isinstance(html, str):
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = str(a["href"])
            # The language segment is optional as a whole: the old
            # "/(?:en|de)?/view/" needed a doubled slash to match an
            # unprefixed "/view/bsb..." href, so those were all skipped.
            m = re.search(r"/(?:(?:en|de)/)?view/([^/?#]+)", href)
            if not m:
                continue
            obj_id = m.group(1)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            title_text = a.get_text(strip=True) or ""
            raw = {
                "title": title_text,
                # Never substitute the query's creator: the HTML result list
                # carries no author, and echoing the searched-for name back
                # would score a perfect 100 against itself and let this
                # candidate outrank providers reporting a real, possibly
                # non-matching author.
                "id": obj_id,
                "item_url": f"https://www.digitale-sammlungen.de/view/{obj_id}",
            }
            results.append(convert_to_searchresult("MDZ", raw))
            if len(results) >= max_results:
                break
    # Return results from primary API or HTML fallback
    # Legacy Solr endpoints are deprecated and removed as of 2024/2025
    return results


def download_mdz_work(
    item_data: SearchResult | dict[str, Any], output_folder: str
) -> bool:
    """Download the IIIF manifest and page images for an MDZ item.

    - Fetches the IIIF Presentation manifest (v2 or v3).
    - Extracts the IIIF Image API service base for each canvas.
    - Downloads up to DEFAULT_MAX_PAGES (override via env MDZ_MAX_PAGES) images
      using the IIIF Image API.
    """

    object_id = resolve_item_id(item_data)
    if not object_id:
        logger.warning("No MDZ object id found in item data.")
        return False

    # MDZ needs a version fallback the shared helper does not know about:
    # fetch v2 first, then v3, and hand the pre-fetched manifest over.
    manifest_url = IIIF_MANIFEST_URL.format(object_id=object_id)
    logger.info("Fetching MDZ IIIF manifest v2: %s", manifest_url)
    manifest = make_request(manifest_url)
    if not isinstance(manifest, dict):
        # Try IIIF v3 manifest
        manifest_url = IIIF_MANIFEST_V3_URL.format(object_id=object_id)
        logger.info("Fetching MDZ IIIF manifest v3: %s", manifest_url)
        manifest = make_request(manifest_url)

    if not isinstance(manifest, dict):
        return False

    return download_iiif_manifest_and_images(
        manifest_url, output_folder, "mdz", object_id, manifest=manifest
    )
