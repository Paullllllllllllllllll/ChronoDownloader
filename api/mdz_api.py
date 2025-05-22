"""Connector for the Münchener DigitalisierungsZentrum (MDZ) API."""

from .utils import save_json, make_request


SEARCH_API_URL = "https://api.digitale-sammlungen.de/solr/mdzsearch/select"
IIIF_MANIFEST_URL = "https://api.digitale-sammlungen.de/iiif/presentation/v2/{object_id}/manifest"


def search_mdz(title, creator=None, max_results=3):
    """Search MDZ using its Solr search interface."""

    query = f'title:"{title}"'
    if creator:
        query += f' AND creator:"{creator}"'

    params = {
        "q": query,
        "rows": max_results,
        "wt": "json",
    }

    print(f"Searching MDZ for: {title}")
    data = make_request(SEARCH_API_URL, params=params)

    results = []
    if data and data.get("response") and data["response"].get("docs"):
        for doc in data["response"]["docs"]:
            results.append(
                {
                    "title": doc.get("title", "N/A"),
                    "creator": ", ".join(doc.get("creator", [])),
                    "id": doc.get("id"),
                    "source": "MDZ",
                }
            )

    return results


def download_mdz_work(item_data, output_folder):
    """Download the IIIF manifest for a MDZ item."""

    object_id = item_data.get("id")
    if not object_id:
        print("No MDZ object id found in item data.")
        return False

    manifest_url = IIIF_MANIFEST_URL.format(object_id=object_id)
    print(f"Fetching MDZ IIIF manifest: {manifest_url}")
    manifest = make_request(manifest_url)

    if manifest:
        save_json(manifest, output_folder, f"mdz_{object_id}_manifest")
        return True

    return False
