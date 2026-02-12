"""Connector for the Google Books API."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import urllib.parse

from .core.config import get_provider_setting
from .core.network import make_request
from .utils import save_json, download_file
from .model import SearchResult, convert_to_searchresult, resolve_item_id

logger = logging.getLogger(__name__)

API_BASE_URL = "https://www.googleapis.com/books/v1/volumes"

def _api_key() -> str | None:
    """Get Google Books API key from environment."""
    # Environment-only API key
    return os.getenv("GOOGLE_BOOKS_API_KEY")

def _gb_free_only() -> bool:
    """Check if only free books should be searched."""
    val = get_provider_setting("google_books", "free_only", True)
    return bool(val)

def _gb_prefer_format() -> str:
    """Get preferred download format (pdf or epub)."""
    val = get_provider_setting("google_books", "prefer", "pdf")
    return str(val or "pdf").lower()

def _gb_allow_drm() -> bool:
    """Check if DRM-protected content is allowed."""
    val = get_provider_setting("google_books", "allow_drm", False)
    return bool(val)

def _gb_max_files() -> int:
    """Get maximum number of files to download per work."""
    val = get_provider_setting("google_books", "max_files", 2)
    try:
        return int(val)
    except Exception:
        return 2

def search_google_books(title: str, creator: str | None = None, max_results: int = 3) -> list[SearchResult]:
    """Search Google Books API for works.
    
    Args:
        title: Work title to search for
        creator: Optional creator/author name
        max_results: Maximum number of results to return
        
    Returns:
        List of SearchResult objects
    """
    key = _api_key()
    free_only = _gb_free_only()
    prefer_fmt = _gb_prefer_format()

    def _params(q: str, use_filter: str | None) -> dict:
        """Build query parameters for Google Books API."""
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
        """Make a request to the Google Books API."""
        return make_request(API_BASE_URL, params=_params(q, use_filter))

    # Build a few query variants
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
    q4 = re.sub(r"[^\w\s]", " ", title).strip()

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

    results: list[SearchResult] = []
    if data and data.get("items"):
        for item in data["items"]:
            volume_info = item.get("volumeInfo", {})
            vol_id = item.get("id")
            access_info = item.get("accessInfo", {}) if isinstance(item.get("accessInfo", {}), dict) else {}
            # Determine if there is a direct downloadable link (pdf/epub)
            def _dl(ai: dict) -> str | None:
                """Get direct downloadable link from access info."""
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

def download_google_books_work(item_data: SearchResult | dict, output_folder: str) -> bool:
    """Download metadata and available files for a Google Books volume.
    
    Google Books only provides actual downloadable PDFs/EPUBs for:
    - Books purchased by the authenticated user
    - True public domain books with full download access (uncommon)
    
    For most "public domain" books, the download links redirect to HTML error pages.
    This function attempts API download links first, then falls back to extracting
    page images via the embedded viewer API for books with full preview.
    
    Args:
        item_data: SearchResult or dict containing volume data
        output_folder: Folder to save files to
        
    Returns:
        True if any object was downloaded
    """

    volume_id = resolve_item_id(item_data)
    if not volume_id:
        logger.warning("No Google Books volume id provided.")
        return False

    key = _api_key()
    params = {"key": key} if key else None
    volume_data = make_request(f"{API_BASE_URL}/{volume_id}", params=params)

    if not isinstance(volume_data, dict):
        return False
        
    save_json(volume_data, output_folder, f"google_{volume_id}_metadata")

    access_info = volume_data.get("accessInfo", {})
    prefer = _gb_prefer_format()
    allow_drm = _gb_allow_drm()
    max_files = _gb_max_files()

    # Check if this book actually has downloadable content
    public_domain = bool(access_info.get("publicDomain"))
    viewability = str(access_info.get("viewability") or "").lower()
    is_full_view = "all_pages" in viewability or viewability == "full"
    
    def _collect_links() -> list[str]:
        """Collect download links from access info."""
        links: list[str] = []
        fmt_order = [prefer, "pdf" if prefer != "pdf" else "epub"]
        for fmt in fmt_order:
            fi = access_info.get(fmt, {})
            if isinstance(fi, dict):
                # Only use downloadLink if the format is actually available
                if fi.get("isAvailable") and fi.get("downloadLink"):
                    links.append(fi["downloadLink"])
                elif allow_drm and fi.get("acsTokenLink"):
                    links.append(fi["acsTokenLink"])
        return links

    download_links = _collect_links()

    # Deduplicate and enforce max_files
    seen = set()
    uniq_links: list[str] = []
    for u in download_links:
        if u and u not in seen:
            seen.add(u)
            uniq_links.append(u)

    any_ok = False
    for idx, url in enumerate(uniq_links[:max_files]):
        fallback = f"google_{volume_id}_file_{idx + 1}"
        path = download_file(url, output_folder, fallback)
        if path:
            any_ok = True
            logger.info("Google Books: Successfully downloaded file from API for %s", volume_id)

    # For public domain / full view books, try page-by-page image extraction
    # This works when direct PDF download fails (which is common)
    if not any_ok and (public_domain or is_full_view):
        logger.info("Google Books: No direct download available for %s, attempting page image extraction", volume_id)
        any_ok = _download_page_images(volume_id, output_folder, max_files)

    # Fallback: Save cover images (at least get something)
    if not any_ok:
        logger.info("Google Books: Falling back to cover images for %s", volume_id)
        image_links = volume_data.get("volumeInfo", {}).get("imageLinks", {})
        cover_candidates = []
        # Preferred metadata image sizes - only keep the best quality ones
        for key_name in ["extraLarge", "large", "medium"]:
            if image_links.get(key_name):
                cover_candidates.append((image_links[key_name], key_name))
        # Heuristic high-quality cover via books/content endpoint
        if not cover_candidates:
            for zoom in [5, 4, 3]:
                cover_candidates.append((
                    f"https://books.google.com/books/content?id={urllib.parse.quote(volume_id)}&printsec=frontcover&img=1&zoom={zoom}&edge=curl",
                    f"content_zoom{zoom}",
                ))

        used = set()
        for url, label in cover_candidates:
            if url in used:
                continue
            used.add(url)
            filename = f"google_{volume_id}_{label}.jpg"
            if download_file(url, output_folder, filename):
                any_ok = True
                break  # One cover is enough

    return any_ok

# Known Google Books "image not available" placeholder signatures
# These are detected by file size and MD5 hash
GB_PLACEHOLDER_SIGNATURES = {
    # Standard "page not available" placeholder (gray image with text)
    (9103, "a64fa89d7ebc97075c1d363fc5fea71f"),
    # Additional known placeholder sizes (may vary slightly)
    (9103, None),  # Size-only check as fallback
}

# Minimum file size for a valid page image (placeholders are typically small)
GB_MIN_VALID_IMAGE_SIZE = 15000  # 15KB - real scanned pages are usually larger

def _is_placeholder_image(filepath: str) -> bool:
    """Check if a downloaded image is a Google Books placeholder.
    
    Google Books returns a placeholder image instead of 404 for unavailable pages.
    These placeholders have consistent file sizes and content hashes.
    
    Args:
        filepath: Path to the downloaded image file
        
    Returns:
        True if the file appears to be a placeholder image
    """
    try:
        file_size = os.path.getsize(filepath)
        
        # Quick check: if file is very small, likely a placeholder
        if file_size < GB_MIN_VALID_IMAGE_SIZE:
            # Compute MD5 hash for verification
            with open(filepath, "rb") as f:
                file_hash = hashlib.md5(f.read()).hexdigest().lower()
            
            # Check against known placeholder signatures
            for sig_size, sig_hash in GB_PLACEHOLDER_SIGNATURES:
                if file_size == sig_size:
                    if sig_hash is None or file_hash == sig_hash:
                        return True
            
            # Also flag very small images as suspicious
            if file_size < 10000:  # Less than 10KB is almost certainly a placeholder
                logger.debug("Google Books: Detected small image (%d bytes), likely placeholder: %s", 
                           file_size, filepath)
                return True
        
        return False
    except Exception as e:
        logger.debug("Error checking placeholder status for %s: %s", filepath, e)
        return False

def _download_page_images(volume_id: str, output_folder: str, max_pages: int = 50) -> bool:
    """Attempt to download page images from Google Books for full-view books.
    
    For public domain books with full view, Google Books allows accessing
    individual page images via the books/content endpoint with pg parameter.
    
    This function detects and rejects placeholder images that Google Books
    returns for unavailable pages (instead of 404 errors).
    
    Args:
        volume_id: Google Books volume ID
        output_folder: Target directory
        max_pages: Maximum number of pages to download
        
    Returns:
        True if any valid (non-placeholder) pages were downloaded
    """
    valid_pages_downloaded = 0
    placeholder_count = 0
    consecutive_placeholders = 0
    max_consecutive_placeholders = 3  # Stop if 3 consecutive placeholders
    consecutive_failures = 0
    max_consecutive_failures = 5  # Stop after 5 consecutive failures
    
    for page_num in range(1, max_pages + 1):
        if consecutive_failures >= max_consecutive_failures:
            logger.info("Google Books: Stopping page extraction after %d consecutive failures", max_consecutive_failures)
            break
        
        if consecutive_placeholders >= max_consecutive_placeholders:
            logger.info("Google Books: Stopping page extraction after %d consecutive placeholder images for %s", 
                       max_consecutive_placeholders, volume_id)
            break
        
        # Try different page URL formats
        # Format 1: PA{n} for page number
        # Format 2: PT{n} for page type (used in some books)
        page_urls = [
            f"https://books.google.com/books/content?id={urllib.parse.quote(volume_id)}&pg=PA{page_num}&img=1&zoom=3",
            f"https://books.google.com/books/content?id={urllib.parse.quote(volume_id)}&pg=PT{page_num}&img=1&zoom=3",
        ]
        
        page_downloaded = False
        for url in page_urls:
            filename = f"google_{volume_id}_page_{page_num:04d}.jpg"
            downloaded_path = download_file(url, output_folder, filename)
            
            if downloaded_path:
                # Check if it's a placeholder image
                if _is_placeholder_image(downloaded_path):
                    logger.debug("Google Books: Detected placeholder image for page %d, removing: %s", 
                               page_num, downloaded_path)
                    try:
                        os.remove(downloaded_path)
                    except Exception:
                        pass
                    placeholder_count += 1
                    consecutive_placeholders += 1
                    # Don't count as downloaded, but don't count as failure either
                    page_downloaded = True  # Mark as "attempted" to avoid trying alternate URL
                    break
                else:
                    # Valid page image
                    valid_pages_downloaded += 1
                    consecutive_placeholders = 0
                    consecutive_failures = 0
                    page_downloaded = True
                    break
        
        if not page_downloaded:
            consecutive_failures += 1
    
    if valid_pages_downloaded > 0:
        logger.info("Google Books: Downloaded %d valid page images for %s (rejected %d placeholders)", 
                   valid_pages_downloaded, volume_id, placeholder_count)
    elif placeholder_count > 0:
        logger.warning("Google Books: All %d downloaded images were placeholders for %s - book may not have full preview",
                      placeholder_count, volume_id)
    
    return valid_pages_downloaded > 0
