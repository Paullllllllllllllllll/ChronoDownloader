"""Filename sanitization and naming conventions for ChronoDownloader.

Provides utilities for converting strings to safe filenames, generating snake_case identifiers,
and building standardized file names with provider slugs and sequence numbers.
"""
from __future__ import annotations

import re
from pathlib import Path

# Provider slug mappings for abbreviated provider keys
PROVIDER_SLUGS = {
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
    "annas_archive": "annas",
}

# Provider abbreviations for display/logging
PROVIDER_ABBREV = {
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
    "annas_archive": "ANNAS",
}


def to_snake_case(value: str) -> str:
    """Convert arbitrary string to snake_case: lowercase, alnum + underscores only.
    
    Args:
        value: Input string to convert
        
    Returns:
        Snake-cased string suitable for identifiers
    """
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


def sanitize_filename(name: str, max_base_len: int = 100) -> str:
    """Sanitize string for safe filenames while preserving extension.
    
    - Keeps the original extension(s) intact (e.g., .pdf, .tar.gz).
    - Limits the base name length only.
    - Removes illegal filesystem characters.
    
    Args:
        name: Input filename or path
        max_base_len: Maximum length for the base name (before extension)
        
    Returns:
        Sanitized filename safe for most filesystems
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


def _split_name_and_suffixes(name: str) -> tuple[str, str]:
    """Split a filename into base name and extension(s).
    
    Preserves multi-suffix like .tar.gz.
    
    Args:
        name: Filename to split
        
    Returns:
        Tuple of (base_name, extensions)
    """
    base = Path(name).name
    suffixes = Path(base).suffixes
    ext = "".join(suffixes)
    
    if ext:
        base_no_ext = base[: -len(ext)]
    else:
        base_no_ext = base
    
    return base_no_ext, ext


def get_provider_slug(pref_key: str | None, url_provider: str | None) -> str:
    """Get a short slug identifier for a provider.
    
    Args:
        pref_key: Preferred provider key from context
        url_provider: Provider key derived from URL
        
    Returns:
        Short slug identifier
    """
    key = pref_key or url_provider or "unknown"
    
    # Prefer mapped short slug
    if key in PROVIDER_SLUGS:
        return PROVIDER_SLUGS[key]
    
    # Otherwise snake-case the key as best effort
    return to_snake_case(key)


def get_provider_abbrev(provider_key: str) -> str:
    """Get a display abbreviation for a provider.
    
    Args:
        provider_key: Provider identifier
        
    Returns:
        Display abbreviation (uppercase)
    """
    return PROVIDER_ABBREV.get(provider_key, provider_key.upper())


def build_work_directory_name(
    entry_id: str | None,
    title: str,
    max_len: int = 80
) -> str:
    """Build a standardized work directory name.
    
    Combines entry_id and title into a snake_case directory name following
    the pattern: <entry_id>_<title_slug> or just <title_slug> if no entry_id.
    
    Args:
        entry_id: Optional entry identifier
        title: Work title
        max_len: Maximum length for title component
        
    Returns:
        Directory name in snake_case format
    """
    entry_slug = to_snake_case(str(entry_id)) if entry_id else None
    title_slug = to_snake_case(str(title))[:max_len] if title else "untitled"
    
    parts = [p for p in [entry_slug, title_slug] if p]
    return "_".join(parts) if parts else "untitled"
