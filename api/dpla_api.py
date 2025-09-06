"""Connector for the Digital Public Library of America (DPLA) API."""

import logging
import os
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

logger = logging.getLogger(__name__)


API_BASE_URL = "https://api.dp.la/v2/"

def _api_key() -> str | None:
    return os.getenv("DPLA_API_KEY")


def _dpla_max_pages() -> int | None:
    """Read max pages from config provider_settings.dpla.max_pages (0/None = all)."""
    val = get_provider_setting("dpla", "max_pages", None)
    if isinstance(val, int):
        return val
    try:
        return int(os.getenv("DPLA_MAX_PAGES", "0"))
    except ValueError:
        return 0


def search_dpla(title, creator=None, max_results=3) -> List[SearchResult]:
    """Search the DPLA API for a title and optional creator."""

    key = _api_key()
    if not key:
        logger.warning("DPLA API key not configured. Skipping search.")
        return []

    params = {
        "q": title,
        "api_key": key,
        "page_size": max_results,
    }
    if creator:
        params["sourceResource.creator"] = creator

    logger.info("Searching DPLA for: %s", title)
    data = make_request(f"{API_BASE_URL}items", params=params)

    results: List[SearchResult] = []
    if data and data.get("docs"):
        for doc in data["docs"]:
            obj = doc.get("object")
            iiif_manifest = obj if (isinstance(obj, str) and ("iiif" in obj and "manifest" in obj)) else None
            raw = {
                "title": doc.get("sourceResource", {}).get("title", "N/A"),
                "creator": ", ".join(doc.get("sourceResource", {}).get("creator", [])),
                "id": doc.get("id"),
                "iiif_manifest": iiif_manifest,
            }
            results.append(convert_to_searchresult("DPLA", raw))

    return results


def download_dpla_work(item_data: Union[SearchResult, dict], output_folder):
    """Download metadata, IIIF manifest, and page images for a DPLA item (when available)."""

    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id")
    else:
        item_id = item_data.get("id")
    if not item_id:
        logger.warning("No DPLA item id found in item data.")
        return False

    key = _api_key()
    params = {"api_key": key} if key else None
    item_details = make_request(f"{API_BASE_URL}items/{item_id}", params=params)
    if not item_details:
        return False

    save_json(item_details, output_folder, f"dpla_{item_id}_metadata")

    manifest_url = item_details.get("object") or (
        item_data.raw.get("iiif_manifest") if isinstance(item_data, SearchResult) else item_data.get("iiif_manifest")
    )
    if not (manifest_url and isinstance(manifest_url, str) and "manifest" in manifest_url):
        logger.info("No IIIF manifest URL found for DPLA item %s", item_id)
        return True

    manifest = make_request(manifest_url)
    if not manifest:
        return False

    save_json(manifest, output_folder, f"dpla_{item_id}_iiif_manifest")

    # Prefer manifest-level renderings (PDF/EPUB) when available
    try:
        renders = download_iiif_renderings(manifest, output_folder, filename_prefix=f"dpla_{item_id}_")
        if renders > 0 and prefer_pdf_over_images():
            logger.info("DPLA: downloaded %d rendering(s); skipping image downloads per config.", renders)
            return True
    except Exception:
        logger.exception("DPLA: error while downloading manifest renderings for %s", item_id)

    # Extract IIIF Image API service bases (v2/v3)
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
        logger.info("No IIIF image services found in DPLA manifest for %s", item_id)
        return True

    def _candidates(base: str) -> list[str]:
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

    max_pages = _dpla_max_pages()
    total = len(service_bases)
    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
    logger.info("DPLA: downloading %d/%d page images for %s", len(to_download), total, item_id)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"dpla_{item_id}_p{idx:05d}.jpg"
            if _download_one(svc, fname):
                ok_any = True
            else:
                logger.warning("Failed to download DPLA image from %s", svc)
        except Exception:
            logger.exception("Error downloading DPLA image for %s from %s", item_id, svc)

    return ok_any
