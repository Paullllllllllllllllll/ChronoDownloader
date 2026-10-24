"""IIIF Image API request URLs for a whole page at full or capped size.

Ported from ChronoDownloader v1.30.3 ``api/iiif/_parsing.py``
(``image_url_candidates``) on 19.10.2026. Changed: an optional size cap in
pixels (a provider's image size cap, or the reduced size of a page sample),
and no info.json cache: callers fetch ``info.json`` themselves.

Without a cap the candidates are the v1.30.3 list: ``full/full``,
``full/max``, ``pct:100``, ``native`` and ``color``; with an ``info.json``
the largest advertised width (clamped by ``maxWidth``, which is a ceiling)
comes first, else fixed widths of 2,000 and 1,000 px are appended. With a cap
no candidate asks for more than the cap in either dimension: without
``info.json`` only the confined size ``!cap,cap``; with it the whole image
when it fits, else the width that scales its longer side to the cap, else
the largest advertised size that fits. A server that offers PNG gets the PNG
variant of every candidate first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "SPECULATIVE_CANDIDATES",
    "image_url_candidates",
    "info_url",
]

SPECULATIVE_CANDIDATES = 3
"""Candidates tried before ``info.json`` is fetched; each miss costs a request."""

_FALLBACK_WIDTHS = (2000, 1000)


def info_url(service_base: str) -> str:
    """Return the ``info.json`` URL of an Image API service."""
    return f"{service_base.rstrip('/')}/info.json"


def _positive_int(value: Any) -> int:
    """Return *value* as a positive integer, or 0 when it is not one."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and value.is_integer():
        return max(int(value), 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _sizes(info: Mapping[str, Any]) -> list[tuple[int, int]]:
    """Return the ``sizes`` entries as (width, height); height 0 when absent."""
    sizes = info.get("sizes") or []
    if not isinstance(sizes, list):
        return []
    return [
        (_positive_int(entry.get("width")), _positive_int(entry.get("height")))
        for entry in sizes
        if isinstance(entry, Mapping)
    ]


def _offers_png(info: Mapping[str, Any]) -> bool:
    formats = info.get("formats") or []
    return isinstance(formats, list) and any(str(x).lower() == "png" for x in formats)


def _uncapped(base: str, info: Mapping[str, Any] | None) -> list[str]:
    candidates = [
        f"{base}/full/full/0/default.jpg",
        f"{base}/full/max/0/default.jpg",
        f"{base}/full/pct:100/0/default.jpg",
        f"{base}/full/full/0/native.jpg",
        f"{base}/full/full/0/color.jpg",
    ]
    if not info:
        return candidates
    max_w = max((width for width, _height in _sizes(info)), default=0)
    ceiling = _positive_int(info.get("maxWidth"))
    if ceiling:
        max_w = min(max_w, ceiling) if max_w else ceiling
    if max_w > 0:
        candidates[:0] = [
            f"{base}/full/{max_w},/0/default.jpg",
            f"{base}/full/{max_w},/0/native.jpg",
        ]
    else:
        candidates.extend(f"{base}/full/{w},/0/default.jpg" for w in _FALLBACK_WIDTHS)
    return candidates


def _capped(base: str, info: Mapping[str, Any] | None, cap: int) -> list[str]:
    confined = f"{base}/full/!{cap},{cap}/0/default.jpg"
    if not info:
        return [confined]
    width = _positive_int(info.get("width"))
    height = _positive_int(info.get("height"))
    candidates: list[str] = []
    if width and height:
        longer = max(width, height)
        if longer <= cap:
            candidates += [
                f"{base}/full/max/0/default.jpg",
                f"{base}/full/full/0/default.jpg",
            ]
        else:
            scaled = max(1, width * cap // longer)
            ceiling = _positive_int(info.get("maxWidth"))
            if ceiling:
                scaled = min(scaled, ceiling)
            candidates.append(f"{base}/full/{scaled},/0/default.jpg")
    else:
        fitting = [w for w, h in _sizes(info) if 0 < w <= cap and h <= cap]
        if fitting:
            candidates.append(f"{base}/full/{max(fitting)},/0/default.jpg")
    candidates.append(confined)
    return candidates


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def image_url_candidates(
    service_base: str,
    info: Mapping[str, Any] | None = None,
    *,
    max_px: int | None = None,
) -> list[str]:
    """Return the image URLs to try for a service, preferred first.

    *info* is the service's ``info.json``; *max_px* caps both dimensions.

    Raises:
        ValueError: *max_px* is below 1.
    """
    if max_px is not None and max_px < 1:
        raise ValueError(f"max_px must be at least 1, got {max_px}")
    base = service_base.rstrip("/")
    if max_px is None:
        candidates = _uncapped(base, info)
    else:
        candidates = _capped(base, info, max_px)
    if info and _offers_png(info):
        pngs = [url[:-4] + ".png" for url in candidates if url.endswith(".jpg")]
        candidates = pngs + candidates
    return _unique(candidates)
