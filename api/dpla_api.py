"""Connector for the Digital Public Library of America (DPLA) API."""

import logging
import os
from typing import List, Union

from .utils import (
    save_json,
    make_request,
    get_max_pages,
    download_iiif_renderings,
    prefer_pdf_over_images,
    download_file,
)
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)


API_BASE_URL = "https://api.dp.la/v2/"


def _api_key() -> str | None:
    """Get DPLA API key from environment."""
    return os.getenv("DPLA_API_KEY")


def search_dpla(title: str, creator: str | None = None, max_results: int = 3) -> List[SearchResult]:
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
            # Try to discover a IIIF manifest URL from common DPLA fields
            def _extract_manifest(d: dict) -> str | None:
                candidates = []
                # Common top-level string fields
                for k in ("object", "isShownAt", "isShownBy"):
                    v = d.get(k)
                    if isinstance(v, str):
                        candidates.append(v)
                # hasView may be list of dicts or strings
                hv = d.get("hasView")
                if isinstance(hv, list):
                    for item in hv:
                        if isinstance(item, str):
                            candidates.append(item)
                        elif isinstance(item, dict):
                            for kk in ("@id", "id", "url"):
                                if isinstance(item.get(kk), str):
                                    candidates.append(item[kk])
                elif isinstance(hv, dict):
                    for kk in ("@id", "id", "url"):
                        if isinstance(hv.get(kk), str):
                            candidates.append(hv[kk])
                # Pick the first that looks like a manifest
                for u in candidates:
                    if isinstance(u, str) and "manifest" in u and "iiif" in u:
                        return u
                return None

            iiif_manifest = _extract_manifest(doc)
            src = doc.get("sourceResource", {}) if isinstance(doc.get("sourceResource", {}), dict) else {}
            title_text = src.get("title")
            if isinstance(title_text, list):
                title_text = title_text[0] if title_text else "N/A"
            creators = src.get("creator") or []
            if isinstance(creators, str):
                creators = [creators]
            raw = {
                "title": title_text or "N/A",
                "creator": ", ".join(creators),
                "id": doc.get("id"),
                "iiif_manifest": iiif_manifest,
                # Keep additional discovery fields for auditing/fallback
                "isShownAt": doc.get("isShownAt"),
                "isShownBy": doc.get("isShownBy"),
                "object": doc.get("object"),
                "hasView": doc.get("hasView"),
                "provider": (doc.get("provider", {}) or {}).get("name"),
            }
            results.append(convert_to_searchresult("DPLA", raw))

    return results


def download_dpla_work(item_data: Union[SearchResult, dict], output_folder: str) -> bool:
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

    # Manifest discovery from details and search fallbacks
    manifest_url = None
    # From details
    for key in ("object", "isShownAt", "isShownBy"):
        v = item_details.get(key)
        if isinstance(v, str) and "manifest" in v and "iiif" in v:
            manifest_url = v
            break
    if not manifest_url:
        hv = item_details.get("hasView")
        def _from_hv(hv) -> str | None:
            if isinstance(hv, list):
                for it in hv:
                    if isinstance(it, str) and "manifest" in it and "iiif" in it:
                        return it
                    if isinstance(it, dict):
                        for kk in ("@id", "id", "url"):
                            u = it.get(kk)
                            if isinstance(u, str) and "manifest" in u and "iiif" in u:
                                return u
            elif isinstance(hv, dict):
                for kk in ("@id", "id", "url"):
                    u = hv.get(kk)
                    if isinstance(u, str) and "manifest" in u and "iiif" in u:
                        return u
            return None
        manifest_url = _from_hv(hv)
    # From search payload
    if not manifest_url:
        if isinstance(item_data, SearchResult):
            manifest_url = item_data.raw.get("iiif_manifest")
        else:
            manifest_url = item_data.get("iiif_manifest")

    if manifest_url and not (isinstance(manifest_url, str) and "manifest" in manifest_url):
        manifest_url = None

    ok_any = False
    if manifest_url:
        manifest = make_request(manifest_url)
        if manifest:
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
            service_bases: List[str] = extract_image_service_bases(manifest)

            if service_bases:
                # Use shared helper to try full-size image candidates per canvas
                max_pages = get_max_pages("dpla")
                total = len(service_bases)
                to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
                logger.info("DPLA: downloading %d/%d page images for %s", len(to_download), total, item_id)
                for idx, svc in enumerate(to_download, start=1):
                    try:
                        fname = f"dpla_{item_id}_p{idx:05d}.jpg"
                        if download_one_from_service(svc, output_folder, fname):
                            ok_any = True
                        else:
                            logger.warning("Failed to download DPLA image from %s", svc)
                    except Exception:
                        logger.exception("Error downloading DPLA image for %s from %s", item_id, svc)
            else:
                logger.info("No IIIF image services found in DPLA manifest for %s", item_id)

    if ok_any:
        return True

    # Fallbacks when we have no manifest or no images from manifest:
    # Try isShownBy (often a direct media resource), then hasView entries, then object (thumbnail).
    def _as_list(v):
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    fallback_fields = []
    # Prefer values from item_details; if not present, use search raw fields
    search_raw = item_data.raw if isinstance(item_data, SearchResult) else item_data
    for field in ("isShownBy", "hasView", "object"):
        val = item_details.get(field)
        if val is None and isinstance(search_raw, dict):
            val = search_raw.get(field)
        if val is not None:
            fallback_fields.append((field, val))

    for field, val in fallback_fields:
        if field == "hasView":
            items = _as_list(val)
            for idx, v in enumerate(items, start=1):
                url = v
                if isinstance(v, dict):
                    url = v.get("@id") or v.get("id") or v.get("url")
                if isinstance(url, str):
                    try:
                        fname = f"dpla_{item_id}_fallback_{idx:02d}"
                        if download_file(url, output_folder, fname):
                            ok_any = True
                            break
                    except Exception:
                        continue
            if ok_any:
                break
        else:
            url = val if isinstance(val, str) else None
            if url:
                fname = f"dpla_{item_id}_fallback"
                if download_file(url, output_folder, fname):
                    ok_any = True
                    break

    return ok_any
