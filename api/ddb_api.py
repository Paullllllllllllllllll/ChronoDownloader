"""Connector for the Deutsche Digitale Bibliothek (DDB) API."""

from .utils import save_json, make_request


API_BASE_URL = "https://api.deutsche-digitale-bibliothek.de/"
API_KEY = "YOUR_DDB_API_KEY_HERE"


def search_ddb(title, creator=None, max_results=3):
    """Search the DDB API for a title and optional creator."""

    if API_KEY == "YOUR_DDB_API_KEY_HERE":
        print("DDB API key not configured. Skipping DDB search.")
        return []

    query_parts = [f'"{title}"']
    if creator:
        query_parts.append(f'AND creator:"{creator}"')
    query = " ".join(query_parts)

    params = {
        "oauth_consumer_key": API_KEY,
        "query": query,
        "rows": max_results,
    }

    print(f"Searching DDB for: {title}")
    data = make_request(f"{API_BASE_URL}search", params=params)

    results = []
    if data and data.get("results"):
        for item in data["results"]:
            results.append(
                {
                    "title": item.get("title", "N/A"),
                    "creator": ", ".join(item.get("creator", [])),
                    "id": item.get("id") or item.get("objectID"),
                    "iiif_manifest": item.get("iiifManifest"),
                    "source": "DDB",
                }
            )

    return results


def download_ddb_work(item_data, output_folder):
    """Download metadata and IIIF manifest for a DDB item."""

    item_id = item_data.get("id")
    if not item_id:
        print("No DDB item id found in item data.")
        return False

    params = {"oauth_consumer_key": API_KEY}
    item_meta = make_request(f"{API_BASE_URL}items/{item_id}", params=params)

    if item_meta:
        save_json(item_meta, output_folder, f"ddb_{item_id}_metadata")

        manifest_url = item_meta.get("iiifManifest") or item_data.get("iiif_manifest")
        if manifest_url:
            manifest_data = make_request(manifest_url)
            if manifest_data:
                save_json(manifest_data, output_folder, f"ddb_{item_id}_iiif_manifest")

        return True

    return False
