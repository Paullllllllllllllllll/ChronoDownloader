"""Connector for the Biblioteca Nacional de España (BNE) APIs."""

import logging
from typing import List, Union

from .utils import (
    save_json,
    make_request,
    get_max_pages,
    download_iiif_renderings,
    prefer_pdf_over_images,
)
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult
from .query_helpers import escape_sparql_string

logger = logging.getLogger(__name__)

SEARCH_API_URL = "https://datos.bne.es/sparql"
# BNE IIIF manifests have been seen with both "/manifest" and "/manifest.json" endings across time.
# Try both patterns for robustness.
IIIF_MANIFEST_PATTERNS = [
    "https://iiif.bne.es/{item_id}/manifest",
    "https://iiif.bne.es/{item_id}/manifest.json",
]


def search_bne(title: str, creator: str | None = None, max_results: int = 3) -> List[SearchResult]:
    """Search the BNE SPARQL endpoint for works by title and creator."""

    t = escape_sparql_string(title)
    # Note: creator filter omitted due to heterogeneous properties in BNE; could be extended.
    query = f"""
        SELECT ?id ?title ?creator WHERE {{
            ?id <http://www.w3.org/2000/01/rdf-schema#label> ?title .
            FILTER(CONTAINS(LCASE(?title), LCASE('{t}')))
            OPTIONAL {{ ?id <http://dbpedia.org/ontology/author> ?creator . }}
        }} LIMIT {max_results}
    """

    params = {
        "query": query,
        "format": "json",
    }

    logger.info("Searching BNE for: %s", title)
    data = make_request(SEARCH_API_URL, params=params)

    results: List[SearchResult] = []
    if data and data.get("results") and data["results"].get("bindings"):
        for b in data["results"]["bindings"]:
            item_id = b.get("id", {}).get("value")
            item_title = b.get("title", {}).get("value")
            item_creator = b.get("creator", {}).get("value")
            if item_id:
                raw = {
                    "title": item_title or "N/A",
                    "creator": item_creator or "N/A",
                    "id": item_id,
                }
                results.append(convert_to_searchresult("BNE", raw))

    return results


def download_bne_work(item_data: Union[SearchResult, dict], output_folder) -> bool:
    """Download IIIF manifest and page images for a BNE item."""

    if isinstance(item_data, SearchResult):
        item_id = item_data.source_id or item_data.raw.get("id")
    else:
        item_id = item_data.get("id")
    if not item_id:
        logger.warning("No BNE item id provided.")
        return False

    if item_id.startswith("http"):
        item_identifier = item_id.rstrip("/").split("/")[-1]
    else:
        item_identifier = item_id

    manifest = None
    manifest_url = None
    for pattern in IIIF_MANIFEST_PATTERNS:
        candidate = pattern.format(item_id=item_identifier)
        logger.info("Fetching BNE IIIF manifest: %s", candidate)
        manifest = make_request(candidate)
        if manifest:
            manifest_url = candidate
            break

    if not manifest:
        return False

    # Save manifest
    save_json(manifest, output_folder, f"bne_{item_identifier}_manifest")

    # Prefer manifest-level PDF/EPUB renderings when available
    try:
        renders = download_iiif_renderings(manifest, output_folder, filename_prefix=f"bne_{item_identifier}_")
        if renders > 0 and prefer_pdf_over_images():
            logger.info("BNE: downloaded %d rendering(s); skipping image downloads per config.", renders)
            return True
    except Exception:
        logger.exception("BNE: error while downloading manifest renderings for %s", item_identifier)

    # Extract IIIF Image API service bases from v2 or v3
    service_bases: List[str] = extract_image_service_bases(manifest)

    if not service_bases:
        logger.info("No IIIF image services found in BNE manifest for %s", item_identifier)
        return True

    # Use shared helper for per-canvas downloads

    max_pages = get_max_pages("bne")
    total = len(service_bases)
    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
    logger.info("BNE: downloading %d/%d page images for %s", len(to_download), total, item_identifier)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"bne_{item_identifier}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                ok_any = True
            else:
                logger.warning("Failed to download BNE image from %s", svc)
        except Exception:
            logger.exception("Error downloading BNE image for %s from %s", item_identifier, svc)
    return ok_any
