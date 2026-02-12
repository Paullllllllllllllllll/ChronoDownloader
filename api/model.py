"""Data models for ChronoDownloader API.

Provides the SearchResult dataclass for unified provider responses
and conversion utilities for legacy dict-based results.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

class QuotaDeferredException(Exception):
    """Raised when a provider's quota is exhausted and download should be deferred.
    
    This exception signals that the download should be retried later when the quota
    resets, rather than blocking the entire pipeline or failing permanently.
    
    Attributes:
        provider: Name of the provider with exhausted quota
        reset_time: UTC datetime when the quota is expected to reset
        message: Human-readable description
    """
    
    def __init__(
        self,
        provider: str,
        reset_time: datetime | None = None,
        message: str | None = None,
    ):
        self.provider = provider
        self.reset_time = reset_time
        self.message = message or f"{provider}: Quota exhausted, download deferred"
        super().__init__(self.message)
    
    def __repr__(self) -> str:
        reset_str = self.reset_time.isoformat() if self.reset_time else "unknown"
        return f"QuotaDeferredException(provider={self.provider!r}, reset_time={reset_str})"

@dataclass
class SearchResult:
    """Canonical search result returned by connector search_* functions.

    Attributes:
        provider: Canonical provider name (e.g., "Internet Archive", "BnF Gallica")
        title: Work title
        creators: List of author/creator strings
        date: Date string or year if available
        source_id: Best available identifier for the provider (id, identifier, ark, etc.)
        iiif_manifest: If a manifest URL is directly known from the search
        item_url: Landing URL for the item, if available
        thumbnail_url: Thumbnail or cover URL if available
        provider_key: Machine-friendly provider key (e.g., "internet_archive", "bnf_gallica")
        raw: Original provider result payload (for backward compatibility with download_* functions)
    """

    provider: str
    title: str
    creators: list[str] = field(default_factory=list)
    date: str | None = None
    source_id: str | None = None
    iiif_manifest: str | None = None
    item_url: str | None = None
    thumbnail_url: str | None = None
    provider_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        """Convert SearchResult to dictionary.

        Args:
            include_raw: If True, include the raw provider payload

        Returns:
            Dictionary representation of the SearchResult
        """
        d = asdict(self)
        if not include_raw:
            d.pop("raw", None)
        return d

def _as_list(value: Any) -> list[str]:
    """Convert various input types to a list of strings.

    Handles None, strings (with comma splitting), lists, and other types.

    Args:
        value: Input value to convert

    Returns:
        List of string values
    """
    if value is None:
        return []
    
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    
    if isinstance(value, str):
        # Split on comma if it's a single string with commas
        if "," in value:
            return [v.strip() for v in value.split(",") if v.strip()]
        return [value]
    
    return [str(value)]

def resolve_item_field(
    item_data: SearchResult | dict,
    raw_key: str,
    *,
    attr: str | None = None,
    default: Any = None,
) -> Any:
    """Extract a field from a SearchResult or plain dict uniformly.

    For a ``SearchResult``, the function first checks the attribute given by
    *attr* (defaults to *raw_key*) and then falls back to ``raw[raw_key]``.
    For a plain ``dict``, it simply returns ``dict.get(raw_key, default)``.

    This eliminates the repeated ``isinstance(item_data, SearchResult)``
    branching found in every provider download function.

    Args:
        item_data: A ``SearchResult`` or a provider-specific dict.
        raw_key: Key to look up in the raw dict (or plain dict).
        attr: SearchResult attribute name to try first. Defaults to *raw_key*.
        default: Value returned when the field is missing everywhere.

    Returns:
        Resolved value or *default*.
    """
    if isinstance(item_data, SearchResult):
        sr_attr = attr or raw_key
        value = getattr(item_data, sr_attr, None)
        if value is not None:
            return value
        return item_data.raw.get(raw_key, default)
    if isinstance(item_data, dict):
        return item_data.get(raw_key, default)
    return default

def resolve_item_id(
    item_data: SearchResult | dict,
    *raw_keys: str,
) -> str | None:
    """Extract the primary identifier from a SearchResult or dict.

    For a ``SearchResult`` the lookup order is:
    ``source_id`` → ``raw[key]`` for each *raw_key* in order.
    For a plain ``dict``: ``dict[key]`` for each *raw_key* in order.

    Args:
        item_data: A ``SearchResult`` or a provider-specific dict.
        *raw_keys: One or more dict keys to try (e.g. ``"id"``, ``"identifier"``).
            Defaults to ``("id",)`` when omitted.

    Returns:
        The first non-empty string found, or ``None``.
    """
    keys = raw_keys or ("id",)
    if isinstance(item_data, SearchResult):
        if item_data.source_id:
            return item_data.source_id
        for key in keys:
            val = item_data.raw.get(key)
            if val:
                return str(val)
        return None
    if isinstance(item_data, dict):
        for key in keys:
            val = item_data.get(key)
            if val:
                return str(val)
    return None

def convert_to_searchresult(provider: str, data: dict[str, Any]) -> SearchResult:
    """Convert a provider-specific search result dict into a SearchResult.

    The original dict is preserved in .raw for downstream compatibility.

    Args:
        provider: Provider display name
        data: Provider-specific result dictionary

    Returns:
        Normalized SearchResult instance
    """
    # Extract title
    title = data.get("title") or data.get("name") or data.get("label") or "N/A"

    # Extract creators
    creators = []
    if "creators" in data:
        creators = _as_list(data.get("creators"))
    elif "creator" in data:
        creators = _as_list(data.get("creator"))
    elif "contributor_names" in data:
        creators = _as_list(data.get("contributor_names"))

    # Extract date
    date = None
    for key in ("date", "year", "issued", "publication_date"):
        if key in data and data.get(key):
            date = str(data.get(key))
            break

    # Extract source ID
    source_id = None
    for key in ("id", "identifier", "ark_id", "source_id", "uid"):
        if key in data and data.get(key):
            source_id = str(data.get(key))
            break

    # Extract URLs
    iiif_manifest = data.get("iiif_manifest") or data.get("manifest")
    item_url = data.get("item_url") or data.get("url") or data.get("guid")
    thumbnail_url = data.get("thumbnail") or data.get("thumbnail_url") or data.get("image")

    return SearchResult(
        provider=provider,
        title=title,
        creators=creators,
        date=date,
        source_id=source_id,
        iiif_manifest=iiif_manifest,
        item_url=item_url,
        thumbnail_url=thumbnail_url,
        raw=data,
    )
