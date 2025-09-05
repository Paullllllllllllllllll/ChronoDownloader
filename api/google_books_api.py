"""Connector for the Google Books API."""

import logging
import os
from typing import List, Union

from .utils import save_json, make_request, download_file
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

API_BASE_URL = "https://www.googleapis.com/books/v1/volumes"

def _api_key() -> str | None:
    return os.getenv("GOOGLE_BOOKS_API_KEY")


def search_google_books(title, creator=None, max_results=3) -> List[SearchResult]:
    key = _api_key()
    if not key:
        logger.warning("Google Books API key not configured. Skipping search.")
        return []
    query = title
    if creator:
        query += f"+inauthor:{creator}"
    params = {
        "q": query,
        "maxResults": str(max_results),
        "key": key,
    }
    logger.info("Searching Google Books for: %s", title)
    data = make_request(API_BASE_URL, params=params)
    results: List[SearchResult] = []
    if data and data.get("items"):
        for item in data["items"]:
            volume_info = item.get("volumeInfo", {})
            raw = {
                "title": volume_info.get("title", "N/A"),
                "creator": ", ".join(volume_info.get("authors", [])),
                "id": item.get("id"),
            }
            results.append(convert_to_searchresult("Google Books", raw))
    return results


def download_google_books_work(item_data: Union[SearchResult, dict], output_folder):
    """Download metadata and available files for a Google Books volume."""

    if isinstance(item_data, SearchResult):
        volume_id = item_data.source_id or item_data.raw.get("id")
    else:
        volume_id = item_data.get("id")
    if not volume_id:
        logger.warning("No Google Books volume id provided.")
        return False

    key = _api_key()
    params = {"key": key} if key else None
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
            # Let Content-Disposition determine the exact filename; provide a sensible fallback
            fallback = f"google_{volume_id}_file_{idx + 1}"
            download_file(url, output_folder, fallback)

        # Save cover image if available
        image_links = volume_data.get("volumeInfo", {}).get("imageLinks", {})
        if image_links.get("thumbnail"):
            download_file(image_links["thumbnail"], output_folder, f"google_{volume_id}_thumb.jpg")

        return True

    return False
