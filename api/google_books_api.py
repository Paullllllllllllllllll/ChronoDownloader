"""Connector for the Google Books API."""

import logging
import os
import urllib.parse
from typing import List, Union

from .utils import save_json, make_request, download_file, get_provider_setting
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

API_BASE_URL = "https://www.googleapis.com/books/v1/volumes"

def _api_key() -> str | None:
    # Environment-only API key
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
    free_only = _gb_free_only()
    prefer_fmt = _gb_prefer_format()

    def _params(q: str, use_filter: str | None) -> dict:
        p = {
            "q": q,
            "maxResults": str(max_results),
            "printType": "books",
            "orderBy": "relevance",
            "projection": "full",
            # Set a default country to normalize viewability; adjust if needed via config later
            "country": "US",
        }
        if key:
            p["key"] = key
        if use_filter:
            p["filter"] = use_filter
        # The Books API supports 'download=epub' to restrict to downloadable EPUBs;
        # only add this hint when EPUB is preferred to avoid excluding PDF-only free books.
        try:
            if _gb_prefer_format() == "epub":
                p.setdefault("download", "epub")
        except Exception:
            pass
        return p

    def _try(q: str, use_filter: str | None):
        return make_request(API_BASE_URL, params=_params(q, use_filter))

    # Build a few query variants
    import re as _re
    # 1) strict intitle/inauthor with quotes
    q1 = f'intitle:"{title}"'
    if creator:
        q1 += f'+inauthor:"{creator}"'
    # 2) unquoted fields (helps with punctuation-heavy titles)
    q2 = f'intitle:{title}'
    if creator:
        q2 += f'+inauthor:{creator}'
    # 3) plain text title+creator
    q3 = f"{title} {creator}" if creator else f"{title}"
    # 4) heavily sanitized plain title only
    q4 = _re.sub(r"[^\w\s]", " ", title).strip()

    queries = [q1, q2, q3, q4]
    filters = []
    if free_only:
        # Try strictly free first, then any ebook, then no filter
        filters = ["free-ebooks", "ebooks", None]
    else:
        filters = ["ebooks", None]

    logger.info("Searching Google Books for: %s", title)
    data = None
    for q in queries:
        for flt in filters:
            data = _try(q, flt)
            if data and data.get("items"):
                break
        if data and data.get("items"):
            break

    results: List[SearchResult] = []
    if data and data.get("items"):
        for item in data["items"]:
            volume_info = item.get("volumeInfo", {})
            vol_id = item.get("id")
            access_info = item.get("accessInfo", {}) if isinstance(item.get("accessInfo", {}), dict) else {}
            # Determine if there is a direct downloadable link (pdf/epub)
            def _dl(ai: dict) -> str | None:
                if not isinstance(ai, dict):
                    return None
                # Prefer configured format, but accept either
                if isinstance(ai.get(prefer_fmt, {}), dict) and ai.get(prefer_fmt, {}).get("downloadLink"):
                    return ai[prefer_fmt]["downloadLink"]
                for alt in ("pdf", "epub"):
                    if isinstance(ai.get(alt, {}), dict) and ai.get(alt, {}).get("downloadLink"):
                        return ai[alt]["downloadLink"]
                return None
            download_link = _dl(access_info)
            # If configured to free_only, accept items that are clearly free/public domain
            # even when the API does not expose a direct downloadLink.
            public_domain = bool(access_info.get("publicDomain"))
            viewability = str(access_info.get("viewability") or volume_info.get("viewability") or "").lower()
            is_full_view = any(k in viewability for k in ("all_pages", "all_pages_public_domain", "full_public_domain", "full"))
            # Only accept clearly downloadable or fully viewable items when free_only is requested
            # Some items report generic ebook availability without a direct download; exclude those
            if free_only and not (download_link or public_domain or is_full_view):
                # Skip items that are not obviously free when free_only is requested
                continue
            raw = {
                "title": volume_info.get("title", "N/A"),
                "creator": ", ".join(volume_info.get("authors", [])),
                "id": vol_id,
                "item_url": f"https://books.google.com/books?id={vol_id}" if vol_id else None,
                "accessInfo": access_info,
                "viewability": viewability,
                "publicDomain": public_domain,
                "has_download": bool(download_link),
            }
            results.append(convert_to_searchresult("Google Books", raw))
    return results


def download_google_books_work(item_data: Union[SearchResult, dict], output_folder):
    """Download metadata and available files for a Google Books volume.

    Strategy:
      1) Fetch volume metadata
      2) Download PDF/EPUB if direct links provided (or DRM link if allowed)
      3) If no main file, download best available cover images (imageLinks and books/content endpoint)
    Returns True if any object was downloaded.
    """

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
            # Preferred format first; try both pdf and epub variants
            fmt_order = [prefer, "pdf" if prefer != "pdf" else "epub"]
            for fmt in fmt_order:
                fi = access_info.get(fmt, {})
                if isinstance(fi, dict):
                    if fi.get("downloadLink"):
                        links.append(fi["downloadLink"])
                    elif allow_drm and fi.get("acsTokenLink"):
                        links.append(fi["acsTokenLink"])
            return links

        download_links = _collect_links()

        # If no explicit download links, try a few heuristic fallbacks for truly free/public domain items
        if not download_links:
            try:
                vi = volume_data.get("volumeInfo", {})
                ai = access_info if isinstance(access_info, dict) else {}
                public_domain = bool(ai.get("publicDomain")) or (vi.get("contentVersion") == "full-public-domain")
                viewability = (ai.get("viewability") or vi.get("viewability") or "").lower()
                # Consider trying fallbacks when volume is likely fully viewable and/or public domain
                if public_domain or "all_pages" in viewability or "full" in viewability:
                    # Build candidate URLs. Some volumes serve PDFs/EPUBs at these endpoints when allowed.
                    base_candidates = [
                        f"https://books.google.com/books/download?id={volume_id}&output=pdf",
                        f"https://books.google.com/books?id={volume_id}&printsec=frontcover&output=pdf",
                        f"https://books.googleusercontent.com/books/content?id={volume_id}&download=1&output=pdf",
                        f"https://books.google.com/books/download?id={volume_id}&output=epub",
                        f"https://books.googleusercontent.com/books/content?id={volume_id}&download=1&output=epub",
                    ]
                    # Also try with source and API key if present
                    key = _api_key()
                    enriched: List[str] = []
                    for u in base_candidates:
                        qs_sep = "&" if "?" in u else "?"
                        enriched.append(f"{u}{qs_sep}source=gbs_api")
                        if key:
                            enriched.append(f"{u}{qs_sep}source=gbs_api&key={urllib.parse.quote(key)}")
                    download_links = base_candidates + enriched
            except Exception:
                logger.exception("Google Books: error preparing heuristic PDF URLs for %s", volume_id)

        # Deduplicate and enforce max_files
        seen = set()
        uniq_links: List[str] = []
        for u in download_links:
            if u and u not in seen:
                seen.add(u)
                uniq_links.append(u)

        any_ok = False
        for idx, url in enumerate(uniq_links[:max_files]):
            # Let Content-Disposition determine the exact filename; provide a sensible fallback
            fallback = f"google_{volume_id}_file_{idx + 1}"
            path = download_file(url, output_folder, fallback)
            if path:
                any_ok = True

        # Always attempt to save cover images (acts as fallback object if no main file)
        image_links = volume_data.get("volumeInfo", {}).get("imageLinks", {})
        cover_candidates = []
        # Preferred metadata image sizes
        for key_name in ["extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail"]:
            if image_links.get(key_name):
                cover_candidates.append((image_links[key_name], key_name))
        # Heuristic high-quality cover via books/content endpoint
        # Try several zoom levels to increase chances
        for zoom in (5, 4, 3):
            cover_candidates.append((
                f"https://books.google.com/books/content?id={urllib.parse.quote(volume_id)}&printsec=frontcover&img=1&zoom={zoom}&edge=curl",
                f"content_zoom{zoom}",
            ))

        saved_any_cover = False
        used = set()
        for url, label in cover_candidates:
            if url in used:
                continue
            used.add(url)
            filename = f"google_{volume_id}_{label}.jpg"
            if download_file(url, output_folder, filename):
                any_ok = True
                saved_any_cover = True
                # Do not break; allow multiple sizes but keep it small implicitly via dedup and network pacing

        return any_ok

    return False
