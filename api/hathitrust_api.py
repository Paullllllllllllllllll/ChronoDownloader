"""Connector for the HathiTrust Bibliographic and Data APIs."""

from bs4 import BeautifulSoup

from .utils import save_json, make_request, download_file


BIB_API_URL = "https://catalog.hathitrust.org/api/volumes/brief/json/"
DATA_API_URL = "https://babel.hathitrust.org/cgi/htd/volume/pages"
API_KEY = "YOUR_HATHI_API_KEY_HERE"


def search_hathitrust(title, creator=None, max_results=3):
    """Perform a simple title search using the catalog website and parse results."""

    query = title.replace(" ", "+")
    url = f"https://catalog.hathitrust.org/Search/Home?lookfor={query}&searchtype=title&ft=ft"

    print(f"Searching HathiTrust for: {title}")
    html = make_request(url)

    results = []
    if isinstance(html, str):
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.select("a.title" )[:max_results]:
            href = link.get("href")
            if href and "/Record/" in href:
                record_id = href.split("/Record/")[-1].strip()
                results.append(
                    {
                        "title": link.get_text(strip=True),
                        "creator": creator or "N/A",
                        "id": record_id,
                        "source": "HathiTrust",
                    }
                )

    return results


def download_hathitrust_work(item_data, output_folder):
    """Download metadata for a HathiTrust record; requires an access key for page images."""

    record_id = item_data.get("id")
    if not record_id:
        print("No HathiTrust record id provided.")
        return False

    metadata = make_request(f"{BIB_API_URL}{record_id}.json")
    if metadata:
        save_json(metadata, output_folder, f"hathi_{record_id}_metadata")

    # If the user configured an API key, attempt to fetch the first page image
    if API_KEY != "YOUR_HATHI_API_KEY_HERE":
        params = {
            "id": record_id,
            "seq": 1,
            "v": "1",
            "format": "json",
            "apikey": API_KEY,
        }
        page_data = make_request(DATA_API_URL, params=params)
        if page_data and page_data.get("url"):
            download_file(page_data["url"], output_folder, f"hathi_{record_id}_p1.jp2")

    return True
