"""Whole-document renderings (PDF, EPUB) a IIIF manifest offers.

Ported from ChronoDownloader v1.30.3 ``api/iiif/_renderings.py`` on
19.10.2026. Changed: the MIME whitelist is the fixed default (``--format``
replaced the settings), malformed entries are skipped by type checks instead
of a broad exception guard, and downloading and provenance moved to the
provider layer, which stages the file and records its source.

Candidates are the manifest-level ``rendering`` entries, then those of each
v2 sequence (Wellcome and the DFG viewer hang the volume PDF there). A
declared MIME format must match the whitelist; an entry without one (or with
a bare v3 ``type`` such as ``Text``) passes when its URL ends in ``.pdf`` or
``.epub``. The whitelist is an ordered preference: candidates are sorted by
the whitelist entry they match, manifest order within a rank.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .manifest import localized_text

__all__ = [
    "DEFAULT_WHITELIST",
    "MAX_RENDERING_ATTEMPTS",
    "Rendering",
    "collect_all_renderings",
    "select_renderings",
]

DEFAULT_WHITELIST: tuple[str, ...] = ("application/pdf", "application/epub+zip")
"""Accepted rendering formats, most preferred first."""

MAX_RENDERING_ATTEMPTS = 10
"""Rendering downloads tried per manifest, so a manifest listing hundreds of
dead renderings cannot turn one file into hundreds of requests."""


@dataclass(frozen=True)
class Rendering:
    """A rendering to download: URL, declared format and label.

    The format is lowercase, '' when none is declared.
    """

    url: str
    format: str
    label: str | None = None


def _entries(obj: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = obj.get("rendering")
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, Mapping)]
    if isinstance(raw, Mapping):
        return [raw]
    return []


def collect_all_renderings(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the manifest-level, then the v2 sequence-level ``rendering`` entries."""
    candidates = _entries(manifest)
    sequences = manifest.get("sequences")
    if isinstance(sequences, list):
        for sequence in sequences:
            if isinstance(sequence, Mapping):
                candidates.extend(_entries(sequence))
    return candidates


def _rendering_format(value: Any) -> str:
    """Return a ``format``/``type`` as a lowercase string; a list gives its first."""
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str)), "")
    if not isinstance(value, str):
        return ""
    return value.lower()


def _is_whitelisted(url: str, fmt: str, whitelist: Sequence[str]) -> bool:
    if "/" in fmt:
        return any(allowed in fmt for allowed in whitelist)
    return url.lower().endswith((".pdf", ".epub"))


def _whitelist_rank(url: str, fmt: str, whitelist: Sequence[str]) -> int:
    """Return the index of the whitelist entry a rendering matches, else its length."""
    if "/" in fmt:
        for index, allowed in enumerate(whitelist):
            if allowed in fmt:
                return index
        return len(whitelist)
    lowered = url.lower()
    for extension, token in ((".pdf", "pdf"), (".epub", "epub")):
        if lowered.endswith(extension):
            for index, allowed in enumerate(whitelist):
                if token in allowed:
                    return index
            break
    return len(whitelist)


def select_renderings(
    manifest: Mapping[str, Any], whitelist: Sequence[str] = DEFAULT_WHITELIST
) -> list[Rendering]:
    """Return the renderings a download would try, in preference order.

    Entries without a string URL, outside the whitelist or repeating a URL
    are dropped.
    """
    allowed = [entry.lower() for entry in whitelist if entry]
    seen: set[str] = set()
    selected: list[Rendering] = []
    for entry in collect_all_renderings(manifest):
        url = entry.get("@id") or entry.get("id")
        if not isinstance(url, str) or not url or url in seen:
            continue
        fmt = _rendering_format(entry.get("format") or entry.get("type"))
        if allowed and not _is_whitelisted(url, fmt, allowed):
            continue
        seen.add(url)
        label = localized_text(entry.get("label")) or None
        selected.append(Rendering(url, fmt, label))
    selected.sort(key=lambda item: _whitelist_rank(item.url, item.format, allowed))
    return selected
