"""Connector for the Google Books API."""

import logging
import os
from typing import List, Union

from .utils import save_json, make_request, download_file, get_provider_setting
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

API_BASE_URL = "https://www.googleapis.com/books/v1/volumes"

def _api_key() -> str | None:
    return os.getenv("GOOGLE_BOOKS_API_KEY")


def _gb_free_only() -> bool:
    val = get_provider_setting("google_books", "free_only", True)
    return bool(val)


def _gb_prefer_format() -> str:
    val = get_provider_setting("google_books", "prefer", "pdf")
    return str(val or "pdf").lower()


def _gb_allow_drm() -> bool:
    val = get_provider_setting("google_books", "allow_drm", False)
    return bool(val)


def _gb_max_files() -> int:
    val = get_provider_setting("google_books", "max_files", 2)
    try:
        return int(val)
    except Exception:
        return 2


def search_google_books(title, creator=None, max_results=3) -> List[SearchResult]:
    key = _api_key()
    if not key:
        logger.warning("Google Books API key not configured. Skipping search.")
        return []
    # Prefer intitle/inauthor structured search
    query = f'intitle:"{title}"'
    if creator:
        query += f'+inauthor:"{creator}"'
    params = {
        "q": query,
        "maxResults": str(max_results),
        "key": key,
        "printType": "books",
        "orderBy": "relevance",
        "projection": "full",
    }
    if _gb_free_only():
        params["filter"] = "free-ebooks"
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
        prefer = _gb_prefer_format()
        allow_drm = _gb_allow_drm()
        max_files = _gb_max_files()

        def _collect_links() -> List[str]:
            links: List[str] = []
            # Preferred format first
            fmt_order = [prefer, "pdf" if prefer != "pdf" else "epub"]
            for fmt in fmt_order:
                fi = access_info.get(fmt, {})
                if isinstance(fi, dict):
                    if fi.get("downloadLink"):
                        links.append(fi["downloadLink"])
                    elif allow_drm and fi.get("acsTokenLink"):
                        links.append(fi["acsTokenLink"])
            return links

        download_links = _collect_links()[:max_files]

        for idx, url in enumerate(download_links):
            # Let Content-Disposition determine the exact filename; provide a sensible fallback
            fallback = f"google_{volume_id}_file_{idx + 1}"
            download_file(url, output_folder, fallback)

        # Save cover image if available
        image_links = volume_data.get("volumeInfo", {}).get("imageLinks", {})
        # Try higher-quality images first
        for key_name in ["extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail"]:
            if image_links.get(key_name):
                filename = f"google_{volume_id}_{key_name}.jpg"
                download_file(image_links[key_name], output_folder, filename)

        return True

    return False
