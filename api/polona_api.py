"""Connector for the Polona.pl API."""

from .utils import save_json, make_request


SEARCH_API_URL = "https://polona.pl/api/search"
DETAIL_API_URL = "https://polona.pl/api/items/{item_id}"
IIIF_MANIFEST_URL = "https://polona.pl/iiif/item/{item_id}/manifest.json"


def search_polona(title, creator=None, max_results=3):
    """Search Polona for items matching the title/creator."""

    query = title
    if creator:
        query += f" {creator}"

    params = {
        "query": query,
        "format": "json",
        "limit": max_results,
    }

    print(f"Searching Polona for: {title}")
    data = make_request(SEARCH_API_URL, params=params)

    results = []
    if data and data.get("items"):
        for item in data["items"]:
            results.append(
                {
                    "title": item.get("title", "N/A"),
                    "creator": item.get("creator", "N/A"),
                    "id": item.get("uid"),
                    "source": "Polona",
                }
            )

    return results


def download_polona_work(item_data, output_folder):
    """Download metadata and IIIF manifest for a Polona item."""

    item_id = item_data.get("id")
    if not item_id:
        print("No Polona item id provided.")
        return False

    detail_url = DETAIL_API_URL.format(item_id=item_id)
    metadata = make_request(detail_url)
    if metadata:
        save_json(metadata, output_folder, f"polona_{item_id}_metadata")

    manifest_url = IIIF_MANIFEST_URL.format(item_id=item_id)
    manifest = make_request(manifest_url)
    if manifest:
        save_json(manifest, output_folder, f"polona_{item_id}_manifest")
        return True

    return False
