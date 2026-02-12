"""Connector for the Polona.pl API."""
from __future__ import annotations

import logging
import urllib.parse

from .core.config import get_max_pages, prefer_pdf_over_images
from .core.network import make_request
from .utils import save_json, download_iiif_renderings
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult, resolve_item_id
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# The previous JSON API endpoints appear to have changed. As a reliable fallback,
# query the website search page and parse item links, then use the stable IIIF manifest.
SEARCH_PAGE_URL = "https://polona.pl/search/?query={query}"
IIIF_MANIFEST_URL = "https://polona.pl/iiif/item/{item_id}/manifest.json"

def search_polona(title: str, creator: str | None = None, max_results: int = 3) -> list[SearchResult]:
    """Search Polona by parsing the public search page for item links.

    Note: Polona does not expose a stable, documented JSON search for items.
    This parser targets links of the form /item/<id>/ and extracts up to max_results.
    """

    query = title if not creator else f"{title} {creator}"
    url = SEARCH_PAGE_URL.format(query=urllib.parse.quote_plus(query))
    logger.info("Searching Polona for: %s", title)
    html = make_request(url)

    results: list[SearchResult] = []
    if isinstance(html, str):
        soup = BeautifulSoup(html, "html.parser")
        seen = set()
        # Find item links
        for a in soup.select('a[href^="/item/"]'):
            href = a.get("href", "")
            # Expect /item/<numeric or uuid>/
            try:
                path = str(href).split("?")[0] if href else ""
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
                            "item_url": f"https://polona.pl/item/{item_id}",
                        }
                        results.append(convert_to_searchresult("Polona", raw))
                        if len(results) >= max_results:
                            break
            except Exception:
                continue
    return results

def download_polona_work(item_data: SearchResult | dict, output_folder: str) -> bool:
    """Download IIIF manifest and page images for a Polona item.

    Polona exposes a stable IIIF manifest per item; we parse v2/v3 and download full-size images.
    """

    item_id = resolve_item_id(item_data)
    if not item_id:
        logger.warning("No Polona item id provided.")
        return False

    manifest_url = IIIF_MANIFEST_URL.format(item_id=item_id)
    logger.info("Fetching Polona IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)
    if not isinstance(manifest, dict):
        return False

    # Save manifest
    save_json(manifest, output_folder, f"polona_{item_id}_manifest")

    # Prefer manifest-level PDF/EPUB renderings when available
    try:
        renders = download_iiif_renderings(manifest, output_folder, filename_prefix=f"polona_{item_id}_")
        if renders > 0 and prefer_pdf_over_images():
            logger.info("Polona: downloaded %d rendering(s); skipping image downloads per config.", renders)
            return True
    except Exception:
        logger.exception("Polona: error while downloading manifest renderings for %s", item_id)

    # Extract IIIF Image API service bases
    service_bases: list[str] = extract_image_service_bases(manifest)

    if not service_bases:
        logger.info("No IIIF image services found in Polona manifest for %s", item_id)
        return True

    # Use shared helper to attempt per-canvas downloads

    max_pages = get_max_pages("polona")
    total = len(service_bases)
    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
    logger.info("Polona: downloading %d/%d page images for %s", len(to_download), total, item_id)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"polona_{item_id}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                ok_any = True
            else:
                logger.warning("Failed to download Polona image from %s", svc)
        except Exception:
            logger.exception("Error downloading Polona image for %s from %s", item_id, svc)
    return ok_any
