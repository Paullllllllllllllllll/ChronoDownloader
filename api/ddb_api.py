"""Connector for the Deutsche Digitale Bibliothek (DDB) API.

DDB aggregates content from German cultural institutions. Most items link to
external providers (e.g., Heidelberg, Göttingen libraries) which have their own
IIIF manifests. This connector attempts to construct IIIF manifest URLs from
the provider's isShownAt links.
"""

import logging
import os
import re
from typing import List, Union, Optional

from .utils import (
    save_json,
    make_request,
    get_max_pages,
    download_file,
    download_iiif_renderings,
    prefer_pdf_over_images,
)
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)


API_BASE_URL = "https://api.deutsche-digitale-bibliothek.de/"

# Known patterns for IIIF manifest URLs from DDB providers
IIIF_MANIFEST_PATTERNS = [
    # Heidelberg University Library
    (r"digi\.ub\.uni-heidelberg\.de/diglit/([^/]+)", 
     "https://digi.ub.uni-heidelberg.de/diglit/iiif/{}/manifest.json"),
    # Göttingen State and University Library
    (r"resolver\.sub\.uni-goettingen\.de/purl\?([^/&]+)",
     "https://manifests.sub.uni-goettingen.de/iiif/presentation/{}/manifest"),
    # Bavarian State Library (BSB/MDZ)
    (r"(?:mdz-nbn-resolving\.de|digitale-sammlungen\.de)/\w+/(bsb\d+)",
     "https://api.digitale-sammlungen.de/iiif/presentation/v2/{}/manifest"),
]


def _extract_iiif_manifest_url(is_shown_at: str) -> Optional[str]:
    """Try to construct IIIF manifest URL from isShownAt link."""
    if not is_shown_at:
        return None
    
    for pattern, template in IIIF_MANIFEST_PATTERNS:
        match = re.search(pattern, is_shown_at)
        if match:
            item_id = match.group(1)
            manifest_url = template.format(item_id)
            logger.debug("DDB: Constructed IIIF manifest URL: %s", manifest_url)
            return manifest_url
    
    return None


def _api_key() -> str | None:
    """Get DDB API key from environment."""
    return os.getenv("DDB_API_KEY")


def search_ddb(title: str, creator: str | None = None, max_results: int = 3) -> List[SearchResult]:
    """Search the DDB API for a title and optional creator."""

    key = _api_key()
    if not key:
        logger.warning("DDB API key not configured. Skipping DDB search.")
        return []

    query_parts = [f'"{title}"']
    if creator:
        query_parts.append(f'AND creator:"{creator}"')
    query = " ".join(query_parts)

    params = {
        "oauth_consumer_key": key,
        "query": query,
        "rows": max_results,
    }

    logger.info("Searching DDB for: %s", title)
    data = make_request(f"{API_BASE_URL}search", params=params)

    results: List[SearchResult] = []
    if not isinstance(data, dict):
        return results
    if data.get("results"):
        # DDB API returns nested structure: results[].docs[]
        for result_group in data["results"]:
            docs = result_group.get("docs", [])
            for item in docs:
                # Clean title by removing <match> tags
                title = item.get("label") or item.get("title") or "N/A"
                title = title.replace("<match>", "").replace("</match>", "")
                
                # Extract creator from view array if available
                creator = ""
                view = item.get("view", [])
                if len(view) > 6:
                    creator = view[6]  # Provider/creator is often at index 6
                
                ddb_id = item.get("id")
                raw = {
                    "title": title,
                    "creator": creator,
                    "id": ddb_id,
                    "item_url": f"https://www.deutsche-digitale-bibliothek.de/item/{ddb_id}" if ddb_id else None,
                    "thumbnail": item.get("thumbnail"),
                    "iiif_manifest": None,  # Will be fetched from item metadata
                }
                results.append(convert_to_searchresult("DDB", raw))
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

    return results


def download_ddb_work(item_data: Union[SearchResult, dict], output_folder: str) -> bool:
    """Download IIIF manifest and page images for a DDB item.

    DDB aggregates items from many German institutions. This function:
    - Fetches item metadata from DDB API
    - Extracts isShownAt/isShownBy links to the original provider
    - Attempts to construct IIIF manifest URL from known provider patterns
    - Falls back to downloading the preview image if no IIIF available
    """

    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id")
    else:
        item_id = item_data.get("id")
    if not item_id:
        logger.warning("No DDB item id found in item data.")
        return False

    key = _api_key()
    params = {"oauth_consumer_key": key} if key else None
    item_meta = make_request(f"{API_BASE_URL}items/{item_id}", params=params)

    if not isinstance(item_meta, dict):
        return False

    save_json(item_meta, output_folder, f"ddb_{item_id}_metadata")

    # Try to find manifest URL from multiple sources
    manifest_url = item_meta.get("iiifManifest") or (
        item_data.raw.get("iiif_manifest") if isinstance(item_data, SearchResult) else item_data.get("iiif_manifest")
    )
    
    # Extract isShownAt and isShownBy from EDM metadata
    is_shown_at = None
    is_shown_by = None
    try:
        edm = item_meta.get("edm", {})
        rdf = edm.get("RDF", {})
        agg = rdf.get("Aggregation", {})
        
        is_shown_at_obj = agg.get("isShownAt", {})
        is_shown_by_obj = agg.get("isShownBy", {})
        
        if isinstance(is_shown_at_obj, dict):
            is_shown_at = is_shown_at_obj.get("@resource")
        elif isinstance(is_shown_at_obj, str):
            is_shown_at = is_shown_at_obj
            
        if isinstance(is_shown_by_obj, dict):
            is_shown_by = is_shown_by_obj.get("@resource")
        elif isinstance(is_shown_by_obj, str):
            is_shown_by = is_shown_by_obj
    except Exception:
        logger.debug("DDB: Could not extract isShownAt/isShownBy from EDM metadata")
    
    # Try to construct IIIF manifest URL from isShownAt if not already found
    if not manifest_url and is_shown_at:
        manifest_url = _extract_iiif_manifest_url(is_shown_at)
        if manifest_url:
            logger.info("DDB: Constructed IIIF manifest URL from isShownAt: %s", manifest_url)
    
    manifest = None
    if manifest_url:
        logger.info("DDB: Fetching IIIF manifest: %s", manifest_url)
        manifest = make_request(manifest_url)
    
    if not isinstance(manifest, dict):
        # Fall back to downloading isShownBy image directly
        if is_shown_by:
            logger.info("DDB: No IIIF manifest, falling back to isShownBy image: %s", is_shown_by)
            if download_file(is_shown_by, output_folder, f"ddb_{item_id}_preview"):
                return True
        logger.warning("DDB: Could not find IIIF manifest or fallback image for %s", item_id)
        return False

    # Save manifest
    save_json(manifest, output_folder, f"ddb_{item_id}_iiif_manifest")

    # Prefer manifest-level renderings (PDF/EPUB) when available
    try:
        renders = download_iiif_renderings(manifest, output_folder, filename_prefix=f"ddb_{item_id}_")
        if renders > 0 and prefer_pdf_over_images():
            logger.info("DDB: downloaded %d rendering(s); skipping image downloads per config.", renders)
            return True
    except Exception:
        logger.exception("DDB: error while downloading manifest renderings for %s", item_id)

    # Extract IIIF image service bases from v2 or v3
    image_service_bases = extract_image_service_bases(manifest)

    if not image_service_bases:
        logger.info("No IIIF image services found in DDB manifest for %s", item_id)
        return True

    # Use shared helper to attempt per-canvas image downloads

    max_pages = get_max_pages("ddb")
    total = len(image_service_bases)
    to_download = image_service_bases[:max_pages] if max_pages and max_pages > 0 else image_service_bases
    logger.info("DDB: downloading %d/%d page images for %s", len(to_download), total, item_id)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"ddb_{item_id}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                ok_any = True
            else:
                logger.warning("Failed to download DDB image from %s", svc)
        except Exception:
            logger.exception("Error downloading DDB image for %s from %s", item_id, svc)

    return ok_any
