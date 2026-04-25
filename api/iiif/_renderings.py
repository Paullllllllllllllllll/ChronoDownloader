"""IIIF manifest rendering download helper.

Downloads alternate-format files referenced in a IIIF manifest's top-level
`rendering` array (e.g. application/pdf, application/epub+zip). Controlled
by `config.download.download_manifest_renderings`,
`config.download.rendering_mime_whitelist`, and
`config.download.max_renderings_per_manifest`.
"""
from __future__ import annotations

import logging
from typing import Any

from ..core.config import get_download_config
from ..core.download import download_file

logger = logging.getLogger(__name__)

__all__ = ["download_iiif_renderings"]


def download_iiif_renderings(
    manifest: dict[str, Any], folder_path: str, filename_prefix: str = ""
) -> int:
    """Download files referenced in IIIF manifest-level 'rendering' entries.

    Args:
        manifest: IIIF manifest dictionary
        folder_path: Target directory for downloads
        filename_prefix: Prefix for downloaded filenames (currently unused;
            download_file builds standardized names from context)

    Returns:
        Number of files successfully downloaded
    """
    dl_cfg = get_download_config()

    if not dl_cfg.get("download_manifest_renderings", True):
        return 0

    whitelist: list[str] = [
        str(m).lower()
        for m in (
            dl_cfg.get("rendering_mime_whitelist")
            or ["application/pdf", "application/epub+zip"]
        )
        if m
    ]

    try:
        limit = int(dl_cfg.get("max_renderings_per_manifest", 1) or 1)
    except Exception:
        limit = 1

    def _collect_renderings(obj: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        r = obj.get("rendering")
        if isinstance(r, list):
            for it in r:
                if isinstance(it, dict):
                    items.append(it)
        elif isinstance(r, dict):
            items.append(r)
        return items

    candidates: list[dict[str, Any]] = _collect_renderings(manifest)

    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for it in candidates:
        url = it.get("@id") or it.get("id")
        fmt = (it.get("format") or it.get("type") or "").lower()
        if not url or not isinstance(url, str):
            continue
        if whitelist and all(w not in fmt for w in whitelist):
            if not any(url.lower().endswith(ext) for ext in (".pdf", ".epub")):
                continue
        if url in seen:
            continue
        seen.add(url)
        selected.append({"url": url, "format": fmt, "label": it.get("label")})
        if len(selected) >= limit:
            break

    count = 0
    for idx, r in enumerate(selected, start=1):
        url = r["url"]
        if download_file(url, folder_path, f"rendering_{idx:02d}"):
            count += 1
    return count
