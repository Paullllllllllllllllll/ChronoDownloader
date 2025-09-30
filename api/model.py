"""Data models for ChronoDownloader API.

Provides the SearchResult dataclass for unified provider responses
and conversion utilities for legacy dict-based results.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


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
    creators: List[str] = field(default_factory=list)
    date: Optional[str] = None
    source_id: Optional[str] = None
    iiif_manifest: Optional[str] = None
    item_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    provider_key: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
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


def _as_list(value: Any) -> List[str]:
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


def convert_to_searchresult(provider: str, data: Dict[str, Any]) -> SearchResult:
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
