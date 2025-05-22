from .utils import save_json, make_request
import xml.etree.ElementTree as ET

# British Library SRU endpoint
SRU_URL = "https://sru.bl.uk/SRU"  # No API key required


def search_british_library(title, creator=None, max_results=3):
    """Search the British Library using SRU."""
    query_parts = [f'title="{title}"']
    if creator:
        query_parts.append(f'and creator="{creator}"')

    params = {
        "version": "1.1",
        "operation": "searchRetrieve",
        "query": " ".join(query_parts),
        "maximumRecords": str(max_results),
        "recordSchema": "dc",
    }

    print(f"Searching British Library for: {title}")
    response_text = make_request(SRU_URL, params=params)

    results = []
    if response_text and isinstance(response_text, str):
        try:
            root = ET.fromstring(response_text)
            ns = {
                "sru": "http://www.loc.gov/zing/srw/",
                "dc": "http://purl.org/dc/elements/1.1/",
            }
            for record in root.findall('.//sru:recordData/dc:dc', ns):
                title_el = record.find('dc:title', ns)
                creator_el = record.find('dc:creator', ns)
                identifier_el = record.find('dc:identifier', ns)
                identifier = identifier_el.text if identifier_el is not None else None
                results.append({
                    "title": title_el.text if title_el is not None else "N/A",
                    "creator": creator_el.text if creator_el is not None else "N/A",
                    "id": identifier,
                    "source": "British Library",
                })
        except ET.ParseError:
            pass
    return results


def download_british_library_work(item_data, output_folder):
    """Try to download a IIIF manifest for a BL item."""
    identifier = item_data.get("id")
    if not identifier:
        return False

    manifest_url = f"https://api.bl.uk/metadata/iiif/ark:/81055/{identifier}/manifest.json"
    manifest = make_request(manifest_url)
    if manifest:
        save_json(manifest, output_folder, f"bl_{identifier}_manifest")
        return True
    return False
