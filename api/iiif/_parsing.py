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
    "extract_page_sources",
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


def _unwrap_v2_choice(resource: Any) -> Any:
    """Descend into a v2 ``oa:Choice`` resource, returning the first usable
    alternative (``default`` preferred, then the first ``item``).

    Non-Choice resources are returned unchanged.
    """
    if isinstance(resource, dict) and resource.get("@type") == "oa:Choice":
        default = resource.get("default")
        if isinstance(default, dict):
            return default
        item = resource.get("item")
        if isinstance(item, list) and item and isinstance(item[0], dict):
            return item[0]
        if isinstance(item, dict):
            return item
    return resource


def _unwrap_v3_choice(body: Any) -> Any:
    """Descend into a v3 ``Choice`` body, returning the first usable ``items``
    entry.

    Non-Choice bodies are returned unchanged.
    """
    if isinstance(body, dict) and body.get("type") == "Choice":
        items = body.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    return body


def _iter_v2_resources(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield the primary image ``resource`` dict for each v2 canvas.

    Walks every ``sequences[n].canvases -> canvas.images[0].resource``, skipping
    any canvas that raises or lacks images. Reading only ``sequences[0]`` lost
    every page of a multi-sequence manifest beyond the first sequence, and a
    dict-valued ``sequences`` (which several v2 producers emit) raised
    ``KeyError`` into the blanket guard and yielded no pages at all.
    """
    resources: list[dict[str, Any]] = []
    try:
        raw_sequences = manifest.get("sequences") or []
        sequences = (
            [raw_sequences] if isinstance(raw_sequences, dict) else raw_sequences
        )
        if not isinstance(sequences, list):
            return resources

        for sequence in sequences:
            if not isinstance(sequence, dict):
                continue
            canvases = sequence.get("canvases", [])
            if not isinstance(canvases, list):
                continue
            for canvas in canvases:
                try:
                    images = canvas.get("images", [])
                    if not images:
                        continue
                    resources.append(_unwrap_v2_choice(images[0].get("resource", {})))
                except Exception:
                    logger.debug("Skipping unparseable v2 canvas", exc_info=True)
                    continue
    except Exception:
        logger.debug("Failed to walk v2 manifest sequences", exc_info=True)
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
                    body = _unwrap_v3_choice(body)
                    bodies.append(body)
                except Exception:
                    logger.debug("Skipping unparseable v3 canvas", exc_info=True)
                    continue
    except Exception:
        logger.debug("Failed to walk v3 manifest items", exc_info=True)
    return bodies


def _first_service_id(service: Any) -> str | None:
    """Return the first usable ``@id``/``id`` among one or more service blocks.

    The IIIF Presentation spec permits ``service`` to be an array, and real
    manifests put non-image services (auth, search) in it. Taking ``service[0]``
    blindly dropped the page whenever the leading entry carried no identifier,
    so every entry is inspected in order.
    """
    entries = service if isinstance(service, list) else [service]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        svc_id = entry.get("@id") or entry.get("id")
        if isinstance(svc_id, str) and svc_id:
            return svc_id
    return None


def _v2_service_base(resource: dict[str, Any]) -> str | None:
    """Return the Image API base for a v2 canvas resource, if it has one."""
    svc_id = _first_service_id(resource.get("service", {}))
    if svc_id:
        return svc_id

    img_id = resource.get("@id") or resource.get("id")
    if isinstance(img_id, str) and "/full/" in img_id:
        return img_id.split("/full/")[0]
    return None


def _v3_service_base(body: dict[str, Any]) -> str | None:
    """Return the Image API base for a v3 annotation body, if it has one."""
    svc_id = _first_service_id(body.get("service") or body.get("services"))
    if svc_id:
        return svc_id

    body_id = body.get("id")
    if isinstance(body_id, str) and "/full/" in body_id:
        return body_id.split("/full/")[0]
    return None


def extract_image_service_bases(manifest: dict[str, Any]) -> list[str]:
    bases: list[str] = []

    for res in _iter_v2_resources(manifest):
        try:
            svc_id = _v2_service_base(res)
            if svc_id:
                bases.append(svc_id)
        except Exception:
            continue

    for body in _iter_v3_bodies(manifest):
        try:
            svc_id = _v3_service_base(body)
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


def extract_page_sources(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """Return one download source per canvas, in canvas order.

    Each entry is ``("service", base_url)`` for a canvas that exposes an Image
    API service, or ``("direct", image_url)`` for one that carries only a
    whole-image URL; the service is preferred when a canvas offers both.
    Canvases offering neither are skipped and duplicate URLs are dropped.

    :func:`extract_image_service_bases` and :func:`extract_direct_image_urls`
    are all-or-nothing at the manifest level: a manifest mixing both kinds of
    canvas silently loses every direct-only page as soon as one canvas
    advertises a service, which understates the expected page count and leaves
    unrecorded gaps. Callers that must account for every page walk this list.
    """
    sources: list[tuple[str, str]] = []

    for res in _iter_v2_resources(manifest):
        try:
            svc_id = _v2_service_base(res)
            if svc_id:
                sources.append(("service", svc_id))
                continue
            img_url = res.get("@id") or res.get("id")
            if img_url and isinstance(img_url, str):
                sources.append(("direct", img_url))
        except Exception:
            logger.debug("Skipping unparseable v2 canvas resource", exc_info=True)
            continue

    for body in _iter_v3_bodies(manifest):
        try:
            svc_id = _v3_service_base(body)
            if svc_id:
                sources.append(("service", svc_id))
                continue
            img_url = body.get("id")
            if img_url and isinstance(img_url, str):
                sources.append(("direct", img_url))
        except Exception:
            logger.debug("Skipping unparseable v3 annotation body", exc_info=True)
            continue

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for kind, url in sources:
        if url not in seen:
            seen.add(url)
            unique.append((kind, url))

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
                # maxWidth is a ceiling, not a capability: the Image API says
                # clients must not expect a request wider than it to be
                # supported. Raising max_w to it built a URL the server is
                # entitled to reject; it can only clamp.
                mw = int(info.get("maxWidth") or 0)
                if mw:
                    max_w = min(max_w, mw) if max_w else mw
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
