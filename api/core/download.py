"""Core file download primitives.

Provides the central `download_file` function used by every provider connector
and the IIIF strategies. Handles rate limiting, exponential backoff, budget
enforcement, content-type validation, magic-byte checks, HTML login-page
detection, and standardized naming.

Also provides `save_json` for metadata persistence under each work's
`metadata/` subdirectory.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from .budget import get_budget
from .config import (
    get_download_config,
    get_network_config,
    include_metadata,
    overwrite_existing,
)
from .context import (
    get_current_name_stem,
    get_current_provider,
    get_current_work,
    increment_counter,
    peek_counter,
)
from .naming import get_provider_slug, sanitize_filename, to_snake_case
from .network import get_provider_for_url, get_rate_limiter, get_session

logger = logging.getLogger(__name__)

_BUDGET = get_budget()

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

_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".jp2",
    ".tif",
    ".tiff",
    ".gif",
    ".bmp",
    ".webp",
}

_ANNAS_LOGIN_MARKERS = (
    "<title>log in / register",
    "<title>login",
    "member login",
    "please log in",
    "__darkreader__",
)


def _infer_extension_from_content_type(content_type: str) -> str:
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
    if "text/html" not in content_type.lower():
        return False, ""

    parsed = urlparse(url)
    url_lower = url.lower()
    path_lower = parsed.path.lower()

    suggests_pdf = (
        ".pdf" in path_lower or "output=pdf" in url_lower or "download" in url_lower
    )
    suggests_epub = ".epub" in path_lower or "output=epub" in url_lower

    if suggests_pdf or suggests_epub:
        return True, "URL suggests PDF/EPUB but server returned HTML (likely error page)"

    if "annas-archive" in url_lower:
        if content_length and 170000 < content_length < 185000:
            return True, "Anna's Archive HTML page likely login/error page (~180KB)"

    return False, ""


def _validate_file_magic_bytes(filepath: str, ext: str) -> tuple[bool, str]:
    if ext not in (".pdf", ".epub"):
        return True, ""

    try:
        with open(filepath, "rb") as f:
            first_bytes = f.read(512)

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
        return True, ""


def _validate_html_not_login_page(
    filepath: str, url: str, provider: str | None
) -> tuple[bool, str]:
    url_lower = url.lower()
    provider_lower = (provider or "").lower()

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
    allowed_exts: list[str] | None,
    save_disallowed_to_metadata: bool,
) -> tuple[str | None, str, bool]:
    if allowed_exts and ext not in allowed_exts:
        if save_disallowed_to_metadata:
            return (
                os.path.join(folder_path, "metadata"),
                f"Extension {ext} not in allowed list; saving to metadata folder",
                False,
            )
        return None, f"Extension {ext} not in allowed list; skipping download", False

    return os.path.join(folder_path, "objects"), "", True


def _build_standardized_filename(
    ext: str,
    stem: str,
    prov_slug: str,
    max_stem_len: int = 50,
) -> str:
    if len(stem) > max_stem_len:
        stem = stem[:max_stem_len].rstrip("_")

    type_key = "image" if ext in _IMAGE_EXTENSIONS else (ext.lstrip(".") or "bin")

    key = (stem, prov_slug, type_key)
    seq = increment_counter(key)

    if type_key == "image":
        safe_base = f"{stem}_{prov_slug}_image_{seq:03d}"
    else:
        safe_base = f"{stem}_{prov_slug}" if seq <= 1 else f"{stem}_{prov_slug}_{seq}"

    return sanitize_filename(f"{safe_base}{ext}")


def _filename_from_content_disposition(cd: str | None) -> str | None:
    if not cd:
        return None
    try:
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
            if "''" in fn:
                try:
                    charset, _, enc = fn.partition("''")
                    return unquote(enc)
                except Exception:
                    return fn
            return fn
    except Exception:
        return None
    return None


def _try_skip_existing(
    url: str, folder_path: str, filename: str, provider: str | None
) -> str | None:
    if overwrite_existing():
        return None

    predicted_ext = (
        Path(urlparse(url).path).suffix.lower()
        or Path(filename or "").suffix.lower()
        or None
    )
    if not predicted_ext:
        return None

    dl_cfg = get_download_config()
    allowed_exts = dl_cfg.get("allowed_object_extensions", [])
    save_disallowed = dl_cfg.get("save_disallowed_to_metadata", True)
    target_dir, _, _ = _determine_target_directory(
        folder_path, predicted_ext, allowed_exts, save_disallowed
    )
    if target_dir is None:
        return None

    stem = get_current_name_stem() or to_snake_case(filename) or "object"
    prov_slug = get_provider_slug(get_current_provider(), provider)

    if len(stem) > 50:
        stem = stem[:50].rstrip("_")

    type_key = (
        "image" if predicted_ext in _IMAGE_EXTENSIONS else (predicted_ext.lstrip(".") or "bin")
    )
    key = (stem, prov_slug, type_key)
    seq = peek_counter(key)

    if type_key == "image":
        safe_base = f"{stem}_{prov_slug}_image_{seq:03d}"
    else:
        safe_base = f"{stem}_{prov_slug}" if seq <= 1 else f"{stem}_{prov_slug}_{seq}"

    predicted_name = sanitize_filename(f"{safe_base}{predicted_ext}")
    predicted_path = os.path.join(target_dir, predicted_name)

    if os.path.exists(predicted_path):
        increment_counter(key)
        logger.info("File already exists (early check), skipping: %s", predicted_path)
        return predicted_path

    return None


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
    provider = get_provider_for_url(url)

    existing = _try_skip_existing(url, folder_path, filename, provider)
    if existing is not None:
        return existing

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

    req_headers = (
        {str(k): str(v) for k, v in provider_headers.items() if v is not None}
        if provider_headers
        else {}
    )

    rl = get_rate_limiter(provider)
    work_id = get_current_work()

    if _BUDGET.exhausted():
        logger.warning("Download budget exhausted; skipping %s", url)
        return None

    if not _BUDGET.allow_new_file(provider, work_id):
        logger.warning("Download budget (files) exceeded; skipping %s", url)
        return None

    def _process_response(
        response: requests.Response, is_insecure_retry: bool = False
    ) -> str | None:
        content_type = response.headers.get("Content-Type", "")

        cl_header = response.headers.get("Content-Length")
        content_len = int(cl_header) if cl_header else None
        should_reject, reject_reason = _should_reject_html_response(
            content_type, url, content_len
        )
        if should_reject:
            log_suffix = " (insecure retry)" if is_insecure_retry else ""
            logger.warning("Rejecting download%s: %s: %s", log_suffix, reject_reason, url)
            return None

        cd_name = _filename_from_content_disposition(
            response.headers.get("Content-Disposition")
        )
        parsed = urlparse(url)
        inferred_ext = (
            Path(parsed.path).suffix
            or _infer_extension_from_content_type(content_type)
            or Path(cd_name or "").suffix
            or Path(filename or "").suffix
            or ".bin"
        ).lower()

        stem = get_current_name_stem() or to_snake_case(filename) or "object"
        prov_slug = get_provider_slug(get_current_provider(), provider)

        dl_cfg = get_download_config()
        allowed_exts = dl_cfg.get("allowed_object_extensions", [])
        save_disallowed = dl_cfg.get("save_disallowed_to_metadata", True)

        target_dir, log_msg, counts_as_success = _determine_target_directory(
            folder_path, inferred_ext, allowed_exts, save_disallowed
        )
        if target_dir is None:
            logger.info(log_msg)
            return None
        if log_msg:
            logger.info(log_msg)

        os.makedirs(target_dir, exist_ok=True)

        safe_name = _build_standardized_filename(inferred_ext, stem, prov_slug)
        filepath = os.path.join(target_dir, safe_name)

        if not overwrite_existing() and os.path.exists(filepath):
            logger.info("File already exists, skipping: %s", filepath)
            return filepath

        if not _BUDGET.allow_new_file(provider, work_id):
            logger.warning("Download budget (files) exceeded; skipping %s", url)
            return None

        content_len_int = int(cl_header) if cl_header else 0
        if content_len_int and not _BUDGET.allow_bytes(
            provider, work_id, content_len_int
        ):
            logger.warning(
                "Download budget (bytes) would be exceeded by %s (%d bytes); skipping.",
                url,
                content_len_int,
            )
            return None

        truncated = False
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                if not _BUDGET.add_bytes(provider, work_id, len(chunk)):
                    logger.error(
                        "Download budget exceeded while writing %s; truncating and removing file.",
                        filepath,
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

        is_valid, error_msg = _validate_file_magic_bytes(filepath, inferred_ext)
        if not is_valid:
            logger.warning("%s; removing: %s", error_msg, filepath)
            try:
                os.remove(filepath)
            except Exception:
                pass
            return None

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

        if not counts_as_success:
            logger.info(
                "File saved to metadata folder; not counting as successful download for work completion"
            )
            return None
        return filepath

    def _calculate_backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), max_backoff)
            except ValueError:
                try:
                    retry_dt = parsedate_to_datetime(retry_after)
                    return min(
                        max(0.0, (retry_dt - datetime.now(retry_dt.tzinfo)).total_seconds()),
                        max_backoff,
                    )
                except Exception:
                    pass
        return min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)

    try:
        verify = verify_default

        for attempt in range(1, max_attempts + 1):
            if rl:
                rl.wait()

            with session.get(
                url,
                stream=True,
                timeout=timeout,
                verify=verify,
                headers=req_headers or None,
            ) as response:
                if response.status_code == 429:
                    sleep_s = _calculate_backoff(
                        attempt, response.headers.get("Retry-After")
                    )
                    logger.warning(
                        "429 Too Many Requests for %s; sleeping %.1fs (attempt %d/%d)",
                        url,
                        sleep_s,
                        attempt,
                        max_attempts,
                    )
                    time.sleep(sleep_s)
                    continue

                if response.status_code in (500, 502, 503, 504):
                    sleep_s = _calculate_backoff(attempt, None)
                    logger.warning(
                        "%s for %s; sleeping %.1fs (attempt %d/%d)",
                        response.status_code,
                        url,
                        sleep_s,
                        attempt,
                        max_attempts,
                    )
                    time.sleep(sleep_s)
                    continue

                if response.status_code in (400, 401, 403, 404, 410, 422):
                    logger.error(
                        "Non-retryable HTTP %s for %s; aborting download",
                        response.status_code,
                        url,
                    )
                    return None

                response.raise_for_status()
                return _process_response(response)

        logger.error(
            "Giving up after %d attempts due to rate limiting for %s",
            max_attempts,
            url,
        )
        return None

    except requests.exceptions.SSLError as e:
        if ssl_policy == "retry_insecure_once":
            logger.warning(
                "SSL verify failed for %s; retrying once with verify=False due to policy.",
                url,
            )
            try:
                with session.get(
                    url,
                    stream=True,
                    timeout=timeout,
                    verify=False,
                    headers=req_headers or None,
                ) as response:
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


def save_json(data: Any, folder_path: str, filename: str) -> str | None:
    """Save data as JSON file in metadata directory.

    Args:
        data: Data to serialize
        folder_path: Base directory (will create metadata/ subdirectory)
        filename: Base filename (will be standardized with provider prefix)

    Returns:
        Path to saved file or None if skipped/failed
    """
    if not include_metadata():
        logger.debug(
            "Config download.include_metadata=false; skipping metadata save for %s",
            filename,
        )
        return None

    os.makedirs(folder_path, exist_ok=True)

    stem = get_current_name_stem() or to_snake_case(filename) or "item"
    prov_slug = get_provider_slug(get_current_provider(), None)
    meta_dir = os.path.join(folder_path, "metadata")
    os.makedirs(meta_dir, exist_ok=True)

    key = (stem, prov_slug or "unknown", "metadata")
    idx = increment_counter(key)

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


__all__ = ["download_file", "save_json"]
