import re
import xml.etree.ElementTree as ET
from .utils import save_json, download_file, make_request

# Gallica API endpoints
SRU_BASE_URL = "https://gallica.bnf.fr/SRU"
IIIF_MANIFEST_BASE_URL = "https://gallica.bnf.fr/iiif/ark:/12148/{ark_id}/manifest.json"

def search_gallica(title, creator=None, max_results=3):
    """Searches Gallica using its SRU API."""
    query_parts = [f'gallica all "{title}"']
    if creator:
        query_parts.append(f'and dc.creator all "{creator}"')
    query = " ".join(query_parts)
    params = {
        "version": "1.2",
        "operation": "searchRetrieve",
        "query": query,
        "maximumRecords": str(max_results),
        "recordSchema": "oai_dc",
    }
    print(f"Searching Gallica for: {title}")
    response_text = make_request(SRU_BASE_URL, params=params)
    if not response_text or not isinstance(response_text, str):
        print("Gallica SRU request did not return valid XML text.")
        return []
    results = []
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
                results.append({
                    "title": item_title,
                    "creator": item_creator,
                    "ark_id": ark_id,
                    "source": "BnF Gallica",
                })
    except ET.ParseError as e:
        print(f"Error parsing Gallica SRU XML response: {e}")
        print(f"Response text snippet: {response_text[:500]}")
    except Exception as e:
        print(f"An unexpected error occurred during Gallica XML parsing: {e}")
    return results

def download_gallica_work(item_data, output_folder):
    """Downloads the IIIF manifest for a Gallica work."""
    ark_id = item_data.get("ark_id")
    if not ark_id:
        print("No ark_id found in item data.")
        return False
    manifest_url = IIIF_MANIFEST_BASE_URL.format(ark_id=ark_id)
    print(f"Fetching Gallica IIIF manifest: {manifest_url}")
    manifest_data = make_request(manifest_url)
    if manifest_data:
        save_json(manifest_data, output_folder, f"gallica_{ark_id}_manifest")
        return True
    return False
