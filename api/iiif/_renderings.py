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

from ..core.config import DEFAULT_MAX_RENDERINGS_PER_MANIFEST, get_download_config
from ..core.download import download_file

logger = logging.getLogger(__name__)

__all__ = ["download_iiif_renderings"]

# Upper bound on rendering downloads attempted for one manifest, so a manifest
# advertising hundreds of dead renderings cannot turn a limit of one file into
# hundreds of requests.
_MAX_RENDERING_ATTEMPTS = 10


def _is_whitelisted(url: str, fmt: str, whitelist: list[str]) -> bool:
    """Decide whether a rendering passes the configured MIME whitelist.

    The URL suffix is only a fallback for a rendering that declares no MIME
    type: applying it unconditionally let a manifest bypass the whitelist
    entirely, so narrowing the list to EPUB still downloaded every ``.pdf``
    rendering. ``fmt`` may also hold a IIIF v3 resource ``type`` ("Text",
    "Dataset"), which carries no slash and settles nothing, so it keeps the
    suffix fallback.

    Args:
        url: Rendering URL
        fmt: Declared format/type, lowercased (may be empty)
        whitelist: Lowercased allowed MIME fragments

    Returns:
        True if the rendering may be downloaded
    """
    if "/" in fmt:
        return any(w in fmt for w in whitelist)
    return any(url.lower().endswith(ext) for ext in (".pdf", ".epub"))


def download_iiif_renderings(manifest: dict[str, Any], folder_path: str) -> int:
    """Download files referenced in IIIF manifest-level 'rendering' entries.

    Args:
        manifest: IIIF manifest dictionary
        folder_path: Target directory for downloads

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
        limit = int(
            dl_cfg.get(
                "max_renderings_per_manifest", DEFAULT_MAX_RENDERINGS_PER_MANIFEST
            )
            or DEFAULT_MAX_RENDERINGS_PER_MANIFEST
        )
    except Exception:
        limit = DEFAULT_MAX_RENDERINGS_PER_MANIFEST

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

    # IIIF Presentation v2 also hangs the whole-work PDF off the sequence
    # (Wellcome and the DFG viewer both do), so a manifest-only scan missed it
    # and fell back to page images. Appended after the manifest-level entries;
    # the dedup below absorbs the overlap when both carry the same URL.
    sequences = manifest.get("sequences")
    if isinstance(sequences, list):
        for seq in sequences:
            if isinstance(seq, dict):
                candidates.extend(_collect_renderings(seq))

    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for it in candidates:
        url = it.get("@id") or it.get("id")
        fmt = (it.get("format") or it.get("type") or "").lower()
        if not url or not isinstance(url, str):
            continue
        if whitelist and not _is_whitelisted(url, fmt, whitelist):
            continue
        if url in seen:
            continue
        seen.add(url)
        selected.append({"url": url, "format": fmt, "label": it.get("label")})

    # The limit counts files obtained, not attempts made. Truncating the
    # candidate list first meant that when the chosen renderings were dead the
    # working ones behind them were never tried, and the caller fell back to
    # downloading every page image instead of the one available PDF. The list
    # is manifest-supplied, so cap the attempts as well.
    count = 0
    for r in selected[:_MAX_RENDERING_ATTEMPTS]:
        if count >= limit:
            break
        if download_file(r["url"], folder_path, f"rendering_{count + 1:02d}"):
            count += 1
    return count
