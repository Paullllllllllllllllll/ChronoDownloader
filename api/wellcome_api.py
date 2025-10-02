"""Connector for Wellcome Collection Catalogue + IIIF Image API.

Docs:
- Catalogue v2: https://developers.wellcomecollection.org/api/catalogue
- IIIF (Image API): https://developers.wellcomecollection.org/docs/iiif

We search /catalogue/v2/works with include=items and pick locations of type
"iiif-image" which point to info.json. From that we derive the IIIF Image API
service base and download full-size images.
"""
from __future__ import annotations

import logging
import os
from typing import List, Union, Dict, Optional

from .utils import make_request, save_json, get_provider_setting, budget_exhausted, download_file
from .iiif import download_one_from_service
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

CATALOGUE_WORKS_URL = "https://api.wellcomecollection.org/catalogue/v2/works"


def _max_images() -> int | None:
    """Read max images per work from config (provider_settings.wellcome.max_images).
    
    Returns:
        Max images limit (0 or None means all images)
    """
    val = get_provider_setting("wellcome", "max_images", None)
    if isinstance(val, int):
        return val
    # fallback env (optional)
    try:
        return int(os.getenv("WELLCOME_MAX_IMAGES", "0"))
    except ValueError:
        return 0


def _extract_image_services(work: Dict) -> List[str]:
    """Return a list of IIIF Image API service base URLs from a Work doc (with include=items).

    Looks for items[].locations[] entries with locationType.id == "iiif-image".
    Each such location has a URL ending with /info.json; we return the base before info.json.
    
    Args:
        work: Wellcome work dictionary
        
    Returns:
        List of IIIF Image API service base URLs
    """
    services: List[str] = []
    for item in work.get("items", []) or []:
        for loc in item.get("locations", []) or []:
            lt = (loc.get("locationType") or {}).get("id")
            if lt == "iiif-image":
                url = loc.get("url") or ""
                if url.endswith("/info.json"):
                    base = url[: -len("/info.json")]
                    services.append(base)
    return services


def search_wellcome(title: str, creator: Optional[str] = None, max_results: int = 3) -> List[SearchResult]:
    """Search Wellcome works and return entries that have IIIF Image services.

    We combine title and optional creator into a simple query string, request include=items,
    then collect works that provide at least one iiif-image location.
    
    Args:
        title: Work title to search for
        creator: Optional creator/author name
        max_results: Maximum number of results to return
        
    Returns:
        List of SearchResult objects
    """
    q = title if not creator else f"{title} {creator}"
    # Pull a few extra results to increase the chance of having iiif images
    page_size = max(25, max_results * 5)
    params = {
        "query": q,
        "include": "items",
        "pageSize": page_size,
    }
    logger.info("Searching Wellcome Collection for: %s", title)
    data = make_request(CATALOGUE_WORKS_URL, params=params)
    results: List[SearchResult] = []
    if not isinstance(data, dict):
        return results
    for work in data.get("results", []) or []:
        services = _extract_image_services(work)
        if not services:
            continue
        raw = {
            "title": work.get("title") or title,
            "creator": None,
            "id": work.get("id"),
            "image_services": services,
            "thumbnail": (work.get("thumbnail") or {}).get("url"),
        }
        results.append(convert_to_searchresult("Wellcome Collection", raw))
        if len(results) >= max_results:
            break
    return results


def download_wellcome_work(item_data: Union[SearchResult, Dict], output_folder: str) -> bool:
    """Download full-size images from Wellcome IIIF Image services.

    If the SearchResult contains raw.image_services, we use them directly.
    Otherwise, we refetch the Work with include=items to discover iiif-image locations.
    
    Args:
        item_data: SearchResult or dict containing item data
        output_folder: Folder to download files to
        
    Returns:
        True if download was successful, False otherwise
    """
    if isinstance(item_data, SearchResult):
        work_id = item_data.source_id or item_data.raw.get("id")
        services = item_data.raw.get("image_services") or []
        title = item_data.title
    else:
        work_id = item_data.get("id")
        services = item_data.get("image_services", [])
        title = item_data.get("title")

    # Refetch work if needed
    if work_id and not services:
        url = f"{CATALOGUE_WORKS_URL}/{work_id}"
        work = make_request(url, params={"include": "items"})
        if isinstance(work, dict):
            services = _extract_image_services(work)

    if not services:
        logger.info("No IIIF image services found for Wellcome work %s", work_id or title)
        return False

    # Download images. Use IIIF Image v2-style URL; Wellcome Image API accepts 'full/full/0/default.jpg'.
    # Use shared helper for per-service download attempts

    max_images = _max_images()
    to_download = services[:max_images] if max_images and max_images > 0 else services
    logger.info(
        "Wellcome: downloading %d/%d image(s) for work %s",
        len(to_download), len(services), work_id or title,
    )
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        if budget_exhausted():
            logger.warning(
                "Download budget exhausted; stopping Wellcome downloads at %d/%d images for %s",
                idx - 1,
                len(to_download),
                work_id or title,
            )
            break
        try:
            fname = f"wellcome_{(work_id or 'work')}_img{idx:04d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                ok_any = True
            else:
                logger.warning("Failed to download image from %s", svc)
        except Exception:
            logger.exception("Error downloading Wellcome image from %s", svc)
    # Attempt thumbnail download as an additional/fallback object
    try:
        thumb_url = None
        if isinstance(item_data, SearchResult):
            thumb_url = item_data.raw.get("thumbnail")
        else:
            thumb_url = item_data.get("thumbnail")
        if not thumb_url and work_id:
            # Try refetching minimal work to get thumbnail if missing
            work = make_request(f"{CATALOGUE_WORKS_URL}/{work_id}")
            if isinstance(work, dict):
                thumb_url = (work.get("thumbnail") or {}).get("url")
        if thumb_url:
            if download_file(thumb_url, output_folder, f"wellcome_{work_id or 'work'}_thumbnail.jpg"):
                ok_any = True
    except Exception:
        logger.exception("Wellcome: error downloading thumbnail for %s", work_id or title)

    return ok_any
