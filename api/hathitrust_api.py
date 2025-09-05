"""Connector for the HathiTrust Bibliographic and Data APIs."""

import logging
import os
from typing import List, Union

from bs4 import BeautifulSoup

from .utils import save_json, make_request, download_file
from .model import SearchResult, convert_to_searchresult


logger = logging.getLogger(__name__)

BIB_API_URL = "https://catalog.hathitrust.org/api/volumes/brief/json/"
DATA_API_URL = "https://babel.hathitrust.org/cgi/htd/volume/pages"
API_KEY = os.getenv("HATHI_API_KEY")


def search_hathitrust(title, creator=None, max_results=3) -> List[SearchResult]:
    """Perform a simple title search using the catalog website and parse results."""

    query = title.replace(" ", "+")
    url = f"https://catalog.hathitrust.org/Search/Home?lookfor={query}&searchtype=title&ft=ft"

    logger.info("Searching HathiTrust for: %s", title)
    html = make_request(url)

    results: List[SearchResult] = []
    if isinstance(html, str):
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.select("a.title")[:max_results]:
            href = link.get("href")
            if href and "/Record/" in href:
                record_id = href.split("/Record/")[-1].strip()
                raw = {
                    "title": link.get_text(strip=True),
                    "creator": creator or "N/A",
                    "id": record_id,
                }
                results.append(convert_to_searchresult("HathiTrust", raw))

    return results


def download_hathitrust_work(item_data: Union[SearchResult, dict], output_folder):
    """Download metadata for a HathiTrust record; requires an access key for page images."""

    if isinstance(item_data, SearchResult):
        record_id = item_data.source_id or item_data.raw.get("id")
    else:
        record_id = item_data.get("id")
    if not record_id:
        logger.warning("No HathiTrust record id provided.")
        return False

    metadata = make_request(f"{BIB_API_URL}{record_id}.json")
    if metadata:
        save_json(metadata, output_folder, f"hathi_{record_id}_metadata")

    # If the user configured an API key, attempt to fetch the first page image
    if API_KEY:
        params = {
            "id": record_id,
            "seq": 1,
            "v": "1",
            "format": "json",
            "apikey": API_KEY,
        }
        page_data = make_request(DATA_API_URL, params=params)
        if page_data and page_data.get("url"):
            download_file(page_data["url"], output_folder, f"hathi_{record_id}_p1")

    return True
