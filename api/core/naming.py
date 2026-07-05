"""Filename sanitization and naming conventions for ChronoDownloader.

Provides utilities for converting strings to safe filenames, generating
snake_case identifiers, and building standardized file names with provider
slugs and sequence numbers.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows applications default to a 260-character MAX_PATH limit. Warn a little
# below it so there is headroom for the objects/ subdirectory and filename.
_WINDOWS_MAX_PATH_WARN = 250

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
    "slub": "slub",
    "e_rara": "erara",
    "sbb_digital": "sbb",
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
    "slub": "SLUB",
    "e_rara": "ERARA",
    "sbb_digital": "SBB",
}


def to_snake_case(value: str | None) -> str:
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


# Windows reserved device names: a file or directory with one of these base
# names (case-insensitive) cannot be created on Windows filesystems.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _guard_reserved_name(base: str) -> str:
    """Prefix Windows reserved device names so they become creatable."""
    if base.lower() in _WINDOWS_RESERVED_NAMES:
        return f"_{base}_"
    return base


def sanitize_filename(name: str, max_base_len: int = 100) -> str:
    """Sanitize string for safe filenames while preserving extension.

    - Keeps the original extension(s) intact (e.g., .pdf, .tar.gz).
    - Limits the base name length only.
    - Removes illegal filesystem characters.
    - Guards Windows reserved device names (con, nul, com1, ...).

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
    base = _guard_reserved_name(base)
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

    base_no_ext = base[: -len(ext)] if ext else base

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
    max_len: int = 80,
    creator: str | None = None,
    year: str | int | None = None,
    include_creator: bool = True,
    include_year: bool = True,
) -> str:
    """Build a standardized work directory name.

    Combines entry_id and title into a snake_case directory name following
    the pattern: <entry_id>_<title_slug>, optionally suffixed with a creator
    slug and/or year when the corresponding naming options are enabled and the
    values are available.

    Args:
        entry_id: Optional entry identifier
        title: Work title
        max_len: Maximum length for title component
        creator: Optional creator/author name
        year: Optional publication year
        include_creator: When True and ``creator`` is set, append a creator slug
        include_year: When True and ``year`` is set, append the year

    Returns:
        Directory name in snake_case format
    """
    entry_slug = to_snake_case(str(entry_id)) if entry_id else None
    title_slug = to_snake_case(str(title))[:max_len] if title else "untitled"

    creator_slug = None
    if include_creator and creator:
        creator_slug = to_snake_case(str(creator))[:max_len] or None

    year_slug = None
    if include_year and year:
        year_slug = to_snake_case(str(year)) or None

    parts = [p for p in [entry_slug, title_slug, creator_slug, year_slug] if p]
    name = "_".join(parts) if parts else "untitled"
    return _guard_reserved_name(name)


def warn_if_path_too_long(path: str, work_label: str) -> None:
    """Log a warning when a path risks the Windows MAX_PATH limit.

    Advisory only: no path rewriting or long-path-prefix magic is attempted.
    Overlong paths can make file operations fail on default Windows setups, so
    surfacing the risk (naming the work) lets the user shorten their output
    directory or title slug.

    Args:
        path: The full path whose length is being checked.
        work_label: Human-readable identifier for the work (title or entry_id).
    """
    if sys.platform == "win32" and len(path) > _WINDOWS_MAX_PATH_WARN:
        logger.warning(
            "Path for '%s' is %d characters and may exceed the Windows MAX_PATH "
            "limit, which can cause file operations to fail: %s",
            work_label,
            len(path),
            path,
        )
