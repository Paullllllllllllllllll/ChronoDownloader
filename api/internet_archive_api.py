import urllib.parse
from .utils import save_json, download_file, make_request

SEARCH_API_URL = "https://archive.org/advancedsearch.php"
METADATA_API_URL = "https://archive.org/metadata/{identifier}"


def search_internet_archive(title, creator=None, max_results=3):
    """Search Internet Archive using the Advanced Search API."""
    query_parts = [f'title:("{title}")']
    if creator:
        query_parts.append(f'creator:("{creator}")')
    query_parts.append('mediatype:(texts)')
    query = " AND ".join(query_parts)
    params = {
        "q": query,
        "fl[]": "identifier,title,creator,mediatype,year",
        "rows": str(max_results),
        "page": "1",
        "output": "json",
    }
    print(f"Searching Internet Archive for: {title}")
    data = make_request(SEARCH_API_URL, params=params)
    results = []
    if data and data.get("response") and data["response"].get("docs"):
        for item in data["response"]["docs"]:
            results.append({
                "title": item.get("title", "N/A"),
                "creator": ", ".join(item.get("creator", ["N/A"])),
                "identifier": item.get("identifier"),
                "year": item.get("year", "N/A"),
                "source": "Internet Archive",
            })
    return results


def download_ia_work(item_data, output_folder):
    """Download metadata and IIIF manifest for an Internet Archive item."""
    identifier = item_data.get("identifier")
    if not identifier:
        print("No identifier found in item data.")
        return False
    metadata_url = METADATA_API_URL.format(identifier=identifier)
    print(f"Fetching Internet Archive metadata: {metadata_url}")
    metadata = make_request(metadata_url)
    if metadata:
        save_json(metadata, output_folder, f"ia_{identifier}_metadata")
        iiif_manifest_url = None
        if metadata.get("misc") and metadata["misc"].get("ia_iiif_url"):
            iiif_manifest_url = metadata["misc"]["ia_iiif_url"]
        if not iiif_manifest_url:
            iiif_manifest_url = f"https://iiif.archivelab.org/iiif/{identifier}/manifest.json"
        print(f"Attempting to fetch IA IIIF manifest: {iiif_manifest_url}")
        iiif_manifest_data = make_request(iiif_manifest_url)
        if iiif_manifest_data:
            save_json(iiif_manifest_data, output_folder, f"ia_{identifier}_iiif_manifest")
        if metadata.get("misc") and metadata["misc"].get("image"):
            cover_image_url = metadata["misc"]["image"]
            if not cover_image_url.startswith("http"):
                cover_image_url = f"https://archive.org{cover_image_url}"
            download_file(cover_image_url, output_folder, f"ia_{identifier}_cover.jpg")
        elif metadata.get("files"):
            for fname, finfo in metadata["files"].items():
                if finfo.get("format") == "Thumbnail":
                    thumb_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(fname)}"
                    download_file(thumb_url, output_folder, f"ia_{identifier}_thumbnail.jpg")
                    break
        return True
    return False
