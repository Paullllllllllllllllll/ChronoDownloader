"""Recognize IIIF manifest URLs and the library and item they name.

Ported from ChronoDownloader v1.30.3 ``api/iiif/_direct.py`` on 19.10.2026.
Changed: no configuration lookups (the ``direct_iiif`` settings are gone);
the item-identifier fallback hash is marked as not used for security.
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "IIIF_MANIFEST_PATTERNS",
    "detect_provider_from_url",
    "extract_item_id_from_url",
    "is_iiif_manifest_url",
]

IIIF_MANIFEST_PATTERNS: tuple[str, ...] = (
    r"manifest\.json$",
    r"/manifest$",
    r"iiif\.archive\.org/iiif/.+/manifest",
    r"iiif\.archivelab\.org/iiif/.+/manifest",
    r"gallica\.bnf\.fr/iiif/ark:",
    r"api\.digitale-sammlungen\.de/iiif/presentation",
    r"digitale-sammlungen\.de/.+/manifest",
    r"babel\.hathitrust\.org/cgi/imgsrv/manifest",
    r"loc\.gov/.+/manifest",
    r"iiif\.wellcomecollection\.org/presentation",
    r"api\.bl\.uk/metadata/iiif",
    r"e-rara\.ch/i3f/v\d+/.+/manifest",
    r"digital\.slub-dresden\.de/.+/manifest",
    r"content\.staatsbibliothek-berlin\.de/.+/manifest",
    r"/iiif/\d+/manifest",
    r"/presentation/v[23]/.+/manifest",
    r"/iiif/presentation/",
)

_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in IIIF_MANIFEST_PATTERNS
)

_HOSTS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("archive.org", "archivelab.org"), "internet_archive", "Internet Archive"),
    (("gallica.bnf.fr",), "bnf_gallica", "BnF Gallica"),
    (("digitale-sammlungen.de",), "mdz", "MDZ"),
    (("hathitrust.org",), "hathitrust", "HathiTrust"),
    (("loc.gov",), "loc", "Library of Congress"),
    (("wellcomecollection.org",), "wellcome", "Wellcome Collection"),
    (("bl.uk",), "british_library", "British Library"),
    (("e-rara.ch",), "e_rara", "e-rara"),
    (("slub-dresden.de",), "slub", "SLUB Dresden"),
    (("staatsbibliothek-berlin.de",), "sbb_digital", "SBB Digital Collections"),
    (("europeana.eu",), "europeana", "Europeana"),
    (("polona.pl",), "polona", "Polona"),
    (("ddb.de", "deutsche-digitale-bibliothek"), "ddb", "DDB"),
    (("bne.es",), "bne", "BNE"),
)

_ITEM_ID_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"/iiif/([^/]+)/manifest",
        r"/view/([^/]+)",
        r"/details/([^/]+)",
        r"/item/([^/]+)",
        r"/ark:/[^/]+/([^/]+)",
        r"/presentation/v\d+/([^/]+)",
        r"/i3f/v\d+/([^/]+)",
        r"([a-zA-Z0-9_-]{5,})$",
    )
)


def is_iiif_manifest_url(url: object) -> bool:
    """Return True for an http(s) URL shaped like a IIIF manifest."""
    if not isinstance(url, str) or not url:
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    return any(pattern.search(url) for pattern in _PATTERNS)


def detect_provider_from_url(url: str) -> tuple[str, str]:
    """Return the provider key and name a URL's host belongs to.

    An unknown host gives ``("direct_iiif", "Direct IIIF")``.
    """
    lowered = url.lower()
    for hosts, key, name in _HOSTS:
        if any(host in lowered for host in hosts):
            return key, name
    return "direct_iiif", "Direct IIIF"


def extract_item_id_from_url(url: str) -> str:
    """Return the item identifier in a manifest URL, else a 12-character hash."""
    cleaned = url.split("://", 1)[-1]
    cleaned = re.sub(r"/manifest\.json$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"/manifest$", "", cleaned, flags=re.IGNORECASE)
    for pattern in _ITEM_ID_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return match.group(1)
    return hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:12]
