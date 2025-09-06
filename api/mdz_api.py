"""Connector for the Münchener DigitalisierungsZentrum (MDZ) API."""

import logging
import os
import re
import urllib.parse
from typing import List, Union

from .utils import (
    save_json,
    make_request,
    download_file,
    get_provider_setting,
    download_iiif_renderings,
    prefer_pdf_over_images,
)
from .model import SearchResult, convert_to_searchresult
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# MDZ switched Solr endpoints; try the current generic select first, then fallback to the old core path
SEARCH_PRIMARY_URL = "https://api.digitale-sammlungen.de/solr/select"
SEARCH_FALLBACK_URL = "https://api.digitale-sammlungen.de/solr/mdzsearch/select"
IIIF_MANIFEST_URL = "https://api.digitale-sammlungen.de/iiif/presentation/v2/{object_id}/manifest"
IIIF_MANIFEST_V3_URL = "https://api.digitale-sammlungen.de/iiif/presentation/v3/{object_id}/manifest"
MDZ_WEB_SEARCH_URL = "https://www.digitale-sammlungen.de/api/search"

# MDZ page download limit (0 or missing means all pages). Controlled via config.json:
# {
#   "provider_settings": { "mdz": { "max_pages": 0 } }
# }
# Falls back to environment MDZ_MAX_PAGES only if not present in config
def _max_pages() -> int | None:
    cfg_val = get_provider_setting("mdz", "max_pages", None)
    if isinstance(cfg_val, int):
        return cfg_val
    try:
        return int(os.getenv("MDZ_MAX_PAGES", "0"))
    except ValueError:
        return 0


def search_mdz(title, creator=None, max_results=3) -> List[SearchResult]:
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
    results: List[SearchResult] = []
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
                raw = {"title": title_text, "creator": creator_text, "id": obj_id}
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
            href = a["href"]
            m = re.search(r"/(?:en|de)?/view/([^/?#]+)", href)
            if not m:
                continue
            obj_id = m.group(1)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            title_text = a.get_text(strip=True) or title
            raw = {"title": title_text, "creator": creator or "N/A", "id": obj_id}
            results.append(convert_to_searchresult("MDZ", raw))
            if len(results) >= max_results:
                break
    if results:
        return results

    # Fallback: legacy Solr search (may be disabled)
    query = f'title:"{title}"'
    if creator:
        query += f' AND creator:"{creator}"'
    params = {"q": query, "rows": max_results, "wt": "json"}
    data = make_request(SEARCH_PRIMARY_URL, params=params)
    if not data or not data.get("response"):
        data = make_request(SEARCH_FALLBACK_URL, params=params)
    if data and data.get("response") and data["response"].get("docs"):
        for doc in data["response"]["docs"]:
            raw = {
                "title": doc.get("title") or doc.get("title_t", "N/A"),
                "creator": ", ".join(doc.get("creator", [])) if isinstance(doc.get("creator"), list) else (doc.get("creator") or "N/A"),
                "id": doc.get("id") or doc.get("pi") or doc.get("recordId"),
            }
            results.append(convert_to_searchresult("MDZ", raw))
    return results


def download_mdz_work(item_data: Union[SearchResult, dict], output_folder):
    """Download the IIIF manifest and page images for an MDZ item.

    - Fetches the IIIF Presentation manifest (v2 or v3).
    - Extracts the IIIF Image API service base for each canvas.
    - Downloads up to DEFAULT_MAX_PAGES (override via env MDZ_MAX_PAGES) images using the IIIF Image API.
    """

    if isinstance(item_data, SearchResult):
        object_id = item_data.source_id or item_data.raw.get("id")
    else:
        object_id = item_data.get("id")
    if not object_id:
        logger.warning("No MDZ object id found in item data.")
        return False

    manifest_url_v2 = IIIF_MANIFEST_URL.format(object_id=object_id)
    logger.info("Fetching MDZ IIIF manifest v2: %s", manifest_url_v2)
    manifest = make_request(manifest_url_v2)
    if not manifest:
        # Try IIIF v3 manifest
        manifest_url_v3 = IIIF_MANIFEST_V3_URL.format(object_id=object_id)
        logger.info("Fetching MDZ IIIF manifest v3: %s", manifest_url_v3)
        manifest = make_request(manifest_url_v3)

    if not manifest:
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

    # Helper to build a direct image URL from an Image API base
    def _build_image_url(image_service_base: str) -> str:
        base = image_service_base.rstrip('/')
        # Choose pattern by API version in path
        if "/image/v3/" in base:
            # IIIF Image API v3 typical path
            return f"{base}/full/max/0/default.jpg"
        # Default to v2 pattern
        return f"{base}/full/full/0/default.jpg"

    # Extract per-canvas Image API service base URLs
    image_service_bases: List[str] = []

    # IIIF v2
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
                        # Fallback: derive from direct image URL if present
                        img_id = res.get("@id") or res.get("id")
                        if img_id and "/full/" in img_id:
                            svc_id = img_id.split("/full/")[0]
                    if svc_id:
                        image_service_bases.append(svc_id)
                except Exception:
                    continue
    except Exception:
        pass

    # IIIF v3
    if not image_service_bases and manifest.get("items"):
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
                    # Body may be a list in some manifests; take first image body
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
                        image_service_bases.append(svc_id)
                except Exception:
                    continue
        except Exception:
            pass

    if not image_service_bases:
        logger.info("No IIIF image services found in MDZ manifest for %s", object_id)
        return True

    # Download images (limit pages by config provider_settings.mdz.max_pages; 0 or missing = all)
    total_pages = len(image_service_bases)
    max_pages = _max_pages()
    to_download = image_service_bases[:max_pages] if max_pages and max_pages > 0 else image_service_bases
    logger.info("MDZ: downloading %d/%d page images for %s", len(to_download), total_pages, object_id)
    success_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            img_url = _build_image_url(svc)
            filename = f"mdz_{object_id}_p{idx:05d}.jpg"
            download_file(img_url, output_folder, filename)
            success_any = True
        except Exception:
            logger.exception("Failed to download MDZ image for %s from %s", object_id, svc)

    return success_any
