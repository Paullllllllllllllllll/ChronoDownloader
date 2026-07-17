"""IIIF manifest parsing and single-image download primitives.

Supports IIIF Presentation v2 and v3 manifests. Extracts image service bases
and direct image URLs; generates candidate Image API URLs; downloads a single
image from a service base using default and info.json-derived candidates.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

from ..core.download import download_file
from ..core.network import make_request

logger = logging.getLogger(__name__)

__all__ = [
    "extract_image_service_bases",
    "extract_direct_image_urls",
    "image_url_candidates",
    "download_one_from_service",
]

# Bounded LRU cache of info.json documents (one per image service). Unbounded
# growth would leak memory across a long multi-work run.
_INFO_JSON_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_INFO_JSON_CACHE_MAX = 512
_INFO_JSON_CACHE_LOCK = threading.Lock()

# Cap on speculative Image-API URL guesses tried per page before consulting
# info.json. Each miss costs a full network retry cycle; the first two guesses
# cover the overwhelming majority of IIIF servers.
_MAX_SPECULATIVE_CANDIDATES = 3


def _fetch_info_json(service_base: str) -> dict[str, Any] | None:
    b = service_base.rstrip("/")
    with _INFO_JSON_CACHE_LOCK:
        cached = _INFO_JSON_CACHE.get(b)
        if isinstance(cached, dict) and cached:
            _INFO_JSON_CACHE.move_to_end(b)
            return cached

    info_url = f"{b}/info.json"
    info = make_request(info_url)

    if isinstance(info, dict) and info:
        with _INFO_JSON_CACHE_LOCK:
            _INFO_JSON_CACHE[b] = info
            _INFO_JSON_CACHE.move_to_end(b)
            while len(_INFO_JSON_CACHE) > _INFO_JSON_CACHE_MAX:
                _INFO_JSON_CACHE.popitem(last=False)
        return info

    return None


def _iter_v2_resources(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield the primary image ``resource`` dict for each v2 canvas.

    Walks ``sequences[0].canvases -> canvas.images[0].resource``, skipping any
    canvas that raises or lacks images. Returns an empty list on any
    structural error, mirroring the defensive traversal in the original
    extractors.
    """
    resources: list[dict[str, Any]] = []
    try:
        sequences = manifest.get("sequences") or []
        if sequences:
            canvases = sequences[0].get("canvases", [])
            for canvas in canvases:
                try:
                    images = canvas.get("images", [])
                    if not images:
                        continue
                    resources.append(images[0].get("resource", {}))
                except Exception:
                    continue
    except Exception:
        pass
    return resources


def _iter_v3_bodies(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield the primary annotation ``body`` dict for each v3 canvas.

    Walks ``items -> canvas.items[0].items[0].body``, normalizing a list-valued
    body to its first element. Skips any canvas that raises or lacks the
    expected nesting, matching the original defensive traversal.
    """
    bodies: list[dict[str, Any]] = []
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
                    bodies.append(body)
                except Exception:
                    continue
    except Exception:
        pass
    return bodies


def extract_image_service_bases(manifest: dict[str, Any]) -> list[str]:
    bases: list[str] = []

    for res in _iter_v2_resources(manifest):
        try:
            # The IIIF Presentation v2 spec permits resource.service to be an
            # array; several real manifests emit this. Normalize to a single
            # dict as the v3 branch below already does, otherwise the whole
            # manifest silently yields zero page images.
            service = res.get("service", {})
            if isinstance(service, list):
                service = service[0] if service else {}
            svc_id = None
            if isinstance(service, dict):
                svc_id = service.get("@id") or service.get("id")

            if not svc_id:
                img_id = res.get("@id") or res.get("id")
                if img_id and "/full/" in img_id:
                    svc_id = img_id.split("/full/")[0]

            if svc_id:
                bases.append(svc_id)
        except Exception:
            continue

    for body in _iter_v3_bodies(manifest):
        try:
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

    seen: set[str] = set()
    unique: list[str] = []
    for b in bases:
        if b not in seen and isinstance(b, str):
            seen.add(b)
            unique.append(b)

    return unique


def extract_direct_image_urls(manifest: dict[str, Any]) -> list[str]:
    urls: list[str] = []

    for res in _iter_v2_resources(manifest):
        try:
            img_url = res.get("@id") or res.get("id")
            if img_url and isinstance(img_url, str):
                urls.append(img_url)
        except Exception:
            continue

    for body in _iter_v3_bodies(manifest):
        try:
            img_url = body.get("id")
            if img_url and isinstance(img_url, str):
                urls.append(img_url)
        except Exception:
            continue

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

            try:
                mw = int(info.get("maxWidth") or 0)
                if mw and mw > max_w:
                    max_w = mw
            except Exception:
                pass

            if max_w > 0:
                candidates[:0] = [
                    f"{b}/full/{max_w},/0/default.jpg",
                    f"{b}/full/{max_w},/0/native.jpg",
                ]

            if max_w == 0:
                candidates.extend(
                    [
                        f"{b}/full/2000,/0/default.jpg",
                        f"{b}/full/1000,/0/default.jpg",
                    ]
                )

            fmts = info.get("formats") or []
            if isinstance(fmts, list) and any(str(x).lower() == "png" for x in fmts):
                pngs: list[str] = []
                for u in candidates:
                    if u.endswith(".jpg"):
                        pngs.append(u[:-4] + ".png")
                candidates = pngs + candidates
    except Exception:
        pass

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
    # Try a bounded number of speculative URL patterns first; each miss costs
    # a full retry cycle, so the long tail is deferred to the info.json pass.
    speculative = image_url_candidates(service_base)[:_MAX_SPECULATIVE_CANDIDATES]
    for url in speculative:
        if download_file(url, output_folder, filename):
            return True

    try:
        info = _fetch_info_json(service_base)
    except Exception:
        info = None

    if info:
        tried = set(speculative)
        for url in image_url_candidates(service_base, info=info):
            if url in tried:
                continue
            if download_file(url, output_folder, filename):
                return True

    return False
