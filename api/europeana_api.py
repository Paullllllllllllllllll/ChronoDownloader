from .utils import save_json, make_request

API_BASE_URL = "https://api.europeana.eu/record/v2/search.json"
API_KEY = "YOUR_API_KEY_HERE"

def search_europeana(title, creator=None, max_results=3):
    if API_KEY == "YOUR_API_KEY_HERE":
        print("Europeana API key not configured. Skipping search.")
        return []
    query_parts = [f'title:"{title}"']
    if creator:
        query_parts.append(f'AND who:"{creator}"')
    query_parts.append('AND proxy_dc_type:"TEXT"')
    query = " ".join(query_parts)
    params = {
        "wskey": API_KEY,
        "query": query,
        "rows": str(max_results),
    }
    print(f"Searching Europeana for: {title}")
    data = make_request(API_BASE_URL, params=params)
    results = []
    if data and data.get("success") and data.get("items"):
        for item in data["items"]:
            item_title = item.get("title", ["N/A"])
            if isinstance(item_title, list):
                item_title = item_title[0]
            item_creator = "N/A"
            if item.get("dcCreator"):
                item_creator = item["dcCreator"][0]
            iiif_manifest = None
            if item.get("edmAggregatedCHO") and item["edmAggregatedCHO"].get("hasView"):
                views = item["edmAggregatedCHO"]["hasView"]
                if not isinstance(views, list):
                    views = [views]
                for view in views:
                    if isinstance(view, str) and "iiif" in view and "manifest" in view:
                        iiif_manifest = view
                        break
                    elif isinstance(view, dict) and view.get("@id") and "iiif" in view["@id"] and "manifest" in view["@id"]:
                        iiif_manifest = view["@id"]
                        break
            if not iiif_manifest and "iiif" in item.get("object", "") and "manifest" in item.get("object", ""):
                iiif_manifest = item.get("object")
            results.append({
                "title": item_title,
                "creator": item_creator,
                "id": item.get("id"),
                "europeana_url": item.get("guid"),
                "provider": item.get("dataProvider", ["N/A"])[0] if item.get("dataProvider") else "N/A",
                "iiif_manifest": iiif_manifest,
                "source": "Europeana",
            })
    elif data and not data.get("success"):
        print(f"Europeana API error: {data.get('error')}")
    return results

def download_europeana_work(item_data, output_folder):
    item_id = item_data.get("id", item_data.get("title", "unknown_item"))
    save_json(item_data, output_folder, f"europeana_{item_id}_search_meta")
    iiif_manifest_url = item_data.get("iiif_manifest")
    if iiif_manifest_url:
        print(f"Fetching Europeana IIIF manifest: {iiif_manifest_url}")
        manifest_data = make_request(iiif_manifest_url)
        if manifest_data:
            save_json(manifest_data, output_folder, f"europeana_{item_id}_iiif_manifest")
        else:
            print(f"Failed to fetch IIIF manifest from {iiif_manifest_url}")
    else:
        print(f"No direct IIIF manifest URL found in Europeana search result for {item_id}.")
    if item_data.get("edmIsShownBy"):
        print(f"Item image (provider link): {item_data['edmIsShownBy'][0]}")
    return True
