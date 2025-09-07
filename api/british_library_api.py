"""Connector for the British Library SRU and IIIF APIs."""

import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Union

from .utils import (
    save_json,
    make_request,
    get_provider_setting,
    download_iiif_renderings,
    prefer_pdf_over_images,
)
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult
from .query_helpers import escape_sru_literal

logger = logging.getLogger(__name__)

SRU_BASE_URL = "https://sru.bl.uk/SRU"
IIIF_MANIFEST_BASE = "https://api.bl.uk/metadata/iiif/ark:/81055/{identifier}/manifest.json"


def _bl_max_pages() -> int | None:
    """Read max pages from config provider_settings.british_library.max_pages (0/None = all)."""
    val = get_provider_setting("british_library", "max_pages", None)
    if isinstance(val, int):
        return val
    return 0


def search_british_library(title, creator=None, max_results=3) -> List[SearchResult]:
    """Search the British Library using SRU."""

    q_title = escape_sru_literal(title)
    query_parts = [f'title all "{q_title}"']
    if creator:
        q_creator = escape_sru_literal(creator)
        query_parts.append(f'and creator all "{q_creator}"')
    query = " ".join(query_parts)

    params = {
        "version": "1.2",
        "operation": "searchRetrieve",
        "query": query,
        "maximumRecords": str(max_results),
        "recordSchema": "dc",
    }

    logger.info("Searching British Library for: %s", title)
    response_text = make_request(SRU_BASE_URL, params=params, headers={"Accept": "application/xml,text/xml"})

    results: List[SearchResult] = []
    if isinstance(response_text, str):
        try:
            namespaces = {
                "srw": "http://www.loc.gov/zing/srw/",
                "dc": "http://purl.org/dc/elements/1.1/",
            }
            root = ET.fromstring(response_text)
            for record in root.findall(".//srw:recordData", namespaces):
                dc = record.find("dc:dc", namespaces)
                if dc is None:
                    continue
                title_el = dc.find("dc:title", namespaces)
                creator_el = dc.find("dc:creator", namespaces)
                date_el = dc.find("dc:date", namespaces)
                identifier_el = dc.find("dc:identifier", namespaces)
                identifier = None
                if identifier_el is not None and identifier_el.text:
                    match = re.search(r"ark:/81055/(.*)", identifier_el.text)
                    if match:
                        identifier = match.group(1)

                raw = {
                    "title": title_el.text if title_el is not None else "N/A",
                    "creator": creator_el.text if creator_el is not None else "N/A",
                    "date": date_el.text if date_el is not None else None,
                    "identifier": identifier,
                }
                results.append(convert_to_searchresult("British Library", raw))
        except ET.ParseError as e:
            logger.error("Error parsing BL SRU XML: %s", e)

    return results


def download_british_library_work(item_data: Union[SearchResult, dict], output_folder) -> bool:
    """Download IIIF manifest and page images for a British Library item."""

    identifier = None
    if isinstance(item_data, SearchResult):
        identifier = item_data.source_id or item_data.raw.get("identifier")
    else:
        identifier = item_data.get("identifier")
    if not identifier:
        logger.warning("No BL identifier provided for download.")
        return False

    # Normalize identifier: viewer ARKs often include a ".0x..." suffix which is not present in the manifest path
    id_for_manifest = identifier.split(".")[0] if "." in identifier else identifier

    manifest_url = IIIF_MANIFEST_BASE.format(identifier=id_for_manifest)
    logger.info("Fetching BL IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)

    # Fallback: if direct manifest fetch failed, try discovering it from the public viewer page
    if not manifest:
        try:
            viewer_url = f"https://access.bl.uk/item/viewer/ark:/81055/{identifier}"
            logger.info("BL fallback: attempting to discover manifest from %s", viewer_url)
            html = make_request(viewer_url)
            if isinstance(html, str):
                import re as _re
                m = _re.search(r"https?://[^\"'<>]+/manifest\.json", html)
                if m:
                    alt_manifest = m.group(0)
                    logger.info("BL fallback: found manifest URL %s", alt_manifest)
                    manifest = make_request(alt_manifest)
                    if manifest:
                        manifest_url = alt_manifest
        except Exception:
            logger.exception("BL: error while attempting viewer-based manifest discovery for %s", identifier)
    if not manifest:
        return False

    # Save manifest for reproducibility
    save_json(manifest, output_folder, f"bl_{identifier}_manifest")

    # Prefer manifest-level PDF/EPUB renderings if available
    try:
        renders = download_iiif_renderings(manifest, output_folder, filename_prefix=f"bl_{identifier}_")
        if renders > 0 and prefer_pdf_over_images():
            logger.info("British Library: downloaded %d rendering(s); skipping image downloads per config.", renders)
            return True
    except Exception:
        logger.exception("BL: error while downloading manifest renderings for %s", identifier)

    # Extract IIIF Image API service bases from v2 or v3
    service_bases: List[str] = extract_image_service_bases(manifest)

    if not service_bases:
        logger.info("No IIIF image services found in BL manifest for %s", identifier)
        return True

    # Use shared helper to attempt per-canvas image downloads

    max_pages = _bl_max_pages()
    total = len(service_bases)
    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
    logger.info("British Library: downloading %d/%d page images for %s", len(to_download), total, identifier)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"bl_{identifier}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                ok_any = True
            else:
                logger.warning("Failed to download BL image from %s", svc)
        except Exception:
            logger.exception("Error downloading BL image for %s from %s", identifier, svc)
    return ok_any
