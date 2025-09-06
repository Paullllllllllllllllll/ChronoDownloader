"""Connector for the Polona.pl API."""

import logging
import urllib.parse
from typing import List, Union

from .utils import save_json, make_request, download_file, get_provider_setting
from .model import SearchResult, convert_to_searchresult
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# The previous JSON API endpoints appear to have changed. As a reliable fallback,
# query the website search page and parse item links, then use the stable IIIF manifest.
SEARCH_PAGE_URL = "https://polona.pl/search/?query={query}"
IIIF_MANIFEST_URL = "https://polona.pl/iiif/item/{item_id}/manifest.json"


def _polona_max_pages() -> int | None:
    """Read max pages from config provider_settings.polona.max_pages (0/None = all)."""
    val = get_provider_setting("polona", "max_pages", None)
    if isinstance(val, int):
        return val
    return 0


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
    """Download IIIF manifest and page images for a Polona item.

    Polona exposes a stable IIIF manifest per item; we parse v2/v3 and download full-size images.
    """

    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id")
    else:
        item_id = item_data.get("id")
    if not item_id:
        logger.warning("No Polona item id provided.")
        return False

    manifest_url = IIIF_MANIFEST_URL.format(item_id=item_id)
    logger.info("Fetching Polona IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)
    if not manifest:
        return False

    # Save manifest
    save_json(manifest, output_folder, f"polona_{item_id}_manifest")

    # Extract IIIF Image API service bases
    service_bases: List[str] = []

    # v2
    try:
        sequences = manifest.get("sequences") or []
        if sequences:
            canvases = sequences[0].get("canvases", [])
            for canvas in canvases:
                try:
                    images = canvas.get("images", [])
                    if not images:
                        continue
                    res = images[0].get("resource", {})
                    service = res.get("service", {})
                    svc_id = service.get("@id") or service.get("id")
                    if not svc_id:
                        img_id = res.get("@id") or res.get("id")
                        if img_id and "/full/" in img_id:
                            svc_id = img_id.split("/full/")[0]
                    if svc_id:
                        service_bases.append(svc_id)
                except Exception:
                    continue
    except Exception:
        pass

    # v3
    if not service_bases and manifest.get("items"):
        try:
            for canvas in manifest.get("items", []):
                try:
                    anno_pages = canvas.get("items", [])
                    if not anno_pages:
                        continue
                    annos = anno_pages[0].get("items", [])
                    if not annos:
                        continue
                    body = annos[0].get("body", {})
                    if isinstance(body, list) and body:
                        body = body[0]
                    service = body.get("service") or body.get("services")
                    svc_obj = None
                    if isinstance(service, list) and service:
                        svc_obj = service[0]
                    elif isinstance(service, dict):
                        svc_obj = service
                    svc_id = None
                    if svc_obj:
                        svc_id = svc_obj.get("@id") or svc_obj.get("id")
                    if not svc_id:
                        body_id = body.get("id")
                        if body_id and "/full/" in body_id:
                            svc_id = body_id.split("/full/")[0]
                    if svc_id:
                        service_bases.append(svc_id)
                except Exception:
                    continue
        except Exception:
            pass

    if not service_bases:
        logger.info("No IIIF image services found in Polona manifest for %s", item_id)
        return True

    # Build candidate URLs for full images (v2/v3 tolerant)
    def _candidates(base: str) -> List[str]:
        b = base.rstrip('/')
        return [
            f"{b}/full/full/0/default.jpg",
            f"{b}/full/max/0/default.jpg",
            f"{b}/full/full/0/native.jpg",
        ]

    def _download_one(base: str, filename: str) -> bool:
        for u in _candidates(base):
            if download_file(u, output_folder, filename):
                return True
        return False

    max_pages = _polona_max_pages()
    total = len(service_bases)
    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
    logger.info("Polona: downloading %d/%d page images for %s", len(to_download), total, item_id)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"polona_{item_id}_p{idx:05d}.jpg"
            if _download_one(svc, fname):
                ok_any = True
            else:
                logger.warning("Failed to download Polona image from %s", svc)
        except Exception:
            logger.exception("Error downloading Polona image for %s from %s", item_id, svc)
    return ok_any
