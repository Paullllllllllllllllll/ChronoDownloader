"""Payload checks of a streamed download: declared length, type and bytes.

Copied from ChronoDownloader v1.30.3 ``api/core/download.py`` (lines 92-162,
228-283, 333-366, 492-515) on 19.10.2026. Changed: the magic-byte check reads
only the bytes buffered while streaming (the whole file when it is shorter),
and the Content-Disposition parser catches no exceptions it cannot raise.
"""

from __future__ import annotations

from urllib.parse import unquote

__all__ = [
    "HEAD_BYTES",
    "content_disposition_filename",
    "extension_for_content_type",
    "magic_mismatch",
    "parse_content_length",
    "sniff_extension",
]

HEAD_BYTES = 4096
"""Leading bytes buffered while streaming, for the checks after the write."""

# The first entry whose MIME fragment appears in the Content-Type wins, so a
# specific type precedes a broader one ("application/epub+zip" before
# "application/zip").
_CONTENT_TYPE_EXTENSIONS = (
    ("application/pdf", ".pdf"),
    ("application/x-pdf", ".pdf"),
    ("application/epub+zip", ".epub"),
    ("image/jpeg", ".jpg"),
    ("image/jpg", ".jpg"),
    ("image/png", ".png"),
    ("image/jp2", ".jp2"),
    ("image/jpx", ".jp2"),
    ("image/jpeg2000", ".jp2"),
    ("image/tiff", ".tif"),
    ("image/tif", ".tif"),
    ("image/gif", ".gif"),
    ("image/webp", ".webp"),
    ("application/zip", ".zip"),
    ("text/plain", ".txt"),
    ("text/html", ".html"),
    ("application/json", ".json"),
    ("application/xml", ".xml"),
    ("text/xml", ".xml"),
)

_GENERIC_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "binary/octet-stream",
        "application/binary",
        "application/download",
        "application/force-download",
        "application/x-download",
        "application/unknown",
    }
)
"""Content types that name a byte stream rather than a format."""

_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x00\x00\x00\x0cjP  \r\n\x87\n", ".jp2"),
    (b"\xff\x4f\xff\x51", ".jp2"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"II+\x00", ".tif"),
    (b"MM\x00+", ".tif"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

_EPUB_MIMETYPE_WINDOW = 128
"""An EPUB is a zip whose first, uncompressed member holds its media type."""

_MAGIC_WINDOW = 512


def parse_content_length(value: str | None) -> int | None:
    """Return a declared Content-Length, or None when absent or malformed."""
    if not value:
        return None
    try:
        length = int(value)
    except ValueError:
        return None
    return length if length >= 0 else None


def extension_for_content_type(content_type: str) -> str:
    """Return the extension a Content-Type declares, or '' for none."""
    lowered = content_type.lower()
    if lowered.split(";", 1)[0].strip() in _GENERIC_CONTENT_TYPES:
        return ""
    for mime, extension in _CONTENT_TYPE_EXTENSIONS:
        if mime in lowered:
            return extension
    return ""


def sniff_extension(head: bytes) -> str:
    """Return the extension a payload's leading bytes imply, or ''."""
    if not head:
        return ""
    for magic, extension in _MAGIC_SIGNATURES:
        if head.startswith(magic):
            return extension
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"PK\x03\x04"):
        if b"application/epub+zip" in head[:_EPUB_MIMETYPE_WINDOW]:
            return ".epub"
        return ".zip"
    return ""


def magic_mismatch(extension: str, head: bytes) -> str:
    """Return why *head* cannot be a *extension* file, or '' when it can.

    Only ``.pdf`` and ``.epub`` are checked.
    """
    if extension not in (".pdf", ".epub"):
        return ""
    first = head[:_MAGIC_WINDOW]
    is_html = b"<!DOCTYPE" in first or b"<html" in first.lower()
    kind = "PDF" if extension == ".pdf" else "EPUB"
    magic = b"%PDF-" if extension == ".pdf" else b"PK\x03\x04"
    if first.startswith(magic):
        return ""
    if is_html:
        return f"file claims to be {kind} but contains HTML"
    return f"file claims to be {kind} but has invalid magic bytes"


def content_disposition_filename(value: str | None) -> str | None:
    """Return the file name a Content-Disposition header proposes, or None."""
    if not value:
        return None
    params: dict[str, str] = {}
    for part in value.split(";")[1:]:
        if "=" in part:
            key, _, text = part.partition("=")
            params[key.strip().lower()] = text.strip().strip('"')
    name = params.get("filename*") or params.get("filename")
    if not name:
        return None
    if "''" in name:
        _charset, _, encoded = name.partition("''")
        return unquote(encoded)
    return name
