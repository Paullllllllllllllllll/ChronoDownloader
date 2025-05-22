import urllib.parse
from .utils import save_json, download_file, make_request

LOC_API_BASE_URL = "https://www.loc.gov/"

def search_loc(title, creator=None, max_results=3):
    query_parts = [title]
    if creator:
        query_parts.append(creator)
    search_query = " ".join(query_parts)
    params = {
        "q": search_query,
        "fo": "json",
        "c": str(max_results),
    }
    search_url = urllib.parse.urljoin(LOC_API_BASE_URL, "search/")
    print(f"Searching Library of Congress for: {title}")
    data = make_request(search_url, params=params)
    results = []
    if data and data.get("results"):
        for item in data["results"]:
            item_id = item.get("id")
            if item_id:
                item_id = item_id.strip('/').split('/')[-1]
            iiif_manifest = item.get("iiif_manifest_url", item.get("resources", [{}])[0].get("iiif_manifest"))
            results.append({
                "title": item.get("title", "N/A"),
                "creator": item.get("contributor_names", ["N/A"])[0] if item.get("contributor_names") else "N/A",
                "id": item_id,
                "item_url": item.get("url"),
                "iiif_manifest": iiif_manifest,
                "source": "Library of Congress",
            })
    elif data and data.get("content") and data["content"].get("results"):
        for item in data["content"]["results"]:
            item_id = item.get("id")
            if item_id:
                item_id = item_id.strip('/').split('/')[-1]
            iiif_manifest = item.get("iiif_manifest_url", item.get("resources", [{}])[0].get("iiif_manifest"))
            results.append({
                "title": item.get("title", "N/A"),
                "creator": item.get("contributor_names", ["N/A"])[0] if item.get("contributor_names") else "N/A",
                "id": item_id,
                "item_url": item.get("url"),
                "iiif_manifest": iiif_manifest,
                "source": "Library of Congress",
            })
    return results

def download_loc_work(item_data, output_folder):
    item_url = item_data.get("item_url")
    item_id = item_data.get("id", item_data.get("title", "unknown_item"))
    if not item_url:
        print(f"No item URL found for LOC item: {item_id}")
        return False
    item_json_url = f"{item_url}?fo=json" if not item_url.endswith("?fo=json") else item_url
    print(f"Fetching LOC item JSON: {item_json_url}")
    item_full_json = make_request(item_json_url)
    if item_full_json:
        save_json(item_full_json, output_folder, f"loc_{item_id}_item_details")
        iiif_manifest_url = item_data.get("iiif_manifest")
        if not iiif_manifest_url and item_full_json.get("item") and item_full_json["item"].get("resources"):
            for res in item_full_json["item"]["resources"]:
                if res.get("iiif_manifest"):
                    iiif_manifest_url = res["iiif_manifest"]
                    break
        elif not iiif_manifest_url and item_full_json.get("resources"):
            for res in item_full_json["resources"]:
                if isinstance(res, dict) and res.get("iiif_manifest"):
                    iiif_manifest_url = res["iiif_manifest"]
                    break
        if iiif_manifest_url:
            print(f"Fetching LOC IIIF manifest: {iiif_manifest_url}")
            iiif_manifest_data = make_request(iiif_manifest_url)
            if iiif_manifest_data:
                save_json(iiif_manifest_data, output_folder, f"loc_{item_id}_iiif_manifest")
            else:
                print(f"Failed to fetch IIIF manifest from {iiif_manifest_url}")
        else:
            print(f"No IIIF manifest URL found for LOC item: {item_id}")
        image_url = None
        if item_full_json.get("item") and item_full_json["item"].get("image_url"):
            if isinstance(item_full_json["item"]["image_url"], dict):
                image_url = item_full_json["item"]["image_url"].get("medium") or item_full_json["item"]["image_url"].get("full")
            elif isinstance(item_full_json["item"]["image_url"], str):
                image_url = item_full_json["item"]["image_url"]
        if image_url:
            if image_url.startswith("//"):
                image_url = "https:" + image_url
            elif not image_url.startswith("http"):
                image_url = "https://www.loc.gov" + image_url
            download_file(image_url, output_folder, f"loc_{item_id}_sample_image.jpg")
        return True
    else:
        print(f"Failed to fetch item JSON from {item_json_url}")
    return False
