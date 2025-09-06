import os
import re
import json
import logging
import requests
import time
import random
from urllib.parse import unquote, urlparse
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional, Union
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_SESSION: Optional[requests.Session] = None

# Map URL hostnames to provider keys for centralized per-provider rate limiting and policies
_PROVIDER_HOST_MAP: dict[str, tuple[str, ...]] = {
    "gallica": ("gallica.bnf.fr",),
    "british_library": ("api.bl.uk", "sru.bl.uk", "iiif.bl.uk"),
    "mdz": ("api.digitale-sammlungen.de", "www.digitale-sammlungen.de", "digitale-sammlungen.de"),
    "europeana": ("api.europeana.eu", "iiif.europeana.eu"),
    "wellcome": ("api.wellcomecollection.org", "iiif.wellcomecollection.org"),
    "loc": ("www.loc.gov", "loc.gov", "tile.loc.gov", "iiif.loc.gov"),
    "ddb": ("api.deutsche-digitale-bibliothek.de", "iiif.deutsche-digitale-bibliothek.de"),
    "polona": ("polona.pl",),
    "bne": ("datos.bne.es", "iiif.bne.es"),
    "dpla": ("api.dp.la",),
    "internet_archive": ("archive.org", "archivelab.org", "iiif.archivelab.org"),
    "google_books": ("www.googleapis.com", "books.google.com", "books.googleusercontent.com", "play.google.com"),
}


class RateLimiter:
    """Simple per-provider rate limiter with jitter, using monotonic time."""

    def __init__(self, min_interval_s: float = 0.0, jitter_s: float = 0.0):
        self.min_interval_s = max(0.0, float(min_interval_s or 0.0))
        self.jitter_s = max(0.0, float(jitter_s or 0.0))
        self._last_ts = 0.0

    def wait(self):
        if self.min_interval_s <= 0 and self.jitter_s <= 0:
            return
        now = time.monotonic()
        # Next ready time is last_ts + base + random jitter
        jitter = random.uniform(0.0, self.jitter_s) if self.jitter_s > 0 else 0.0
        next_ready = self._last_ts + self.min_interval_s + jitter
        sleep_s = next_ready - now
        if sleep_s > 0:
            time.sleep(sleep_s)
            now = time.monotonic()
        self._last_ts = now


_RATE_LIMITERS: dict[str, RateLimiter] = {}


def _provider_for_url(url: str) -> Optional[str]:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None
    for provider, host_parts in _PROVIDER_HOST_MAP.items():
        for part in host_parts:
            if part in host:
                return provider
    return None


def get_network_config(provider_key: Optional[str]) -> dict:
    """Return network policy for a provider, with sensible defaults.

    Structure (per provider in config.json):
    {
      "network": {
        "delay_ms": 0,
        "jitter_ms": 0,
        "max_attempts": 5,
        "base_backoff_s": 1.5,
        "backoff_multiplier": 1.5,
        "timeout_s": null
      },
      # Back-compat: allow legacy "delay_ms" at provider root
      "delay_ms": 1200
    }
    """
    cfg = get_config()
    prov_cfg = cfg.get("provider_settings", {}).get(provider_key or "", {}) if provider_key else {}
    net = dict(prov_cfg.get("network", {}) or {})
    # Back-compat: lift legacy delay_ms into network if not provided
    if "delay_ms" not in net and "delay_ms" in prov_cfg:
        net["delay_ms"] = prov_cfg.get("delay_ms")
    # Defaults
    net.setdefault("delay_ms", 0)
    net.setdefault("jitter_ms", 0)
    net.setdefault("max_attempts", 5)
    net.setdefault("base_backoff_s", 1.5)
    net.setdefault("backoff_multiplier", 1.5)
    # timeout_s may be None to use function default
    return net


def _get_rate_limiter(provider_key: Optional[str]) -> Optional[RateLimiter]:
    if not provider_key:
        return None
    net = get_network_config(provider_key)
    delay_s = float(net.get("delay_ms", 0) or 0) / 1000.0
    jitter_s = float(net.get("jitter_ms", 0) or 0) / 1000.0
    rl = _RATE_LIMITERS.get(provider_key)
    if rl is None or rl.min_interval_s != delay_s or rl.jitter_s != jitter_s:
        rl = RateLimiter(delay_s, jitter_s)
        _RATE_LIMITERS[provider_key] = rl
    return rl


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
    provider = _provider_for_url(url)
    net = get_network_config(provider)
    max_attempts = int(net.get("max_attempts", 5) or 5)
    base_backoff = float(net.get("base_backoff_s", 1.5) or 1.5)
    backoff_mult = float(net.get("backoff_multiplier", 1.5) or 1.5)
    timeout_s = net.get("timeout_s")
    timeout = float(timeout_s) if timeout_s is not None else 30.0
    rl = _get_rate_limiter(provider)
    try:
        for attempt in range(1, max_attempts + 1):
            # Centralized pacing
            if rl:
                rl.wait()
            with session.get(url, stream=True, timeout=timeout) as response:
                # Handle rate-limiting explicitly to respect Retry-After
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    # Retry-After may be seconds or HTTP-date
                    sleep_s = None
                    if retry_after:
                        try:
                            sleep_s = float(retry_after)
                        except ValueError:
                            try:
                                dt = parsedate_to_datetime(retry_after)
                                sleep_s = max(0.0, (dt - dt.now(dt.tzinfo)).total_seconds())
                            except Exception:
                                sleep_s = None
                    if sleep_s is None:
                        sleep_s = base_backoff * (backoff_mult ** (attempt - 1))
                    logger.warning("429 Too Many Requests for %s; sleeping %.1fs (attempt %d/%d)", url, sleep_s, attempt, max_attempts)
                    time.sleep(sleep_s)
                    continue
                # Retry transient 5xx
                if response.status_code in (500, 502, 503, 504):
                    sleep_s = base_backoff * (backoff_mult ** (attempt - 1))
                    logger.warning("%s for %s; sleeping %.1fs (attempt %d/%d)", response.status_code, url, sleep_s, attempt, max_attempts)
                    time.sleep(sleep_s)
                    continue
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
        # If we exit the loop without returning, all attempts failed due to 429
        logger.error("Giving up after %d attempts due to rate limiting for %s", max_attempts, url)
        return None
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
    """HTTP GET with centralized per-provider pacing and backoff.

    Returns:
      - dict for JSON responses
      - str for text/xml/html
      - bytes for other/binary content
    """
    session = get_session()
    provider = _provider_for_url(url)
    net = get_network_config(provider)
    max_attempts = int(net.get("max_attempts", 5) or 5)
    base_backoff = float(net.get("base_backoff_s", 1.5) or 1.5)
    backoff_mult = float(net.get("backoff_multiplier", 1.5) or 1.5)
    net_timeout = net.get("timeout_s")
    effective_timeout = float(net_timeout) if net_timeout is not None else float(timeout)
    rl = _get_rate_limiter(provider)

    for attempt in range(1, max_attempts + 1):
        try:
            if rl:
                rl.wait()
            resp = session.get(url, params=params, headers=headers, timeout=effective_timeout)
            # Explicit 429 handling with Retry-After
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                sleep_s = None
                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except ValueError:
                        try:
                            dt = parsedate_to_datetime(retry_after)
                            sleep_s = max(0.0, (dt - dt.now(dt.tzinfo)).total_seconds())
                        except Exception:
                            sleep_s = None
                if sleep_s is None:
                    sleep_s = base_backoff * (backoff_mult ** (attempt - 1))
                logger.warning("429 Too Many Requests for %s; sleeping %.1fs (attempt %d/%d)", url, sleep_s, attempt, max_attempts)
                time.sleep(sleep_s)
                continue
            if resp.status_code in (500, 502, 503, 504):
                sleep_s = base_backoff * (backoff_mult ** (attempt - 1))
                logger.warning("%s for %s; sleeping %.1fs (attempt %d/%d)", resp.status_code, url, sleep_s, attempt, max_attempts)
                time.sleep(sleep_s)
                continue
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
        except requests.exceptions.Timeout:
            if attempt < max_attempts:
                sleep_s = base_backoff * (backoff_mult ** (attempt - 1))
                logger.warning("Timeout for %s; sleeping %.1fs (attempt %d/%d)", url, sleep_s, attempt, max_attempts)
                time.sleep(sleep_s)
                continue
            logger.error("Request timed out: %s", url)
            return None
        except requests.exceptions.RequestException as e:
            if attempt < max_attempts:
                sleep_s = base_backoff * (backoff_mult ** (attempt - 1))
                logger.warning("Request error for %s: %s; sleeping %.1fs (attempt %d/%d)", url, e, sleep_s, attempt, max_attempts)
                time.sleep(sleep_s)
                continue
            logger.error("Request failed for %s: %s", url, e)
            return None
    logger.error("Giving up after %d attempts for %s", max_attempts, url)
    return None


# --- Configuration helpers ---
_CONFIG_CACHE: Optional[dict] = None


def get_config(force_reload: bool = False) -> dict:
    """Load project configuration JSON.

    Looks for the path in CHRONO_CONFIG_PATH env var; falls back to 'config.json' in CWD.
    Caches the result unless force_reload is True.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE
    path = os.environ.get("CHRONO_CONFIG_PATH", "config.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _CONFIG_CACHE = json.load(f) or {}
        else:
            _CONFIG_CACHE = {}
    except Exception as e:
        logger.error("Failed to load config from %s: %s", path, e)
        _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def get_provider_setting(provider_key: str, setting: str, default=None):
    cfg = get_config()
    return (
        cfg.get("provider_settings", {})
        .get(provider_key, {})
        .get(setting, default)
    )
