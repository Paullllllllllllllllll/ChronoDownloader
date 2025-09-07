import os
import re
import json
import logging
import requests
import time
import random
import threading
from urllib.parse import unquote, urlparse
from email.utils import parsedate_to_datetime
from datetime import datetime
from pathlib import Path
from typing import Optional, Union, Dict, List, Any, Tuple
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_SESSION: Optional[requests.Session] = None

# Map URL hostnames to provider keys for centralized per-provider rate limiting and policies
_PROVIDER_HOST_MAP: dict[str, tuple[str, ...]] = {
    "gallica": ("gallica.bnf.fr",),
    "british_library": ("api.bl.uk", "sru.bl.uk", "iiif.bl.uk", "access.bl.uk", "bnb.data.bl.uk"),
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
    # Add HathiTrust hosts so provider-specific network policies apply
    "hathitrust": ("catalog.hathitrust.org", "babel.hathitrust.org"),
}

# Preferred short slugs for provider registry keys (snake_case, lower)
_PROVIDER_SLUGS: dict[str, str] = {
    "bnf_gallica": "gallica",
    "british_library": "bl",
    "mdz": "mdz",
    "europeana": "europeana",
    "wellcome": "wellcome",
    "loc": "loc",
    "ddb": "ddb",
    "polona": "polona",
    "bne": "bne",
    "dpla": "dpla",
    "internet_archive": "ia",
    "google_books": "gb",
    "hathitrust": "hathi",
}

# Abbreviations for providers (keys correspond to _PROVIDER_HOST_MAP keys)
_PROVIDER_ABBREV: dict[str, str] = {
    # Host-map keys
    "gallica": "GAL",
    "british_library": "BL",
    "mdz": "MDZ",
    "europeana": "EUROPEANA",
    "wellcome": "WELLCOME",
    "loc": "LOC",
    "ddb": "DDB",
    "polona": "POLONA",
    "bne": "BNE",
    "dpla": "DPLA",
    "internet_archive": "IA",
    "google_books": "GB",
    # Provider registry keys (may differ from host-map keys)
    "bnf_gallica": "GAL",
    "hathitrust": "HATHI",
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
    # Strip port if present
    if ":" in host:
        host = host.split(":", 1)[0]
    # Match exact domain or subdomain of known host parts, but not arbitrary suffixes
    def _host_matches(h: str, part: str) -> bool:
        return h == part or h.endswith("." + part)
    for provider, host_parts in _PROVIDER_HOST_MAP.items():
        for part in host_parts:
            if _host_matches(host, part):
                return provider
    return None


# ---------------- Centralized download budgeting & context -----------------

class _DownloadBudget:
    """Tracks and enforces download limits across the whole run.

    Limits are configured under config.json -> download_limits:
    {
      "max_total_files": 0,            # 0 or missing = unlimited
      "max_total_bytes": 0,
      "per_work": { "max_files": 0, "max_bytes": 0 },
      "per_provider": { "mdz": {"max_files": 0, "max_bytes": 0}, ... },
      "on_exceed": "skip"             # "skip" | "stop"
    }
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.total_files = 0
        self.total_bytes = 0
        self.per_work: Dict[str, Dict[str, int]] = {}
        self.per_provider: Dict[str, Dict[str, int]] = {}
        self._exhausted = False

    # ---- helpers ----
    @staticmethod
    def _limit_value(v: Any) -> Optional[int]:
        try:
            iv = int(v)
            return iv if iv > 0 else None
        except Exception:
            return None

    @staticmethod
    def _limits() -> Dict[str, Any]:
        cfg = get_config()
        return dict(cfg.get("download_limits", {}) or {})

    def _policy(self) -> str:
        dl = self._limits()
        pol = str(dl.get("on_exceed", "skip") or "skip").lower()
        return "stop" if pol == "stop" else "skip"

    def exhausted(self) -> bool:
        with self._lock:
            return self._exhausted

    def _inc(self, bucket: Dict[str, Dict[str, int]], key: str, field: str, delta: int) -> int:
        m = bucket.setdefault(key, {"files": 0, "bytes": 0})
        m[field] = int(m.get(field, 0)) + int(delta)
        return m[field]

    def _get(self, bucket: Dict[str, Dict[str, int]], key: str, field: str) -> int:
        return int(bucket.get(key, {}).get(field, 0))

    # ---- checks ----
    def allow_new_file(self, provider: Optional[str], work_id: Optional[str]) -> bool:
        dl = self._limits()
        max_total_files = self._limit_value(dl.get("max_total_files"))
        if max_total_files is not None and (self.total_files + 1) > max_total_files:
            if self._policy() == "stop":
                with self._lock:
                    self._exhausted = True
            return False
        if provider:
            per = dict(dl.get("per_provider", {}) or {})
            pl = self._limit_value((per.get(provider) or {}).get("max_files"))
            if pl is not None and (self._get(self.per_provider, provider, "files") + 1) > pl:
                if self._policy() == "stop":
                    with self._lock:
                        self._exhausted = True
                return False
        if work_id:
            pw = dict(dl.get("per_work", {}) or {})
            wl = self._limit_value(pw.get("max_files"))
            if wl is not None and (self._get(self.per_work, work_id, "files") + 1) > wl:
                if self._policy() == "stop":
                    with self._lock:
                        self._exhausted = True
                return False
        return True

    def allow_bytes(self, provider: Optional[str], work_id: Optional[str], add_bytes: Optional[int]) -> bool:
        if not add_bytes or add_bytes <= 0:
            return True
        dl = self._limits()
        mtb = self._limit_value(dl.get("max_total_bytes"))
        if mtb is not None and (self.total_bytes + add_bytes) > mtb:
            if self._policy() == "stop":
                with self._lock:
                    self._exhausted = True
            return False
        if provider:
            per = dict(dl.get("per_provider", {}) or {})
            pl = self._limit_value((per.get(provider) or {}).get("max_bytes"))
            if pl is not None and (self._get(self.per_provider, provider, "bytes") + add_bytes) > pl:
                if self._policy() == "stop":
                    with self._lock:
                        self._exhausted = True
                return False
        if work_id:
            pw = dict(dl.get("per_work", {}) or {})
            wl = self._limit_value(pw.get("max_bytes"))
            if wl is not None and (self._get(self.per_work, work_id, "bytes") + add_bytes) > wl:
                if self._policy() == "stop":
                    with self._lock:
                        self._exhausted = True
                return False
        return True

    # ---- mutators ----
    def add_bytes(self, provider: Optional[str], work_id: Optional[str], n: int) -> bool:
        """Add bytes to counters; return True if still within limits, False if exceeded.

        If exceeded and policy is 'stop', mark exhausted.
        """
        if n <= 0:
            return True
        with self._lock:
            self.total_bytes += n
            if provider:
                self._inc(self.per_provider, provider, "bytes", n)
            if work_id:
                self._inc(self.per_work, work_id, "bytes", n)
            ok = self.allow_bytes(provider, work_id, 0)
            if not ok and self._policy() == "stop":
                self._exhausted = True
            return ok

    def add_file(self, provider: Optional[str], work_id: Optional[str]) -> bool:
        with self._lock:
            self.total_files += 1
            if provider:
                self._inc(self.per_provider, provider, "files", 1)
            if work_id:
                self._inc(self.per_work, work_id, "files", 1)
            ok = self.allow_new_file(provider, work_id)
            if not ok and self._policy() == "stop":
                self._exhausted = True
            return ok


_BUDGET = _DownloadBudget()

# Thread-local current work context so providers don't need to pass work_id around
_TLS = threading.local()
setattr(_TLS, "work_id", None)
setattr(_TLS, "entry_id", None)
setattr(_TLS, "provider_key", None)
setattr(_TLS, "name_stem", None)  # e.g., e_0001_the_raven
setattr(_TLS, "counters", {})     # per-entry counters for numbering


def set_current_work(work_id: Optional[str]) -> None:
    setattr(_TLS, "work_id", work_id)


def clear_current_work() -> None:
    setattr(_TLS, "work_id", None)


def set_current_entry(entry_id: Optional[str]) -> None:
    setattr(_TLS, "entry_id", entry_id)


def clear_current_entry() -> None:
    setattr(_TLS, "entry_id", None)


def set_current_provider(provider_key: Optional[str]) -> None:
    setattr(_TLS, "provider_key", provider_key)


def clear_current_provider() -> None:
    setattr(_TLS, "provider_key", None)


def set_current_name_stem(stem: Optional[str]) -> None:
    setattr(_TLS, "name_stem", stem)


def clear_current_name_stem() -> None:
    setattr(_TLS, "name_stem", None)


def _current_name_stem() -> Optional[str]:
    try:
        return getattr(_TLS, "name_stem", None)
    except Exception:
        return None


def _counters() -> Dict[Tuple[str, str, str], int]:
    try:
        c = getattr(_TLS, "counters", None)
        if c is None:
            c = {}
            setattr(_TLS, "counters", c)
        return c
    except Exception:
        # Fall back to a module-level dictionary if TLS fails
        if not hasattr(_counters, "_fallback"):
            setattr(_counters, "_fallback", {})
        return getattr(_counters, "_fallback")


def reset_counters() -> None:
    try:
        setattr(_TLS, "counters", {})
    except Exception:
        pass


def _current_work_id() -> Optional[str]:
    try:
        return getattr(_TLS, "work_id", None)
    except Exception:
        return None


def _current_entry_id() -> Optional[str]:
    try:
        return getattr(_TLS, "entry_id", None)
    except Exception:
        return None


def _current_provider_key() -> Optional[str]:
    try:
        return getattr(_TLS, "provider_key", None)
    except Exception:
        return None


def budget_exhausted() -> bool:
    return _BUDGET.exhausted()


def _prefer_pdf_over_images() -> bool:
    cfg = get_config()
    dl = cfg.get("download", {}) or {}
    val = dl.get("prefer_pdf_over_images")
    return True if val is None else bool(val)


def _overwrite_existing() -> bool:
    cfg = get_config()
    dl = cfg.get("download", {}) or {}
    return bool(dl.get("overwrite_existing", False))


# Public wrappers for config-driven preferences
def prefer_pdf_over_images() -> bool:
    return _prefer_pdf_over_images()


def overwrite_existing() -> bool:
    return _overwrite_existing()


def _include_metadata() -> bool:
    cfg = get_config()
    dl = cfg.get("download", {}) or {}
    val = dl.get("include_metadata")
    return True if val is None else bool(val)


def include_metadata() -> bool:
    return _include_metadata()


def to_snake_case(value: str) -> str:
    """Convert arbitrary string to snake_case: lowercase, alnum + underscores only."""
    if value is None:
        return ""
    s = str(value)
    # Replace non-alnum with underscores
    s = re.sub(r"[^0-9A-Za-z]+", "_", s)
    # Insert underscore between letter-number boundaries (e.g., e0001 -> e_0001)
    s = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", s)
    s = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", s)
    # Collapse underscores
    s = re.sub(r"_+", "_", s)
    # Trim underscores and lowercase
    s = s.strip("_").lower()
    return s


def _provider_slug(pref_key: Optional[str], url_provider: Optional[str]) -> str:
    key = pref_key or url_provider or "unknown"
    # Prefer mapped short slug
    if key in _PROVIDER_SLUGS:
        return _PROVIDER_SLUGS[key]
    # Otherwise snake-case the key as best effort
    return to_snake_case(key)


def download_iiif_renderings(manifest: Dict[str, Any], folder_path: str, filename_prefix: str = "") -> int:
    """Download files referenced in IIIF manifest-level 'rendering' entries.

    Many IIIF manifests include a top-level 'rendering' array with alternate formats
    such as application/pdf or application/epub+zip. This helper downloads a small
    number of such files according to config:

    config.download:
      - download_manifest_renderings: true|false (default true)
      - rendering_mime_whitelist: ["application/pdf", "application/epub+zip"]
      - max_renderings_per_manifest: 1
    """
    cfg = get_config()
    dcfg = cfg.get("download", {}) or {}
    if dcfg.get("download_manifest_renderings", True) is False:
        return 0
    whitelist: List[str] = [
        str(m).lower() for m in (dcfg.get("rendering_mime_whitelist") or ["application/pdf", "application/epub+zip"]) if m
    ]
    try:
        limit = int(dcfg.get("max_renderings_per_manifest", 1) or 1)
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

    candidates: List[Dict[str, Any]] = []
    candidates += _collect_renderings(manifest)
    # Also consider provider-level metadata block in v3: manifest.get('rendering') already covers
    # Per-canvas renderings are not considered here (per-page PDFs), to keep volume small.

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
        "timeout_s": null,
        "verify_ssl": true,
        "ssl_error_policy": "fail",              # "fail" | "retry_insecure_once"
        "dns_retry": false,                       # if true, retry on DNS errors with backoff
        "headers": { }                            # optional per-provider default headers
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
    net.setdefault("verify_ssl", True)
    net.setdefault("ssl_error_policy", "fail")
    net.setdefault("dns_retry", False)
    # Ensure headers is a dict if provided
    if not isinstance(net.get("headers", {}), dict):
        net["headers"] = {}
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
    # Avoid urllib3 retries on connection errors (DNS/SSL) so our outer logic can decide quickly.
    # Keep a small retry budget for read timeouts and HTTP status where it helps.
    retry = Retry(
        total=3,
        connect=0,  # no retries on connection errors (e.g., DNS/SSL)
        read=2,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
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
            # Encourage English-language responses and better cache hits across providers
            "Accept-Language": "en-US,en;q=0.9",
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
    verify_default = bool(net.get("verify_ssl", True))
    ssl_policy = str(net.get("ssl_error_policy", "fail") or "fail").lower()
    provider_headers = dict(net.get("headers", {}) or {})
    # Merge headers: session defaults < provider headers
    req_headers = {}
    if provider_headers:
        req_headers.update({str(k): str(v) for k, v in provider_headers.items() if v is not None})
    rl = _get_rate_limiter(provider)
    work_id = _current_work_id()
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
                    # Retry-After may be seconds or HTTP-date
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
                # Non-retryable client errors
                if response.status_code in (400, 401, 403, 404, 410, 422):
                    logger.error("Non-retryable HTTP %s for %s; aborting download", response.status_code, url)
                    return None
                response.raise_for_status()
                cd_name = _filename_from_content_disposition(response.headers.get("Content-Disposition"))
                # Determine extension and type up front

                # If no explicit extension, try to infer from URL or Content-Type
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

                # Determine target directory
                target_dir = os.path.join(folder_path, "objects")
                os.makedirs(target_dir, exist_ok=True)

                # Resolve sequence number
                key = (stem, prov_slug, type_key)
                counters = _counters()
                counters[key] = int(counters.get(key, 0)) + 1
                seq = counters[key]

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
                if not _overwrite_existing() and os.path.exists(filepath):
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
                    logger.warning("Download budget (bytes) would be exceeded by %s (%d bytes); skipping.", url, content_len)
                    return None
                with open(filepath, "wb") as f:
                    truncated = False
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        # Enforce byte budget dynamically for unknown sizes
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
                    # Fallback to generic name if needed
                    cd_name = _filename_from_content_disposition(response.headers.get("Content-Disposition"))
                    inferred_ext = Path(cd_name or "").suffix or ".bin"
                    stem = _current_name_stem() or to_snake_case(filename) or "object"
                    prov_slug = _provider_slug(_current_provider_key(), provider)
                    target_dir = os.path.join(folder_path, "objects")
                    os.makedirs(target_dir, exist_ok=True)
                    safe_name = sanitize_filename(f"{stem}_{prov_slug}{inferred_ext}")
                    filepath = os.path.join(target_dir, safe_name)
                    with open(filepath, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
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


def save_json(data, folder_path, filename) -> Optional[str]:
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
    verify_default = bool(net.get("verify_ssl", True))
    ssl_policy = str(net.get("ssl_error_policy", "fail") or "fail").lower()
    provider_headers = dict(net.get("headers", {}) or {})
    # Merge headers: session defaults < provider headers < per-call headers
    req_headers = {}
    if provider_headers:
        req_headers.update({str(k): str(v) for k, v in provider_headers.items() if v is not None})
    if headers:
        req_headers.update(headers)

    insecure_retry_used = False
    verify = verify_default
    for attempt in range(1, max_attempts + 1):
        try:
            if rl:
                rl.wait()
            resp = session.get(url, params=params, headers=req_headers or None, timeout=effective_timeout, verify=verify)
            # Explicit 429 handling with Retry-After
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
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
                    sleep_s = base_backoff * (backoff_mult ** (attempt - 1))
                logger.warning("429 Too Many Requests for %s; sleeping %.1fs (attempt %d/%d)", url, sleep_s, attempt, max_attempts)
                time.sleep(sleep_s)
                continue
            if resp.status_code in (500, 502, 503, 504):
                sleep_s = base_backoff * (backoff_mult ** (attempt - 1))
                logger.warning("%s for %s; sleeping %.1fs (attempt %d/%d)", resp.status_code, url, sleep_s, attempt, max_attempts)
                time.sleep(sleep_s)
                continue
            # Non-retryable client errors
            if resp.status_code in (400, 401, 403, 404, 410, 422):
                logger.warning("Non-retryable HTTP %s for %s; not retrying", resp.status_code, url)
                return None
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
            # Treat DNS/Name resolution errors according to policy (default non-retryable)
            msg = str(e).lower()
            if (
                "nameresolutionerror" in msg
                or "failed to resolve" in msg
                or "getaddrinfo failed" in msg
                or "temporary failure in name resolution" in msg
            ):
                dns_retry = bool(net.get("dns_retry", False))
                if dns_retry and attempt < max_attempts:
                    sleep_s = base_backoff * (backoff_mult ** (attempt - 1))
                    logger.warning("Name resolution error for %s: %s; dns_retry=true, sleeping %.1fs (attempt %d/%d)", url, e, sleep_s, attempt, max_attempts)
                    time.sleep(sleep_s)
                    continue
                logger.warning("Name resolution error for %s: %s; not retrying", url, e)
                return None
            # Treat SSL certificate verification errors as non-retryable as well
            if isinstance(e, requests.exceptions.SSLError) or (
                "certificate verify failed" in msg
                or "sslcertverificationerror" in msg
                or "ssl: certificate_verify_failed" in msg
            ):
                if not verify and insecure_retry_used:
                    # We already tried insecure; give up
                    logger.warning("SSL error (insecure retry already used) for %s: %s; not retrying", url, e)
                    return None
                if ssl_policy == "retry_insecure_once" and verify:
                    logger.warning("SSL verify failed for %s; retrying once with verify=False due to policy.", url)
                    verify = False
                    insecure_retry_used = True
                    # retry immediately without sleeping/backoff
                    continue
                logger.warning("SSL certificate verification error for %s: %s; not retrying", url, e)
                return None
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
    ps = cfg.get("provider_settings", {})
    # Map known aliases to config keys
    aliases = {
        "bnf_gallica": "gallica",
    }
    key = provider_key
    if key not in ps:
        key = aliases.get(provider_key, provider_key)
    return ps.get(key, {}).get(setting, default)
