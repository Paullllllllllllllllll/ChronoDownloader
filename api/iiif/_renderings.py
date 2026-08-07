"""IIIF manifest rendering download helper.

Downloads alternate-format files referenced in a IIIF manifest's top-level
`rendering` array (e.g. application/pdf, application/epub+zip). Controlled
by `config.download.download_manifest_renderings`,
`config.download.rendering_mime_whitelist`, and
`config.download.max_renderings_per_manifest`.

A rendering is a whole-document derivative, so it lands in the work's
`objects/` directory under the extension of its actual payload (see
`api.core.download`), and its source URL and resolved media type are recorded
under `renderings` in the work's `work.json`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ..core.atomic import atomic_write_json
from ..core.config import DEFAULT_MAX_RENDERINGS_PER_MANIFEST, get_download_config
from ..core.download import download_file, media_type_for_extension

logger = logging.getLogger(__name__)

__all__ = ["download_iiif_renderings", "select_renderings"]

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


def _whitelist_rank(url: str, fmt: str, whitelist: list[str]) -> int:
    """Rank a rendering by the position of its format in the whitelist.

    The whitelist is an ordered preference, not a set: with the shipped
    ``["application/pdf", "application/epub+zip"]`` and a limit of one file, a
    manifest listing the EPUB before the whole-work PDF used to yield the EPUB
    purely because candidates were tried in document order.

    Args:
        url: Rendering URL
        fmt: Declared format/type, lowercased (may be empty)
        whitelist: Lowercased allowed MIME fragments, most preferred first

    Returns:
        The index of the matching whitelist entry, or ``len(whitelist)`` when
        nothing matches (such renderings sort last).
    """
    if "/" in fmt:
        for idx, w in enumerate(whitelist):
            if w in fmt:
                return idx
        return len(whitelist)

    # No usable MIME type: fall back to the same suffixes _is_whitelisted
    # accepts, mapped onto whichever whitelist entry names that format.
    lowered = url.lower()
    for ext, token in ((".pdf", "pdf"), (".epub", "epub")):
        if lowered.endswith(ext):
            for idx, w in enumerate(whitelist):
                if token in w:
                    return idx
            break

    return len(whitelist)


def _collect_renderings(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ``rendering`` entries of a manifest or sequence as dicts."""
    items: list[dict[str, Any]] = []
    r = obj.get("rendering")
    if isinstance(r, list):
        for it in r:
            if isinstance(it, dict):
                items.append(it)
    elif isinstance(r, dict):
        items.append(r)
    return items


def collect_all_renderings(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return manifest-level and IIIF v2 sequence-level ``rendering`` entries.

    Wellcome and the DFG viewer hang the whole-work PDF off the sequence rather
    than the manifest, so a manifest-only scan misses it. Sequence entries are
    appended after the manifest-level ones; callers dedup on URL.
    """
    candidates: list[dict[str, Any]] = _collect_renderings(manifest)

    sequences = manifest.get("sequences")
    if isinstance(sequences, list):
        for seq in sequences:
            if isinstance(seq, dict):
                candidates.extend(_collect_renderings(seq))

    return candidates


def _rendering_format(value: Any) -> str:
    """Normalize a rendering's ``format``/``type`` to a lowercase string.

    Some manifests give ``format`` as a list. Calling ``.lower()`` on it raised
    ``AttributeError``, and because the loop had no per-candidate guard that
    single bad entry aborted every rendering of the manifest.
    """
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str)), "")
    if not isinstance(value, str):
        return ""
    return value.lower()


def _configured_whitelist(dl_cfg: dict[str, Any]) -> list[str]:
    """Return the lowercased, order-preserving rendering MIME whitelist."""
    return [
        str(m).lower()
        for m in (
            dl_cfg.get("rendering_mime_whitelist")
            or ["application/pdf", "application/epub+zip"]
        )
        if m
    ]


def select_renderings(
    manifest: dict[str, Any], whitelist: list[str] | None = None
) -> list[dict[str, Any]]:
    """Return the renderings a download would attempt, in preference order.

    Shared by :func:`download_iiif_renderings` and the manifest preview so the
    two cannot disagree: a preview that merely reported every entry declaring
    a format announced renderings for a ``text/html``-only manifest that
    downloads nothing, and hid the format-less bare ``.pdf`` rendering that
    the download path does fetch via its URL-suffix fallback.

    Args:
        manifest: IIIF manifest dictionary
        whitelist: Lowercased allowed MIME fragments, most preferred first;
            read from the download config when omitted

    Returns:
        Deduplicated ``{"url", "format", "label"}`` dicts, whitelist order
        first and manifest order within a rank.
    """
    if whitelist is None:
        whitelist = _configured_whitelist(get_download_config())

    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for it in collect_all_renderings(manifest):
        # One malformed entry must not cost the manifest its other renderings.
        try:
            url = it.get("@id") or it.get("id")
            fmt = _rendering_format(it.get("format") or it.get("type"))
            if not url or not isinstance(url, str):
                continue
            if whitelist and not _is_whitelisted(url, fmt, whitelist):
                continue
            if url in seen:
                continue
            seen.add(url)
            selected.append({"url": url, "format": fmt, "label": it.get("label")})
        except Exception:
            logger.debug("Skipping unparseable rendering entry", exc_info=True)
            continue

    # The whitelist is ordered by preference, so honor it before document
    # order: a manifest listing the EPUB first and the whole-work PDF later
    # otherwise handed back the EPUB under the default limit of one file. The
    # sort is stable, so equally ranked renderings keep manifest order.
    selected.sort(key=lambda r: _whitelist_rank(r["url"], r["format"], whitelist))
    return selected


def _provenance_record(
    rendering: dict[str, Any], saved_path: str, folder_path: str
) -> dict[str, Any]:
    """Describe one downloaded rendering for the work's metadata."""
    ext = Path(saved_path).suffix.lower()
    try:
        relative = os.path.relpath(saved_path, folder_path).replace(os.sep, "/")
    except ValueError:
        relative = os.path.basename(saved_path)
    return {
        "url": rendering["url"],
        "declared_format": rendering.get("format") or None,
        "label": rendering.get("label"),
        "saved_as": relative,
        "resolved_media_type": media_type_for_extension(ext),
    }


def _record_rendering_provenance(
    folder_path: str, records: list[dict[str, Any]]
) -> None:
    """Merge rendering provenance into the work's ``work.json``.

    The saved filename follows the payload type rather than the URL, so a
    rendering fetched from a CGI endpoint no longer carries its origin in its
    name. Recording the source URL, the manifest-declared format, the resolved
    media type and the saved path keeps that provenance. Existing entries are
    upserted by URL, so a re-run neither duplicates nor loses them.

    Best-effort, and only for a work directory that exists: a metadata write
    must never fail a download that already succeeded.
    """
    if not folder_path or not os.path.isdir(folder_path):
        return

    work_json_path = os.path.join(folder_path, "work.json")
    try:
        meta: dict[str, Any] = {}
        if os.path.exists(work_json_path):
            with open(work_json_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                meta = loaded

        existing = meta.get("renderings")
        merged: dict[str, dict[str, Any]] = {}
        if isinstance(existing, list):
            for entry in existing:
                if isinstance(entry, dict) and entry.get("url"):
                    merged[str(entry["url"])] = entry
        for record in records:
            merged[str(record["url"])] = record

        meta["renderings"] = list(merged.values())
        atomic_write_json(work_json_path, meta)
    except Exception:
        logger.warning(
            "Failed to record rendering provenance in %s",
            work_json_path,
            exc_info=True,
        )


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

    try:
        limit = int(
            dl_cfg.get(
                "max_renderings_per_manifest", DEFAULT_MAX_RENDERINGS_PER_MANIFEST
            )
            or DEFAULT_MAX_RENDERINGS_PER_MANIFEST
        )
    except Exception:
        limit = DEFAULT_MAX_RENDERINGS_PER_MANIFEST

    selected = select_renderings(manifest, _configured_whitelist(dl_cfg))

    # The limit counts files obtained, not attempts made. Truncating the
    # candidate list first meant that when the chosen renderings were dead the
    # working ones behind them were never tried, and the caller fell back to
    # downloading every page image instead of the one available PDF. The list
    # is manifest-supplied, so cap the attempts as well.
    count = 0
    records: list[dict[str, Any]] = []
    for r in selected[:_MAX_RENDERING_ATTEMPTS]:
        if count >= limit:
            break
        saved = download_file(r["url"], folder_path, f"rendering_{count + 1:02d}")
        if saved:
            count += 1
            records.append(_provenance_record(r, saved, folder_path))

    if records:
        _record_rendering_provenance(folder_path, records)
    return count
