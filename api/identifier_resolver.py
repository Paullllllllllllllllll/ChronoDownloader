"""Resolve provider-specific identifiers to IIIF manifest URLs.

Maps (provider_key, identifier) pairs to downloadable manifest URLs,
enabling the ``--id`` CLI flag. Supports auto-detection of provider
from identifier format patterns when ``--provider`` is omitted.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .model import SearchResult
from .providers import PROVIDERS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Manifest URL templates
# ---------------------------------------------------------------------------
# Each entry maps a provider key to one or more URL templates.
# Templates use ``{id}`` as the single placeholder for the identifier.
# Sources: the IIIF_MANIFEST_* constants in each provider's *_api.py module.

MANIFEST_TEMPLATES: dict[str, str | list[str]] = {
    "mdz": "https://api.digitale-sammlungen.de/iiif/presentation/v2/{id}/manifest",
    "bnf_gallica": "https://gallica.bnf.fr/iiif/ark:/12148/{id}/manifest.json",
    "internet_archive": "https://iiif.archivelab.org/iiif/{id}/manifest.json",
    "e_rara": "https://www.e-rara.ch/i3f/v20/{id}/manifest",
    "slub": "https://iiif.slub-dresden.de/iiif/2/{id}/manifest.json",
    "loc": "https://www.loc.gov/item/{id}/manifest.json",
    "british_library": "https://api.bl.uk/metadata/iiif/ark:/81055/{id}/manifest.json",
    "hathitrust": "https://babel.hathitrust.org/cgi/imgsrv/manifest/{id}",
    "polona": "https://polona.pl/iiif/item/{id}/manifest.json",
    "bne": [
        "https://iiif.bne.es/{id}/manifest",
        "https://iiif.bne.es/{id}/manifest.json",
    ],
    "europeana": "https://iiif.europeana.eu/presentation/{id}/manifest",
}

# Providers that lack a simple IIIF Presentation manifest template and must
# use their native ``download_*`` function instead.
NATIVE_DOWNLOAD_PROVIDERS: frozenset[str] = frozenset({
    "google_books",
    "annas_archive",
    "sbb_digital",
    "wellcome",  # uses IIIF Image API directly; no presentation manifest from ID
    "dpla",      # aggregator -- IDs don't map to a single manifest template
    "ddb",       # aggregator -- IDs don't map to a single manifest template
})

# ---------------------------------------------------------------------------
# Identifier auto-detection
# ---------------------------------------------------------------------------
# Ordered list of (compiled regex, provider_key).  More specific patterns
# should come before less specific ones.

IDENTIFIER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # MDZ / Bavarian State Library -- identifiers like bsb11280551
    (re.compile(r"^bsb\d+$", re.IGNORECASE), "mdz"),
    # BnF Gallica -- ark sub-identifiers
    (re.compile(r"^bpt6k\w+$", re.IGNORECASE), "bnf_gallica"),
    (re.compile(r"^btv1b\w+$", re.IGNORECASE), "bnf_gallica"),
    (re.compile(r"^cb\d+[a-z]?$", re.IGNORECASE), "bnf_gallica"),
    (re.compile(r"^ark:/12148/\w+$"), "bnf_gallica"),
    # HathiTrust -- institution-prefixed volume IDs
    (re.compile(r"^mdp\.\w+$"), "hathitrust"),
    (re.compile(r"^inu\.\w+$"), "hathitrust"),
    (re.compile(r"^uc[12]\.\w+$"), "hathitrust"),
    (re.compile(r"^hvd\.\w+$"), "hathitrust"),
    (re.compile(r"^nyp\.\w+$"), "hathitrust"),
    (re.compile(r"^njp\.\w+$"), "hathitrust"),
    (re.compile(r"^chi\.\w+$"), "hathitrust"),
    (re.compile(r"^wu\.\w+$"), "hathitrust"),
    # British Library -- VDC identifiers
    (re.compile(r"^vdc_\w+$", re.IGNORECASE), "british_library"),
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ResolvedIdentifier:
    """Result of resolving an identifier to a downloadable target."""

    provider_key: str
    manifest_urls: list[str] = field(default_factory=list)
    use_native: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_manifest_url(provider_key: str, identifier: str) -> list[str]:
    """Construct IIIF manifest URL(s) for *provider_key* and *identifier*.

    Args:
        provider_key: Canonical provider key (e.g. ``"mdz"``).
        identifier: Provider-specific item identifier.

    Returns:
        One or more candidate manifest URLs.

    Raises:
        ValueError: If the provider has no manifest template
            (i.e. it is a native-download provider).
        KeyError: If the provider key is not recognised at all.
    """
    if provider_key in NATIVE_DOWNLOAD_PROVIDERS:
        raise ValueError(
            f"Provider '{provider_key}' does not support IIIF manifest "
            f"URL construction; use its native download function instead."
        )
    template = MANIFEST_TEMPLATES.get(provider_key)
    if template is None:
        raise KeyError(
            f"No manifest URL template registered for provider '{provider_key}'."
        )
    if isinstance(template, list):
        return [t.format(id=identifier) for t in template]
    return [template.format(id=identifier)]


def detect_provider(identifier: str) -> list[str]:
    """Infer provider key(s) from an identifier's format.

    Args:
        identifier: Raw identifier string.

    Returns:
        List of matching provider keys (may be empty).
    """
    matches: list[str] = []
    seen: set[str] = set()
    for pattern, pkey in IDENTIFIER_PATTERNS:
        if pattern.match(identifier) and pkey not in seen:
            matches.append(pkey)
            seen.add(pkey)
    return matches


def resolve_identifier(
    identifier: str,
    provider_key: str | None = None,
) -> list[ResolvedIdentifier]:
    """Resolve an identifier to one or more downloadable targets.

    When *provider_key* is given, returns a single ``ResolvedIdentifier``
    for that provider.  When omitted, attempts auto-detection via
    :func:`detect_provider`.

    Args:
        identifier: Provider-specific item identifier.
        provider_key: Optional explicit provider key.

    Returns:
        List of ``ResolvedIdentifier`` objects (may be empty if the
        provider is unknown and auto-detection fails).

    Raises:
        KeyError: If an explicit *provider_key* is not found in the
            provider registry.
    """
    if provider_key is not None:
        if provider_key not in PROVIDERS:
            raise KeyError(
                f"Unknown provider key '{provider_key}'. "
                f"Valid keys: {', '.join(sorted(PROVIDERS.keys()))}"
            )
        if provider_key in NATIVE_DOWNLOAD_PROVIDERS:
            return [ResolvedIdentifier(
                provider_key=provider_key,
                manifest_urls=[],
                use_native=True,
            )]
        urls = build_manifest_url(provider_key, identifier)
        return [ResolvedIdentifier(
            provider_key=provider_key,
            manifest_urls=urls,
            use_native=False,
        )]

    # Auto-detect
    detected = detect_provider(identifier)
    if not detected:
        return []

    results: list[ResolvedIdentifier] = []
    for pkey in detected:
        if pkey in NATIVE_DOWNLOAD_PROVIDERS:
            results.append(ResolvedIdentifier(
                provider_key=pkey,
                manifest_urls=[],
                use_native=True,
            ))
        else:
            urls = build_manifest_url(pkey, identifier)
            results.append(ResolvedIdentifier(
                provider_key=pkey,
                manifest_urls=urls,
                use_native=False,
            ))
    return results


def download_by_native_provider(
    identifier: str,
    provider_key: str,
    output_folder: str,
    title: str | None = None,
) -> bool:
    """Download using a provider's native download function.

    Constructs a minimal :class:`SearchResult` with the identifier and
    delegates to the provider's registered ``download_*`` function.

    Args:
        identifier: Provider-specific item identifier.
        provider_key: Canonical provider key.
        output_folder: Target directory for downloaded files.
        title: Optional work title for metadata.

    Returns:
        ``True`` if the download succeeded, ``False`` otherwise.

    Raises:
        KeyError: If the provider key is not in the registry.
    """
    if provider_key not in PROVIDERS:
        raise KeyError(f"Unknown provider key '{provider_key}'.")

    _search_fn, download_fn, display_name = PROVIDERS[provider_key]

    sr = SearchResult(
        provider=display_name,
        title=title or identifier,
        source_id=identifier,
        provider_key=provider_key,
        raw={"id": identifier, "identifier": identifier},
    )

    logger.info(
        "Downloading '%s' via native %s download function",
        identifier,
        display_name,
    )
    try:
        return bool(download_fn(sr, output_folder))
    except Exception:
        logger.exception(
            "Native download failed for '%s' via %s",
            identifier,
            display_name,
        )
        return False


__all__ = [
    "MANIFEST_TEMPLATES",
    "NATIVE_DOWNLOAD_PROVIDERS",
    "IDENTIFIER_PATTERNS",
    "ResolvedIdentifier",
    "build_manifest_url",
    "detect_provider",
    "resolve_identifier",
    "download_by_native_provider",
]
