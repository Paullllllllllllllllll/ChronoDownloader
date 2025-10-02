"""Connector for the HathiTrust Bibliographic and Data APIs."""

import logging
import os
import re
from typing import Any, Dict, List, Union

from .utils import save_json, make_request, download_file
from .model import SearchResult, convert_to_searchresult


logger = logging.getLogger(__name__)

BIB_BASE_URL = "https://catalog.hathitrust.org/api/volumes/brief/"
DATA_API_URL = "https://babel.hathitrust.org/cgi/htd/volume/pages"


def _api_key() -> str | None:
    """Get HathiTrust API key from environment."""
    return os.getenv("HATHI_API_KEY")


def _bib_url(id_type: str, value: str) -> str:
    """Build Bibliographic API URL for a given identifier type and value.
    
    Args:
        id_type: Identifier type (e.g., 'oclc', 'isbn', 'lccn')
        value: Identifier value
        
    Returns:
        Full API URL for the bibliographic record
    """
    id_type = id_type.lower().strip()
    return f"{BIB_BASE_URL}{id_type}/{value}.json"


def _parse_identifiers(text: str) -> Dict[str, List[str]]:
    """Parse explicit identifier hints from a string like 'oclc:12345 isbn:978...'.
    
    Args:
        text: Text containing identifiers
        
    Returns:
        Dictionary mapping identifier types to lists of values
    """
    if not text:
        return {}
    s = str(text)
    ids: Dict[str, List[str]] = {"oclc": [], "isbn": [], "lccn": [], "issn": [], "htid": []}
    # Patterns are lenient to allow punctuation; we strip surrounding cruft
    for m in re.finditer(r"oclc\s*:\s*(\d+)", s, flags=re.I):
        ids["oclc"].append(m.group(1))
    for m in re.finditer(r"isbn\s*:\s*([0-9Xx\-]+)", s, flags=re.I):
        ids["isbn"].append(m.group(1).replace("-", ""))
    for m in re.finditer(r"lccn\s*:\s*([0-9A-Za-z\-/.]+)", s, flags=re.I):
        ids["lccn"].append(m.group(1))
    for m in re.finditer(r"issn\s*:\s*(\d{4}-?\d{3}[0-9Xx])", s, flags=re.I):
        ids["issn"].append(m.group(1))
    for m in re.finditer(r"htid\s*:\s*([^\s,;]+)", s, flags=re.I):
        ids["htid"].append(m.group(1))
    # Drop empty lists
    return {k: v for k, v in ids.items() if v}


def search_hathitrust(title: str, creator: str | None = None, max_results: int = 3) -> List[SearchResult]:
    """Identifier-aware search using the HathiTrust Bibliographic API.
    
    Args:
        title: Work title to search for
        creator: Optional creator/author name
        max_results: Maximum number of results to return
        
    Returns:
        List of SearchResult objects
    """
    id_hints = {}
    id_hints.update(_parse_identifiers(str(title)))
    if creator:
        # Sometimes IDs are passed in creator field; check there too
        for k, v in _parse_identifiers(str(creator)).items():
            id_hints.setdefault(k, []).extend(v)

    results: List[SearchResult] = []
    if not id_hints:
        logger.info(
            "HathiTrust: no explicit identifiers found in query '%s'; skipping search (no public keyword API).",
            title,
        )
        return results

    # Helper to transform bib record to SearchResult(s)
    def _records_to_results(data: Dict[str, Any]) -> List[SearchResult]:
        out: List[SearchResult] = []
        try:
            recs = (data or {}).get("records", {})
            for rec_id, rec in recs.items():
                titles = rec.get("titles") or rec.get("title") or []
                if isinstance(titles, list):
                    title_text = titles[0] if titles else (title or "N/A")
                else:
                    title_text = titles or (title or "N/A")
                authors = rec.get("authors") or rec.get("mainAuthor") or []
                if isinstance(authors, str):
                    authors = [authors]
                date = None
                for k in ("pubDate", "publishDates", "date"):
                    v = rec.get(k)
                    if isinstance(v, list) and v:
                        date = str(v[0])
                        break
                    if isinstance(v, (str, int)):
                        date = str(v)
                        break
                items = rec.get("items") or []
                htid = None
                item_url = None
                for it in items:
                    if isinstance(it, dict):
                        if not htid and it.get("htid"):
                            htid = it.get("htid")
                        if not item_url and it.get("itemURL"):
                            item_url = it.get("itemURL")
                raw = {
                    "title": title_text,
                    "creator": ", ".join(authors) if authors else None,
                    "date": date,
                    "record_id": rec_id,
                    "htid": htid,
                    "item_url": item_url,
                    "bib": rec,
                }
                # Prefer HTID as source_id when available; fall back to record id
                sid = htid or rec_id
                out.append(convert_to_searchresult("HathiTrust", raw | {"identifier": sid}))
                if len(out) >= max_results:
                    break
        except Exception:
            logger.exception("HathiTrust: error parsing Bibliographic API response")
        return out

    order = ["htid", "oclc", "isbn", "lccn", "issn"]
    for id_type in order:
        vals = id_hints.get(id_type) or []
        for val in vals:
            if id_type == "htid":
                raw = {"title": title, "creator": creator, "identifier": val, "htid": val}
                results.append(convert_to_searchresult("HathiTrust", raw))
            else:
                url = _bib_url(id_type, val)
                data = make_request(url)
                if data:
                    results.extend(_records_to_results(data))
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break
    return results


def download_hathitrust_work(item_data: Union[SearchResult, dict], output_folder: str) -> bool:
    """Download metadata and a representative page for a HathiTrust item.

    - Save Bibliographic API record (if present in raw.bib).
    - If HATHI_API_KEY is set, fetch the first page image via the Data API using HTID.
    
    Args:
        item_data: SearchResult or dict containing item data
        output_folder: Folder to download files to
        
    Returns:
        True if download was successful, False otherwise
    """
    if isinstance(item_data, SearchResult):
        htid = item_data.source_id or item_data.raw.get("htid")
        bib = item_data.raw.get("bib")
        rec_id = item_data.raw.get("record_id")
    else:
        htid = item_data.get("htid") or item_data.get("identifier") or item_data.get("id")
        bib = item_data.get("bib")
        rec_id = item_data.get("record_id")

    # Save bib metadata if available in raw
    if isinstance(bib, dict):
        save_json(bib, output_folder, f"hathi_{(rec_id or htid or 'item')}_metadata")

    # Fetch first page image if possible
    key = _api_key()
    if key and htid:
        params = {
            "id": htid,
            "seq": 1,
            "v": "1",
            "format": "json",
            "apikey": key,
        }
        page_data = make_request(DATA_API_URL, params=params)
        if isinstance(page_data, dict) and page_data.get("url"):
            download_file(page_data["url"], output_folder, f"hathi_{htid}_p1")

    return True
