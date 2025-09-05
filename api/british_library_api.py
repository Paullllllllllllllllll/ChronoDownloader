"""Connector for the British Library SRU and IIIF APIs."""

import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Union

from .utils import save_json, make_request
from .model import SearchResult, convert_to_searchresult
from .query_helpers import escape_sru_literal

logger = logging.getLogger(__name__)

SRU_BASE_URL = "https://sru.bl.uk/SRU"
IIIF_MANIFEST_BASE = "https://api.bl.uk/metadata/iiif/ark:/81055/{identifier}/manifest.json"


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
    """Download the IIIF manifest for a British Library item."""

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
    manifest_data = make_request(manifest_url)

    if manifest_data:
        save_json(manifest_data, output_folder, f"bl_{identifier}_manifest")
        return True

    return False
