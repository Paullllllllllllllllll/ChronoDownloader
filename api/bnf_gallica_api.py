import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Union

from .utils import save_json, download_file, make_request
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

def download_gallica_work(item_data: Union[SearchResult, dict], output_folder):
    """Downloads the IIIF manifest for a Gallica work."""
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
    manifest_data = make_request(manifest_url)
    if manifest_data:
        save_json(manifest_data, output_folder, f"gallica_{ark_id}_manifest")
        return True
    return False
