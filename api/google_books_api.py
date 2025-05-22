from .utils import save_json, download_file, make_request

API_BASE_URL = "https://www.googleapis.com/books/v1/volumes"
API_KEY = "YOUR_GOOGLE_API_KEY_HERE"


def search_google_books(title, creator=None, max_results=3):
    if API_KEY == "YOUR_GOOGLE_API_KEY_HERE":
        print("Google Books API key not configured. Skipping search.")
        return []
    query = title
    if creator:
        query += f"+inauthor:{creator}"
    params = {
        "q": query,
        "maxResults": str(max_results),
        "key": API_KEY,
    }
    print(f"Searching Google Books for: {title}")
    data = make_request(API_BASE_URL, params=params)
    results = []
    if data and data.get("items"):
        for item in data["items"]:
            volume_info = item.get("volumeInfo", {})
            results.append({
                "title": volume_info.get("title", "N/A"),
                "creator": ", ".join(volume_info.get("authors", [])),
                "id": item.get("id"),
                "source": "Google Books",
            })
    return results


def download_google_books_work(item_data, output_folder):
    """Download volume metadata and public domain PDF if available."""
    if API_KEY == "YOUR_GOOGLE_API_KEY_HERE":
        print("Google Books API key not configured. Skipping download.")
        return False

    volume_id = item_data.get("id")
    if not volume_id:
        return False

    detail_url = f"{API_BASE_URL}/{volume_id}"
    params = {"key": API_KEY}
    metadata = make_request(detail_url, params=params)
    if metadata:
        save_json(metadata, output_folder, f"google_{volume_id}_metadata")

        access = metadata.get("accessInfo", {})
        pdf_link = access.get("pdf", {}).get("downloadLink")
        if pdf_link:
            download_file(pdf_link, output_folder, f"google_{volume_id}.pdf")
        return True
    return False
