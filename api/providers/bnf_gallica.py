"""BnF Gallica API connector for ChronoDownloader.

Provides search and download functionality for the Bibliothèque nationale de France's
Gallica digital library using SRU (Search/Retrieve via URL) and IIIF APIs.

Gallica hosts millions of digitized documents including books, manuscripts,
maps, periodicals, and more from the BnF collections.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

from ..core.config import prefer_pdf_over_images
from ..core.download import save_json
from ..core.network import make_request
from ..iiif import (
    download_iiif_renderings,
    download_page_images,
    extract_image_service_bases,
)
from ..model import SearchResult, convert_to_searchresult, resolve_item_id
from ..query_helpers import escape_sru_literal

logger = logging.getLogger(__name__)

# Gallica API endpoints
SRU_BASE_URL = "https://gallica.bnf.fr/SRU"
IIIF_MANIFEST_BASE_URL = "https://gallica.bnf.fr/iiif/ark:/12148/{ark_id}/manifest.json"


def search_gallica(
    title: str, creator: str | None = None, max_results: int = 3
) -> list[SearchResult]:
    """Search Gallica using its SRU API.

    Args:
        title: Work title to search for
        creator: Optional creator/author name
        max_results: Maximum number of results to return

    Returns:
        List of SearchResult objects
    """
    q_title = escape_sru_literal(title)
    query_parts = [f'gallica all "{q_title}"']
    if creator:
        q_creator = escape_sru_literal(creator)
        query_parts.append(f'and dc.creator all "{q_creator}"')
    query = " ".join(query_parts)
    params = {
        "version": "1.2",
        "operation": "searchRetrieve",
        "query": query,
        "maximumRecords": str(max_results),
        "recordSchema": "oai_dc",
    }
    logger.info("Searching Gallica for: %s", title)
    response_text = make_request(SRU_BASE_URL, params=params)
    if not response_text or not isinstance(response_text, str):
        logger.warning("Gallica SRU request did not return valid XML text.")
        return []
    results: list[SearchResult] = []
    namespaces = {
        "sru": "http://www.loc.gov/zing/srw/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
    }
    root = None
    try:
        root = ET.fromstring(response_text)
    except ET.ParseError as e:
        logger.error("Error parsing Gallica SRU XML response: %s", e)
        logger.debug("Gallica response snippet: %s", response_text[:500])
    except Exception as e:
        logger.exception("Unexpected error during Gallica XML parsing: %s", e)
    if root is None:
        return results
    for record in root.findall(".//sru:recordData/oai_dc:dc", namespaces):
        try:
            title_elements = record.findall("dc:title", namespaces)
            item_title = title_elements[0].text if title_elements else ""
            creator_elements = record.findall("dc:creator", namespaces)
            item_creator = creator_elements[0].text if creator_elements else None
            ark_id = None
            for identifier_el in record.findall("dc:identifier", namespaces):
                if identifier_el.text and "ark:/" in identifier_el.text:
                    match = re.search(r"ark:/12148/([^/]+)", identifier_el.text)
                    if match:
                        ark_id = match.group(1)
                        break
            if ark_id:
                raw = {
                    "title": item_title,
                    "creator": item_creator,
                    "ark_id": ark_id,
                }
                results.append(convert_to_searchresult("BnF Gallica", raw))
        except Exception:
            logger.warning("Gallica: skipping malformed SRU record", exc_info=True)
            continue
    return results


def download_gallica_work(
    item_data: SearchResult | dict[str, Any], output_folder: str
) -> bool:
    """Download Gallica IIIF manifest and full-size page images.

    - Fetches IIIF manifest (usually v2; handle v3 structures too).
    - Extracts IIIF Image API service base per canvas.
    - Downloads images with a small set of quality/size fallbacks to ensure
      compatibility.

    Args:
        item_data: SearchResult or dict with ark_id
        output_folder: Folder to save files to

    Returns:
        True if any files were downloaded, False otherwise
    """
    ark_id = resolve_item_id(item_data, "ark_id")
    if not ark_id:
        logger.warning("No ark_id found in item data.")
        return False
    manifest_url = IIIF_MANIFEST_BASE_URL.format(ark_id=ark_id)
    logger.info("Fetching Gallica IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)
    if not isinstance(manifest, dict):
        return False

    # Save manifest for reproducibility
    save_json(manifest, output_folder, f"gallica_{ark_id}_manifest")

    # Prefer manifest-level PDF/EPUB renderings when available
    renders = 0
    try:
        renders = download_iiif_renderings(manifest, output_folder)
        if renders > 0 and prefer_pdf_over_images():
            logger.info(
                "Gallica: downloaded %d rendering(s); skipping image downloads "
                "per config.",
                renders,
            )
            return True
    except Exception:
        logger.exception(
            "Gallica: error while downloading manifest renderings for %s", ark_id
        )

    # Extract image service bases from IIIF v2 or v3 and download page images
    image_service_bases = extract_image_service_bases(manifest)
    success_any = download_page_images(
        image_service_bases, output_folder, "gallica", ark_id
    )

    return success_any or renders > 0
