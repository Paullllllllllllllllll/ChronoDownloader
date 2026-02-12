"""Shared download helpers for provider connectors.

This module extracts common download patterns used across multiple providers
to reduce code duplication and improve maintainability.
"""
from __future__ import annotations

import logging

from .iiif import download_one_from_service, extract_image_service_bases
from .core.budget import budget_exhausted
from .core.config import get_max_pages, prefer_pdf_over_images
from .core.network import make_request
from .utils import download_file, download_iiif_renderings, save_json

logger = logging.getLogger(__name__)

__all__ = [
    "download_iiif_manifest_and_images",
    "download_page_images",
    "try_pdf_first_then_images",
]

def download_page_images(
    service_bases: list[str],
    output_folder: str,
    provider_key: str,
    item_id: str,
    *,
    max_pages: int | None = None,
) -> bool:
    """Download page images from IIIF Image API service bases.

    Applies page limits and budget checks, logging progress and errors.
    This is the shared loop extracted from multiple provider connectors.

    Args:
        service_bases: List of IIIF Image API service base URLs.
        output_folder: Target directory for downloads.
        provider_key: Provider identifier for filenames and logging.
        item_id: Item identifier for filenames and logging.
        max_pages: Override page limit. When ``None`` the limit is read
            from config via ``get_max_pages(provider_key)``.

    Returns:
        ``True`` if at least one image was downloaded.
    """
    if not service_bases:
        logger.info(
            "No IIIF image services found for %s item %s",
            provider_key,
            item_id,
        )
        return False

    if max_pages is None:
        max_pages = get_max_pages(provider_key)
    total = len(service_bases)
    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases

    logger.info(
        "%s: downloading %d/%d page images for %s",
        provider_key.upper(),
        len(to_download),
        total,
        item_id,
    )

    any_downloaded = False
    for idx, svc in enumerate(to_download, start=1):
        if budget_exhausted():
            logger.warning(
                "Download budget exhausted; stopping %s downloads at %d/%d pages for %s",
                provider_key,
                idx - 1,
                len(to_download),
                item_id,
            )
            break

        try:
            fname = f"{provider_key}_{item_id}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                any_downloaded = True
            else:
                if budget_exhausted():
                    logger.warning(
                        "Download budget hit while downloading %s %s; stopping.",
                        provider_key,
                        item_id,
                    )
                    break
                logger.warning(
                    "Failed to download %s image from service %s",
                    provider_key,
                    svc,
                )
        except Exception:
            logger.exception(
                "Error downloading %s image for %s from %s",
                provider_key,
                item_id,
                svc,
            )

    return any_downloaded

def download_iiif_manifest_and_images(
    manifest_url: str,
    output_folder: str,
    provider_key: str,
    item_id: str,
    skip_images_if_rendering: bool = True,
) -> bool:
    """Download IIIF manifest, renderings, and page images.
    
    This is a common pattern used by providers with IIIF support:
    1. Fetch and save manifest
    2. Try to download manifest-level renderings (PDF/EPUB)
    3. If configured to prefer PDFs and renderings succeeded, skip images
    4. Otherwise download page images via IIIF Image API
    
    Args:
        manifest_url: URL of IIIF manifest
        output_folder: Target directory for downloads
        provider_key: Provider identifier (e.g., 'gallica', 'loc')
        item_id: Item identifier for filename prefixes
        skip_images_if_rendering: If True and renderings downloaded, skip images
        
    Returns:
        True if any content was downloaded
    """
    logger.info("Fetching IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)
    
    if not isinstance(manifest, dict):
        logger.warning("Failed to fetch IIIF manifest from %s", manifest_url)
        return False
    
    # Save manifest
    save_json(manifest, output_folder, f"{provider_key}_{item_id}_manifest")
    
    any_downloaded = False
    
    # Try manifest-level renderings (PDF/EPUB)
    try:
        renders = download_iiif_renderings(
            manifest,
            output_folder,
            filename_prefix=f"{provider_key}_{item_id}_"
        )
        if renders > 0:
            any_downloaded = True
            if skip_images_if_rendering and prefer_pdf_over_images():
                logger.info(
                    "%s: downloaded %d rendering(s); skipping image downloads per config.",
                    provider_key.upper(),
                    renders,
                )
                return True
    except Exception:
        logger.exception(
            "%s: error while downloading manifest renderings for %s",
            provider_key.upper(),
            item_id,
        )
    
    # Extract and download page images
    service_bases = extract_image_service_bases(manifest)
    if download_page_images(service_bases, output_folder, provider_key, item_id):
        any_downloaded = True

    return any_downloaded

def try_pdf_first_then_images(
    pdf_urls: list[str],
    manifest_url: str | None,
    output_folder: str,
    provider_key: str,
    item_id: str,
) -> bool:
    """Try downloading PDFs first, then fall back to IIIF images if needed.
    
    This pattern is used by providers that offer both direct PDF downloads
    and IIIF image access (e.g., Internet Archive).
    
    Args:
        pdf_urls: List of PDF URLs to try
        manifest_url: Optional IIIF manifest URL for image fallback
        output_folder: Target directory
        provider_key: Provider identifier
        item_id: Item identifier
        
    Returns:
        True if any content was downloaded
    """
    any_downloaded = False
    
    # Try PDFs first
    for url in pdf_urls:
        if not url:
            continue
        try:
            if download_file(url, output_folder, f"{provider_key}_{item_id}_content"):
                any_downloaded = True
                # If we got a PDF and prefer PDFs, we're done
                if prefer_pdf_over_images():
                    logger.info(
                        "%s: downloaded PDF; skipping images per config.",
                        provider_key.upper(),
                    )
                    return True
        except Exception:
            logger.exception(
                "%s: error downloading PDF from %s",
                provider_key.upper(),
                url,
            )
    
    # Fall back to IIIF images if no PDF or config allows both
    if manifest_url and not (any_downloaded and prefer_pdf_over_images()):
        try:
            if download_iiif_manifest_and_images(
                manifest_url,
                output_folder,
                provider_key,
                item_id,
                skip_images_if_rendering=False,
            ):
                any_downloaded = True
        except Exception:
            logger.exception(
                "%s: error downloading IIIF images for %s",
                provider_key.upper(),
                item_id,
            )
    
    return any_downloaded
