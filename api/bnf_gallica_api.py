import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Union

from .utils import (
    save_json,
    make_request,
    get_max_pages,
    download_iiif_renderings,
    prefer_pdf_over_images,
    budget_exhausted,
)
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult
from .query_helpers import escape_sru_literal

logger = logging.getLogger(__name__)

# Gallica API endpoints
SRU_BASE_URL = "https://gallica.bnf.fr/SRU"
IIIF_MANIFEST_BASE_URL = "https://gallica.bnf.fr/iiif/ark:/12148/{ark_id}/manifest.json"


def search_gallica(title: str, creator: str | None = None, max_results: int = 3) -> List[SearchResult]:
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
    results: List[SearchResult] = []
    try:
        namespaces = {
            'sru': 'http://www.loc.gov/zing/srw/',
            'dc': 'http://purl.org/dc/elements/1.1/',
            'oai_dc': 'http://www.openarchives.org/OAI/2.0/oai_dc/'
        }
        root = ET.fromstring(response_text)
        for record in root.findall('.//sru:recordData/oai_dc:dc', namespaces):
            title_elements = record.findall('dc:title', namespaces)
            item_title = title_elements[0].text if title_elements else "N/A"
            creator_elements = record.findall('dc:creator', namespaces)
            item_creator = creator_elements[0].text if creator_elements else "N/A"
            ark_id = None
            for identifier_el in record.findall('dc:identifier', namespaces):
                if identifier_el.text and "ark:/" in identifier_el.text:
                    match = re.search(r'ark:/12148/([^/]+)', identifier_el.text)
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
    except ET.ParseError as e:
        logger.error("Error parsing Gallica SRU XML response: %s", e)
        logger.debug("Gallica response snippet: %s", response_text[:500])
    except Exception as e:
        logger.exception("Unexpected error during Gallica XML parsing: %s", e)
    return results


def download_gallica_work(item_data: Union[SearchResult, dict], output_folder: str) -> bool:
    """Download Gallica IIIF manifest and full-size page images.

    - Fetches IIIF manifest (usually v2; handle v3 structures too).
    - Extracts IIIF Image API service base per canvas.
    - Downloads images with a small set of quality/size fallbacks to ensure compatibility.

    Args:
        item_data: SearchResult or dict with ark_id
        output_folder: Folder to save files to

    Returns:
        True if any files were downloaded, False otherwise
    """
    ark_id = None
    if isinstance(item_data, SearchResult):
        ark_id = item_data.source_id or item_data.raw.get("ark_id")
    else:
        ark_id = item_data.get("ark_id")
    if not ark_id:
        logger.warning("No ark_id found in item data.")
        return False
    manifest_url = IIIF_MANIFEST_BASE_URL.format(ark_id=ark_id)
    logger.info("Fetching Gallica IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)
    if not manifest:
        return False

    # Save manifest for reproducibility
    save_json(manifest, output_folder, f"gallica_{ark_id}_manifest")

    # Prefer manifest-level PDF/EPUB renderings when available
    try:
        renders = download_iiif_renderings(manifest, output_folder, filename_prefix=f"gallica_{ark_id}_")
        if renders > 0 and prefer_pdf_over_images():
            logger.info("Gallica: downloaded %d rendering(s); skipping image downloads per config.", renders)
            return True
    except Exception:
        logger.exception("Gallica: error while downloading manifest renderings for %s", ark_id)

    # Extract image service bases from IIIF v2 or v3
    image_service_bases: List[str] = extract_image_service_bases(manifest)

    if not image_service_bases:
        logger.info("No IIIF image services found in Gallica manifest for %s", ark_id)
        return True

    # Use shared helper to try full-size image candidates per canvas

    max_pages = get_max_pages("gallica")
    total = len(image_service_bases)
    to_download = image_service_bases[:max_pages] if max_pages and max_pages > 0 else image_service_bases
    logger.info("Gallica: downloading %d/%d page images for %s", len(to_download), total, ark_id)
    success_any = False
    for idx, svc in enumerate(to_download, start=1):
        if budget_exhausted():
            logger.warning(
                "Download budget exhausted; stopping Gallica downloads at %d/%d pages for %s",
                idx - 1,
                len(to_download),
                ark_id,
            )
            break
        try:
            fname = f"gallica_{ark_id}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                success_any = True
            else:
                logger.warning("Failed to download Gallica image from service %s", svc)
        except Exception:
            logger.exception("Error downloading Gallica image for %s from %s", ark_id, svc)

    return success_any
