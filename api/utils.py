"""Core utilities for ChronoDownloader API.

This module serves as a backward-compatible façade, re-exporting functionality
from the modular api.core package while maintaining the original public API.

New code should import directly from api.core submodules for clarity.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

# Re-export core module functionality for backward compatibility
from .core.budget import (
    DownloadBudget as _DownloadBudget,
    budget_exhausted,
    get_budget,
)
from .core.config import (
    get_config,
    get_download_config,
    get_download_limits,
    get_max_pages,
    get_network_config,
    get_provider_setting,
    get_resume_mode,
    include_metadata,
    overwrite_existing,
    prefer_pdf_over_images,
)
from .core.context import (
    clear_current_entry,
    clear_current_name_stem,
    clear_current_provider,
    clear_current_work,
    get_counters as _counters,
    get_current_entry as _current_entry_id,
    get_current_name_stem as _current_name_stem,
    get_current_provider as _current_provider_key,
    get_current_work as _current_work_id,
    increment_counter,
    reset_counters,
    set_current_entry,
    set_current_name_stem,
    set_current_provider,
    set_current_work,
)
from .core.naming import (
    PROVIDER_ABBREV as _PROVIDER_ABBREV,
    PROVIDER_SLUGS as _PROVIDER_SLUGS,
    get_provider_slug as _provider_slug,
    sanitize_filename,
    to_snake_case,
)
from .core.network import (
    PROVIDER_HOST_MAP as _PROVIDER_HOST_MAP,
    RateLimiter,
    get_provider_for_url as _provider_for_url,
    get_rate_limiter as _get_rate_limiter,
    get_session,
    make_request,
)

logger = logging.getLogger(__name__)

# Expose budget singleton for backward compatibility
_BUDGET = get_budget()


# Legacy internal helpers for backward compatibility
def _prefer_pdf_over_images() -> bool:
    """DEPRECATED: Use prefer_pdf_over_images() instead."""
    return prefer_pdf_over_images()


def _overwrite_existing() -> bool:
    """DEPRECATED: Use overwrite_existing() instead."""
    return overwrite_existing()


def _include_metadata() -> bool:
    """DEPRECATED: Use include_metadata() instead."""
    return include_metadata()


def download_iiif_renderings(
    manifest: Dict[str, Any], folder_path: str, filename_prefix: str = ""
) -> int:
    """Download files referenced in IIIF manifest-level 'rendering' entries.

    Many IIIF manifests include a top-level 'rendering' array with alternate formats
    such as application/pdf or application/epub+zip. This helper downloads a small
    number of such files according to config:

    config.download:
      - download_manifest_renderings: true|false (default true)
      - rendering_mime_whitelist: ["application/pdf", "application/epub+zip"]
      - max_renderings_per_manifest: 1

    Args:
        manifest: IIIF manifest dictionary
        folder_path: Target directory for downloads
        filename_prefix: Prefix for downloaded filenames

    Returns:
        Number of files successfully downloaded
    """
    dl_cfg = get_download_config()
    
    if not dl_cfg.get("download_manifest_renderings", True):
        return 0
    
    whitelist: List[str] = [
        str(m).lower()
        for m in (dl_cfg.get("rendering_mime_whitelist") or ["application/pdf", "application/epub+zip"])
        if m
    ]
    
    try:
        limit = int(dl_cfg.get("max_renderings_per_manifest", 1) or 1)
    except Exception:
        limit = 1

    def _collect_renderings(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        r = obj.get("rendering")
        if isinstance(r, list):
            for it in r:
                if isinstance(it, dict):
                    items.append(it)
        elif isinstance(r, dict):
            items.append(r)
        return items

    candidates: List[Dict[str, Any]] = _collect_renderings(manifest)

    # Deduplicate by URL
    seen: set[str] = set()
    selected: List[Dict[str, Any]] = []
    for it in candidates:
        url = it.get("@id") or it.get("id")
        fmt = (it.get("format") or it.get("type") or "").lower()
        if not url or not isinstance(url, str):
            continue
        if whitelist and all(w not in fmt for w in whitelist):
            # If server omits format, allow PDFs by URL suffix
            if not any(url.lower().endswith(ext) for ext in (".pdf", ".epub")):
                continue
        if url in seen:
            continue
        seen.add(url)
        selected.append({"url": url, "format": fmt, "label": it.get("label")})
        if len(selected) >= limit:
            break

    count = 0
    for idx, r in enumerate(selected, start=1):
        url = r["url"]
        # Delegate naming to download_file() to ensure standardization
        if download_file(url, folder_path, f"rendering_{idx:02d}"):
            count += 1
    return count


def download_file(url: str, folder_path: str, filename: str) -> Optional[str]:
    """Download a file with centralized rate limiting, retries, and budget enforcement.

    Args:
        url: URL to download
        folder_path: Target directory (will create objects/ subdirectory)
        filename: Base filename (will be standardized with provider prefix and counter)

    Returns:
        Path to downloaded file or None on failure
    """
    os.makedirs(folder_path, exist_ok=True)
    
    session = get_session()
    provider = _provider_for_url(url)
    net = get_network_config(provider)
    
    max_attempts = int(net.get("max_attempts", 5) or 5)
    base_backoff = float(net.get("base_backoff_s", 1.5) or 1.5)
    backoff_mult = float(net.get("backoff_multiplier", 1.5) or 1.5)
    max_backoff = float(net.get("max_backoff_s", 60.0) or 60.0)
    timeout_s = net.get("timeout_s")
    timeout = float(timeout_s) if timeout_s is not None else 30.0
    verify_default = bool(net.get("verify_ssl", True))
    ssl_policy = str(net.get("ssl_error_policy", "fail") or "fail").lower()
    provider_headers = dict(net.get("headers", {}) or {})
    
    # Merge headers: session defaults < provider headers
    req_headers = {}
    if provider_headers:
        req_headers.update({str(k): str(v) for k, v in provider_headers.items() if v is not None})
    
    rl = _get_rate_limiter(provider)
    work_id = _current_work_id()
    
    # If the download budget has already been exhausted globally, avoid any HTTP requests
    if _BUDGET.exhausted():
        logger.warning("Download budget exhausted; skipping %s", url)
        return None
    
    # Pre-check file-count limits so we don't make a request we can't save
    if not _BUDGET.allow_new_file(provider, work_id):
        logger.warning("Download budget (files) exceeded; skipping %s", url)
        return None
    
    try:
        insecure_retry_used = False
        verify = verify_default
        
        for attempt in range(1, max_attempts + 1):
            # Centralized pacing
            if rl:
                rl.wait()
            
            with session.get(url, stream=True, timeout=timeout, verify=verify, headers=req_headers or None) as response:
                # Handle rate-limiting explicitly to respect Retry-After
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    sleep_s = None
                    
                    if retry_after:
                        try:
                            sleep_s = float(retry_after)
                        except ValueError:
                            try:
                                retry_dt = parsedate_to_datetime(retry_after)
                                sleep_s = max(0.0, (retry_dt - datetime.now(retry_dt.tzinfo)).total_seconds())
                            except Exception:
                                sleep_s = None
                    
                    if sleep_s is None:
                        sleep_s = min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)
                    else:
                        sleep_s = min(sleep_s, max_backoff)
                    
                    logger.warning(
                        "429 Too Many Requests for %s; sleeping %.1fs (attempt %d/%d)",
                        url, sleep_s, attempt, max_attempts
                    )
                    import time
                    time.sleep(sleep_s)
                    continue
                
                # Retry transient 5xx
                if response.status_code in (500, 502, 503, 504):
                    sleep_s = min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)
                    logger.warning(
                        "%s for %s; sleeping %.1fs (attempt %d/%d)",
                        response.status_code, url, sleep_s, attempt, max_attempts
                    )
                    import time
                    time.sleep(sleep_s)
                    continue
                
                # Non-retryable client errors
                if response.status_code in (400, 401, 403, 404, 410, 422):
                    logger.error("Non-retryable HTTP %s for %s; aborting download", response.status_code, url)
                    return None
                
                response.raise_for_status()
                
                # Validate Content-Type to prevent saving HTML error pages as PDFs/EPUBs
                content_type = response.headers.get("Content-Type", "").lower()
                from urllib.parse import urlparse
                parsed_url = urlparse(url)
                url_suggests_pdf = ".pdf" in parsed_url.path.lower() or "output=pdf" in url.lower() or "download" in url.lower()
                url_suggests_epub = ".epub" in parsed_url.path.lower() or "output=epub" in url.lower()
                is_annas_archive = "annas-archive" in url.lower()
                
                # Reject HTML when expecting PDF/EPUB
                if "text/html" in content_type:
                    if url_suggests_pdf or url_suggests_epub:
                        logger.warning(
                            "Rejecting download: URL suggests PDF/EPUB but server returned HTML (likely error page): %s",
                            url
                        )
                        return None
                    # Also reject Anna's Archive HTML pages (likely login/error pages) unless explicitly allowed
                    if is_annas_archive:
                        # Check if this is a very small HTML page (likely error/login page)
                        cl_header_check = response.headers.get("Content-Length")
                        try:
                            content_len_check = int(cl_header_check) if cl_header_check else 0
                            # Anna's Archive login/error pages are typically ~180KB
                            if 170000 < content_len_check < 185000:
                                logger.warning(
                                    "Rejecting download: Anna's Archive HTML page likely login/error page (~180KB): %s",
                                    url
                                )
                                return None
                        except Exception:
                            pass
                
                cd_name = _filename_from_content_disposition(response.headers.get("Content-Disposition"))
                
                # Determine extension and type up front
                def _infer_ext() -> str:
                    suffix = Path(parsed_url.path).suffix
                    if suffix:
                        return suffix
                    ct = response.headers.get("Content-Type", "").lower()
                    ct_map = {
                        "application/pdf": ".pdf",
                        "application/epub+zip": ".epub",
                        "image/jpeg": ".jpg",
                        "image/jpg": ".jpg",
                        "image/png": ".png",
                        "image/jp2": ".jp2",
                        "text/plain": ".txt",
                        "text/html": ".html",
                        "application/json": ".json",
                    }
                    for k, v in ct_map.items():
                        if k in ct:
                            return v
                    return ""
                
                # Build standardized filename in 'objects/' directory
                inferred_ext = _infer_ext()
                ext = inferred_ext or Path(cd_name or "").suffix or Path(filename or "").suffix or ""
                if not ext:
                    ext = ".bin"
                ext = ext.lower()

                stem = _current_name_stem() or to_snake_case(filename) or "object"
                prov_slug = _provider_slug(_current_provider_key(), provider)
                
                # Determine type key for numbering
                image_exts = {".jpg", ".jpeg", ".png", ".jp2", ".tif", ".tiff", ".gif", ".bmp", ".webp"}
                if ext in image_exts:
                    type_key = "image"
                else:
                    type_key = ext.lstrip(".") or "bin"

                # Check if extension is allowed in objects folder
                dl_cfg = get_download_config()
                allowed_exts = dl_cfg.get("allowed_object_extensions", [])
                save_disallowed_to_metadata = dl_cfg.get("save_disallowed_to_metadata", True)
                
                # Determine target directory based on extension whitelist
                if allowed_exts and ext not in allowed_exts:
                    if save_disallowed_to_metadata:
                        # Save non-content files (HTML, TXT, etc.) to metadata folder
                        target_dir = os.path.join(folder_path, "metadata")
                        logger.info("Extension %s not in allowed list; saving to metadata folder", ext)
                    else:
                        # Skip download if not allowed and not saving to metadata
                        logger.info("Extension %s not in allowed list; skipping download", ext)
                        return None
                else:
                    # Allowed extension or no whitelist configured - save to objects
                    target_dir = os.path.join(folder_path, "objects")
                
                os.makedirs(target_dir, exist_ok=True)

                # Resolve sequence number using context
                key = (stem, prov_slug, type_key)
                seq = increment_counter(key)

                if type_key == "image":
                    safe_base = f"{stem}_{prov_slug}_image_{seq:03d}"
                else:
                    # For non-image types, only number when more than one exists
                    if seq <= 1:
                        safe_base = f"{stem}_{prov_slug}"
                    else:
                        safe_base = f"{stem}_{prov_slug}_{seq}"

                safe_name = sanitize_filename(f"{safe_base}{ext}")
                filepath = os.path.join(target_dir, safe_name)

                # Respect overwrite setting
                if not overwrite_existing() and os.path.exists(filepath):
                    logger.info("File already exists, skipping: %s", filepath)
                    return filepath

                # Budget pre-checks
                if not _BUDGET.allow_new_file(provider, work_id):
                    logger.warning("Download budget (files) exceeded; skipping %s", url)
                    if _BUDGET.exhausted():
                        return None
                    return None

                # If server advertises Content-Length, ensure it fits before downloading
                cl_header = response.headers.get("Content-Length")
                try:
                    content_len = int(cl_header) if cl_header is not None else 0
                except Exception:
                    content_len = 0
                
                if content_len and not _BUDGET.allow_bytes(provider, work_id, content_len):
                    logger.warning(
                        "Download budget (bytes) would be exceeded by %s (%d bytes); skipping.",
                        url, content_len
                    )
                    return None
                
                with open(filepath, "wb") as f:
                    truncated = False
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        # Enforce byte budget dynamically for unknown sizes
                        if not _BUDGET.add_bytes(provider, work_id, len(chunk)):
                            logger.error(
                                "Download budget exceeded while writing %s; truncating and removing file.",
                                filepath
                            )
                            truncated = True
                            break
                        f.write(chunk)
                
                if truncated:
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                    return None
                
                # Validate file content for PDF/EPUB files to catch misnamed HTML pages
                if ext.lower() in [".pdf", ".epub"]:
                    try:
                        with open(filepath, "rb") as check_f:
                            first_bytes = check_f.read(512)
                            # Check for PDF magic bytes
                            if ext.lower() == ".pdf" and not first_bytes.startswith(b"%PDF-"):
                                # Check if it's HTML instead
                                if b"<!DOCTYPE" in first_bytes or b"<html" in first_bytes.lower():
                                    logger.warning(
                                        "Downloaded file claims to be PDF but contains HTML; removing: %s",
                                        filepath
                                    )
                                    os.remove(filepath)
                                    return None
                            # Check for EPUB (ZIP) magic bytes
                            elif ext.lower() == ".epub" and not first_bytes.startswith(b"PK\x03\x04"):
                                # Check if it's HTML instead
                                if b"<!DOCTYPE" in first_bytes or b"<html" in first_bytes.lower():
                                    logger.warning(
                                        "Downloaded file claims to be EPUB but contains HTML; removing: %s",
                                        filepath
                                    )
                                    os.remove(filepath)
                                    return None
                    except Exception as ve:
                        logger.warning("Error validating downloaded file %s: %s", filepath, ve)
                
                # Validate HTML files to ensure they're not login/error pages
                if ext.lower() == ".html":
                    try:
                        with open(filepath, "r", encoding="utf-8", errors="ignore") as check_f:
                            html_content = check_f.read(2048)  # Read first 2KB
                            # Check for Anna's Archive login/register pages
                            if "annas-archive" in url.lower() or "annas-archive" in provider.lower():
                                if any(marker in html_content.lower() for marker in [
                                    "<title>log in / register",
                                    "<title>login",
                                    "member login",
                                    "please log in",
                                    "__darkreader__",  # Anna's Archive specific JS
                                ]):
                                    logger.warning(
                                        "Downloaded HTML file appears to be Anna's Archive login/error page; removing: %s",
                                        filepath
                                    )
                                    os.remove(filepath)
                                    return None
                    except Exception as ve:
                        logger.warning("Error validating HTML file %s: %s", filepath, ve)
                
                logger.info("Downloaded %s -> %s", url, filepath)
                # Count file after successful write
                _BUDGET.add_file(provider, work_id)
                return filepath
        
        # If we exit the loop without returning, all attempts failed due to 429
        logger.error("Giving up after %d attempts due to rate limiting for %s", max_attempts, url)
        return None
        
    except requests.exceptions.SSLError as e:
        # Handle SSL errors similarly to make_request
        if ssl_policy == "retry_insecure_once" and not insecure_retry_used:
            logger.warning("SSL verify failed for %s; retrying once with verify=False due to policy.", url)
            try:
                with session.get(url, stream=True, timeout=timeout, verify=False, headers=req_headers or None) as response:
                    response.raise_for_status()
                    
                    # Validate Content-Type (same as main path)
                    content_type_ssl = response.headers.get("Content-Type", "").lower()
                    from urllib.parse import urlparse
                    parsed_url_ssl = urlparse(url)
                    url_suggests_pdf_ssl = ".pdf" in parsed_url_ssl.path.lower() or "output=pdf" in url.lower()
                    url_suggests_epub_ssl = ".epub" in parsed_url_ssl.path.lower() or "output=epub" in url.lower()
                    is_annas_archive_ssl = "annas-archive" in url.lower()
                    
                    if "text/html" in content_type_ssl:
                        if url_suggests_pdf_ssl or url_suggests_epub_ssl:
                            logger.warning(
                                "Rejecting download (insecure retry): URL suggests PDF/EPUB but server returned HTML: %s",
                                url
                            )
                            return None
                        if is_annas_archive_ssl:
                            cl_header_ssl = response.headers.get("Content-Length")
                            try:
                                content_len_ssl = int(cl_header_ssl) if cl_header_ssl else 0
                                if 170000 < content_len_ssl < 185000:
                                    logger.warning(
                                        "Rejecting download (insecure retry): Anna's Archive HTML page likely login/error page: %s",
                                        url
                                    )
                                    return None
                            except Exception:
                                pass
                    
                    # Fallback to generic name if needed
                    cd_name = _filename_from_content_disposition(response.headers.get("Content-Disposition"))
                    inferred_ext = Path(cd_name or "").suffix or ".bin"
                    inferred_ext = inferred_ext.lower()
                    stem = _current_name_stem() or to_snake_case(filename) or "object"
                    prov_slug = _provider_slug(_current_provider_key(), provider)
                    
                    # Check extension whitelist (same as main path)
                    dl_cfg_ssl = get_download_config()
                    allowed_exts_ssl = dl_cfg_ssl.get("allowed_object_extensions", [])
                    save_disallowed_ssl = dl_cfg_ssl.get("save_disallowed_to_metadata", True)
                    
                    if allowed_exts_ssl and inferred_ext not in allowed_exts_ssl:
                        if save_disallowed_ssl:
                            target_dir = os.path.join(folder_path, "metadata")
                            logger.info("Extension %s not in allowed list; saving to metadata folder (SSL retry)", inferred_ext)
                        else:
                            logger.info("Extension %s not in allowed list; skipping download (SSL retry)", inferred_ext)
                            return None
                    else:
                        target_dir = os.path.join(folder_path, "objects")
                    
                    os.makedirs(target_dir, exist_ok=True)
                    safe_name = sanitize_filename(f"{stem}_{prov_slug}{inferred_ext}")
                    filepath = os.path.join(target_dir, safe_name)
                    with open(filepath, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    
                    # Validate file content (same as main path)
                    if inferred_ext.lower() in [".pdf", ".epub"]:
                        try:
                            with open(filepath, "rb") as check_f:
                                first_bytes = check_f.read(512)
                                if inferred_ext.lower() == ".pdf" and not first_bytes.startswith(b"%PDF-"):
                                    if b"<!DOCTYPE" in first_bytes or b"<html" in first_bytes.lower():
                                        logger.warning(
                                            "Downloaded file claims to be PDF but contains HTML; removing: %s",
                                            filepath
                                        )
                                        os.remove(filepath)
                                        return None
                                elif inferred_ext.lower() == ".epub" and not first_bytes.startswith(b"PK\x03\x04"):
                                    if b"<!DOCTYPE" in first_bytes or b"<html" in first_bytes.lower():
                                        logger.warning(
                                            "Downloaded file claims to be EPUB but contains HTML; removing: %s",
                                            filepath
                                        )
                                        os.remove(filepath)
                                        return None
                        except Exception as ve:
                            logger.warning("Error validating downloaded file %s: %s", filepath, ve)
                    
                    # Validate HTML files
                    if inferred_ext.lower() == ".html":
                        try:
                            with open(filepath, "r", encoding="utf-8", errors="ignore") as check_f:
                                html_content = check_f.read(2048)
                                if "annas-archive" in url.lower() or "annas-archive" in provider.lower():
                                    if any(marker in html_content.lower() for marker in [
                                        "<title>log in / register",
                                        "<title>login",
                                        "member login",
                                        "please log in",
                                        "__darkreader__",
                                    ]):
                                        logger.warning(
                                            "Downloaded HTML file appears to be Anna's Archive login/error page; removing: %s",
                                            filepath
                                        )
                                        os.remove(filepath)
                                        return None
                        except Exception as ve:
                            logger.warning("Error validating HTML file %s: %s", filepath, ve)
                    
                    logger.info("Downloaded %s -> %s (insecure)", url, filepath)
                    _BUDGET.add_file(provider, work_id)
                    return filepath
            except Exception as ee:
                logger.error("Insecure retry failed for %s: %s", url, ee)
                return None
        logger.error("SSL error downloading %s: %s", url, e)
        return None
        
    except requests.exceptions.RequestException as e:
        logger.error("Error downloading %s: %s", url, e)
        return None
        
    except OSError as e:
        logger.error("Error saving file to %s: %s", folder_path, e)
        return None


def _filename_from_content_disposition(cd: Optional[str]) -> Optional[str]:
    """Parse filename from Content-Disposition header."""
    if not cd:
        return None
    try:
        # Parse simple Content-Disposition header parameters without cgi module (removed in Py3.13)
        # Example: attachment; filename="example.pdf"; filename*=UTF-8''example.pdf
        parts = [p.strip() for p in cd.split(";")]
        params: dict[str, str] = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                k = k.strip().lower()
                v = v.strip().strip('"')
                params[k] = v
        fn = params.get("filename*") or params.get("filename")
        if fn:
            # filename* may be like: UTF-8''example.pdf (RFC 5987)
            if "''" in fn:
                try:
                    from urllib.parse import unquote
                    charset, _, enc = fn.partition("''")
                    return unquote(enc)
                except Exception:
                    return fn
            return fn
    except Exception:
        return None
    return None


def save_json(data: Any, folder_path: str, filename: str) -> Optional[str]:
    """Save data as JSON file in metadata directory.

    Args:
        data: Data to serialize
        folder_path: Base directory (will create metadata/ subdirectory)
        filename: Base filename (will be standardized with provider prefix)

    Returns:
        Path to saved file or None if skipped/failed
    """
    if not include_metadata():
        logger.debug("Config download.include_metadata=false; skipping metadata save for %s", filename)
        return None
    
    os.makedirs(folder_path, exist_ok=True)
    
    # Standardize metadata naming and directory
    stem = _current_name_stem() or to_snake_case(filename) or "item"
    prov_slug = _provider_slug(_current_provider_key(), None)
    meta_dir = os.path.join(folder_path, "metadata")
    os.makedirs(meta_dir, exist_ok=True)
    
    # For metadata, do not number the first file for a provider; append _2, _3... when multiple
    key = (stem, prov_slug or "unknown", "metadata")
    counters = _counters()
    counters[key] = int(counters.get(key, 0)) + 1
    idx = counters[key]
    
    if idx <= 1:
        base = f"{stem}_{prov_slug}"
    else:
        base = f"{stem}_{prov_slug}_{idx}"
    
    filepath = os.path.join(meta_dir, sanitize_filename(base) + ".json")
    
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("Saved JSON: %s", filepath)
        return filepath
    except (OSError, TypeError) as e:
        logger.error("Error saving JSON %s: %s", filepath, e)
        return None


# Export all public symbols for backward compatibility
__all__ = [
    # Config
    "get_config",
    "get_provider_setting",
    "get_network_config",
    "get_download_config",
    "get_download_limits",
    "get_max_pages",
    "get_resume_mode",
    "prefer_pdf_over_images",
    "overwrite_existing",
    "include_metadata",
    # Network
    "get_session",
    "make_request",
    "RateLimiter",
    # Context
    "set_current_work",
    "clear_current_work",
    "set_current_entry",
    "clear_current_entry",
    "set_current_provider",
    "clear_current_provider",
    "set_current_name_stem",
    "clear_current_name_stem",
    "reset_counters",
    # Budget
    "budget_exhausted",
    # Naming
    "sanitize_filename",
    "to_snake_case",
    # File operations
    "download_file",
    "save_json",
    "download_iiif_renderings",
]
