"""Connector for Wellcome Collection Catalogue + IIIF Image API.

Docs:
- Catalogue v2: https://developers.wellcomecollection.org/api/catalogue
- IIIF (Image API): https://developers.wellcomecollection.org/docs/iiif

We search /catalogue/v2/works with include=items. Historically every digitised
work carried an "iiif-image" location pointing at an info.json, from which the
Image API service base was derived directly. Wellcome has since moved to
Presentation-API locations: live responses now carry "iiif-presentation"
manifest URLs and no "iiif-image" entries at all, so an image-only filter
discards every hit and the provider returns nothing. Both location types are
therefore accepted, and a manifest is resolved to its image services on the
download path.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..core.budget import budget_exhausted
from ..core.config import get_provider_setting
from ..core.download import download_file
from ..core.network import make_request
from ..iiif import download_iiif_manifest_and_images, download_one_from_service
from ..model import (
    SearchResult,
    convert_to_searchresult,
    resolve_item_field,
    resolve_item_id,
)

logger = logging.getLogger(__name__)

CATALOGUE_WORKS_URL = "https://api.wellcomecollection.org/catalogue/v2/works"


def _max_images() -> int | None:
    """Read max images per work from config (provider_settings.wellcome.max_images).

    Returns:
        Max images limit (0 or None means all images)
    """
    val = get_provider_setting("wellcome", "max_images", None)
    if val is not None:
        # JSON admits "50" and 50.0 as readily as 50; an isinstance(int) test
        # dropped both to the env fallback and silently uncapped the work.
        try:
            return int(val)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid provider_settings.wellcome.max_images=%r; ignoring.", val
            )
    # fallback env (optional)
    try:
        return int(os.getenv("WELLCOME_MAX_IMAGES", "0"))
    except ValueError:
        return 0


def _extract_image_services(work: dict[str, Any]) -> list[str]:
    """Return a list of IIIF Image API service base URLs from a Work doc.

    Requires ``include=items``. Looks for items[].locations[] entries with
    locationType.id == "iiif-image". Each such location has a URL ending with
    /info.json; we return the base before info.json.

    Args:
        work: Wellcome work dictionary

    Returns:
        List of IIIF Image API service base URLs
    """
    services: list[str] = []
    for item in work.get("items", []) or []:
        for loc in item.get("locations", []) or []:
            lt = (loc.get("locationType") or {}).get("id")
            if lt == "iiif-image":
                url = loc.get("url") or ""
                if url.endswith("/info.json"):
                    base = url[: -len("/info.json")]
                    services.append(base)
    return services


def _extract_manifest_urls(work: dict[str, Any]) -> list[str]:
    """Return the IIIF Presentation manifest URLs of a Work doc.

    Wellcome's current catalogue responses expose digitised content as
    ``iiif-presentation`` locations rather than the per-image
    ``iiif-image``/info.json locations this connector was written against.

    Args:
        work: Wellcome work dictionary.

    Returns:
        List of IIIF Presentation manifest URLs.
    """
    manifests: list[str] = []
    for item in work.get("items", []) or []:
        for loc in item.get("locations", []) or []:
            if (loc.get("locationType") or {}).get("id") != "iiif-presentation":
                continue
            url = loc.get("url")
            if url and url not in manifests:
                manifests.append(str(url))
    return manifests


def _first_contributor(work: dict[str, Any]) -> str | None:
    """Return the first named contributor of a catalogue work.

    Args:
        work: Wellcome catalogue work document

    Returns:
        Contributor label, or None when the work names nobody
    """
    for contributor in work.get("contributors") or []:
        if not isinstance(contributor, dict):
            continue
        label = (contributor.get("agent") or {}).get("label")
        if label:
            return str(label).strip() or None
    return None


def _production_date(work: dict[str, Any]) -> str | None:
    """Return the printed publication date of a catalogue work.

    Args:
        work: Wellcome catalogue work document

    Returns:
        Date label as printed ("[1890]"), or None

    """
    for event in work.get("production") or []:
        if not isinstance(event, dict):
            continue
        for date in event.get("dates") or []:
            if isinstance(date, dict) and date.get("label"):
                return str(date["label"]).strip() or None
    return None


def search_wellcome(
    title: str, creator: str | None = None, max_results: int = 3
) -> list[SearchResult]:
    """Search Wellcome works and return entries that have IIIF Image services.

    We combine title and optional creator into a simple query string, request
    include=items, then collect works that provide at least one iiif-image
    location.

    Args:
        title: Work title to search for
        creator: Optional creator/author name
        max_results: Maximum number of results to return

    Returns:
        List of SearchResult objects
    """
    q = title if not creator else f"{title} {creator}"
    # Pull a few extra results to increase the chance of having iiif images,
    # but stay within the Wellcome catalogue API's pageSize cap of 100 (a
    # larger value 400s and make_request then returns no results).
    page_size = min(100, max(25, max_results * 5))
    params = {
        # contributors and production ride along on the same request, so the
        # creator and the printed date cost nothing extra. Asking only for
        # items left every Wellcome candidate with no author and no year:
        # unranked against providers that supply one, and an empty year
        # column in the run index.
        "query": q,
        "include": "items,contributors,production",
        "pageSize": page_size,
    }
    logger.info("Searching Wellcome Collection for: %s", title)
    data = make_request(CATALOGUE_WORKS_URL, params=params)
    results: list[SearchResult] = []
    if not isinstance(data, dict):
        return results
    for work in data.get("results", []) or []:
        # One malformed work must not discard the whole result set.
        try:
            services = _extract_image_services(work)
            manifests = _extract_manifest_urls(work)
            if not services and not manifests:
                continue
            work_id = work.get("id")
            raw = {
                "title": work.get("title") or "",
                "creator": _first_contributor(work),
                "date": _production_date(work),
                "id": work_id,
                "item_url": f"https://wellcomecollection.org/works/{work_id}"
                if work_id
                else None,
                "image_services": services,
                "iiif_manifest": manifests[0] if manifests else None,
                "thumbnail": (work.get("thumbnail") or {}).get("url"),
            }
            results.append(convert_to_searchresult("Wellcome Collection", raw))
        except Exception:
            logger.warning("Wellcome: skipping malformed record", exc_info=True)
            continue
        if len(results) >= max_results:
            break
    return results


def download_wellcome_work(
    item_data: SearchResult | dict[str, Any], output_folder: str
) -> bool:
    """Download full-size images from Wellcome IIIF Image services.

    If the SearchResult contains raw.image_services, we use them directly.
    Otherwise, we refetch the Work with include=items to discover iiif-image locations.

    Args:
        item_data: SearchResult or dict containing item data
        output_folder: Folder to download files to

    Returns:
        True if download was successful, False otherwise
    """
    work_id = resolve_item_id(item_data)
    services = resolve_item_field(item_data, "image_services", default=[]) or []
    manifest_url = resolve_item_field(item_data, "iiif_manifest")
    title = resolve_item_field(item_data, "title", attr="title", default="")

    # Refetch work if needed
    if work_id and not services and not manifest_url:
        url = f"{CATALOGUE_WORKS_URL}/{work_id}"
        work = make_request(url, params={"include": "items"})
        if isinstance(work, dict):
            services = _extract_image_services(work)
            manifests = _extract_manifest_urls(work)
            manifest_url = manifests[0] if manifests else None

    # A Presentation manifest is the shape Wellcome now serves; resolve it
    # through the shared IIIF strategy, which saves the manifest, tries PDF or
    # EPUB renderings, and falls back to per-canvas images under the budget.
    if not services and manifest_url:
        return download_iiif_manifest_and_images(
            str(manifest_url), output_folder, "wellcome", work_id or "work"
        )

    if not services:
        logger.info(
            "No IIIF image services found for Wellcome work %s", work_id or title
        )
        return False

    # Download images. Use IIIF Image v2-style URL; Wellcome Image API accepts
    # 'full/full/0/default.jpg'.
    # Use shared helper for per-service download attempts

    max_images = _max_images()
    to_download = services[:max_images] if max_images and max_images > 0 else services
    logger.info(
        "Wellcome: downloading %d/%d image(s) for work %s",
        len(to_download),
        len(services),
        work_id or title,
    )
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        if budget_exhausted():
            logger.warning(
                "Download budget exhausted; stopping Wellcome downloads at "
                "%d/%d images for %s",
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
    # Attempt thumbnail download as a bonus object only. A thumbnail alone is
    # not real content, so it must not mark the work as successfully retrieved.
    try:
        thumb_url = resolve_item_field(item_data, "thumbnail")
        if not thumb_url and work_id:
            # Try refetching minimal work to get thumbnail if missing
            work = make_request(f"{CATALOGUE_WORKS_URL}/{work_id}")
            if isinstance(work, dict):
                thumb_url = (work.get("thumbnail") or {}).get("url")
        if thumb_url:
            download_file(
                thumb_url, output_folder, f"wellcome_{work_id or 'work'}_thumbnail.jpg"
            )
    except Exception:
        logger.exception(
            "Wellcome: error downloading thumbnail for %s", work_id or title
        )

    return ok_any
