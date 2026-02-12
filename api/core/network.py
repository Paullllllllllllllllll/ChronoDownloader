"""Network utilities for HTTP requests, rate limiting, and session management.

Provides centralized HTTP session with retries, per-provider rate limiting,
circuit breaker pattern, and robust error handling for API calls and file downloads.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry

from .config import get_network_config

logger = logging.getLogger(__name__)

# =============================================================================
# Circuit Breaker Pattern
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Provider disabled due to failures
    HALF_OPEN = "half_open"  # Testing if provider recovered

@dataclass
class CircuitBreaker:
    """Circuit breaker for a single provider.
    
    Tracks consecutive failures and temporarily disables providers that are
    consistently failing (e.g., due to rate limiting).
    """
    failure_threshold: int = 3  # Consecutive failures before opening
    cooldown_seconds: float = 300.0  # How long circuit stays open (5 min default)
    
    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = field(default=0)
    last_failure_time: float = field(default=0.0)
    opened_at: float = field(default=0.0)
    
    def record_success(self) -> None:
        """Record a successful request - resets failure count and closes circuit."""
        if self.state == CircuitState.HALF_OPEN:
            logger.info("Circuit breaker: Provider recovered, closing circuit")
        self.failure_count = 0
        self.state = CircuitState.CLOSED
    
    def record_failure(self, provider: str = "") -> None:
        """Record a failure (e.g., 429). May open the circuit."""
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        
        if self.state == CircuitState.HALF_OPEN:
            # Failed during test - reopen circuit
            self.state = CircuitState.OPEN
            self.opened_at = time.monotonic()
            logger.warning(
                "Circuit breaker: %s still failing, reopening circuit for %.0fs",
                provider or "Provider", self.cooldown_seconds
            )
        elif self.failure_count >= self.failure_threshold and self.state == CircuitState.CLOSED:
            self.state = CircuitState.OPEN
            self.opened_at = time.monotonic()
            logger.warning(
                "Circuit breaker: %s hit %d consecutive failures, disabling for %.0fs",
                provider or "Provider", self.failure_count, self.cooldown_seconds
            )
    
    def allow_request(self) -> bool:
        """Check if a request should be allowed.
        
        Returns:
            True if request can proceed, False if circuit is open
        """
        if self.state == CircuitState.CLOSED:
            return True
        
        if self.state == CircuitState.OPEN:
            # Check if cooldown has elapsed
            elapsed = time.monotonic() - self.opened_at
            if elapsed >= self.cooldown_seconds:
                self.state = CircuitState.HALF_OPEN
                logger.info(
                    "Circuit breaker: Cooldown elapsed (%.0fs), testing provider",
                    elapsed
                )
                return True
            return False
        
        # HALF_OPEN: allow the test request
        return True
    
    def time_until_retry(self) -> float:
        """Get seconds until circuit will allow requests again.
        
        Returns:
            Seconds remaining, or 0 if requests are allowed
        """
        if self.state != CircuitState.OPEN:
            return 0.0
        elapsed = time.monotonic() - self.opened_at
        return max(0.0, self.cooldown_seconds - elapsed)

# Per-provider circuit breakers
_CIRCUIT_BREAKERS: dict[str, CircuitBreaker] = {}

def get_circuit_breaker(provider_key: str | None) -> CircuitBreaker | None:
    """Get or create a circuit breaker for a provider.
    
    Args:
        provider_key: Provider identifier
        
    Returns:
        CircuitBreaker instance or None if circuit breaker disabled
    """
    if not provider_key:
        return None
    
    net = get_network_config(provider_key)
    
    # Check if circuit breaker is enabled for this provider
    if not net.get("circuit_breaker_enabled", True):
        return None
    
    threshold = int(net.get("circuit_breaker_threshold", 3) or 3)
    cooldown = float(net.get("circuit_breaker_cooldown_s", 300.0) or 300.0)
    
    cb = _CIRCUIT_BREAKERS.get(provider_key)
    if cb is None:
        cb = CircuitBreaker(failure_threshold=threshold, cooldown_seconds=cooldown)
        _CIRCUIT_BREAKERS[provider_key] = cb
    else:
        # Update settings if changed
        cb.failure_threshold = threshold
        cb.cooldown_seconds = cooldown
    
    return cb

def is_provider_available(provider_key: str | None) -> bool:
    """Check if a provider is currently available (circuit not open).
    
    Args:
        provider_key: Provider identifier
        
    Returns:
        True if provider can be used, False if circuit is open
    """
    cb = get_circuit_breaker(provider_key)
    if cb is None:
        return True
    return cb.allow_request()

def get_provider_cooldown(provider_key: str | None) -> float:
    """Get remaining cooldown time for a provider.
    
    Args:
        provider_key: Provider identifier
        
    Returns:
        Seconds until provider is available, or 0 if available now
    """
    cb = get_circuit_breaker(provider_key)
    if cb is None:
        return 0.0
    return cb.time_until_retry()

# Global session (lazy-initialized)
_SESSION: requests.Session | None = None

# Map URL hostnames to provider keys for rate limiting and policies
PROVIDER_HOST_MAP: dict[str, tuple[str, ...]] = {
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
    "annas_archive": ("annas-archive.li", "annas-archive.pm", "annas-archive.in"),
    "slub": ("data.slub-dresden.de", "digital.slub-dresden.de", "iiif.slub-dresden.de"),
    "e_rara": ("www.e-rara.ch", "e-rara.ch"),
    "sbb_digital": (
        "sru.gbv.de",
        "digital.staatsbibliothek-berlin.de",
        "content.staatsbibliothek-berlin.de",
        "oai.sbb.berlin",
    ),
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
_RATE_LIMITERS: dict[str, RateLimiter] = {}

# =============================================================================
# Rate Limiter Functions
# =============================================================================

def get_provider_for_url(url: str) -> str | None:
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

def get_rate_limiter(provider_key: str | None) -> RateLimiter | None:
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
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 15,
) -> dict | str | bytes | None:
    """HTTP GET with centralized per-provider pacing, backoff, and circuit breaker.
    
    Args:
        url: URL to request
        params: Query parameters
        headers: Additional headers
        timeout: Request timeout in seconds (overridden by provider config)
        
    Returns:
        - dict for JSON responses
        - str for text/xml/html
        - bytes for other/binary content
        - None on error (including circuit breaker open)
    """
    session = get_session()
    provider = get_provider_for_url(url)
    net = get_network_config(provider)
    
    # Check circuit breaker before making any requests
    cb = get_circuit_breaker(provider)
    if cb and not cb.allow_request():
        remaining = cb.time_until_retry()
        logger.warning(
            "Circuit breaker OPEN for %s; skipping request (retry in %.0fs): %s",
            provider or "unknown", remaining, url
        )
        return None
    
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
    
    # Track if we hit 429 during this request (for circuit breaker)
    hit_rate_limit = False
    
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
                hit_rate_limit = True
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
            
            # Success! Record it for circuit breaker
            if cb:
                cb.record_success()
            
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
    
    # Record failure for circuit breaker if we exhausted retries due to rate limiting
    if cb and hit_rate_limit:
        cb.record_failure(provider or "unknown")
    
    logger.error("Giving up after %d attempts for %s", max_attempts, url)
    return None

def make_json_request(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 15,
) -> dict[str, Any] | None:
    """HTTP GET expecting a JSON response, with type-safe return.
    
    This is a convenience wrapper around make_request() that returns only
    dict or None, making it suitable for API calls where JSON is expected.
    
    Args:
        url: URL to request
        params: Query parameters
        headers: Additional headers
        timeout: Request timeout in seconds
        
    Returns:
        Parsed JSON dict or None on error/non-JSON response
    """
    result = make_request(url, params=params, headers=headers, timeout=timeout)
    if isinstance(result, dict):
        return result
    return None
