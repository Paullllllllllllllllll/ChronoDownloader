"""Connector for Anna's Archive.

Anna's Archive is a comprehensive shadow library aggregating books from
Library Genesis, Z-Library, Internet Archive, and other sources.

Search:
  - Attempts table display format for structured parsing (display=table)
  - Falls back to standard HTML scraping if table not available
  - Extracts MD5 hashes as primary identifiers

Download:
  - With API key (member): Uses fast download API for direct downloads
    * Handles HTTP 200 (success), 204 (no fast download), and errors
    * Provides specific error messages (quota, invalid key, invalid MD5)
    * Falls back to public scraping when the fast API returns no URL
  - Without API key: Scrapes download links from the MD5 page

This module is deliberately quota-agnostic. Pre-flight quota checks and
post-success consumption tracking are the responsibility of the
orchestration layer (see :mod:`main.pipeline`), keeping the provider free
of any dependency on main/.

API Key Configuration:
  Set ANNAS_ARCHIVE_API_KEY environment variable for member fast downloads.

Note:
  Title extraction from search results may be limited due to HTML structure.
  MD5 hashes serve as reliable identifiers even when titles are incomplete.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from bs4 import BeautifulSoup

from ..core.config import get_provider_setting
from ..core.download import download_file, save_json
from ..core.network import make_request
from ..matching import normalize_text, title_score
from ..model import QuotaDeferredException, SearchResult, convert_to_searchresult, resolve_item_id

logger = logging.getLogger(__name__)

SEARCH_URL = "https://annas-archive.gl/search"
MD5_PAGE_URL = "https://annas-archive.gl/md5/{md5}"
FAST_DOWNLOAD_API_URL = "https://annas-archive.gl/dyn/api/fast_download.json"

# Mirror list for fallback (updated Mar 2026: .gl is the active domain)
MIRRORS = [
    "https://annas-archive.gl",   # primary (active as of Mar 2026)
    "https://annas-archive.li",
    "https://annas-archive.pm",
    "https://annas-archive.in",
]

def is_api_backed() -> bool:
    """Return True when a member API key is configured for fast downloads.

    The orchestration layer uses this to decide whether quota tracking
    applies to a given download attempt.
    """
    return bool(_get_api_key())

def _clean_title_candidate(text: str) -> str:
    """Normalize spacing and trim overly long or concatenated titles."""
    if not text:
        return ""

    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""

    # If we see multiple year patterns, keep text up to the second year
    year_pattern = r"\b(19|20)\d{2}\b"
    years = list(re.finditer(year_pattern, cleaned))
    if len(years) >= 2:
        second_year_pos = years[1].start()
        if second_year_pos > 20:
            cleaned = cleaned[:second_year_pos].strip()

    # Remove trailing edition info in parentheses when repeated
    if cleaned.count("(") > 1 and cleaned.endswith(")"):
        last_paren = cleaned.rfind("(")
        if last_paren > 20:
            potential = cleaned[:last_paren].strip()
            if len(potential) > 15:
                cleaned = potential

    # Trim trailing punctuation and separators
    cleaned = cleaned.rstrip(" -:;|/")

    if len(cleaned) > 100:
        truncated = cleaned[:100].strip()
        last_space = truncated.rfind(" ")
        if last_space > 40:
            truncated = truncated[:last_space].strip()
        cleaned = truncated

    return cleaned

def _collect_title_candidates(texts: list[str]) -> list[str]:
    """Collect unique, cleaned title candidates from raw text snippets."""
    candidates: list[str] = []
    seen_norm = set()

    for raw_text in texts:
        if not raw_text:
            continue
        cleaned_raw = re.sub(r"\s+", " ", raw_text).strip()
        if not cleaned_raw:
            continue

        norm = normalize_text(cleaned_raw)
        if not norm or len(norm) < 3:
            continue

        # Skip MD5 hashes or obvious non-title tokens
        if len(norm) == 32 and all(c in "0123456789abcdef" for c in norm):
            continue
        if norm in {"download", "download options", "download mirror"}:
            continue

        if norm in seen_norm:
            continue
        seen_norm.add(norm)

        candidate = _clean_title_candidate(cleaned_raw)
        if candidate:
            candidates.append(candidate)

    return candidates

def _extract_title_candidates(title_cell: Any) -> list[str]:
    """Extract potential title strings from the table title cell."""
    if not title_cell:
        return []

    snippets: list[str] = []

    # Anchor texts pointing to MD5 pages are strong signals
    for link in title_cell.find_all("a", href=lambda x: x and "/md5/" in x):
        snippets.append(link.get_text(" ", strip=True))

    # Full cell text with various separators
    full_text = title_cell.get_text(separator="\n", strip=True)
    if full_text:
        snippets.append(full_text)
        for part in re.split(r"[\n\r]+", full_text):
            snippets.append(part)
            for sub in re.split(r"\s*[\|•·;︰：/]+\s*", part):
                snippets.append(sub)

    # Some pages use custom delimiters like '|||'
    if full_text and "|||" in full_text:
        snippets.extend(full_text.split("|||"))

    return _collect_title_candidates(snippets)

def _extract_creators_from_cell(creator_cell: Any) -> list[str]:
    """Extract creator names from the creators cell."""
    if not creator_cell:
        return []

    raw_text = creator_cell.get_text(separator=";", strip=True)
    if not raw_text:
        return []

    creators: list[str] = []
    seen_norm = set()
    for part in re.split(r"[,;/\|]+", raw_text):
        candidate = re.sub(r"\s+", " ", part).strip()
        if not candidate or len(candidate) < 2:
            continue
        norm = normalize_text(candidate)
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        creators.append(candidate)

    return creators

def _select_best_title(query_title: str, candidates: list[str]) -> tuple[str | None, dict[str, int]]:
    """Select the best-matching title candidate and return detailed scores."""
    best_title: str | None = None
    best_scores = {"token": 0, "simple": 0, "combined": 0}

    for candidate in candidates:
        token_score = title_score(query_title, candidate, method="token_set")
        simple_score = title_score(query_title, candidate, method="simple")
        combined = max(token_score, simple_score)

        if combined > best_scores["combined"]:
            best_title = candidate
            best_scores = {"token": token_score, "simple": simple_score, "combined": combined}

    return best_title, best_scores

def _get_api_key() -> str | None:
    """Get Anna's Archive API key from environment or config.
    
    Returns:
        API key string or None if not configured
    """
    # Try environment variable first
    api_key = os.environ.get("ANNAS_ARCHIVE_API_KEY")
    if api_key:
        return api_key
    
    # Try config
    try:
        api_key = get_provider_setting("annas_archive", "api_key")
        if api_key:
            return str(api_key)
    except Exception:
        pass
    
    return None

def search_annas_archive(title: str, creator: str | None = None, max_results: int = 3) -> list[SearchResult]:
    """Search Anna's Archive by scraping the public search page.
    
    Uses table display format for cleaner, more structured results.
    
    Args:
        title: Work title to search for
        creator: Optional creator/author name
        max_results: Maximum number of results to return
        
    Returns:
        List of SearchResult objects
    """
    query = title if not creator else f"{title} {creator}"
    # Use table display format for cleaner parsing
    params = {
        "q": query,
        "display": "table",
        "ext": "pdf",  # Prioritize PDFs
    }
    
    logger.info("Searching Anna's Archive for: %s", title)
    
    # Make request to search page with table display
    html = make_request(SEARCH_URL, params=params)
    
    results: list[SearchResult] = []
    
    if isinstance(html, str):
        soup = BeautifulSoup(html, "html.parser")
        
        # Table display format: parse <tr> rows for clean extraction
        seen_md5s = set()
        
        # Find all table rows (skip header row)
        table_rows = soup.find_all('tr')
        
        if table_rows and len(table_rows) > 1:
            # Skip header row, process data rows
            for row in table_rows[1:]:
                if len(results) >= max_results:
                    break
                
                try:
                    # Find MD5 link in row
                    md5_link = row.find("a", href=lambda x: x and "/md5/" in x)
                    if not md5_link:
                        continue
                    
                    href = md5_link.get("href", "")
                    href_str = str(href) if href else ""
                    
                    # Extract MD5 from URL
                    parts = href_str.split("/md5/")
                    if len(parts) < 2:
                        continue
                    
                    md5_part = parts[1].split("?")[0].split("/")[0]
                    if len(md5_part) != 32 or not all(c in "0123456789abcdefABCDEF" for c in md5_part):
                        continue
                    
                    md5 = md5_part.lower()
                    
                    if md5 in seen_md5s:
                        continue
                    seen_md5s.add(md5)
                    
                    # Extract metadata from table cells
                    # Table structure: [icons, title, authors, publisher, year, filename]
                    cells = row.find_all(['td', 'th'])

                    title_candidates: list[str] = []
                    extracted_creators: list[str] = []
                    title_scores = {"token": 0, "simple": 0, "combined": 0}

                    if len(cells) > 1:
                        title_candidates = _extract_title_candidates(cells[1])

                    if len(cells) > 2:
                        extracted_creators = _extract_creators_from_cell(cells[2])

                    best_title, title_scores = _select_best_title(title, title_candidates)

                    if not best_title and title_candidates:
                        best_title = title_candidates[0]

                    if not best_title or len(best_title) < 3:
                        best_title = f"Book {md5[:8]}"

                    raw = {
                        "title": best_title,
                        "creators": extracted_creators,
                        "creator": creator or "N/A",
                        "md5": md5,
                        "id": md5,
                        "item_url": f"https://annas-archive.org/md5/{md5}",
                        "title_candidates": title_candidates,
                        "title_scores": title_scores,
                    }
                    
                    sr = convert_to_searchresult("Anna's Archive", raw)
                    sr.raw.setdefault("__matching__", {}).update({
                        "title_token_score": title_scores.get("token", 0),
                        "title_simple_score": title_scores.get("simple", 0),
                    })
                    if extracted_creators:
                        sr.creators = extracted_creators
                    results.append(sr)
                    
                except Exception as e:
                    logger.debug("Error parsing Anna's Archive table row: %s", e)
                    continue
        
        # Strategy 2: If no structured containers found, fall back to scanning all MD5 links
        if not results:
            for link in soup.find_all("a", href=True):
                if len(results) >= max_results:
                    break

                href_attr = link.get("href", "")
                href = str(href_attr) if href_attr else ""

                # Match MD5 links: /md5/<32-char-hex>
                if "/md5/" in href:
                    try:
                        # Extract MD5 from URL
                        parts = href.split("/md5/")
                        if len(parts) > 1:
                            md5_part = parts[1].split("?")[0].split("/")[0]
                            # Validate MD5 format (32 hex characters)
                            if len(md5_part) == 32 and all(c in "0123456789abcdefABCDEF" for c in md5_part):
                                md5 = md5_part.lower()

                                if md5 in seen_md5s:
                                    continue
                                seen_md5s.add(md5)

                                title_attr = link.get("title", "")
                                snippets = [link.get_text(" ", strip=True), str(title_attr) if title_attr else ""]

                                parent = link.find_parent()
                                if parent:
                                    snippets.append(parent.get_text(" ", strip=True))
                                    for elem in parent.find_all(['div', 'span', 'h1', 'h2', 'h3', 'h4', 'p']):
                                        snippets.append(elem.get_text(" ", strip=True))

                                title_candidates = _collect_title_candidates([str(s) for s in snippets if s])
                                best_title, title_scores = _select_best_title(title, title_candidates)

                                if not best_title and title_candidates:
                                    best_title = title_candidates[0]

                                if not best_title or best_title == md5 or len(best_title) < 3:
                                    best_title = f"Book {md5[:8]}"

                                raw = {
                                    "title": best_title,
                                    "creator": creator or "N/A",
                                    "creators": [],
                                    "md5": md5,
                                    "id": md5,
                                    "item_url": f"https://annas-archive.org/md5/{md5}",
                                    "title_candidates": title_candidates,
                                    "title_scores": title_scores,
                                }

                                sr = convert_to_searchresult("Anna's Archive", raw)
                                sr.raw.setdefault("__matching__", {}).update({
                                    "title_token_score": title_scores.get("token", 0),
                                    "title_simple_score": title_scores.get("simple", 0),
                                })
                                results.append(sr)

                    except Exception as e:
                        logger.debug("Error parsing Anna's Archive link %s: %s", href, e)
                        continue
    
    logger.info("Found %d results from Anna's Archive", len(results))
    return results

def download_annas_archive_work(
    item_data: SearchResult | dict[str, Any], output_folder: str
) -> bool:
    """Download a work from Anna's Archive.

    Args:
        item_data: SearchResult or dict containing item metadata with MD5
        output_folder: Target directory for downloads

    Returns:
        True if any content was downloaded, False otherwise

    The orchestration layer is responsible for pre-flight quota checks and
    for recording quota consumption after a successful fast-download. This
    function is deliberately quota-agnostic: when a member API key is
    configured it attempts the fast download first and falls back to public
    scraping if the API has no URL to offer.
    """
    md5 = resolve_item_id(item_data, "md5", "id")

    if not md5:
        logger.warning("No MD5 hash found in item data for Anna's Archive")
        return False

    api_key = _get_api_key()

    if api_key:
        logger.info("Anna's Archive: Using fast download API (member)")
        if _download_with_api(md5, api_key, output_folder):
            return True
        logger.info(
            "Anna's Archive: Fast download API unavailable; falling back to public scraping"
        )
        return _download_via_scraping(md5, output_folder)

    logger.info("Anna's Archive: Using public download scraping (no API key)")
    return _download_via_scraping(md5, output_folder)

def _download_with_api(md5: str, api_key: str, output_folder: str) -> bool:
    """Download using Anna's Archive fast download API (requires membership).
    
    Handles proper HTTP status codes:
    - 200: Success with download_url
    - 204: Valid request but no fast download available (fallback needed)
    - Other: Error with error field
    
    Args:
        md5: MD5 hash of the file
        api_key: Member API key
        output_folder: Target directory for downloads
        
    Returns:
        True if download succeeded, False otherwise
    """
    params = {
        "md5": md5,
        "key": api_key,
        # Optional: path_index and domain_index can be added here
    }
    
    logger.info("Fetching fast download URL for MD5: %s", md5)
    
    try:
        response = make_request(FAST_DOWNLOAD_API_URL, params=params)
        
        if not isinstance(response, dict):
            logger.warning("No response from Anna's Archive fast download API")
            return False
        
        # Check for errors (invalid MD5, invalid key, quota exceeded, etc.)
        if response.get("error"):
            error_msg = str(response.get("error") or "")
            logger.warning("Anna's Archive API error: %s", error_msg)
            
            # Specific error handling
            error_lower = error_msg.lower() if error_msg else ""
            if "quota" in error_lower or "limit" in error_lower:
                logger.error("Download quota reached. Wait for daily reset.")
            elif "invalid key" in error_lower:
                logger.error("Invalid API key. Check ANNAS_ARCHIVE_API_KEY configuration.")
            elif "invalid md5" in error_lower:
                logger.error("Invalid MD5 hash: %s", md5)
            
            return False
        
        # Get download URL
        download_url = response.get("download_url")
        
        # Handle 204 No Content case - valid request but no fast download
        if not download_url:
            logger.info("No fast download URL available (HTTP 204). File may require fallback download.")
            # Save API response showing quota info even without download
            if response.get("account_fast_download_info"):
                save_json(response, output_folder, f"annas_{md5}_api_response")
            return False
        
        # Save API response as metadata (includes quota info)
        save_json(response, output_folder, f"annas_{md5}_api_response")
        
        # Log quota information if available
        quota_info = response.get("account_fast_download_info")
        if quota_info:
            logger.info("Fast downloads remaining today: %s", quota_info.get("remaining", "unknown"))
        
        # Download the file
        logger.info("Downloading from fast download URL")
        filename = f"annas_{md5}_content"
        
        if download_file(download_url, output_folder, filename):
            logger.info("Successfully downloaded via Anna's Archive fast download API")
            return True
        else:
            logger.warning("Failed to download from fast download URL")
            return False
            
    except Exception as e:
        logger.exception("Error using Anna's Archive fast download API: %s", e)
        return False

def _download_via_scraping(md5: str, output_folder: str) -> bool:
    """Download by scraping the MD5 page for download links.
    
    Anna's Archive provides multiple download options:
    - Direct file links (best - end in .pdf, .epub, etc.)
    - Mirror page links (require additional navigation)
    - Slow download links (rate-limited but usually work)
    
    Args:
        md5: MD5 hash of the file
        output_folder: Target directory for downloads
        
    Returns:
        True if any content was downloaded, False otherwise
    """
    
    # Fetch the MD5 page to get download links
    md5_page_url = MD5_PAGE_URL.format(md5=md5)
    logger.info("Fetching Anna's Archive MD5 page: %s", md5_page_url)
    
    html = make_request(md5_page_url)
    
    if not html or not isinstance(html, str):
        logger.warning("Failed to fetch Anna's Archive MD5 page for %s", md5)
        return False
    
    any_downloaded = False
    
    # Parse the page to extract metadata and download links
    soup = BeautifulSoup(html, "html.parser")
    
    # Try to extract metadata
    metadata = {
        "md5": md5,
        "url": md5_page_url,
    }
    
    # Look for title
    title_elem = soup.find("h1") or soup.find("div", class_="text-xl")
    if title_elem:
        metadata["title"] = title_elem.get_text(strip=True)
    
    # Look for file extension from page
    file_ext = ".pdf"  # Default
    for text_elem in soup.find_all(text=True):
        text_lower = str(text_elem).lower()
        if ".epub" in text_lower:
            file_ext = ".epub"
            break
        elif ".djvu" in text_lower:
            file_ext = ".djvu"
            break
    
    # Save metadata
    save_json(metadata, output_folder, f"annas_{md5}_metadata")
    
    # Categorize download links by priority
    direct_file_links = []  # Links that end in file extensions
    slow_download_links = []  # Anna's Archive slow download
    mirror_links = []  # External mirror pages
    
    # File extensions to look for in URLs
    file_extensions = ('.pdf', '.epub', '.djvu', '.mobi', '.azw', '.azw3', '.fb2')
    
    # Find all links
    for link in soup.find_all("a", href=True):
        href_val = link.get("href", "")
        href = str(href_val).strip() if href_val else ""
        text = link.get_text(strip=True).lower()
        
        if not href:
            continue
        
        # Skip member-only fast downloads
        if "fast_download" in href or "member" in text:
            continue
        
        # Skip non-download links
        if any(skip in href.lower() for skip in [
            "account", "login", "register", "donate", "torrents", 
            "datasets", "blog", "about", "faq", "#"
        ]):
            continue
        
        # Build full URL
        full_url = href
        if href.startswith("/"):
            full_url = f"https://annas-archive.li{href}"
        elif not href.startswith("http"):
            continue
        
        # Check if this is a direct file link
        url_lower = full_url.lower()
        is_direct_file = any(url_lower.endswith(ext) or f"{ext}?" in url_lower for ext in file_extensions)
        
        # Check if this is the slow download link
        if "slow_download" in href or "slow download" in text:
            slow_download_links.append({"url": full_url, "source": text or "slow download"})
        elif is_direct_file:
            direct_file_links.append({"url": full_url, "source": text or "direct file"})
        elif any(mirror in url_lower for mirror in ["libgen", "library.lol", "z-lib", "zlibrary", "ipfs"]):
            # These are mirror page links - deprioritize
            mirror_links.append({"url": full_url, "source": text or "mirror"})
    
    # Also look for download links in JavaScript or data attributes
    for script in soup.find_all("script"):
        script_text = script.string or ""
        # Look for direct CDN/file URLs in scripts
        for match in re.finditer(r'https?://[^"\s<>]+(?:\.pdf|\.epub|\.djvu|\.mobi)[^"\s<>]*', script_text):
            url = match.group(0).rstrip(',\"\');')
            if url not in [l["url"] for l in direct_file_links]:
                direct_file_links.append({"url": url, "source": "script"})
    
    # Prioritized download attempts
    all_links = direct_file_links + slow_download_links + mirror_links
    
    if not all_links:
        logger.warning("No download links found on Anna's Archive page for %s", md5)
        logger.info("You may need to visit the page manually: %s", md5_page_url)
        return False
    
    logger.info("Anna's Archive: Found %d direct, %d slow, %d mirror links for %s",
                len(direct_file_links), len(slow_download_links), len(mirror_links), md5)
    
    max_attempts = 5  # Try up to 5 different links
    attempts = 0
    
    for link_info in all_links:
        if any_downloaded or attempts >= max_attempts:
            break
        
        attempts += 1
        url = link_info["url"]
        source = link_info["source"]
        
        logger.info("Attempting download from Anna's Archive source: %s", source)
        
        try:
            filename = f"annas_{md5}_content"
            result = download_file(url, output_folder, filename)
            if result:
                any_downloaded = True
                logger.info("Successfully downloaded from Anna's Archive source: %s", source)
                break
        except Exception as e:
            logger.debug("Failed to download from %s: %s", url, e)
            continue
    
    if not any_downloaded:
        logger.warning("No files could be downloaded for Anna's Archive MD5: %s", md5)
        logger.info("Available mirrors may require manual access: %s", md5_page_url)
    
    return any_downloaded
