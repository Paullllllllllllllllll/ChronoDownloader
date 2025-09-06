"""Shared IIIF helpers for manifest parsing and image downloads.

This module centralizes common logic used by provider connectors when working with
IIIF Presentation (v2/v3) and IIIF Image API endpoints.

Functions exported:
- extract_image_service_bases(manifest): List[str]
- image_url_candidates(service_base): List[str]
- download_one_from_service(service_base, output_folder, filename): bool
"""
from __future__ import annotations

from typing import Any, Dict, List

from .utils import download_file

__all__ = [
    "extract_image_service_bases",
    "image_url_candidates",
    "download_one_from_service",
]


def extract_image_service_bases(manifest: Dict[str, Any]) -> List[str]:
    """Extract IIIF Image API service base URLs from a Presentation manifest.

    Supports both IIIF v2 and v3 structures. Duplicates are removed while
    preserving order.
    """
    bases: List[str] = []

    # IIIF v2: sequences[0].canvases[].images[0].resource.service['@id'|'id']
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
                        bases.append(svc_id)
                except Exception:
                    continue
    except Exception:
        pass

    # IIIF v3: items[].items[0].items[0].body[0?].service/services['@id'|'id']
    try:
        if manifest.get("items"):
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
                        bases.append(svc_id)
                except Exception:
                    continue
    except Exception:
        pass

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: List[str] = []
    for b in bases:
        if b not in seen and isinstance(b, str):
            seen.add(b)
            unique.append(b)
    return unique


def image_url_candidates(service_base: str) -> List[str]:
    """Return a list of likely IIIF Image API URLs for a service base.

    Includes variants compatible with common v2 and v3 servers.
    """
    b = service_base.rstrip("/")
    return [
        f"{b}/full/full/0/default.jpg",
        f"{b}/full/max/0/default.jpg",
        f"{b}/full/full/0/native.jpg",
    ]


def download_one_from_service(service_base: str, output_folder: str, filename: str) -> bool:
    """Try downloading a single image for one canvas using default candidates.

    Returns True on first successful download.
    """
    for url in image_url_candidates(service_base):
        if download_file(url, output_folder, filename):
            return True
    return False
