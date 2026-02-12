"""Direct IIIF manifest download support.

This module enables downloading directly from IIIF manifest URLs provided in the
input CSV, bypassing the search phase. It detects IIIF manifest URLs and downloads
content using the existing IIIF infrastructure.

Supported IIIF manifest URL patterns (major digital libraries):
- Internet Archive: https://iiif.archive.org/iiif/{id}/manifest.json
- BnF Gallica: https://gallica.bnf.fr/iiif/ark:/{id}/manifest.json
- MDZ: https://api.digitale-sammlungen.de/iiif/presentation/v2/{id}/manifest
- HathiTrust: https://babel.hathitrust.org/cgi/imgsrv/manifest/{id}
- Library of Congress: https://www.loc.gov/item/{id}/manifest.json
- Wellcome Collection: https://iiif.wellcomecollection.org/presentation/{id}
- British Library: https://api.bl.uk/metadata/iiif/{id}/manifest.json
- Europeana: various IIIF providers
- e-rara: https://www.e-rara.ch/i3f/v20/{id}/manifest
- SLUB Dresden: https://digital.slub-dresden.de/data/kitodo/{id}/manifest.json
- Many others following IIIF Presentation API conventions
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

from .core.budget import budget_exhausted
from .core.config import get_config, get_max_pages, prefer_pdf_over_images
from .core.network import make_request
from .iiif import extract_image_service_bases, download_one_from_service
from .utils import download_iiif_renderings, save_json

logger = logging.getLogger(__name__)

# IIIF manifest URL patterns for major digital libraries
IIIF_MANIFEST_PATTERNS = [
    # Generic manifest.json suffix
    r"manifest\.json$",
    r"/manifest$",
    # Internet Archive
    r"iiif\.archive\.org/iiif/.+/manifest",
    r"iiif\.archivelab\.org/iiif/.+/manifest",
    # BnF Gallica
    r"gallica\.bnf\.fr/iiif/ark:",
    # MDZ (Bavarian State Library)
    r"api\.digitale-sammlungen\.de/iiif/presentation",
    r"digitale-sammlungen\.de/.+/manifest",
    # HathiTrust
    r"babel\.hathitrust\.org/cgi/imgsrv/manifest",
    # Library of Congress
    r"loc\.gov/.+/manifest",
    # Wellcome Collection
    r"iiif\.wellcomecollection\.org/presentation",
    # British Library
    r"api\.bl\.uk/metadata/iiif",
    # e-rara (Swiss e-manuscripts)
    r"e-rara\.ch/i3f/v\d+/.+/manifest",
    # SLUB Dresden
    r"digital\.slub-dresden\.de/.+/manifest",
    # SBB Berlin
    r"content\.staatsbibliothek-berlin\.de/.+/manifest",
    # Generic IIIF presentation patterns
    r"/iiif/\d+/manifest",
    r"/presentation/v[23]/.+/manifest",
    r"/iiif/presentation/",
]

# Compiled regex patterns for efficient matching
_IIIF_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in IIIF_MANIFEST_PATTERNS]

def is_iiif_manifest_url(url: str) -> bool:
    """Check if a URL appears to be a IIIF manifest URL.
    
    Args:
        url: URL to check
        
    Returns:
        True if the URL matches known IIIF manifest patterns
    """
    if not url or not isinstance(url, str):
        return False
    
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    
    for pattern in _IIIF_PATTERNS_COMPILED:
        if pattern.search(url):
            return True
    
    return False

def detect_provider_from_url(url: str) -> tuple[str, str]:
    """Detect the provider from a IIIF manifest URL.
    
    Args:
        url: IIIF manifest URL
        
    Returns:
        Tuple of (provider_key, provider_name)
    """
    url_lower = url.lower()
    
    if "archive.org" in url_lower or "archivelab.org" in url_lower:
        return "internet_archive", "Internet Archive"
    if "gallica.bnf.fr" in url_lower:
        return "bnf_gallica", "BnF Gallica"
    if "digitale-sammlungen.de" in url_lower:
        return "mdz", "MDZ"
    if "hathitrust.org" in url_lower:
        return "hathitrust", "HathiTrust"
    if "loc.gov" in url_lower:
        return "loc", "Library of Congress"
    if "wellcomecollection.org" in url_lower:
        return "wellcome", "Wellcome Collection"
    if "bl.uk" in url_lower:
        return "british_library", "British Library"
    if "e-rara.ch" in url_lower:
        return "e_rara", "e-rara"
    if "slub-dresden.de" in url_lower:
        return "slub", "SLUB Dresden"
    if "staatsbibliothek-berlin.de" in url_lower:
        return "sbb_digital", "SBB Digital Collections"
    if "europeana.eu" in url_lower:
        return "europeana", "Europeana"
    if "polona.pl" in url_lower:
        return "polona", "Polona"
    if "ddb.de" in url_lower or "deutsche-digitale-bibliothek" in url_lower:
        return "ddb", "DDB"
    if "bne.es" in url_lower:
        return "bne", "BNE"
    
    return "direct_iiif", "Direct IIIF"

def extract_item_id_from_url(url: str) -> str:
    """Extract a usable item identifier from a IIIF manifest URL.
    
    Args:
        url: IIIF manifest URL
        
    Returns:
        Extracted item identifier or hash-based fallback
    """
    # Remove protocol and common suffixes
    cleaned = url.split("://", 1)[-1]
    cleaned = re.sub(r"/manifest\.json$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"/manifest$", "", cleaned, flags=re.IGNORECASE)
    
    # Try to extract identifier from common patterns
    patterns = [
        r"/iiif/([^/]+)/manifest",
        r"/view/([^/]+)",
        r"/details/([^/]+)",
        r"/item/([^/]+)",
        r"/ark:/[^/]+/([^/]+)",
        r"/presentation/v\d+/([^/]+)",
        r"/i3f/v\d+/([^/]+)",
        r"([a-zA-Z0-9_-]{5,})$",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            return match.group(1)
    
    # Fallback: create a short hash of the URL
    return hashlib.md5(url.encode()).hexdigest()[:12]

def _extract_localized_str(obj: Any) -> str:
    """Extract a plain string from a IIIF v2/v3 localized value.

    Handles plain strings, v3 language maps (``{"en": ["..."]}``),
    and v2 lists (``["..."]``).
    """
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for _lang, vals in obj.items():
            if isinstance(vals, list) and vals:
                return str(vals[0])
    if isinstance(obj, list) and obj:
        return str(obj[0])
    return str(obj) if obj else ""

def extract_manifest_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    """Extract label, attribution, and other metadata from a IIIF manifest.

    Handles both IIIF Presentation v2 and v3 structures.

    Args:
        manifest: Parsed IIIF manifest dictionary

    Returns:
        Dictionary with keys: label, attribution, metadata (dict of key-value pairs)
    """
    result: dict[str, Any] = {"label": None, "attribution": None, "metadata": {}}

    # --- label ---
    raw_label = manifest.get("label")
    if isinstance(raw_label, str):
        result["label"] = raw_label
    elif isinstance(raw_label, dict):
        # v3: {"en": ["..."], "fr": ["..."]}
        for _lang, vals in raw_label.items():
            if isinstance(vals, list) and vals:
                result["label"] = str(vals[0])
                break
    elif isinstance(raw_label, list) and raw_label:
        # v2 fallback: ["..."]
        result["label"] = str(raw_label[0]) if raw_label else None

    # --- attribution ---
    raw_attr = manifest.get("attribution") or manifest.get("requiredStatement")
    if isinstance(raw_attr, str):
        result["attribution"] = raw_attr
    elif isinstance(raw_attr, dict):
        val = raw_attr.get("value")
        if isinstance(val, str):
            result["attribution"] = val
        elif isinstance(val, dict):
            for _lang, vals in val.items():
                if isinstance(vals, list) and vals:
                    result["attribution"] = str(vals[0])
                    break

    # --- metadata key-value pairs ---
    raw_meta = manifest.get("metadata") or []
    if isinstance(raw_meta, list):
        for entry in raw_meta:
            if not isinstance(entry, dict):
                continue
            k = _extract_localized_str(entry.get("label", ""))
            v = _extract_localized_str(entry.get("value", ""))
            if k:
                result["metadata"][k] = v

    return result

def preview_manifest(manifest_url: str) -> dict[str, Any] | None:
    """Fetch a IIIF manifest and return structured info without downloading.

    Args:
        manifest_url: URL of the IIIF manifest

    Returns:
        Dictionary with manifest preview information, or None on failure.
        Keys: url, provider, provider_key, item_id, label, page_count,
        has_renderings, rendering_formats, metadata.
    """
    manifest = make_request(manifest_url)
    if not isinstance(manifest, dict):
        logger.warning("Failed to fetch IIIF manifest from %s", manifest_url)
        return None

    provider_key, provider_name = detect_provider_from_url(manifest_url)
    item_id = extract_item_id_from_url(manifest_url)
    meta = extract_manifest_metadata(manifest)
    service_bases = extract_image_service_bases(manifest)

    # Check for renderings
    rendering_formats: list[str] = []
    renderings = manifest.get("rendering") or []
    if isinstance(renderings, dict):
        renderings = [renderings]
    if isinstance(renderings, list):
        for r in renderings:
            fmt = ""
            if isinstance(r, dict):
                fmt = r.get("format") or r.get("type") or ""
            if fmt:
                rendering_formats.append(str(fmt))

    return {
        "url": manifest_url,
        "provider": provider_name,
        "provider_key": provider_key,
        "item_id": item_id,
        "label": meta.get("label"),
        "page_count": len(service_bases),
        "has_renderings": len(rendering_formats) > 0,
        "rendering_formats": rendering_formats,
        "metadata": meta.get("metadata", {}),
    }

def download_from_iiif_manifest(
    manifest_url: str,
    output_folder: str,
    title: str | None = None,
    entry_id: str | None = None,
    file_stem: str | None = None,
) -> dict[str, Any]:
    """Download content from a direct IIIF manifest URL.

    Args:
        manifest_url: URL of the IIIF manifest
        output_folder: Target directory for downloads
        title: Optional work title for logging/metadata
        entry_id: Optional entry ID for tracking
        file_stem: Optional naming stem for output files. When provided, files
            are named ``{file_stem}_p{page:05d}.jpg`` instead of the default
            ``{provider_key}_{item_id}_p{page:05d}.jpg``.

    Returns:
        Dictionary with download result:
        - 'success': bool
        - 'provider': str
        - 'provider_key': str
        - 'item_url': str (the manifest URL)
        - 'item_id': str
        - 'error': str | None
    """
    provider_key, provider_name = detect_provider_from_url(manifest_url)
    item_id = extract_item_id_from_url(manifest_url)
    if file_stem:
        prefix = file_stem
    else:
        template = get_naming_template()
        prefix = resolve_file_stem(
            template,
            entry_id=entry_id,
            name=title,
            provider_key=provider_key,
            item_id=item_id,
        )

    log_title = title or item_id
    logger.info(
        "Direct IIIF download for '%s' from %s: %s",
        log_title,
        provider_name,
        manifest_url,
    )

    result: dict[str, Any] = {
        "success": False,
        "provider": provider_name,
        "provider_key": provider_key,
        "item_url": manifest_url,
        "item_id": item_id,
        "error": None,
    }

    # Check configuration
    config = get_config()
    direct_iiif_cfg = config.get("direct_iiif", {})
    if not direct_iiif_cfg.get("enabled", True):
        result["error"] = "Direct IIIF downloads disabled in config"
        logger.warning(result["error"])
        return result

    # Create output folder
    os.makedirs(output_folder, exist_ok=True)

    # Fetch manifest
    logger.info("Fetching IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)

    if not isinstance(manifest, dict):
        result["error"] = f"Failed to fetch IIIF manifest from {manifest_url}"
        logger.warning(result["error"])
        return result

    # Save manifest
    save_json(manifest, output_folder, f"{prefix}_manifest")

    any_downloaded = False

    # Try manifest-level renderings (PDF/EPUB) first
    try:
        renders = download_iiif_renderings(
            manifest,
            output_folder,
            filename_prefix=f"{prefix}_"
        )
        if renders > 0:
            any_downloaded = True
            logger.info("Downloaded %d rendering(s) from manifest", renders)
            if prefer_pdf_over_images():
                logger.info(
                    "Skipping image downloads per config (prefer PDF over images)."
                )
                result["success"] = True
                return result
    except Exception as e:
        logger.exception("Error downloading manifest renderings: %s", e)

    # Download page images via IIIF Image API
    service_bases = extract_image_service_bases(manifest)

    if not service_bases:
        logger.info("No IIIF image services found in manifest")
        if any_downloaded:
            result["success"] = True
        else:
            result["error"] = "No downloadable content found in manifest"
        return result

    # Apply page limits based on provider
    max_pages = get_max_pages(provider_key)
    total = len(service_bases)
    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases

    logger.info(
        "Downloading %d/%d page images for %s",
        len(to_download),
        total,
        item_id,
    )

    for idx, svc in enumerate(to_download, start=1):
        if budget_exhausted():
            logger.warning(
                "Download budget exhausted; stopping at %d/%d pages",
                idx - 1,
                len(to_download),
            )
            break

        try:
            fname = f"{prefix}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                any_downloaded = True
            else:
                logger.warning("Failed to download page %d from %s", idx, svc)
        except Exception as e:
            logger.exception("Error downloading page %d: %s", idx, e)

    result["success"] = any_downloaded
    if not any_downloaded:
        result["error"] = "No images could be downloaded"

    return result

def get_naming_template() -> str:
    """Get IIIF file naming template from config.

    Template variables:
    - ``{entry_id}`` - entry identifier
    - ``{name}`` - user-supplied or manifest-derived name
    - ``{provider}`` - auto-detected provider key
    - ``{item_id}`` - identifier extracted from manifest URL

    The page suffix (``_p{page:05d}``) is always appended by the download
    function and should **not** appear in the template.

    Returns:
        Naming template string.
    """
    config = get_config()
    direct_iiif_cfg = config.get("direct_iiif", {})
    return direct_iiif_cfg.get("naming_template", "{provider}_{item_id}")

def resolve_file_stem(
    template: str,
    entry_id: str | None = None,
    name: str | None = None,
    provider_key: str | None = None,
    item_id: str | None = None,
) -> str:
    """Resolve a naming template to a file stem (without page number).

    Unknown placeholders are left as-is so the caller can detect problems.
    Empty or whitespace-only results fall back to ``{provider_key}_{item_id}``.

    Args:
        template: Naming template string (e.g. ``{entry_id}_{name}``)
        entry_id: Entry identifier
        name: User-supplied or manifest-derived name
        provider_key: Auto-detected provider key
        item_id: Identifier extracted from manifest URL

    Returns:
        Resolved file stem string.
    """
    mapping = {
        "entry_id": entry_id or "",
        "name": name or "",
        "provider": provider_key or "direct_iiif",
        "item_id": item_id or "unknown",
    }
    try:
        stem = template.format_map(mapping)
    except (KeyError, ValueError):
        stem = f"{mapping['provider']}_{mapping['item_id']}"

    stem = stem.strip().strip("_")
    if not stem:
        stem = f"{mapping['provider']}_{mapping['item_id']}"
    return stem

def is_direct_download_enabled() -> bool:
    """Check if direct IIIF downloads are enabled in config.
    
    Returns:
        True if enabled
    """
    config = get_config()
    direct_iiif_cfg = config.get("direct_iiif", {})
    return direct_iiif_cfg.get("enabled", True)

def get_direct_link_column() -> str:
    """Get the column name used for direct links.
    
    Returns:
        Column name from config or default 'direct_link'
    """
    config = get_config()
    direct_iiif_cfg = config.get("direct_iiif", {})
    return direct_iiif_cfg.get("link_column", "direct_link")

__all__ = [
    "is_iiif_manifest_url",
    "detect_provider_from_url",
    "extract_item_id_from_url",
    "extract_manifest_metadata",
    "preview_manifest",
    "download_from_iiif_manifest",
    "is_direct_download_enabled",
    "get_direct_link_column",
    "get_naming_template",
    "resolve_file_stem",
]
