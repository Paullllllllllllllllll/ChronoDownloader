"""Connector for the HathiTrust Bibliographic and Data APIs."""

import logging
import os
from typing import List, Union

from .utils import save_json, make_request, download_file
from .model import SearchResult, convert_to_searchresult


logger = logging.getLogger(__name__)

BIB_API_URL = "https://catalog.hathitrust.org/api/volumes/brief/json/"
DATA_API_URL = "https://babel.hathitrust.org/cgi/htd/volume/pages"

def _api_key() -> str | None:
    return os.getenv("HATHI_API_KEY")


def search_hathitrust(title, creator=None, max_results=3) -> List[SearchResult]:
    """HathiTrust does not expose a keyword search API; skip search.

    According to the official docs, the Bibliographic API supports lookups by known
    identifiers (OCLC, LCCN, ISBN, etc.) but is not intended for free-text search.
    To avoid fragile HTML scraping and frequent 403s, we return no results here.
    """
    logger.info(
        "Skipping HathiTrust search for '%s': no public keyword search API; provide an identifier to use download endpoints.",
        title,
    )
    return []


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
    key = _api_key()
    if key:
        params = {
            "id": record_id,
            "seq": 1,
            "v": "1",
            "format": "json",
            "apikey": key,
        }
        page_data = make_request(DATA_API_URL, params=params)
        if page_data and page_data.get("url"):
            download_file(page_data["url"], output_folder, f"hathi_{record_id}_p1")

    return True
