"""Connector for the Google Books API."""

from .utils import save_json, make_request, download_file

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
    """Download metadata and available files for a Google Books volume."""

    volume_id = item_data.get("id")
    if not volume_id:
        print("No Google Books volume id provided.")
        return False

    params = {"key": API_KEY}
    volume_data = make_request(f"{API_BASE_URL}/{volume_id}", params=params)

    if volume_data:
        save_json(volume_data, output_folder, f"google_{volume_id}_metadata")

        access_info = volume_data.get("accessInfo", {})
        download_links = []
        if access_info.get("pdf", {}).get("downloadLink"):
            download_links.append(access_info["pdf"]["downloadLink"])
        if access_info.get("epub", {}).get("downloadLink"):
            download_links.append(access_info["epub"]["downloadLink"])

        for idx, url in enumerate(download_links):
            filename = f"google_{volume_id}_file_{idx + 1}.pdf" if url.endswith("pdf") else f"google_{volume_id}_file_{idx + 1}.epub"
            download_file(url, output_folder, filename)

        # Save cover image if available
        image_links = volume_data.get("volumeInfo", {}).get("imageLinks", {})
        if image_links.get("thumbnail"):
            download_file(image_links["thumbnail"], output_folder, f"google_{volume_id}_thumb.jpg")

        return True

    return False
