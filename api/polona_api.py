from .utils import save_json, make_request

# Polona search API base
API_BASE_URL = "https://polona.pl/api/search"


def search_polona(title, creator=None, max_results=3):
    """Search Polona (National Library of Poland)."""
    params = {
        "query": title,
        "format": "json",
        "rows": max_results,
    }
    if creator:
        params["creator"] = creator

    print(f"Searching Polona for: {title}")
    data = make_request(API_BASE_URL, params=params)

    results = []
    if data and data.get("items"):
        for item in data["items"]:
            results.append({
                "title": item.get("title"),
                "id": item.get("id"),
                "iiif_manifest": item.get("manifest"),
                "source": "Polona",
            })
    return results


def download_polona_work(item_data, output_folder):
    """Download a IIIF manifest for a Polona item."""
    manifest_url = item_data.get("iiif_manifest")
    if not manifest_url and item_data.get("id"):
        manifest_url = f"https://polona.pl/iiif/item/{item_data['id']}/manifest.json"
    if manifest_url:
        manifest_data = make_request(manifest_url)
        if manifest_data:
            save_json(manifest_data, output_folder, f"polona_{item_data.get('id')}_manifest")
            return True
    return False
