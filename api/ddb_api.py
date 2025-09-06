"""Connector for the Deutsche Digitale Bibliothek (DDB) API."""

import logging
import os
from typing import List, Union

from .utils import save_json, make_request, download_file, get_provider_setting
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)


API_BASE_URL = "https://api.deutsche-digitale-bibliothek.de/"

def _api_key() -> str | None:
    return os.getenv("DDB_API_KEY")


def _ddb_max_pages() -> int | None:
    """Read max pages from config provider_settings.ddb.max_pages (0/None = all)."""
    val = get_provider_setting("ddb", "max_pages", None)
    if isinstance(val, int):
        return val
    try:
        return int(os.getenv("DDB_MAX_PAGES", "0"))
    except ValueError:
        return 0


def search_ddb(title, creator=None, max_results=3) -> List[SearchResult]:
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


def download_ddb_work(item_data: Union[SearchResult, dict], output_folder):
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

    # Extract IIIF image service bases from v2 or v3
    image_service_bases: List[str] = []

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
                        image_service_bases.append(svc_id)
                except Exception:
                    continue
    except Exception:
        pass

    # v3
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
        logger.info("No IIIF image services found in DDB manifest for %s", item_id)
        return True

    # Build candidate full-size URLs (v2/v3 compatible patterns)
    def _candidates(base: str) -> list[str]:
        b = base.rstrip('/')
        return [
            f"{b}/full/full/0/default.jpg",
            f"{b}/full/max/0/default.jpg",
        ]

    def _download_one(base: str, filename: str) -> bool:
        for u in _candidates(base):
            if download_file(u, output_folder, filename):
                return True
        return False

    max_pages = _ddb_max_pages()
    total = len(image_service_bases)
    to_download = image_service_bases[:max_pages] if max_pages and max_pages > 0 else image_service_bases
    logger.info("DDB: downloading %d/%d page images for %s", len(to_download), total, item_id)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"ddb_{item_id}_p{idx:05d}.jpg"
            if _download_one(svc, fname):
                ok_any = True
            else:
                logger.warning("Failed to download DDB image from %s", svc)
        except Exception:
            logger.exception("Error downloading DDB image for %s from %s", item_id, svc)

    return ok_any
