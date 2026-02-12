"""Connector for SLUB Dresden (data.slub-dresden.de) + IIIF.

Search uses the SLUB LOD API /search endpoint and downloads via IIIF manifests
hosted by SLUB's digital collections.
"""
from __future__ import annotations

import logging
import re

from .download_helpers import download_iiif_manifest_and_images
from .model import SearchResult, convert_to_searchresult, resolve_item_id, resolve_item_field
from .query_helpers import escape_sru_literal
from .core.network import make_request
from .utils import save_json

logger = logging.getLogger(__name__)

SEARCH_URL = "https://data.slub-dresden.de/search"
SOURCE_URL = "https://data.slub-dresden.de/source/kxp-de14/{record_id}"
IIIF_MANIFEST_URL = "https://iiif.slub-dresden.de/iiif/2/{ppn}/manifest.json"
DIGITAL_ITEM_URL = "https://digital.slub-dresden.de/id{ppn}"

def _extract_title(item: dict) -> str:
    title = item.get("preferredName") or ""
    if isinstance(item.get("title"), dict):
        title = item["title"].get("mainTitle") or item["title"].get("preferredName") or title
    elif isinstance(item.get("title"), str):
        title = item.get("title")
    return title or "N/A"

def _extract_creator(item: dict) -> str:
    contrib = item.get("contributor") or []
    if isinstance(contrib, list):
        for c in contrib:
            if isinstance(c, dict) and c.get("name"):
                return str(c.get("name"))
    return "N/A"

def _extract_record_id(item: dict) -> str | None:
    record_id = item.get("@id") or item.get("id")
    if record_id and isinstance(record_id, str):
        return record_id.rstrip("/").split("/")[-1]
    return None

def _extract_ppn_from_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    match = re.search(r"ppn(\d+)", url, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"id(\d+)", url, re.IGNORECASE)
    if match:
        return match.group(1)
    return None

def _resolve_ppn_from_source(record_id: str, output_folder: str) -> tuple[str | None, str | None]:
    """Fetch source record and extract digital URL + PPN."""
    url = SOURCE_URL.format(record_id=record_id)
    data = make_request(url)
    if isinstance(data, dict):
        try:
            save_json(data, output_folder, f"slub_{record_id}_source")
        except Exception:
            pass
    if not isinstance(data, dict):
        return None, None

    urls: list[str] = []
    for entry in data.get("856", []) or []:
        if isinstance(entry, dict):
            for subfield in entry.values():
                if isinstance(subfield, list):
                    for sf in subfield:
                        if isinstance(sf, dict) and sf.get("u"):
                            urls.append(str(sf["u"]))
    digital_url = urls[0] if urls else None
    ppn = _extract_ppn_from_url(digital_url)
    return ppn, digital_url

def search_slub(title: str, creator: str | None = None, max_results: int = 3) -> list[SearchResult]:
    """Search SLUB LOD API for digitized resources."""
    q = title if not creator else f"{title} {creator}"
    q = escape_sru_literal(q)
    filter_value = "@type:http://schema.org/CreativeWork"
    params = {
        "q": q,
        "size": str(max_results),
        "format": "json",
        "filter": filter_value,
    }
    logger.info("Searching SLUB Dresden for: %s", title)
    data = make_request(SEARCH_URL, params=params)
    if not isinstance(data, list):
        return []

    results: list[SearchResult] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        access_mode = str(item.get("accessMode") or "").lower()
        reproduction_type = str(item.get("reproductionType") or "").lower()
        if access_mode and access_mode != "online" and "online" not in reproduction_type:
            continue

        record_id = _extract_record_id(item)
        if not record_id:
            continue

        raw = {
            "title": _extract_title(item),
            "creator": _extract_creator(item),
            "id": record_id,
            "item_url": None,
        }
        results.append(convert_to_searchresult("SLUB Dresden", raw))
        if len(results) >= max_results:
            break

    return results

def download_slub_work(item_data: SearchResult | dict, output_folder: str) -> bool:
    """Download SLUB item via IIIF manifest derived from the source record."""
    record_id = resolve_item_id(item_data)
    manifest_url = resolve_item_field(item_data, "iiif_manifest", attr="iiif_manifest")

    if not manifest_url:
        ppn = None
        digital_url = None
        if record_id:
            ppn, digital_url = _resolve_ppn_from_source(str(record_id), output_folder)
        if ppn:
            manifest_url = IIIF_MANIFEST_URL.format(ppn=ppn)
        if isinstance(item_data, SearchResult) and digital_url:
            item_data.item_url = digital_url
        if isinstance(item_data, dict) and digital_url:
            item_data["item_url"] = digital_url

    if not manifest_url:
        logger.warning("No IIIF manifest URL found for SLUB record %s", record_id)
        return False

    item_id = str(record_id or "slub")
    return download_iiif_manifest_and_images(
        manifest_url=manifest_url,
        output_folder=output_folder,
        provider_key="slub",
        item_id=item_id,
    )
