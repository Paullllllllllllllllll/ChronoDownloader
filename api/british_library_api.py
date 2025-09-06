"""Connector for the British Library SRU and IIIF APIs."""

import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Union

from .utils import save_json, make_request, download_file, get_provider_setting
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
    response_text = make_request(SRU_BASE_URL, params=params)

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
                identifier_el = dc.find("dc:identifier", namespaces)
                identifier = None
                if identifier_el is not None and identifier_el.text:
                    match = re.search(r"ark:/81055/(.*)", identifier_el.text)
                    if match:
                        identifier = match.group(1)

                raw = {
                    "title": title_el.text if title_el is not None else "N/A",
                    "creator": creator_el.text if creator_el is not None else "N/A",
                    "identifier": identifier,
                }
                results.append(convert_to_searchresult("British Library", raw))
        except ET.ParseError as e:
            logger.error("Error parsing BL SRU XML: %s", e)

    return results


def download_british_library_work(item_data: Union[SearchResult, dict], output_folder):
    """Download IIIF manifest and page images for a British Library item."""

    identifier = None
    if isinstance(item_data, SearchResult):
        identifier = item_data.source_id or item_data.raw.get("identifier")
    else:
        identifier = item_data.get("identifier")
    if not identifier:
        logger.warning("No BL identifier provided for download.")
        return False

    manifest_url = IIIF_MANIFEST_BASE.format(identifier=identifier)
    logger.info("Fetching BL IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)

    if not manifest:
        return False

    # Save manifest for reproducibility
    save_json(manifest, output_folder, f"bl_{identifier}_manifest")

    # Extract IIIF Image API service bases from v2 or v3
    service_bases: List[str] = []

    # v2
    try:
        sequences = manifest.get("sequences") or []
        if sequences:
            canvases = sequences[0].get("canvases", [])
            for canvas in canvases:
                try:
                    images = canvas.get("images", [])
                    if not images:
                        continue
                    res = images[0].get("resource", {})
                    service = res.get("service", {})
                    svc_id = service.get("@id") or service.get("id")
                    if not svc_id:
                        img_id = res.get("@id") or res.get("id")
                        if img_id and "/full/" in img_id:
                            svc_id = img_id.split("/full/")[0]
                    if svc_id:
                        service_bases.append(svc_id)
                except Exception:
                    continue
    except Exception:
        pass

    # v3
    if not service_bases and manifest.get("items"):
        try:
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
                    service = body.get("service") or body.get("services")
                    svc_obj = None
                    if isinstance(service, list) and service:
                        svc_obj = service[0]
                    elif isinstance(service, dict):
                        svc_obj = service
                    svc_id = None
                    if svc_obj:
                        svc_id = svc_obj.get("@id") or svc_obj.get("id")
                    if not svc_id:
                        body_id = body.get("id")
                        if body_id and "/full/" in body_id:
                            svc_id = body_id.split("/full/")[0]
                    if svc_id:
                        service_bases.append(svc_id)
                except Exception:
                    continue
        except Exception:
            pass

    if not service_bases:
        logger.info("No IIIF image services found in BL manifest for %s", identifier)
        return True

    # Build candidate image URLs (full size), tolerant to v2/v3
    def _candidates(base: str) -> List[str]:
        b = base.rstrip('/')
        return [
            f"{b}/full/full/0/default.jpg",
            f"{b}/full/max/0/default.jpg",
            f"{b}/full/full/0/native.jpg",
        ]

    def _download_one(base: str, filename: str) -> bool:
        for u in _candidates(base):
            if download_file(u, output_folder, filename):
                return True
        return False

    max_pages = _bl_max_pages()
    total = len(service_bases)
    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
    logger.info("British Library: downloading %d/%d page images for %s", len(to_download), total, identifier)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"bl_{identifier}_p{idx:05d}.jpg"
            if _download_one(svc, fname):
                ok_any = True
            else:
                logger.warning("Failed to download BL image from %s", svc)
        except Exception:
            logger.exception("Error downloading BL image for %s from %s", identifier, svc)
    return ok_any
