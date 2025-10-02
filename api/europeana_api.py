import logging
import os
from typing import List, Union

from .utils import (
    save_json,
    make_request,
    get_max_pages,
    download_iiif_renderings,
    prefer_pdf_over_images,
    download_file,
)
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.europeana.eu/record/v2/search.json"
RECORD_API_BASE = "https://api.europeana.eu/record/v2"
EUROPEANA_MANIFEST_HOST = "https://iiif.europeana.eu"


def _api_key() -> str | None:
    """Get Europeana API key from environment."""
    # Read at call time so keys loaded from .env or environment later are picked up
    return os.getenv("EUROPEANA_API_KEY")


def _build_manifest_url_from_id(euro_id: str, api_key: str | None, prefer_v3: bool = True) -> str | None:
    """Construct the Europeana IIIF Manifest API URL from a Europeana record id.

    Europeana search results typically have ids like "/9200379/BibliographicResource_3000117247947".
    The manifest URL format is:
      https://iiif.europeana.eu/presentation/{collectionId}/{recordId}/manifest?wskey=KEY[&format=3]
    """
    if not euro_id:
        return None
    parts = [p for p in euro_id.strip().split('/') if p]
    # Expect [collectionId, recordId]
    if len(parts) >= 2:
        collection_id, record_id = parts[-2], parts[-1]
        url = f"{EUROPEANA_MANIFEST_HOST}/presentation/{collection_id}/{record_id}/manifest"
        params = []
        if api_key:
            params.append(f"wskey={api_key}")
        if prefer_v3:
            params.append("format=3")
        if params:
            url = url + "?" + "&".join(params)
        return url
    return None


def search_europeana(title: str, creator: str | None = None, max_results: int = 3) -> List[SearchResult]:
    key = _api_key()
    if not key:
        logger.warning("Europeana API key not configured. Skipping search.")
        return []
    query_parts = [f'title:"{title}"']
    if creator:
        query_parts.append(f'AND who:"{creator}"')
    query = " ".join(query_parts)
    params = {
        "wskey": key,
        "query": query,
        "rows": str(max_results * 3),  # pull extras to increase chance of IIIF availability
        "profile": "rich",
        "media": "true",
    }
    logger.info("Searching Europeana for: %s", title)
    data = make_request(API_BASE_URL, params=params)
    results: List[SearchResult] = []
    if data and data.get("success") and data.get("items"):
        for item in data["items"]:
            item_title = item.get("title", ["N/A"]) 
            if isinstance(item_title, list):
                item_title = item_title[0]
            item_creator = "N/A"
            if item.get("dcCreator"):
                item_creator = item["dcCreator"][0]
            iiif_manifest = None
            # Prefer direct manifest URL if given by provider
            # Check edmIsShownBy / hasView / object
            try:
                if item.get("edmAggregatedCHO") and item["edmAggregatedCHO"].get("hasView"):
                    views = item["edmAggregatedCHO"]["hasView"]
                    if not isinstance(views, list):
                        views = [views]
                    for view in views:
                        if isinstance(view, str) and "manifest" in view:
                            iiif_manifest = view
                            break
                        elif isinstance(view, dict) and view.get("@id") and "manifest" in view["@id"]:
                            iiif_manifest = view["@id"]
                            break
            except Exception:
                pass
            if not iiif_manifest:
                obj = item.get("object")
                if isinstance(obj, str) and "manifest" in obj:
                    iiif_manifest = obj
            # If still none, construct Europeana Manifest API URL from id
            if not iiif_manifest and item.get("id"):
                built = _build_manifest_url_from_id(item.get("id"), key, prefer_v3=True)
                if built:
                    iiif_manifest = built
            raw = {
                "title": item_title,
                "creator": item_creator,
                "id": item.get("id"),
                "europeana_url": item.get("guid"),
                "provider": item.get("dataProvider", ["N/A"])[0] if item.get("dataProvider") else "N/A",
                "iiif_manifest": iiif_manifest,
            }
            results.append(convert_to_searchresult("Europeana", raw))
            if len(results) >= max_results:
                break
    elif data and not data.get("success"):
        logger.error("Europeana API error: %s", data.get("error"))
    return results


def download_europeana_work(item_data: Union[SearchResult, dict], output_folder: str) -> bool:
    # Save search metadata
    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id") or item_data.title or "unknown_item"
        if item_data.raw:
            save_json(item_data.raw, output_folder, f"europeana_{item_id}_search_meta")
        iiif_manifest_url = item_data.iiif_manifest or item_data.raw.get("iiif_manifest")
    else:
        item_id = item_data.get("id", item_data.get("title", "unknown_item"))
        save_json(item_data, output_folder, f"europeana_{item_id}_search_meta")
        iiif_manifest_url = item_data.get("iiif_manifest")

    # If missing, construct Europeana Manifest API URL
    if not iiif_manifest_url:
        key = _api_key()
        built = _build_manifest_url_from_id(item_id, key, prefer_v3=True)
        iiif_manifest_url = built

    if not iiif_manifest_url:
        logger.info("No IIIF manifest URL found or constructed for Europeana item: %s", item_id)
        return False

    logger.info("Fetching Europeana IIIF manifest: %s", iiif_manifest_url)
    manifest_data = make_request(iiif_manifest_url)
    if not manifest_data:
        logger.warning("Failed to fetch IIIF manifest from %s", iiif_manifest_url)
        # Continue to fallback below
        manifest_data = None

    if manifest_data:
        save_json(manifest_data, output_folder, f"europeana_{item_id}_iiif_manifest")

    # Try manifest-level renderings (PDF/EPUB) first
    if manifest_data:
        try:
            renders = download_iiif_renderings(manifest_data, output_folder, filename_prefix=f"europeana_{item_id}_")
            if renders > 0 and prefer_pdf_over_images():
                logger.info("Europeana: downloaded %d rendering(s); skipping image downloads per config.", renders)
                return True
        except Exception:
            logger.exception("Europeana: error while downloading manifest renderings for %s", item_id)

    # Extract IIIF Image API service bases and download images (v2/v3)
    ok_any = False
    if manifest_data:
        service_bases: List[str] = extract_image_service_bases(manifest_data)

        if not service_bases:
            logger.info("No IIIF image services found in Europeana manifest for %s", item_id)
        else:
            # Use shared helper to download a single image per canvas
            max_pages = get_max_pages("europeana")
            total = len(service_bases)
            to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
            logger.info("Europeana: downloading %d/%d page images for %s", len(to_download), total, item_id)
            for idx, svc in enumerate(to_download, start=1):
                try:
                    fname = f"europeana_{item_id}_p{idx:05d}.jpg"
                    if download_one_from_service(svc, output_folder, fname):
                        ok_any = True
                    else:
                        logger.warning("Failed to download Europeana image from %s", svc)
                except Exception:
                    logger.exception("Error downloading Europeana image for %s from %s", item_id, svc)

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
                candidates: List[str] = []
                def _add(u: str | None):
                    if u and isinstance(u, str):
                        candidates.append(u)
                obj = rec.get("object") or {}
                _add(obj.get("edmIsShownBy"))
                _add(obj.get("edmPreview"))
                # Also look inside aggregations
                for agg in rec.get("aggregations", []) or []:
                    if isinstance(agg, dict):
                        _add(agg.get("edmIsShownBy"))
                        _add(agg.get("edmPreview"))
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
    return False
