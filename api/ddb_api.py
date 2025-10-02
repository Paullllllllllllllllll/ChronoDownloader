"""Connector for the Deutsche Digitale Bibliothek (DDB) API."""

import logging
import os
from typing import List, Union

from .utils import (
    save_json,
    make_request,
    get_max_pages,
    download_iiif_renderings,
    prefer_pdf_over_images,
)
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)


API_BASE_URL = "https://api.deutsche-digitale-bibliothek.de/"


def _api_key() -> str | None:
    """Get DDB API key from environment."""
    return os.getenv("DDB_API_KEY")


def search_ddb(title: str, creator: str | None = None, max_results: int = 3) -> List[SearchResult]:
    """Search the DDB API for a title and optional creator."""

    key = _api_key()
    if not key:
        logger.warning("DDB API key not configured. Skipping DDB search.")
        return []

    query_parts = [f'"{title}"']
    if creator:
        query_parts.append(f'AND creator:"{creator}"')
    query = " ".join(query_parts)

    params = {
        "oauth_consumer_key": key,
        "query": query,
        "rows": max_results,
    }

    logger.info("Searching DDB for: %s", title)
    data = make_request(f"{API_BASE_URL}search", params=params)

    results: List[SearchResult] = []
    if data and data.get("results"):
        for item in data["results"]:
            raw = {
                "title": item.get("title", "N/A"),
                "creator": ", ".join(item.get("creator", [])),
                "id": item.get("id") or item.get("objectID"),
                "iiif_manifest": item.get("iiifManifest"),
            }
            results.append(convert_to_searchresult("DDB", raw))

    return results


def download_ddb_work(item_data: Union[SearchResult, dict], output_folder: str) -> bool:
    """Download IIIF manifest and page images for a DDB item.

    - Fetch item metadata (for provenance and possible manifest URL).
    - Resolve IIIF manifest URL (from metadata or search result).
    - Save manifest JSON.
    - Parse v2/v3 manifest to find IIIF Image API service per canvas and download images.
    """

    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id")
    else:
        item_id = item_data.get("id")
    if not item_id:
        logger.warning("No DDB item id found in item data.")
        return False

    key = _api_key()
    params = {"oauth_consumer_key": key} if key else None
    item_meta = make_request(f"{API_BASE_URL}items/{item_id}", params=params)

    if not item_meta:
        return False

    save_json(item_meta, output_folder, f"ddb_{item_id}_metadata")

    manifest_url = item_meta.get("iiifManifest") or (
        item_data.raw.get("iiif_manifest") if isinstance(item_data, SearchResult) else item_data.get("iiif_manifest")
    )
    if not manifest_url:
        logger.info("No IIIF manifest URL found for DDB item %s", item_id)
        return False

    manifest = make_request(manifest_url)
    if not manifest:
        return False

    # Save manifest
    save_json(manifest, output_folder, f"ddb_{item_id}_iiif_manifest")

    # Prefer manifest-level renderings (PDF/EPUB) when available
    try:
        renders = download_iiif_renderings(manifest, output_folder, filename_prefix=f"ddb_{item_id}_")
        if renders > 0 and prefer_pdf_over_images():
            logger.info("DDB: downloaded %d rendering(s); skipping image downloads per config.", renders)
            return True
    except Exception:
        logger.exception("DDB: error while downloading manifest renderings for %s", item_id)

    # Extract IIIF image service bases from v2 or v3
    image_service_bases: List[str] = extract_image_service_bases(manifest)

    if not image_service_bases:
        logger.info("No IIIF image services found in DDB manifest for %s", item_id)
        return True

    # Use shared helper to attempt per-canvas image downloads

    max_pages = get_max_pages("ddb")
    total = len(image_service_bases)
    to_download = image_service_bases[:max_pages] if max_pages and max_pages > 0 else image_service_bases
    logger.info("DDB: downloading %d/%d page images for %s", len(to_download), total, item_id)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"ddb_{item_id}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                ok_any = True
            else:
                logger.warning("Failed to download DDB image from %s", svc)
        except Exception:
            logger.exception("Error downloading DDB image for %s from %s", item_id, svc)

    return ok_any
