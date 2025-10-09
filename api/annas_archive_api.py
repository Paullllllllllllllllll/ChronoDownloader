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
import urllib.parse
from typing import List, Union, Optional

from bs4 import BeautifulSoup

from .utils import (
    save_json,
    download_file,
    make_request,
    get_max_pages,
    prefer_pdf_over_images,
    get_provider_setting,
)
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
                    
                    # Title is in cell index 1 (second cell)
                    title_text = None
                    if len(cells) > 1:
                        title_cell = cells[1]
                        # Get title from first MD5 link within the cell (primary title)
                        title_links = title_cell.find_all('a', href=lambda x: x and '/md5/' in x)
                        if title_links:
                            # Use the first link's text as primary title
                            # Get only direct text, not from nested elements
                            first_link = title_links[0]
                            # Try to get cleaner text by looking at direct string children
                            if first_link.string:
                                title_text = first_link.string.strip()
                            else:
                                title_text = first_link.get_text(separator=' ', strip=True)
                        
                        # If still empty or looks bad, try cell text but split on common delimiters
                        if not title_text or len(title_text) < 5:
                            full_text = title_cell.get_text(separator='|||', strip=True)
                            # Split on delimiter and take first part
                            parts = full_text.split('|||')
                            if parts:
                                title_text = parts[0].strip()
                    
                    # Clean up title - Anna's Archive often concatenates multiple title variants
                    if title_text and len(title_text) > 3:
                        # Strategy: Take first title variant before repetition or unreasonable length
                        # Look for signs of concatenation: year patterns, repeated words
                        
                        # If we see year patterns like "2016" appearing multiple times, split there
                        import re
                        year_pattern = r'\b(19|20)\d{2}\b'
                        years = list(re.finditer(year_pattern, title_text))
                        if len(years) >= 2:
                            # Multiple years suggest multiple editions concatenated
                            # Take text up to second year
                            second_year_pos = years[1].start()
                            if second_year_pos > 20:  # Ensure we keep reasonable amount
                                title_text = title_text[:second_year_pos].strip()
                        
                        # Remove trailing edition info in parentheses
                        if title_text.count('(') > 1 and title_text.endswith(')'):
                            last_paren = title_text.rfind('(')
                            if last_paren > 20:
                                potential = title_text[:last_paren].strip()
                                if len(potential) > 15:
                                    title_text = potential
                        
                        # Hard limit at 100 characters for clean display
                        if len(title_text) > 100:
                            title_text = title_text[:100].strip()
                            # Try to end at a word boundary
                            if ' ' in title_text[80:]:
                                last_space = title_text.rfind(' ', 80)
                                if last_space > 40:
                                    title_text = title_text[:last_space].strip()
                    
                    if not title_text or len(title_text) < 3:
                        title_text = f"Book {md5[:8]}"
                    
                    raw = {
                        "title": title_text,
                        "creator": creator or "N/A",
                        "md5": md5,
                        "id": md5,
                        "item_url": f"https://annas-archive.org/md5/{md5}",
                    }
                    
                    sr = convert_to_searchresult("Anna's Archive", raw)
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
                                
                                # Extract title - try multiple approaches
                                title_text = link.get_text(strip=True)
                                
                                # Don't use MD5 as title
                                if not title_text or title_text == md5 or len(title_text) < 3:
                                    # Look in parent for better title
                                    parent = link.find_parent()
                                    if parent:
                                        # Try to find any text element that looks like a title
                                        for elem in parent.find_all(['div', 'span', 'h1', 'h2', 'h3', 'h4', 'p']):
                                            text = elem.get_text(strip=True)
                                            if text and text != md5 and len(text) > 3 and text != title_text:
                                                title_text = text
                                                break
                                
                                # Final fallback
                                if not title_text or title_text == md5:
                                    title_text = f"Book {md5[:8]}"
                                
                                raw = {
                                    "title": title_text,
                                    "creator": creator or "N/A",
                                    "md5": md5,
                                    "id": md5,
                                    "item_url": f"https://annas-archive.org/md5/{md5}",
                                }
                                
                                sr = convert_to_searchresult("Anna's Archive", raw)
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
