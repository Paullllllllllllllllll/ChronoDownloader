"""Shared IIIF helpers for manifest parsing and image downloads.

This module centralizes common logic used by provider connectors when working with
IIIF Presentation (v2/v3) and IIIF Image API endpoints.

Functions exported:
- extract_image_service_bases(manifest): Extract IIIF Image API service URLs from manifest
- image_url_candidates(service_base, info=None): Generate candidate image URLs
- download_one_from_service(service_base, output_folder, filename): Download single image
"""
from __future__ import annotations

import logging
from typing import Any

from .utils import download_file, make_request

logger = logging.getLogger(__name__)

__all__ = [
    "extract_image_service_bases",
    "image_url_candidates",
    "download_one_from_service",
]

# Simple, process-local cache for IIIF info.json documents
_INFO_JSON_CACHE: dict[str, dict[str, Any]] = {}

def _fetch_info_json(service_base: str) -> dict[str, Any] | None:
    """Fetch and cache the IIIF Image API info.json for a service base.

    Args:
        service_base: Base URL of the IIIF Image API service

    Returns:
        Parsed info.json dictionary or None on failure
    """
    b = service_base.rstrip("/")
    cached = _INFO_JSON_CACHE.get(b)
    
    if isinstance(cached, dict) and cached:
        return cached
    
    info_url = f"{b}/info.json"
    info = make_request(info_url)
    
    if isinstance(info, dict) and info:
        _INFO_JSON_CACHE[b] = info
        return info
    
    return None

def extract_image_service_bases(manifest: dict[str, Any]) -> list[str]:
    """Extract IIIF Image API service base URLs from a Presentation manifest.

    Supports both IIIF v2 and v3 structures. Duplicates are removed while
    preserving order.

    Args:
        manifest: IIIF Presentation manifest dictionary

    Returns:
        List of unique IIIF Image API service base URLs
    """
    bases: list[str] = []

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
    unique: list[str] = []
    for b in bases:
        if b not in seen and isinstance(b, str):
            seen.add(b)
            unique.append(b)
    
    return unique

def extract_direct_image_urls(manifest: dict[str, Any]) -> list[str]:
    """Extract direct image URLs from a Presentation manifest.
    
    Some manifests (especially simplified IIIF v3) provide direct image URLs
    without IIIF Image API services. This function extracts those URLs.
    
    Args:
        manifest: IIIF Presentation manifest dictionary
        
    Returns:
        List of direct image URLs
    """
    urls: list[str] = []
    
    # IIIF v2: sequences[0].canvases[].images[0].resource['@id'|'id']
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
                    img_url = res.get("@id") or res.get("id")
                    if img_url and isinstance(img_url, str):
                        urls.append(img_url)
                except Exception:
                    continue
    except Exception:
        pass
    
    # IIIF v3: items[].items[0].items[0].body.id
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
                    
                    img_url = body.get("id")
                    if img_url and isinstance(img_url, str):
                        urls.append(img_url)
                except Exception:
                    continue
    except Exception:
        pass
    
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    
    return unique

def image_url_candidates(
    service_base: str, info: dict[str, Any] | None = None
) -> list[str]:
    """Return a list of likely IIIF Image API URLs for a service base.

    Includes variants compatible with common v2 and v3 servers. If `info` (info.json)
    is provided, generate size-aware candidates using the largest available size.

    Args:
        service_base: Base URL of the IIIF Image API service
        info: Optional info.json dictionary for size-aware candidates

    Returns:
        List of candidate image URLs to try
    """
    b = service_base.rstrip("/")
    candidates: list[str] = [
        f"{b}/full/full/0/default.jpg",
        f"{b}/full/max/0/default.jpg",
        f"{b}/full/pct:100/0/default.jpg",
        f"{b}/full/full/0/native.jpg",
        f"{b}/full/full/0/color.jpg",
    ]
    
    try:
        if isinstance(info, dict) and info:
            # v2 has 'sizes' list of {width,height}; choose the largest width
            sizes = info.get("sizes") or []
            max_w = 0
            
            if isinstance(sizes, list) and sizes:
                for s in sizes:
                    try:
                        w = int(s.get("width") or 0)
                        if w > max_w:
                            max_w = w
                    except Exception:
                        continue
            
            # Prefer explicit maxWidth/maxHeight when provided
            try:
                mw = int(info.get("maxWidth") or 0)
                if mw and mw > max_w:
                    max_w = mw
            except Exception:
                pass
            
            if max_w > 0:
                # Width-only size request (server chooses height): {w},
                candidates[:0] = [
                    f"{b}/full/{max_w},/0/default.jpg",
                    f"{b}/full/{max_w},/0/native.jpg",
                ]
            
            # Also try a couple of fixed large widths if sizes are absent
            if max_w == 0:
                candidates.extend([
                    f"{b}/full/2000,/0/default.jpg",
                    f"{b}/full/1000,/0/default.jpg",
                ])
            
            # If server advertises PNG support, add .png alternatives up front
            fmts = info.get("formats") or []
            if isinstance(fmts, list) and any(str(x).lower() == "png" for x in fmts):
                pngs: list[str] = []
                for u in candidates:
                    if u.endswith(".jpg"):
                        pngs.append(u[:-4] + ".png")
                # Prepend PNGs to try lossless first when available
                candidates = pngs + candidates
    except Exception:
        pass
    
    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    
    return uniq

def download_one_from_service(
    service_base: str, output_folder: str, filename: str
) -> bool:
    """Try downloading a single image for one canvas using default candidates.

    If those fail, fetch info.json to derive size-aware candidates, then retry.

    Args:
        service_base: Base URL of the IIIF Image API service
        output_folder: Target directory for download
        filename: Target filename

    Returns:
        True on successful download, False otherwise
    """
    # First, quick defaults
    for url in image_url_candidates(service_base):
        if download_file(url, output_folder, filename):
            return True
    
    # Fall back to info.json-derived sizes
    try:
        info = _fetch_info_json(service_base)
    except Exception:
        info = None
    
    if info:
        for url in image_url_candidates(service_base, info=info):
            if download_file(url, output_folder, filename):
                return True
    
    return False
