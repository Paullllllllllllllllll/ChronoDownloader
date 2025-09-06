import logging
import re
import xml.etree.ElementTree as ET
import time
from typing import List, Union

from .utils import save_json, download_file, make_request, get_provider_setting
from .model import SearchResult, convert_to_searchresult
from .query_helpers import escape_sru_literal

logger = logging.getLogger(__name__)

# Gallica API endpoints
SRU_BASE_URL = "https://gallica.bnf.fr/SRU"
IIIF_MANIFEST_BASE_URL = "https://gallica.bnf.fr/iiif/ark:/12148/{ark_id}/manifest.json"

def search_gallica(title, creator=None, max_results=3) -> List[SearchResult]:
    """Searches Gallica using its SRU API."""
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

def _gallica_max_pages() -> int | None:
    """Read max pages from config provider_settings.gallica.max_pages (0 or None = all)."""
    val = get_provider_setting("gallica", "max_pages", None)
    if isinstance(val, int):
        return val
    return 0


def download_gallica_work(item_data: Union[SearchResult, dict], output_folder):
    """Download Gallica IIIF manifest and full-size page images.

    - Fetches IIIF manifest (usually v2; handle v3 structures too).
    - Extracts IIIF Image API service base per canvas.
    - Downloads images with a small set of quality/size fallbacks to ensure compatibility.
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

    # Extract image service bases from IIIF v2 or v3
    image_service_bases: List[str] = []

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
                        image_service_bases.append(svc_id)
                except Exception:
                    continue
    except Exception:
        pass

    # v3
    if not image_service_bases and manifest.get("items"):
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
                        image_service_bases.append(svc_id)
                except Exception:
                    continue
        except Exception:
            pass

    if not image_service_bases:
        logger.info("No IIIF image services found in Gallica manifest for %s", ark_id)
        return True

    def _candidate_urls(base: str) -> List[str]:
        b = base.rstrip('/')
        # Try a few variants to tolerate v2/v3 and quality strings
        return [
            f"{b}/full/full/0/default.jpg",
            f"{b}/full/full/0/native.jpg",
            f"{b}/full/max/0/default.jpg",
            f"{b}/full/max/0/native.jpg",
        ]

    def _download_with_fallbacks(base: str, filename: str) -> bool:
        for url in _candidate_urls(base):
            path = download_file(url, output_folder, filename)
            if path:
                return True
        return False

    max_pages = _gallica_max_pages()
    total = len(image_service_bases)
    to_download = image_service_bases[:max_pages] if max_pages and max_pages > 0 else image_service_bases
    logger.info("Gallica: downloading %d/%d page images for %s", len(to_download), total, ark_id)
    delay_ms = get_provider_setting("gallica", "delay_ms", 0) or 0
    success_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"gallica_{ark_id}_p{idx:05d}.jpg"
            if _download_with_fallbacks(svc, fname):
                success_any = True
            else:
                logger.warning("Failed to download Gallica image from service %s", svc)
        except Exception:
            logger.exception("Error downloading Gallica image for %s from %s", ark_id, svc)
        # Provider-friendly pacing to avoid 429 rate limits
        if delay_ms and idx < len(to_download):
            time.sleep(float(delay_ms) / 1000.0)

    return success_any
