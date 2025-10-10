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
    * Logs quota information and remaining downloads
    * Provides specific error messages (quota, invalid key, invalid MD5)
  - Without API key: Scrapes download links from MD5 page

API Key Configuration:
  Set ANNAS_ARCHIVE_API_KEY environment variable for member fast downloads.
  
Note:
  Title extraction from search results may be limited due to HTML structure.
  MD5 hashes serve as reliable identifiers even when titles are incomplete.
"""

import logging
import os
import re
import urllib.parse
from typing import Dict, List, Tuple, Union, Optional

from bs4 import BeautifulSoup

from .utils import (
    save_json,
    download_file,
    make_request,
    get_max_pages,
    prefer_pdf_over_images,
    get_provider_setting,
)
from .matching import title_score, normalize_text
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

SEARCH_URL = "https://annas-archive.org/search"
MD5_PAGE_URL = "https://annas-archive.org/md5/{md5}"
FAST_DOWNLOAD_API_URL = "https://annas-archive.org/dyn/api/fast_download.json"

# Mirror list for fallback
MIRRORS = [
    "https://annas-archive.org",
    "https://annas-archive.se",
    "https://annas-archive.li",
]


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


def _collect_title_candidates(texts: List[str]) -> List[str]:
    """Collect unique, cleaned title candidates from raw text snippets."""
    candidates: List[str] = []
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


def _extract_title_candidates(title_cell) -> List[str]:
    """Extract potential title strings from the table title cell."""
    if not title_cell:
        return []

    snippets: List[str] = []

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


def _extract_creators_from_cell(creator_cell) -> List[str]:
    """Extract creator names from the creators cell."""
    if not creator_cell:
        return []

    raw_text = creator_cell.get_text(separator=";", strip=True)
    if not raw_text:
        return []

    creators: List[str] = []
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


def _select_best_title(query_title: str, candidates: List[str]) -> Tuple[Optional[str], Dict[str, int]]:
    """Select the best-matching title candidate and return detailed scores."""
    best_title: Optional[str] = None
    best_scores = {"token": 0, "simple": 0, "combined": 0}

    for candidate in candidates:
        token_score = title_score(query_title, candidate, method="token_set")
        simple_score = title_score(query_title, candidate, method="simple")
        combined = max(token_score, simple_score)

        if combined > best_scores["combined"]:
            best_title = candidate
            best_scores = {"token": token_score, "simple": simple_score, "combined": combined}

    return best_title, best_scores


def _get_api_key() -> Optional[str]:
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


def search_annas_archive(title: str, creator: str | None = None, max_results: int = 3) -> List[SearchResult]:
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
    
    results: List[SearchResult] = []
    
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
                    
                    # Extract MD5 from URL
                    parts = href.split("/md5/")
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

                    title_candidates: List[str] = []
                    extracted_creators: List[str] = []
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

                href = link.get("href", "")

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

                                snippets = [link.get_text(" ", strip=True), link.get("title", "")]

                                parent = link.find_parent()
                                if parent:
                                    snippets.append(parent.get_text(" ", strip=True))
                                    for elem in parent.find_all(['div', 'span', 'h1', 'h2', 'h3', 'h4', 'p']):
                                        snippets.append(elem.get_text(" ", strip=True))

                                title_candidates = _collect_title_candidates(snippets)
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


def download_annas_archive_work(item_data: Union[SearchResult, dict], output_folder: str) -> bool:
    """Download a work from Anna's Archive.
    
    Anna's Archive aggregates files from multiple sources. This function:
    1. If API key is available: Uses fast download API for direct downloads (member feature)
    2. Otherwise: Fetches the MD5 page to scrape download links
    3. Attempts to download from available mirrors
    4. Saves metadata about the file
    
    API Key: Set ANNAS_ARCHIVE_API_KEY environment variable or configure in provider_settings
    
    Args:
        item_data: SearchResult or dict containing item metadata with MD5
        output_folder: Target directory for downloads
        
    Returns:
        True if any content was downloaded, False otherwise
    """
    md5 = None
    if isinstance(item_data, SearchResult):
        md5 = item_data.source_id or item_data.raw.get("md5") or item_data.raw.get("id")
    else:
        md5 = item_data.get("md5") or item_data.get("id")
    
    if not md5:
        logger.warning("No MD5 hash found in item data for Anna's Archive")
        return False
    
    # Check if we have an API key for fast downloads
    api_key = _get_api_key()
    
    if api_key:
        logger.info("Anna's Archive: Using fast download API (member)")
        return _download_with_api(md5, api_key, output_folder)
    else:
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
        
        if not response:
            logger.warning("No response from Anna's Archive fast download API")
            return False
        
        # Check for errors (invalid MD5, invalid key, quota exceeded, etc.)
        if response.get("error"):
            error_msg = response.get("error")
            logger.warning("Anna's Archive API error: %s", error_msg)
            
            # Specific error handling
            if "quota" in error_msg.lower() or "limit" in error_msg.lower():
                logger.error("Download quota reached. Wait for daily reset.")
            elif "invalid key" in error_msg.lower():
                logger.error("Invalid API key. Check ANNAS_ARCHIVE_API_KEY configuration.")
            elif "invalid md5" in error_msg.lower():
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
    
    # Look for metadata fields (these vary on Anna's Archive pages)
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            key = dt.get_text(strip=True).lower().replace(":", "")
            value = dd.get_text(strip=True)
            metadata[key] = value
    
    # Save metadata
    save_json(metadata, output_folder, f"annas_{md5}_metadata")
    
    # Look for download links
    # Anna's Archive typically has multiple download options from different sources
    download_links = []
    
    # Find all links that might be downloads
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        text = link.get_text(strip=True).lower()
        
        # Look for download-related links
        # Common patterns: "download", "fast download", mirror links
        if any(keyword in text for keyword in ["download", "mirror", "libgen", "z-library", "zlibrary"]):
            # Skip member-only fast downloads (requires API key)
            if "fast_download" in href and "member" in text.lower():
                logger.info("Skipping member-only fast download (requires API key)")
                continue
            
            # Collect potential download URLs
            if href.startswith("http"):
                download_links.append({
                    "url": href,
                    "source": text,
                })
            elif href.startswith("/"):
                download_links.append({
                    "url": f"https://annas-archive.org{href}",
                    "source": text,
                })
    
    # Try to download from available links
    # Prioritize direct file links over page links
    max_downloads = 1 if prefer_pdf_over_images() else 3
    downloaded_count = 0
    
    for idx, link_info in enumerate(download_links):
        if downloaded_count >= max_downloads:
            break
        
        url = link_info["url"]
        source = link_info["source"]
        
        logger.info("Attempting download from Anna's Archive source: %s", source)
        
        # Try to download the file
        # Note: Some links may redirect to external mirrors
        try:
            filename = f"annas_{md5}_content_{idx+1}"
            if download_file(url, output_folder, filename):
                any_downloaded = True
                downloaded_count += 1
                logger.info("Successfully downloaded from source: %s", source)
                
                # If we prefer PDFs and got one, stop here
                if prefer_pdf_over_images() and downloaded_count >= 1:
                    break
        except Exception as e:
            logger.debug("Failed to download from %s: %s", url, e)
            continue
    
    if not any_downloaded:
        logger.warning("No files could be downloaded for Anna's Archive MD5: %s", md5)
        logger.info("You may need to visit the page manually: %s", md5_page_url)
    
    return any_downloaded
