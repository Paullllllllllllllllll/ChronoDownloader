from .utils import save_json, make_request

API_BASE_URL = "https://api.digitale-sammlungen.de/"  # public MDZ API base


def search_mdz(title, creator=None, max_results=3):
    """Search the Munich Digitization Center (BSB) API."""
    params = {
        "query": title,
        "rows": max_results,
        "format": "json",
    }
    if creator:
        params["creator"] = creator

    print(f"Searching MDZ for: {title}")
    data = make_request(API_BASE_URL + "items", params=params)

    results = []
    if data and data.get("items"):
        for item in data["items"]:
            results.append({
                "title": item.get("title"),
                "id": item.get("id"),
                "iiif_manifest": item.get("manifest"),
                "source": "MDZ",
            })
    return results


def download_mdz_work(item_data, output_folder):
    """Download IIIF manifest for an MDZ item."""
    manifest_url = item_data.get("iiif_manifest")
    if not manifest_url:
        item_id = item_data.get("id")
        if item_id:
            manifest_url = f"https://api.digitale-sammlungen.de/iiif/presentation/v2/{item_id}/manifest"
    if manifest_url:
        manifest = make_request(manifest_url)
        if manifest:
            save_json(manifest, output_folder, f"mdz_{item_data.get('id')}_manifest")
            return True
    return False
