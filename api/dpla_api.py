"""Connector for the Digital Public Library of America (DPLA) API."""

from .utils import save_json, make_request


API_BASE_URL = "https://api.dp.la/v2/"
API_KEY = "YOUR_DPLA_API_KEY_HERE"


def search_dpla(title, creator=None, max_results=3):
    """Search the DPLA API for a title and optional creator."""

    if API_KEY == "YOUR_DPLA_API_KEY_HERE":
        print("DPLA API key not configured. Skipping search.")
        return []

    params = {
        "q": title,
        "api_key": API_KEY,
        "page_size": max_results,
    }
    if creator:
        params["sourceResource.creator"] = creator

    print(f"Searching DPLA for: {title}")
    data = make_request(f"{API_BASE_URL}items", params=params)

    results = []
    if data and data.get("docs"):
        for doc in data["docs"]:
            results.append(
                {
                    "title": doc.get("sourceResource", {}).get("title", "N/A"),
                    "creator": ", ".join(doc.get("sourceResource", {}).get("creator", [])),
                    "id": doc.get("id"),
                    "iiif_manifest": doc.get("object"),
                    "source": "DPLA",
                }
            )

    return results


def download_dpla_work(item_data, output_folder):
    """Download metadata and IIIF manifest for a DPLA item."""

    item_id = item_data.get("id")
    if not item_id:
        print("No DPLA item id found in item data.")
        return False

    params = {"api_key": API_KEY}
    item_details = make_request(f"{API_BASE_URL}items/{item_id}", params=params)
    if item_details:
        save_json(item_details, output_folder, f"dpla_{item_id}_metadata")

        manifest_url = item_details.get("object") or item_data.get("iiif_manifest")
        if manifest_url and "manifest" in manifest_url:
            manifest_data = make_request(manifest_url)
            if manifest_data:
                save_json(manifest_data, output_folder, f"dpla_{item_id}_iiif_manifest")

        return True

    return False
