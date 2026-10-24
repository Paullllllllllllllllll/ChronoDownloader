"""Writing a download's files into its staging folder.

A file is streamed by :meth:`~chronodownloader.net.Network.download` to
``<stem>.part`` and renamed to ``<stem><extension>``. The extension is the
one the response's Content-Type or leading bytes give, else the URL's own
suffix, and must belong to the file's kind (an image for a page, PDF or EPUB
for a rendering); the file's leading bytes must then carry that format's
signature (PDF, EPUB, JPEG, PNG, GIF, WebP, TIFF, JP2). A body of another
type is discarded and counts as an invalid payload. Metadata documents are
written atomically under ``metadata/``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from chronodownloader.coord import SlotToken
from chronodownloader.net import Cause, Network, RequestFailed
from chronodownloader.net.atomic import atomic_write_json, replace_with_retry
from chronodownloader.net.payload import HEAD_BYTES, sniff_extension

from .base import FileKind, StagedFile
from .registry import serving_key

__all__ = [
    "DOCUMENT_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "METADATA_DIR",
    "SIZE_CAUSES",
    "serving",
    "stage_download",
    "stage_first",
    "stage_metadata",
]

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = frozenset({".jpg", ".png", ".jp2", ".tif", ".gif", ".webp"})
DOCUMENT_EXTENSIONS = frozenset({".pdf", ".epub"})
METADATA_DIR = "metadata"

SIZE_CAUSES = frozenset(
    {Cause.NOT_FOUND, Cause.REJECTED, Cause.INVALID_PAYLOAD, Cause.INCOMPLETE}
)
"""Failures of one URL that another URL of the same image may avoid (a size
the server does not offer, a body of the wrong type); other causes concern
the server and end the attempts for that file."""

_SUFFIX_ALIASES = {".jpeg": ".jpg", ".tiff": ".tif"}


def _allowed(kind: FileKind) -> frozenset[str]:
    return DOCUMENT_EXTENSIONS if kind == "pdf" else IMAGE_EXTENSIONS


def _url_suffix(url: str) -> str:
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return _SUFFIX_ALIASES.get(suffix, suffix)


def _remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _signature(path: Path) -> str:
    """Return the extension the file's leading bytes prove, or ''.

    Raises:
        OSError: The file could not be read.
    """
    with path.open("rb") as handle:
        return sniff_extension(handle.read(HEAD_BYTES))


@contextlib.contextmanager
def serving(
    network: Network, url: str, provider: str, slot: SlotToken | None
) -> Iterator[str]:
    """Yield the key a request for *url* is paced under, holding its slot.

    The key is that of the registered provider serving the URL's host, else
    *provider* (:func:`~.registry.serving_key`). A transfer under a held
    *slot* from another registered provider's host also holds one of that
    provider's slots while it runs
    (:meth:`~chronodownloader.net.Network.hold_slot`); *slot* stays the one
    every pacing reservation checks.
    """
    key = serving_key(url, provider)
    if slot is None or key == slot.provider:
        yield key
        return
    with network.hold_slot(key):
        yield key


def stage_download(
    network: Network,
    provider: str,
    url: str,
    staging_dir: Path,
    stem: str,
    *,
    slot: SlotToken,
    kind: FileKind,
    final_statuses: frozenset[int] = frozenset(),
) -> StagedFile:
    """Download *url* to ``staging_dir/<stem><extension>``.

    *stem* is also the item the file satisfies. The request is paced and
    breaker-checked under the key of the registered provider serving the
    URL's host, else under *provider* (:func:`~.registry.serving_key`);
    *slot* is checked as given. *final_statuses* passes to
    :meth:`~chronodownloader.net.Network.download`.

    Raises:
        RequestFailed: The download failed, or its body is not of *kind*
            (cause ``invalid_payload``).
        BreakerOpen, QuotaSpent: Nothing was sent.
        SlotLost: The slot expired or passed to another holder.
        OSError: The file could not be written.
    """
    with serving(network, url, provider, slot) as key:
        outcome = network.download(
            key,
            url,
            staging_dir / f"{stem}.part",
            slot=slot,
            final_statuses=final_statuses,
        )
    allowed = _allowed(kind)
    extension = outcome.extension or _url_suffix(url)
    if extension not in allowed:
        _remove(outcome.path)
        raise RequestFailed(
            Cause.INVALID_PAYLOAD,
            outcome.url,
            detail=f"expected {kind}, got {extension or 'an unknown type'}",
        )
    found = _signature(outcome.path)
    if found != extension:
        _remove(outcome.path)
        raise RequestFailed(
            Cause.INVALID_PAYLOAD,
            outcome.url,
            detail=f"{extension} expected, bytes are {found or 'of no known type'}",
        )
    final = staging_dir / f"{stem}{extension}"
    replace_with_retry(final, outcome.path)
    return StagedFile(
        name=final.relative_to(staging_dir).as_posix(),
        kind=kind,
        item=stem,
        size=outcome.size,
        content_type=outcome.content_type,
        source_url=outcome.url,
    )


def stage_first(  # noqa: PLR0913 - two optional attempt records (tried, failures)
    network: Network,
    provider: str,
    urls: Iterable[str],
    staging_dir: Path,
    stem: str,
    *,
    slot: SlotToken,
    kind: FileKind,
    tried: set[str] | None = None,
    failures: list[RequestFailed] | None = None,
) -> StagedFile | RequestFailed | None:
    """Stage the first of *urls* that downloads.

    A URL failing for a :data:`SIZE_CAUSES` cause moves on to the next one;
    any other failure ends the attempts and is returned. None means every
    URL failed for a size cause. URLs in *tried* are skipped and every URL
    sent is added to it; every failure is appended to *failures*.

    Raises:
        BreakerOpen, QuotaSpent, SlotLost, OSError: As
            :func:`stage_download`.
    """
    for url in urls:
        if tried is not None:
            if url in tried:
                continue
            tried.add(url)
        try:
            return stage_download(
                network, provider, url, staging_dir, stem, slot=slot, kind=kind
            )
        except RequestFailed as exc:
            logger.info("%s: %s failed: %s", provider, stem, exc)
            if failures is not None:
                failures.append(exc)
            if exc.cause not in SIZE_CAUSES:
                return exc
    return None


def stage_metadata(
    staging_dir: Path, name: str, document: Any, *, source_url: str = ""
) -> StagedFile:
    """Write *document* as ``metadata/<name>.json`` and return its record.

    Raises:
        OSError: The file could not be written.
    """
    path = staging_dir / METADATA_DIR / f"{name}.json"
    atomic_write_json(path, document)
    return StagedFile(
        name=path.relative_to(staging_dir).as_posix(),
        kind="metadata",
        item=name,
        size=path.stat().st_size,
        content_type="application/json",
        source_url=source_url,
    )
