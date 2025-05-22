from .utils import save_json, make_request

# HathiTrust Bibliographic API
API_BASE_URL = "https://catalog.hathitrust.org/api/volumes/brief/json/search"
API_KEY = "YOUR_HATHI_API_KEY_HERE"  # Data API key (optional for basic search)


def search_hathitrust(title, creator=None, max_results=3):
    """Search HathiTrust bibliographic records."""
    params = {"q": title, "limit": max_results}
    if creator:
        params["author"] = creator
    if API_KEY != "YOUR_HATHI_API_KEY_HERE":
        params["key"] = API_KEY

    print(f"Searching HathiTrust for: {title}")
    data = make_request(API_BASE_URL, params=params)

    results = []
    if data and data.get("records"):
        for rec_id, rec in list(data["records"].items())[:max_results]:
            results.append({
                "title": rec.get("title"),
                "id": rec_id,
                "source": "HathiTrust",
            })
    return results


def download_hathitrust_work(item_data, output_folder):
    """Download metadata for a HathiTrust volume."""
    volume_id = item_data.get("id")
    if not volume_id:
        return False

    detail_url = f"https://catalog.hathitrust.org/api/volumes/full/{volume_id}.json"
    if API_KEY != "YOUR_HATHI_API_KEY_HERE":
        detail_url += f"?key={API_KEY}"

    metadata = make_request(detail_url)
    if metadata:
        save_json(metadata, output_folder, f"hathi_{volume_id}_metadata")
        return True
    return False
