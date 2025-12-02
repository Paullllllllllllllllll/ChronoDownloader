"""Network utilities for HTTP requests, rate limiting, and session management.

Provides centralized HTTP session with retries, per-provider rate limiting,
and robust error handling for API calls and file downloads.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, Optional, Union
from urllib.parse import urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry

from .config import get_network_config

logger = logging.getLogger(__name__)

# Global session (lazy-initialized)
_SESSION: Optional[requests.Session] = None

# Map URL hostnames to provider keys for rate limiting and policies
PROVIDER_HOST_MAP: Dict[str, tuple[str, ...]] = {
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
    "hathitrust": ("catalog.hathitrust.org", "babel.hathitrust.org"),
    "annas_archive": ("annas-archive.org", "annas-archive.se", "annas-archive.li"),
}


class RateLimiter:
    """Simple per-provider rate limiter with jitter, using monotonic time."""

    def __init__(self, min_interval_s: float = 0.0, jitter_s: float = 0.0):
        self.min_interval_s = max(0.0, float(min_interval_s or 0.0))
        self.jitter_s = max(0.0, float(jitter_s or 0.0))
        self._last_ts = 0.0

    def wait(self) -> None:
        """Wait until the minimum interval has passed since the last request."""
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


# Per-provider rate limiter instances
_RATE_LIMITERS: Dict[str, RateLimiter] = {}


def get_provider_for_url(url: str) -> Optional[str]:
    """Determine the provider key for a given URL.
    
    Args:
        url: URL to check
        
    Returns:
        Provider key or None if not recognized
    """
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None
    
    # Strip port if present
    if ":" in host:
        host = host.split(":", 1)[0]
    
    # Match exact domain or subdomain of known host parts
    def _host_matches(h: str, part: str) -> bool:
        return h == part or h.endswith("." + part)
    
    for provider, host_parts in PROVIDER_HOST_MAP.items():
        for part in host_parts:
            if _host_matches(host, part):
                return provider
    
    return None


def get_rate_limiter(provider_key: Optional[str]) -> Optional[RateLimiter]:
    """Get or create a rate limiter for a provider.
    
    Args:
        provider_key: Provider identifier
        
    Returns:
        RateLimiter instance or None if no rate limiting configured
    """
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


def build_session() -> requests.Session:
    """Build a configured requests session with retries and default headers.
    
    Returns:
        Configured Session instance
    """
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
    session.headers.update({
        # Use a modern browser-like UA to avoid occasional 403s from some providers
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        # Encourage English-language responses and better cache hits across providers
        "Accept-Language": "en-US,en;q=0.9",
    })
    
    # Silence warnings when a provider's policy mandates an insecure retry once
    try:
        urllib3.disable_warnings(InsecureRequestWarning)
    except Exception:
        pass
    
    return session


def get_session() -> requests.Session:
    """Get the global HTTP session (lazy initialization).
    
    Returns:
        Configured Session instance
    """
    global _SESSION
    if _SESSION is None:
        _SESSION = build_session()
    return _SESSION


def make_request(
    url: str,
    params: Optional[Dict] = None,
    headers: Optional[Dict] = None,
    timeout: int = 15,
) -> Optional[Union[Dict, str, bytes]]:
    """HTTP GET with centralized per-provider pacing and backoff.
    
    Args:
        url: URL to request
        params: Query parameters
        headers: Additional headers
        timeout: Request timeout in seconds (overridden by provider config)
        
    Returns:
        - dict for JSON responses
        - str for text/xml/html
        - bytes for other/binary content
        - None on error
    """
    session = get_session()
    provider = get_provider_for_url(url)
    net = get_network_config(provider)
    
    max_attempts = int(net.get("max_attempts", 5) or 5)
    base_backoff = float(net.get("base_backoff_s", 1.5) or 1.5)
    backoff_mult = float(net.get("backoff_multiplier", 1.5) or 1.5)
    max_backoff = float(net.get("max_backoff_s", 60.0) or 60.0)
    net_timeout = net.get("timeout_s")
    effective_timeout = float(net_timeout) if net_timeout is not None else float(timeout)
    
    rl = get_rate_limiter(provider)
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
            
            resp = session.get(
                url,
                params=params,
                headers=req_headers or None,
                timeout=effective_timeout,
                verify=verify
            )
            
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
                    sleep_s = min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)
                else:
                    sleep_s = min(sleep_s, max_backoff)
                
                logger.warning(
                    "429 Too Many Requests for %s; sleeping %.1fs (attempt %d/%d)",
                    url, sleep_s, attempt, max_attempts
                )
                time.sleep(sleep_s)
                continue
            
            # Retry transient 5xx
            if resp.status_code in (500, 502, 503, 504):
                sleep_s = min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)
                logger.warning(
                    "%s for %s; sleeping %.1fs (attempt %d/%d)",
                    resp.status_code, url, sleep_s, attempt, max_attempts
                )
                time.sleep(sleep_s)
                continue
            
            # Non-retryable client errors
            if resp.status_code in (400, 401, 403, 404, 410, 422):
                logger.warning("Non-retryable HTTP %s for %s; not retrying", resp.status_code, url)
                return None
            
            resp.raise_for_status()
            
            # Parse response based on content type
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
                sleep_s = min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)
                logger.warning(
                    "Timeout for %s; sleeping %.1fs (attempt %d/%d)",
                    url, sleep_s, attempt, max_attempts
                )
                time.sleep(sleep_s)
                continue
            logger.error("Request timed out: %s", url)
            return None
            
        except requests.exceptions.RequestException as e:
            msg = str(e).lower()
            
            # Handle DNS/Name resolution errors
            if any(term in msg for term in [
                "nameresolutionerror",
                "failed to resolve",
                "getaddrinfo failed",
                "temporary failure in name resolution"
            ]):
                dns_retry = bool(net.get("dns_retry", False))
                if dns_retry and attempt < max_attempts:
                    sleep_s = min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)
                    logger.warning(
                        "Name resolution error for %s: %s; dns_retry=true, sleeping %.1fs (attempt %d/%d)",
                        url, e, sleep_s, attempt, max_attempts
                    )
                    time.sleep(sleep_s)
                    continue
                logger.warning("Name resolution error for %s: %s; not retrying", url, e)
                return None
            
            # Handle SSL certificate verification errors
            if isinstance(e, requests.exceptions.SSLError) or any(term in msg for term in [
                "certificate verify failed",
                "sslcertverificationerror",
                "ssl: certificate_verify_failed"
            ]):
                if not verify and insecure_retry_used:
                    logger.warning(
                        "SSL error (insecure retry already used) for %s: %s; not retrying",
                        url, e
                    )
                    return None
                
                if ssl_policy == "retry_insecure_once" and verify:
                    logger.warning(
                        "SSL verify failed for %s; retrying once with verify=False due to policy.",
                        url
                    )
                    verify = False
                    insecure_retry_used = True
                    continue
                
                logger.warning("SSL certificate verification error for %s: %s; not retrying", url, e)
                return None
            
            # Generic retry for other errors
            if attempt < max_attempts:
                sleep_s = min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)
                logger.warning(
                    "Request error for %s: %s; sleeping %.1fs (attempt %d/%d)",
                    url, e, sleep_s, attempt, max_attempts
                )
                time.sleep(sleep_s)
                continue
            
            logger.error("Request failed for %s: %s", url, e)
            return None
    
    logger.error("Giving up after %d attempts for %s", max_attempts, url)
    return None
