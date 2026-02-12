"""Connector for the Münchener DigitalisierungsZentrum (MDZ) API."""
from __future__ import annotations

import logging
import re
import urllib.parse

from .download_helpers import download_page_images
from .iiif import extract_image_service_bases
from .core.config import prefer_pdf_over_images
from .core.network import make_request
from .utils import save_json, download_iiif_renderings
from .model import SearchResult, convert_to_searchresult, resolve_item_id
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# MDZ API endpoints (Solr endpoints deprecated as of 2024/2025)
# Primary search endpoint is the web API which returns JSON
MDZ_WEB_SEARCH_URL = "https://www.digitale-sammlungen.de/api/search"
IIIF_MANIFEST_URL = "https://api.digitale-sammlungen.de/iiif/presentation/v2/{object_id}/manifest"
IIIF_MANIFEST_V3_URL = "https://api.digitale-sammlungen.de/iiif/presentation/v3/{object_id}/manifest"

def search_mdz(title: str, creator: str | None = None, max_results: int = 3) -> list[SearchResult]:
    """Search MDZ using the public JSON search endpoint, with HTML/Solr fallbacks.

    Primary endpoint: /api/search (same domain as the website), returns JSON with 'docs'.
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
                title_html = doc.get("title") or title
                # Strip simple tags from highlighted title
                title_text = re.sub(r"<[^>]+>", "", title_html)
                authors = doc.get("authors") or []
                creator_text = ", ".join(authors) if isinstance(authors, list) else (authors or "N/A")
                raw = {
                    "title": title_text,
                    "creator": creator_text,
                    "id": obj_id,
                    "item_url": f"https://www.digitale-sammlungen.de/view/{obj_id}",
                }
                results.append(convert_to_searchresult("MDZ", raw))
            except Exception:
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
            m = re.search(r"/(?:en|de)?/view/([^/?#]+)", href)
            if not m:
                continue
            obj_id = m.group(1)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            title_text = a.get_text(strip=True) or title
            raw = {
                "title": title_text,
                "creator": creator or "N/A",
                "id": obj_id,
                "item_url": f"https://www.digitale-sammlungen.de/view/{obj_id}",
            }
            results.append(convert_to_searchresult("MDZ", raw))
            if len(results) >= max_results:
                break
    # Return results from primary API or HTML fallback
    # Legacy Solr endpoints are deprecated and removed as of 2024/2025
    return results

def download_mdz_work(item_data: SearchResult | dict, output_folder: str) -> bool:
    """Download the IIIF manifest and page images for an MDZ item.

    - Fetches the IIIF Presentation manifest (v2 or v3).
    - Extracts the IIIF Image API service base for each canvas.
    - Downloads up to DEFAULT_MAX_PAGES (override via env MDZ_MAX_PAGES) images using the IIIF Image API.
    """

    object_id = resolve_item_id(item_data)
    if not object_id:
        logger.warning("No MDZ object id found in item data.")
        return False

    manifest_url_v2 = IIIF_MANIFEST_URL.format(object_id=object_id)
    logger.info("Fetching MDZ IIIF manifest v2: %s", manifest_url_v2)
    manifest = make_request(manifest_url_v2)
    if not isinstance(manifest, dict):
        # Try IIIF v3 manifest
        manifest_url_v3 = IIIF_MANIFEST_V3_URL.format(object_id=object_id)
        logger.info("Fetching MDZ IIIF manifest v3: %s", manifest_url_v3)
        manifest = make_request(manifest_url_v3)

    if not isinstance(manifest, dict):
        return False

    # Always save the manifest for reproducibility
    save_json(manifest, output_folder, f"mdz_{object_id}_manifest")

    # Try to download manifest-level PDF/EPUB renderings first
    try:
        renderings_downloaded = download_iiif_renderings(manifest, output_folder, filename_prefix=f"mdz_{object_id}_")
        if renderings_downloaded > 0 and prefer_pdf_over_images():
            logger.info("MDZ: downloaded %d manifest rendering(s); skipping image downloads per config.", renderings_downloaded)
            return True
    except Exception:
        logger.exception("MDZ: error while downloading manifest renderings for %s", object_id)

    # Extract per-canvas Image API service base URLs and download
    image_service_bases = extract_image_service_bases(manifest)

    if not image_service_bases:
        logger.info("No IIIF image services found in MDZ manifest for %s", object_id)
        return True

    return download_page_images(image_service_bases, output_folder, "mdz", object_id)
