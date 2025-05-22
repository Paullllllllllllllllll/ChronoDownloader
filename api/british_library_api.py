"""Connector for the British Library SRU and IIIF APIs."""

import re
import xml.etree.ElementTree as ET

from .utils import save_json, make_request

SRU_BASE_URL = "https://sru.bl.uk/SRU"
IIIF_MANIFEST_BASE = "https://api.bl.uk/metadata/iiif/ark:/81055/{identifier}/manifest.json"


def search_british_library(title, creator=None, max_results=3):
    """Search the British Library using SRU."""

    query_parts = [f'title all "{title}"']
    if creator:
        query_parts.append(f'and creator all "{creator}"')
    query = " ".join(query_parts)

    params = {
        "version": "1.2",
        "operation": "searchRetrieve",
        "query": query,
        "maximumRecords": str(max_results),
        "recordSchema": "dc",
    }

    print(f"Searching British Library for: {title}")
    response_text = make_request(SRU_BASE_URL, params=params)

    results = []
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

                results.append(
                    {
                        "title": title_el.text if title_el is not None else "N/A",
                        "creator": creator_el.text if creator_el is not None else "N/A",
                        "identifier": identifier,
                        "source": "British Library",
                    }
                )
        except ET.ParseError as e:
            print(f"Error parsing BL SRU XML: {e}")

    return results


def download_british_library_work(item_data, output_folder):
    """Download the IIIF manifest for a British Library item."""

    identifier = item_data.get("identifier")
    if not identifier:
        print("No BL identifier provided for download.")
        return False

    manifest_url = IIIF_MANIFEST_BASE.format(identifier=identifier)
    print(f"Fetching BL IIIF manifest: {manifest_url}")
    manifest_data = make_request(manifest_url)

    if manifest_data:
        save_json(manifest_data, output_folder, f"bl_{identifier}_manifest")
        return True

    return False
