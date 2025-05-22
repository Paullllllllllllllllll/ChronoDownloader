from .utils import save_json, make_request

# DPLA Search API
API_BASE_URL = "https://api.dp.la/v2/"
API_KEY = "YOUR_DPLA_API_KEY_HERE"  # Replace with your key


def search_dpla(title, creator=None, max_results=3):
    """Search DPLA for a given title and optional creator."""
    if API_KEY == "YOUR_DPLA_API_KEY_HERE":
        print("DPLA API key not configured. Skipping search.")
        return []

    params = {
        "api_key": API_KEY,
        "q": title,
        "page_size": max_results,
    }
    if creator:
        # `sourceResource.creator` is the field used for creators
        params["sourceResource.creator"] = creator

    print(f"Searching DPLA for: {title}")
    data = make_request(API_BASE_URL + "items", params=params)

    results = []
    if data and data.get("docs"):
        for item in data["docs"]:
            title_val = item.get("sourceResource", {}).get("title")
            if isinstance(title_val, list):
                title_val = title_val[0]
            creator_val = item.get("sourceResource", {}).get("creator")
            if isinstance(creator_val, list):
                creator_val = creator_val[0]
            results.append({
                "title": title_val or "N/A",
                "creator": creator_val or "N/A",
                "id": item.get("id"),
                "iiif_manifest": item.get("object"),
                "source": "DPLA",
            })
    return results


def download_dpla_work(item_data, output_folder):
    """Download metadata for a DPLA item and its IIIF manifest if provided."""
    if API_KEY == "YOUR_DPLA_API_KEY_HERE":
        print("DPLA API key not configured. Skipping download.")
        return False

    item_id = item_data.get("id")
    if not item_id:
        print("No item ID present in DPLA data.")
        return False

    detail_url = API_BASE_URL + f"items/{item_id}"
    params = {"api_key": API_KEY}
    print(f"Fetching DPLA item metadata: {detail_url}")
    metadata = make_request(detail_url, params=params)

    if metadata:
        save_json(metadata, output_folder, f"dpla_{item_id}_metadata")

        manifest_url = item_data.get("iiif_manifest")
        if not manifest_url and metadata.get("docs"):
            doc = metadata["docs"][0]
            manifest_url = doc.get("object")

        if manifest_url and manifest_url.endswith("manifest.json"):
            manifest_data = make_request(manifest_url)
            if manifest_data:
                save_json(manifest_data, output_folder, f"dpla_{item_id}_manifest")
        return True
    return False
