from __future__ import annotations

import logging
import os
from typing import Any

from ..core.config import get_api_key_envvar, prefer_pdf_over_images
from ..core.download import download_file, save_json
from ..core.network import make_request
from ..iiif import (
    download_direct_image_urls,
    download_iiif_renderings,
    download_page_images,
    extract_direct_image_urls,
    extract_image_service_bases,
)
from ..model import (
    SearchResult,
    convert_to_searchresult,
    resolve_item_field,
    resolve_item_id,
)

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.europeana.eu/record/v2/search.json"
RECORD_API_BASE = "https://api.europeana.eu/record/v2"
EUROPEANA_MANIFEST_HOST = "https://iiif.europeana.eu"


def _api_key() -> str | None:
    """Get Europeana API key from environment."""
    # Read at call time so keys loaded from .env or environment later are picked up
    return os.getenv(get_api_key_envvar("europeana", "EUROPEANA_API_KEY"))


def _build_manifest_url_from_id(euro_id: str, prefer_v3: bool = True) -> str | None:
    """Construct the Europeana IIIF Manifest API URL from a Europeana record id.

    Europeana search results typically have ids like
    "/9200379/BibliographicResource_3000117247947".
    The manifest URL format is:
      https://iiif.europeana.eu/presentation/{collectionId}/{recordId}/manifest[?format=3]

    The API key is deliberately omitted here: this URL is persisted in search
    results (work.json, index.csv, ``--search`` output). It is appended at
    fetch time by :func:`_append_wskey`.
    """
    if not euro_id:
        return None
    parts = [p for p in euro_id.strip().split("/") if p]
    # Expect [collectionId, recordId]
    if len(parts) >= 2:
        collection_id, record_id = parts[-2], parts[-1]
        url = (
            f"{EUROPEANA_MANIFEST_HOST}/presentation/"
            f"{collection_id}/{record_id}/manifest"
        )
        if prefer_v3:
            url = url + "?format=3"
        return url
    return None


def _append_wskey(url: str, api_key: str | None) -> str:
    """Add the Europeana API key to a Europeana-hosted manifest URL.

    Only Europeana's own IIIF host requires (and understands) the ``wskey``
    parameter; manifests discovered on provider hosts are left untouched.
    """
    if not api_key or "wskey=" in url or not url.startswith(EUROPEANA_MANIFEST_HOST):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}wskey={api_key}"


def search_europeana(
    title: str, creator: str | None = None, max_results: int = 3
) -> list[SearchResult]:
    key = _api_key()
    if not key:
        logger.warning("Europeana API key not configured. Skipping search.")
        return []
    # Strip embedded double quotes so they cannot terminate the quoted phrase.
    safe_title = title.replace('"', " ")
    query_parts = [f'title:"{safe_title}"']
    if creator:
        safe_creator = creator.replace('"', " ")
        query_parts.append(f'AND who:"{safe_creator}"')
    query = " ".join(query_parts)
    params = {
        "wskey": key,
        "query": query,
        "rows": str(
            max_results * 3
        ),  # pull extras to increase chance of IIIF availability
        "profile": "rich",
        "media": "true",
    }
    logger.info("Searching Europeana for: %s", title)
    data = make_request(API_BASE_URL, params=params)
    results: list[SearchResult] = []
    if not isinstance(data, dict):
        return results
    if data.get("success") and data.get("items"):
        for item in data["items"]:
            # A present-but-empty "title": [] (records lacking dc:title) would
            # make the default unused and [][0] raise IndexError, aborting the
            # whole search; guard for the empty-list case explicitly.
            titles = item.get("title") or [""]
            if isinstance(titles, list):
                item_title = titles[0] if titles else ""
            else:
                item_title = titles
            item_creator = None
            dc_creator = item.get("dcCreator")
            if isinstance(dc_creator, list) and dc_creator:
                item_creator = dc_creator[0]
            elif isinstance(dc_creator, str) and dc_creator:
                item_creator = dc_creator
            iiif_manifest = None
            # Prefer direct manifest URL if given by provider
            # Check edmIsShownBy / hasView / object
            try:
                if item.get("edmAggregatedCHO") and item["edmAggregatedCHO"].get(
                    "hasView"
                ):
                    views = item["edmAggregatedCHO"]["hasView"]
                    if not isinstance(views, list):
                        views = [views]
                    for view in views:
                        if isinstance(view, str) and "manifest" in view:
                            iiif_manifest = view
                            break
                        elif (
                            isinstance(view, dict)
                            and view.get("@id")
                            and "manifest" in view["@id"]
                        ):
                            iiif_manifest = view["@id"]
                            break
            except Exception:
                logger.debug(
                    "Europeana: hasView manifest discovery failed for %s",
                    item.get("id"),
                    exc_info=True,
                )
            if not iiif_manifest:
                obj = item.get("object")
                if isinstance(obj, str) and "manifest" in obj:
                    iiif_manifest = obj
            # If still none, construct Europeana Manifest API URL from id
            if not iiif_manifest and item.get("id"):
                built = _build_manifest_url_from_id(item.get("id"), prefer_v3=True)
                if built:
                    iiif_manifest = built
            data_provider = item.get("dataProvider")
            if isinstance(data_provider, list) and data_provider:
                provider = data_provider[0]
            elif isinstance(data_provider, str) and data_provider:
                provider = data_provider
            else:
                provider = None
            year = item.get("year")
            if isinstance(year, list):
                year = year[0] if year else None
            raw = {
                "title": item_title,
                "creator": item_creator,
                "date": str(year) if year else None,
                "id": item.get("id"),
                "item_url": item.get("guid"),
                "europeana_url": item.get("guid"),
                "provider": provider,
                "iiif_manifest": iiif_manifest,
            }
            results.append(convert_to_searchresult("Europeana", raw))
            if len(results) >= max_results:
                break
    elif data and not data.get("success"):
        logger.error("Europeana API error: %s", data.get("error"))
    return results


def download_europeana_work(
    item_data: SearchResult | dict[str, Any], output_folder: str
) -> bool:
    # Save search metadata
    item_id = resolve_item_id(item_data) or resolve_item_field(
        item_data, "title", attr="title", default="unknown_item"
    )
    raw_data = item_data.raw if isinstance(item_data, SearchResult) else item_data
    if raw_data:
        save_json(raw_data, output_folder, f"europeana_{item_id}_search_meta")
    iiif_manifest_url = resolve_item_field(
        item_data, "iiif_manifest", attr="iiif_manifest"
    )

    # If missing, construct Europeana Manifest API URL
    if not iiif_manifest_url:
        built = _build_manifest_url_from_id(item_id, prefer_v3=True)
        iiif_manifest_url = built

    if not iiif_manifest_url:
        logger.info(
            "No IIIF manifest URL found or constructed for Europeana item: %s", item_id
        )
        return False

    # The API key is added here rather than being stored in the search result.
    fetch_url = _append_wskey(iiif_manifest_url, _api_key())
    logger.info("Fetching Europeana IIIF manifest: %s", iiif_manifest_url)
    manifest_data = make_request(fetch_url)
    if not isinstance(manifest_data, dict):
        logger.warning("Failed to fetch IIIF manifest from %s", iiif_manifest_url)
        return False

    save_json(manifest_data, output_folder, f"europeana_{item_id}_iiif_manifest")

    # Try manifest-level renderings (PDF/EPUB) first
    renders = 0
    try:
        renders = download_iiif_renderings(manifest_data, output_folder)
        if renders > 0 and prefer_pdf_over_images():
            logger.info(
                "Europeana: downloaded %d rendering(s); skipping image downloads "
                "per config.",
                renders,
            )
            return True
    except Exception:
        logger.exception(
            "Europeana: error while downloading manifest renderings for %s", item_id
        )

    # Extract IIIF Image API service bases and download images (v2/v3)
    service_bases = extract_image_service_bases(manifest_data)

    if service_bases:
        ok_any = download_page_images(
            service_bases, output_folder, "europeana", item_id
        )
    else:
        # Fallback: try direct image URLs (common in simplified IIIF v3 manifests)
        direct_urls = extract_direct_image_urls(manifest_data)
        ok_any = download_direct_image_urls(
            direct_urls, output_folder, "europeana", item_id
        )

    if ok_any:
        return True

    # Fallback: query Europeana Record API for media links (edmIsShownBy, edmPreview)
    try:
        key = _api_key()
        record_url = None
        if isinstance(item_data, SearchResult):
            euro_id = item_data.raw.get("id") or item_id
        else:
            euro_id = item_id
        if euro_id and isinstance(euro_id, str) and euro_id.startswith("/"):
            record_url = f"{RECORD_API_BASE}{euro_id}.json"
        elif euro_id:
            # Try to coerce
            record_url = f"{RECORD_API_BASE}/{euro_id.strip('/')}.json"
        if record_url:
            params = {"wskey": key} if key else None
            logger.info("Europeana fallback: fetching Record API JSON %s", record_url)
            rec = make_request(record_url, params=params)
            if isinstance(rec, dict):
                # Try common fields
                candidates: list[str] = []

                def _add(u: str | None) -> None:
                    if u and isinstance(u, str):
                        candidates.append(u)

                # Record API v2 nests the media links one level down: the
                # aggregations live under "object", not at the top level, and
                # edmPreview sits on object.europeanaAggregation. Reading them
                # off the envelope found nothing.
                obj = rec.get("object") or {}
                if not isinstance(obj, dict):
                    obj = {}
                for agg in obj.get("aggregations", []) or []:
                    if isinstance(agg, dict):
                        _add(agg.get("edmIsShownBy"))
                        _add(agg.get("edmPreview"))
                # Thumbnail last: edmIsShownBy is the full media file.
                euro_agg = obj.get("europeanaAggregation") or {}
                if isinstance(euro_agg, dict):
                    _add(euro_agg.get("edmPreview"))
                # Download first working candidate
                for idx, u in enumerate(candidates, start=1):
                    try:
                        fname = f"europeana_{item_id}_fallback_{idx:02d}"
                        if download_file(u, output_folder, fname):
                            return True
                    except Exception:
                        continue
    except Exception:
        logger.exception("Europeana fallback failed for %s", item_id)
    return renders > 0
