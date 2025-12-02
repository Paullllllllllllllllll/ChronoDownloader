"""Core utilities for ChronoDownloader API.

This module serves as a backward-compatible façade, re-exporting functionality
from the modular api.core package while maintaining the original public API.

New code should import directly from api.core submodules for clarity.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

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

# Content-type to extension mapping
_CONTENT_TYPE_EXT_MAP = {
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

# Image file extensions for type classification
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".jp2", ".tif", ".tiff", ".gif", ".bmp", ".webp"}

# Anna's Archive login/error page markers
_ANNAS_LOGIN_MARKERS = (
    "<title>log in / register",
    "<title>login",
    "member login",
    "please log in",
    "__darkreader__",
)


def _infer_extension_from_content_type(content_type: str) -> str:
    """Infer file extension from Content-Type header.
    
    Args:
        content_type: Content-Type header value
        
    Returns:
        File extension (e.g., '.pdf') or empty string if unknown
    """
    ct_lower = content_type.lower()
    for mime, ext in _CONTENT_TYPE_EXT_MAP.items():
        if mime in ct_lower:
            return ext
    return ""


def _should_reject_html_response(
    content_type: str,
    url: str,
    content_length: int | None = None,
) -> tuple[bool, str]:
    """Check if an HTML response should be rejected based on URL expectations.
    
    Args:
        content_type: Response Content-Type header
        url: Request URL
        content_length: Optional Content-Length value
        
    Returns:
        Tuple of (should_reject, reason)
    """
    if "text/html" not in content_type.lower():
        return False, ""
    
    parsed = urlparse(url)
    url_lower = url.lower()
    path_lower = parsed.path.lower()
    
    # Check if URL suggests PDF/EPUB content
    suggests_pdf = ".pdf" in path_lower or "output=pdf" in url_lower or "download" in url_lower
    suggests_epub = ".epub" in path_lower or "output=epub" in url_lower
    
    if suggests_pdf or suggests_epub:
        return True, "URL suggests PDF/EPUB but server returned HTML (likely error page)"
    
    # Check Anna's Archive specific patterns
    if "annas-archive" in url_lower:
        if content_length and 170000 < content_length < 185000:
            return True, "Anna's Archive HTML page likely login/error page (~180KB)"
    
    return False, ""


def _validate_file_magic_bytes(filepath: str, ext: str) -> tuple[bool, str]:
    """Validate downloaded file by checking magic bytes.
    
    Args:
        filepath: Path to downloaded file
        ext: File extension (lowercase, with dot)
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if ext not in (".pdf", ".epub"):
        return True, ""
    
    try:
        with open(filepath, "rb") as f:
            first_bytes = f.read(512)
        
        # Check for HTML content masquerading as PDF/EPUB
        is_html = b"<!DOCTYPE" in first_bytes or b"<html" in first_bytes.lower()
        
        if ext == ".pdf":
            if not first_bytes.startswith(b"%PDF-"):
                if is_html:
                    return False, "File claims to be PDF but contains HTML"
        elif ext == ".epub":
            if not first_bytes.startswith(b"PK\x03\x04"):
                if is_html:
                    return False, "File claims to be EPUB but contains HTML"
        
        return True, ""
    except Exception as e:
        logger.warning("Error validating file %s: %s", filepath, e)
        return True, ""  # Don't reject on validation error


def _validate_html_not_login_page(filepath: str, url: str, provider: str | None) -> tuple[bool, str]:
    """Check if HTML file is a login/error page that should be rejected.
    
    Args:
        filepath: Path to HTML file
        url: Original URL
        provider: Provider key
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    url_lower = url.lower()
    provider_lower = (provider or "").lower()
    
    # Only check Anna's Archive pages
    if "annas-archive" not in url_lower and "annas-archive" not in provider_lower:
        return True, ""
    
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            html_content = f.read(2048).lower()
        
        if any(marker in html_content for marker in _ANNAS_LOGIN_MARKERS):
            return False, "Anna's Archive login/error page detected"
        
        return True, ""
    except Exception as e:
        logger.warning("Error validating HTML file %s: %s", filepath, e)
        return True, ""


def _determine_target_directory(
    folder_path: str,
    ext: str,
    allowed_exts: list | None,
    save_disallowed_to_metadata: bool,
) -> tuple[str | None, str]:
    """Determine target directory based on extension whitelist.
    
    Args:
        folder_path: Base folder path
        ext: File extension (lowercase, with dot)
        allowed_exts: List of allowed extensions or None/empty for no filtering
        save_disallowed_to_metadata: Whether to save disallowed files to metadata
        
    Returns:
        Tuple of (target_directory, log_message). target_directory is None if should skip.
    """
    if allowed_exts and ext not in allowed_exts:
        if save_disallowed_to_metadata:
            return os.path.join(folder_path, "metadata"), f"Extension {ext} not in allowed list; saving to metadata folder"
        return None, f"Extension {ext} not in allowed list; skipping download"
    
    return os.path.join(folder_path, "objects"), ""


def _build_standardized_filename(
    ext: str,
    stem: str,
    prov_slug: str,
) -> str:
    """Build a standardized filename with provider slug and sequence number.
    
    Args:
        ext: File extension (lowercase, with dot)
        stem: Base name stem
        prov_slug: Provider slug
        
    Returns:
        Sanitized filename
    """
    # Determine type key for numbering
    type_key = "image" if ext in _IMAGE_EXTENSIONS else (ext.lstrip(".") or "bin")
    
    # Get sequence number
    key = (stem, prov_slug, type_key)
    seq = increment_counter(key)
    
    # Build filename based on type
    if type_key == "image":
        safe_base = f"{stem}_{prov_slug}_image_{seq:03d}"
    else:
        safe_base = f"{stem}_{prov_slug}" if seq <= 1 else f"{stem}_{prov_slug}_{seq}"
    
    return sanitize_filename(f"{safe_base}{ext}")


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


def download_file(url: str, folder_path: str, filename: str) -> str | None:
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
    
    req_headers = {str(k): str(v) for k, v in provider_headers.items() if v is not None} if provider_headers else {}
    
    rl = _get_rate_limiter(provider)
    work_id = _current_work_id()
    
    if _BUDGET.exhausted():
        logger.warning("Download budget exhausted; skipping %s", url)
        return None
    
    if not _BUDGET.allow_new_file(provider, work_id):
        logger.warning("Download budget (files) exceeded; skipping %s", url)
        return None
    
    def _process_response(response: requests.Response, is_insecure_retry: bool = False) -> str | None:
        """Process a successful response and save the file."""
        content_type = response.headers.get("Content-Type", "")
        
        # Check for HTML rejection
        cl_header = response.headers.get("Content-Length")
        content_len = int(cl_header) if cl_header else None
        should_reject, reject_reason = _should_reject_html_response(content_type, url, content_len)
        if should_reject:
            log_suffix = " (insecure retry)" if is_insecure_retry else ""
            logger.warning("Rejecting download%s: %s: %s", log_suffix, reject_reason, url)
            return None
        
        # Determine file extension
        cd_name = _filename_from_content_disposition(response.headers.get("Content-Disposition"))
        parsed = urlparse(url)
        inferred_ext = (
            Path(parsed.path).suffix
            or _infer_extension_from_content_type(content_type)
            or Path(cd_name or "").suffix
            or Path(filename or "").suffix
            or ".bin"
        ).lower()
        
        stem = _current_name_stem() or to_snake_case(filename) or "object"
        prov_slug = _provider_slug(_current_provider_key(), provider)
        
        # Determine target directory
        dl_cfg = get_download_config()
        allowed_exts = dl_cfg.get("allowed_object_extensions", [])
        save_disallowed = dl_cfg.get("save_disallowed_to_metadata", True)
        
        target_dir, log_msg = _determine_target_directory(folder_path, inferred_ext, allowed_exts, save_disallowed)
        if target_dir is None:
            logger.info(log_msg)
            return None
        if log_msg:
            logger.info(log_msg)
        
        os.makedirs(target_dir, exist_ok=True)
        
        # Build filename
        safe_name = _build_standardized_filename(inferred_ext, stem, prov_slug)
        filepath = os.path.join(target_dir, safe_name)
        
        # Check overwrite setting
        if not overwrite_existing() and os.path.exists(filepath):
            logger.info("File already exists, skipping: %s", filepath)
            return filepath
        
        # Budget checks
        if not _BUDGET.allow_new_file(provider, work_id):
            logger.warning("Download budget (files) exceeded; skipping %s", url)
            return None
        
        content_len_int = int(cl_header) if cl_header else 0
        if content_len_int and not _BUDGET.allow_bytes(provider, work_id, content_len_int):
            logger.warning("Download budget (bytes) would be exceeded by %s (%d bytes); skipping.", url, content_len_int)
            return None
        
        # Write file
        truncated = False
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                if not _BUDGET.add_bytes(provider, work_id, len(chunk)):
                    logger.error("Download budget exceeded while writing %s; truncating and removing file.", filepath)
                    truncated = True
                    break
                f.write(chunk)
        
        if truncated:
            try:
                os.remove(filepath)
            except Exception:
                pass
            return None
        
        # Validate file content
        is_valid, error_msg = _validate_file_magic_bytes(filepath, inferred_ext)
        if not is_valid:
            logger.warning("%s; removing: %s", error_msg, filepath)
            try:
                os.remove(filepath)
            except Exception:
                pass
            return None
        
        # Validate HTML files
        if inferred_ext == ".html":
            is_valid, error_msg = _validate_html_not_login_page(filepath, url, provider)
            if not is_valid:
                logger.warning("%s; removing: %s", error_msg, filepath)
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                return None
        
        log_suffix = " (insecure)" if is_insecure_retry else ""
        logger.info("Downloaded %s -> %s%s", url, filepath, log_suffix)
        _BUDGET.add_file(provider, work_id)
        return filepath
    
    def _calculate_backoff(attempt: int, retry_after: str | None) -> float:
        """Calculate sleep duration for retry."""
        if retry_after:
            try:
                return min(float(retry_after), max_backoff)
            except ValueError:
                try:
                    retry_dt = parsedate_to_datetime(retry_after)
                    return min(max(0.0, (retry_dt - datetime.now(retry_dt.tzinfo)).total_seconds()), max_backoff)
                except Exception:
                    pass
        return min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)
    
    try:
        verify = verify_default
        
        for attempt in range(1, max_attempts + 1):
            if rl:
                rl.wait()
            
            with session.get(url, stream=True, timeout=timeout, verify=verify, headers=req_headers or None) as response:
                # Handle rate limiting
                if response.status_code == 429:
                    sleep_s = _calculate_backoff(attempt, response.headers.get("Retry-After"))
                    logger.warning("429 Too Many Requests for %s; sleeping %.1fs (attempt %d/%d)", url, sleep_s, attempt, max_attempts)
                    time.sleep(sleep_s)
                    continue
                
                # Retry transient 5xx
                if response.status_code in (500, 502, 503, 504):
                    sleep_s = _calculate_backoff(attempt, None)
                    logger.warning("%s for %s; sleeping %.1fs (attempt %d/%d)", response.status_code, url, sleep_s, attempt, max_attempts)
                    time.sleep(sleep_s)
                    continue
                
                # Non-retryable client errors
                if response.status_code in (400, 401, 403, 404, 410, 422):
                    logger.error("Non-retryable HTTP %s for %s; aborting download", response.status_code, url)
                    return None
                
                response.raise_for_status()
                return _process_response(response)
        
        logger.error("Giving up after %d attempts due to rate limiting for %s", max_attempts, url)
        return None
        
    except requests.exceptions.SSLError as e:
        if ssl_policy == "retry_insecure_once":
            logger.warning("SSL verify failed for %s; retrying once with verify=False due to policy.", url)
            try:
                with session.get(url, stream=True, timeout=timeout, verify=False, headers=req_headers or None) as response:
                    response.raise_for_status()
                    return _process_response(response, is_insecure_retry=True)
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
