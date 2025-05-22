from .utils import save_json, make_request

# Deutsche Digitale Bibliothek API
API_BASE_URL = "https://api.deutsche-digitale-bibliothek.de/"
API_KEY = "YOUR_DDB_API_KEY_HERE"  # Replace with your key


def search_ddb(title, creator=None, max_results=3):
    """Search the DDB for a given title."""
    if API_KEY == "YOUR_DDB_API_KEY_HERE":
        print("DDB API key not configured. Skipping DDB search.")
        return []

    params = {
        "oauth_consumer_key": API_KEY,
        "query": title,
        "rows": max_results,
    }
    if creator:
        params["facet.creator"] = creator

    print(f"Searching DDB for: {title}")
    data = make_request(API_BASE_URL + "search", params=params)

    results = []
    if data and data.get("results"):
        for item in data["results"]:
            results.append({
                "title": item.get("title"),
                "id": item.get("id"),
                "iiif_manifest": item.get("object"),
                "source": "DDB",
            })
    return results


def download_ddb_work(item_data, output_folder):
    """Download metadata for a DDB item and its IIIF manifest if available."""
    if API_KEY == "YOUR_DDB_API_KEY_HERE":
        return False

    item_id = item_data.get("id")
    if not item_id:
        return False

    detail_url = API_BASE_URL + f"items/{item_id}"
    params = {"oauth_consumer_key": API_KEY}
    metadata = make_request(detail_url, params=params)

    if metadata:
        save_json(metadata, output_folder, f"ddb_{item_id}_metadata")
        manifest_url = item_data.get("iiif_manifest")
        if not manifest_url and metadata.get("digitalObject"):
            manifest_url = metadata["digitalObject"].get("iiifManifest")
        if manifest_url:
            manifest = make_request(manifest_url)
            if manifest:
                save_json(manifest, output_folder, f"ddb_{item_id}_manifest")
        return True
    return False
