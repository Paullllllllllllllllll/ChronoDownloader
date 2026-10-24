"""IIIF Presentation manifests (v2 and v3): canvases, page sources, metadata.

Ported from ChronoDownloader v1.30.3 ``api/iiif/_parsing.py`` (manifest walk
and extractors) and ``api/iiif/_direct.py`` (metadata) on 19.10.2026.
Changed: type checks replace the broad exception guards, so a malformed
canvas is skipped by inspection; canvases carry their position and label;
the functions take any mapping and keep no state.

A v2 manifest is walked through every ``sequences[n].canvases`` (a
dict-valued ``sequences`` counts as one sequence) to the first image's
``resource``, descending into an ``oa:Choice``; a v3 manifest through
``items -> canvas.items[0].items[0].body``, descending into a ``Choice``. A
canvas's source is its Image API service when it names one (the first
service entry with an identifier, else an image identifier containing
``/full/``), else its whole-image URL. :func:`extract_canvases` keeps every
canvas at its manifest position, a canvas without a usable source included
(its source is None), and never drops a canvas whose URL repeats; the URL
lists drop duplicate URLs.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, NamedTuple

__all__ = [
    "Canvas",
    "ManifestMetadata",
    "PageSource",
    "SourceKind",
    "extract_canvases",
    "extract_direct_image_urls",
    "extract_image_service_bases",
    "extract_manifest_metadata",
    "extract_page_sources",
    "localized_text",
]

type Json = Mapping[str, Any]
type SourceKind = Literal["service", "direct"]


class PageSource(NamedTuple):
    """One canvas's download source: an Image API service base or an image URL."""

    kind: SourceKind
    url: str


@dataclass(frozen=True)
class Canvas:
    """A canvas of the manifest.

    ``index`` is the 1-based position among all canvases, in manifest
    order; ``source`` is None when the canvas names no usable image;
    ``label`` is the canvas label, if any.
    """

    index: int
    source: PageSource | None
    label: str | None = None


def _no_metadata() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True)
class ManifestMetadata:
    """A manifest's label, attribution and metadata pairs (first language)."""

    label: str | None = None
    attribution: str | None = None
    metadata: Mapping[str, str] = field(default_factory=_no_metadata)


def _unwrap_v2_choice(resource: Any) -> Any:
    """Return the ``default`` alternative of an ``oa:Choice``, else the first
    ``item``; any other resource unchanged."""
    if isinstance(resource, Mapping) and resource.get("@type") == "oa:Choice":
        default = resource.get("default")
        if isinstance(default, Mapping):
            return default
        item = resource.get("item")
        if isinstance(item, list) and item and isinstance(item[0], Mapping):
            return item[0]
        if isinstance(item, Mapping):
            return item
    return resource


def _unwrap_v3_choice(body: Any) -> Any:
    """Return the first ``items`` entry of a v3 ``Choice``; any other body
    unchanged."""
    if isinstance(body, Mapping) and body.get("type") == "Choice":
        items = body.get("items")
        if isinstance(items, list) and items and isinstance(items[0], Mapping):
            return items[0]
    return body


def _first_mapping(value: Any) -> Json | None:
    """Return ``value[0]`` when *value* is a non-empty list starting with a
    mapping."""
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return value[0]
    return None


def _v2_canvases(manifest: Json) -> Iterator[tuple[Json, Json | None]]:
    """Yield each v2 canvas with its primary image resource, or None."""
    raw = manifest.get("sequences") or []
    sequences = [raw] if isinstance(raw, Mapping) else raw
    if not isinstance(sequences, list):
        return
    for sequence in sequences:
        if not isinstance(sequence, Mapping):
            continue
        canvases = sequence.get("canvases", [])
        if not isinstance(canvases, list):
            continue
        for canvas in canvases:
            if not isinstance(canvas, Mapping):
                continue
            image = _first_mapping(canvas.get("images", []))
            resource = _unwrap_v2_choice(image.get("resource", {})) if image else None
            yield canvas, resource if isinstance(resource, Mapping) else None


def _v3_canvases(manifest: Json) -> Iterator[tuple[Json, Json | None]]:
    """Yield each v3 canvas with its primary annotation body, or None."""
    items = manifest.get("items")
    if not isinstance(items, list):
        return
    for canvas in items:
        if not isinstance(canvas, Mapping):
            continue
        page = _first_mapping(canvas.get("items", []))
        annotation = _first_mapping(page.get("items", [])) if page else None
        body = annotation.get("body", {}) if annotation else None
        if isinstance(body, list) and body:
            body = body[0]
        body = _unwrap_v3_choice(body)
        yield canvas, body if isinstance(body, Mapping) else None


def _v2_entries(manifest: Json) -> Iterator[tuple[Json, Json]]:
    """Yield each v2 canvas that has a primary image resource, with it."""
    for canvas, resource in _v2_canvases(manifest):
        if resource is not None:
            yield canvas, resource


def _v3_entries(manifest: Json) -> Iterator[tuple[Json, Json]]:
    """Yield each v3 canvas that has a primary annotation body, with it."""
    for canvas, body in _v3_canvases(manifest):
        if body is not None:
            yield canvas, body


def _first_service_id(service: Any) -> str | None:
    """Return the first ``@id``/``id`` among one or more service blocks.

    ``service`` may be an array that leads with non-image services (auth,
    search) carrying no identifier, so every entry is inspected in order.
    """
    entries = service if isinstance(service, list) else [service]
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        service_id = entry.get("@id") or entry.get("id")
        if isinstance(service_id, str) and service_id:
            return service_id
    return None


def _base_from_image_id(image_id: Any) -> str | None:
    if isinstance(image_id, str) and "/full/" in image_id:
        return image_id.split("/full/")[0]
    return None


def _v2_service_base(resource: Json) -> str | None:
    return _first_service_id(resource.get("service", {})) or _base_from_image_id(
        resource.get("@id") or resource.get("id")
    )


def _v3_service_base(body: Json) -> str | None:
    return _first_service_id(
        body.get("service") or body.get("services")
    ) or _base_from_image_id(body.get("id"))


def _v2_direct_url(resource: Json) -> str | None:
    url = resource.get("@id") or resource.get("id")
    return url if isinstance(url, str) and url else None


def _v3_direct_url(body: Json) -> str | None:
    url = body.get("id")
    return url if isinstance(url, str) and url else None


def _unique[T](values: Iterator[T]) -> list[T]:
    seen: set[T] = set()
    unique: list[T] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def extract_image_service_bases(manifest: Json) -> list[str]:
    """Return the Image API service bases of the manifest's canvases."""

    def bases() -> Iterator[str]:
        for _canvas, resource in _v2_entries(manifest):
            base = _v2_service_base(resource)
            if base:
                yield base
        for _canvas, body in _v3_entries(manifest):
            base = _v3_service_base(body)
            if base:
                yield base

    return _unique(bases())


def extract_direct_image_urls(manifest: Json) -> list[str]:
    """Return the whole-image URLs the canvases carry."""

    def urls() -> Iterator[str]:
        for _canvas, resource in _v2_entries(manifest):
            url = _v2_direct_url(resource)
            if url:
                yield url
        for _canvas, body in _v3_entries(manifest):
            url = _v3_direct_url(body)
            if url:
                yield url

    return _unique(urls())


def _source(base: str | None, url: str | None) -> PageSource | None:
    if base:
        return PageSource("service", base)
    return PageSource("direct", url) if url else None


def _canvas_sources(
    manifest: Json,
) -> Iterator[tuple[PageSource | None, str | None]]:
    for canvas, resource in _v2_canvases(manifest):
        label = localized_text(canvas.get("label")) or None
        if resource is None:
            yield None, label
        else:
            yield _source(_v2_service_base(resource), _v2_direct_url(resource)), label
    for canvas, body in _v3_canvases(manifest):
        label = localized_text(canvas.get("label")) or None
        if body is None:
            yield None, label
        else:
            yield _source(_v3_service_base(body), _v3_direct_url(body)), label


def extract_canvases(manifest: Json) -> list[Canvas]:
    """Return one :class:`Canvas` per canvas, at its manifest position.

    The service wins when a canvas offers both a service and an image URL;
    a canvas offering neither keeps its position with source None; a canvas
    repeating another's URL is kept (a download fetches the URL once).
    """
    return [
        Canvas(index, source, label)
        for index, (source, label) in enumerate(_canvas_sources(manifest), 1)
    ]


def extract_page_sources(manifest: Json) -> list[PageSource]:
    """Return the distinct download sources of the canvases, in canvas order.

    Unlike :func:`extract_image_service_bases` and
    :func:`extract_direct_image_urls`, a manifest mixing service-backed and
    direct-only canvases keeps every page; canvases without a source are
    left out.
    """
    return _unique(
        canvas.source
        for canvas in extract_canvases(manifest)
        if canvas.source is not None
    )


def localized_text(value: Any) -> str:
    """Return a plain string, the first value of a language map or list, or ''."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for values in value.values():
            if isinstance(values, list) and values:
                return str(values[0])
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value) if value else ""


def _label(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        for values in raw.values():
            if isinstance(values, list) and values:
                return str(values[0])
        return None
    if isinstance(raw, list) and raw:
        return str(raw[0])
    return None


def _attribution(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("value")
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for values in value.values():
            if isinstance(values, list) and values:
                return str(values[0])
    return None


def extract_manifest_metadata(manifest: Json) -> ManifestMetadata:
    """Return the label, attribution (or v3 ``requiredStatement``) and the
    ``metadata`` pairs of a manifest, each in its first language."""
    pairs: dict[str, str] = {}
    raw_pairs = manifest.get("metadata") or []
    if isinstance(raw_pairs, list):
        for entry in raw_pairs:
            if not isinstance(entry, Mapping):
                continue
            key = localized_text(entry.get("label", ""))
            if key:
                pairs[key] = localized_text(entry.get("value", ""))
    return ManifestMetadata(
        label=_label(manifest.get("label")),
        attribution=_attribution(
            manifest.get("attribution") or manifest.get("requiredStatement")
        ),
        metadata=MappingProxyType(pairs),
    )
