import os
import re
import json
import logging
import requests
from urllib.parse import unquote
from pathlib import Path
from typing import Optional, Union
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_SESSION: Optional[requests.Session] = None


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    # Default headers
    session.headers.update(
        {
            # Use a modern browser-like UA to avoid occasional 403s from some providers
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        }
    )
    return session


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
    return _SESSION


def _split_name_and_suffixes(name: str) -> tuple[str, str]:
    # Preserve multi-suffix like .tar.gz
    base = Path(name).name
    suffixes = Path(base).suffixes
    ext = "".join(suffixes)
    if ext:
        base_no_ext = base[: -len(ext)]
    else:
        base_no_ext = base
    return base_no_ext, ext


def sanitize_filename(name: str, max_base_len: int = 100) -> str:
    """Sanitize string for safe filenames while preserving extension.

    - Keeps the original extension(s) intact (e.g., .pdf, .tar.gz).
    - Limits the base name length only.
    """
    if not name:
        return "_untitled_"
    base, ext = _split_name_and_suffixes(name)
    # Remove illegal characters from base
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", base)
    # Collapse whitespace and separators into single underscore
    base = re.sub(r"[\s._-]+", "_", base).strip("._-")
    if not base:
        base = "_untitled_"
    # Truncate base only
    base = base[:max_base_len]
    return f"{base}{ext}"


def _filename_from_content_disposition(cd: Optional[str]) -> Optional[str]:
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
                    charset, _, enc = fn.partition("''")
                    return unquote(enc)
                except Exception:
                    return fn
            return fn
    except Exception:
        return None
    return None


def download_file(url: str, folder_path: str, filename: str) -> Optional[str]:
    os.makedirs(folder_path, exist_ok=True)
    session = get_session()
    try:
        with session.get(url, stream=True, timeout=30) as response:
            response.raise_for_status()
            cd_name = _filename_from_content_disposition(response.headers.get("Content-Disposition"))
            chosen_name = cd_name if cd_name else filename
            safe_name = sanitize_filename(chosen_name)

            # If no extension on safe_name, try to infer from URL or Content-Type
            def _infer_ext() -> str:
                from urllib.parse import urlparse

                parsed = urlparse(url)
                suffix = Path(parsed.path).suffix
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

            if not Path(safe_name).suffix:
                inferred = _infer_ext()
                if inferred:
                    safe_name = f"{safe_name}{inferred}"
            filepath = os.path.join(folder_path, safe_name)
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:  # filter out keep-alive chunks
                        f.write(chunk)
            logger.info("Downloaded %s -> %s", url, filepath)
            return filepath
    except requests.exceptions.RequestException as e:
        logger.error("Error downloading %s: %s", url, e)
        return None
    except OSError as e:
        logger.error("Error saving file to %s: %s", folder_path, e)
        return None


def save_json(data, folder_path, filename) -> Optional[str]:
    os.makedirs(folder_path, exist_ok=True)
    filepath = os.path.join(folder_path, sanitize_filename(filename) + ".json")
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("Saved JSON: %s", filepath)
        return filepath
    except (OSError, TypeError) as e:
        logger.error("Error saving JSON %s: %s", filepath, e)
        return None


def make_request(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = 15,
) -> Optional[Union[dict, str, bytes]]:
    """HTTP GET with retries using a shared Session.

    Returns:
      - dict for JSON responses
      - str for text/xml/html
      - bytes for other/binary content
    """
    session = get_session()
    try:
        resp = session.get(url, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").lower()
        if "json" in content_type:
            try:
                return resp.json()
            except json.JSONDecodeError as e:
                logger.error("JSON decode error for %s: %s", url, e)
                return None
        if any(t in content_type for t in ("text/", "xml", "html")):
            return resp.text
        return resp.content
    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        reason = getattr(e.response, "reason", "")
        logger.error("HTTP error %s %s for %s", status, reason, url)
    except requests.exceptions.Timeout:
        logger.error("Request timed out: %s", url)
    except requests.exceptions.RequestException as e:
        logger.error("Request failed for %s: %s", url, e)
    return None
