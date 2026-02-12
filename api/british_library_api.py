"""Connector for the British Library SRU and IIIF APIs."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

from .core.config import get_max_pages, prefer_pdf_over_images
from .core.network import make_request
from .utils import save_json, download_iiif_renderings
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult, resolve_item_id
from .query_helpers import escape_sparql_string, escape_sru_literal

logger = logging.getLogger(__name__)

SRU_BASE_URL = "https://sru.bl.uk/SRU"
IIIF_MANIFEST_BASE = "https://api.bl.uk/metadata/iiif/ark:/81055/{identifier}/manifest.json"
BNB_SPARQL_URL = "https://bnb.data.bl.uk/sparql"

def _search_bnb_sparql(title: str, creator: str | None, max_results: int) -> list[SearchResult]:
    """Fallback search using BNB SPARQL endpoint to discover BL identifiers.

    We look for works whose title contains the query, optionally filtered by creator
    label. We extract any owl:sameAs/rdfs:seeAlso/dct:identifier values that include
    a BL ARK (ark:/81055/...).
    """
    t = escape_sparql_string(title)
    c = escape_sparql_string(creator) if creator else None
    # Keep query conservative and fast; limit results
    sparql = (
        "PREFIX dct: <http://purl.org/dc/terms/>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "PREFIX foaf: <http://xmlns.com/foaf/0.1/>\n"
        "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
        "SELECT ?work ?title ?creatorName ?same ?ident WHERE {\n"
        "  ?work dct:title ?title .\n"
        f"  FILTER(CONTAINS(LCASE(STR(?title)), LCASE(\"{t}\")))\n"
        "  OPTIONAL {\n"
        "    ?work dct:creator ?creator .\n"
        "    OPTIONAL { ?creator foaf:name ?creatorName }\n"
        "    OPTIONAL { ?creator rdfs:label ?creatorName }\n"
        "  }\n"
        "  OPTIONAL { ?work owl:sameAs ?same }\n"
        "  OPTIONAL { ?work rdfs:seeAlso ?same }\n"
        "  OPTIONAL { ?work dct:identifier ?ident }\n"
        + (f"  FILTER(CONTAINS(LCASE(COALESCE(STR(?creatorName), \"\")), LCASE(\"{c}\")))\n" if c else "")
        + "}\n"
        + f"LIMIT {max(5, max_results * 3)}\n"
    )
    try:
        data = make_request(
            BNB_SPARQL_URL,
            params={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
        )
    except Exception:
        data = None
    results: list[SearchResult] = []
    try:
        bindings = (data if isinstance(data, dict) else {}).get("results", {}).get("bindings", [])
        for b in bindings:
            def _val(name: str) -> str | None:
                v = b.get(name)
                if isinstance(v, dict):
                    return v.get("value")
                return None
            title_v = _val("title") or title
            creator_v = _val("creatorName")
            ark = None
            for key in ("same", "ident", "work"):
                v = _val(key)
                if isinstance(v, str) and "ark:/81055/" in v:
                    m = re.search(r'ark:/81055/([^\s"\'<>]+)', v)
                    if m:
                        ark = m.group(1)
                        break
            if ark:
                raw = {
                    "title": title_v or "N/A",
                    "creator": creator_v or (creator or "N/A"),
                    "identifier": ark,
                    "source": "bnb_sparql",
                }
                results.append(convert_to_searchresult("British Library", raw))
                if len(results) >= max_results:
                    break
    except Exception:
        logger.exception("BNB SPARQL fallback parsing error")
    return results

def search_british_library(title: str, creator: str | None = None, max_results: int = 3) -> list[SearchResult]:
    """Search the British Library using SRU; fallback to BNB SPARQL if needed."""

    q_title = escape_sru_literal(title)
    query_parts = [f'title all "{q_title}"']
    if creator:
        q_creator = escape_sru_literal(creator)
        query_parts.append(f'and creator all "{q_creator}"')
    query = " ".join(query_parts)

    params = {
        "version": "1.2",
        "operation": "searchRetrieve",
        "query": query,
        "maximumRecords": str(max_results),
        "recordSchema": "dc",
    }

    logger.info("Searching British Library (SRU) for: %s", title)
    response_text = make_request(SRU_BASE_URL, params=params, headers={"Accept": "application/xml,text/xml"})

    results: list[SearchResult] = []
    if isinstance(response_text, str):
        try:
            namespaces = {
                "srw": "http://www.loc.gov/zing/srw/",
                "dc": "http://purl.org/dc/elements/1.1/",
            }
            root = ET.fromstring(response_text)
            for record in root.findall(".//srw:recordData", namespaces):
                dc = record.find("dc:dc", namespaces)
                if dc is None:
                    continue
                title_el = dc.find("dc:title", namespaces)
                creator_el = dc.find("dc:creator", namespaces)
                date_el = dc.find("dc:date", namespaces)
                identifier_el = dc.find("dc:identifier", namespaces)
                identifier = None
                if identifier_el is not None and identifier_el.text:
                    match = re.search(r"ark:/81055/(.*)", identifier_el.text)
                    if match:
                        identifier = match.group(1)

                raw = {
                    "title": title_el.text if title_el is not None else "N/A",
                    "creator": creator_el.text if creator_el is not None else "N/A",
                    "date": date_el.text if date_el is not None else None,
                    "identifier": identifier,
                }
                results.append(convert_to_searchresult("British Library", raw))
        except ET.ParseError as e:
            logger.error("Error parsing BL SRU XML: %s", e)

    if results:
        return results

    logger.info("BL SRU returned no results; trying BNB SPARQL fallback for: %s", title)
    try:
        sparql_results = _search_bnb_sparql(title, creator, max_results)
    except Exception:
        logger.exception("BNB SPARQL fallback failed")
        sparql_results = []
    return sparql_results

def download_british_library_work(item_data: SearchResult | dict, output_folder: str) -> bool:
    """Download IIIF manifest and page images for a British Library item."""

    identifier = resolve_item_id(item_data, "identifier")
    if not identifier:
        logger.warning("No BL identifier provided for download.")
        return False

    # Normalize identifier: viewer ARKs often include a ".0x..." suffix which is not present in the manifest path
    id_for_manifest = identifier.split(".")[0] if "." in identifier else identifier

    manifest_url = IIIF_MANIFEST_BASE.format(identifier=id_for_manifest)
    logger.info("Fetching BL IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)

    # Fallback: if direct manifest fetch failed, try discovering it from the public viewer page
    if not manifest:
        try:
            viewer_url = f"https://access.bl.uk/item/viewer/ark:/81055/{identifier}"
            logger.info("BL fallback: attempting to discover manifest from %s", viewer_url)
            html = make_request(viewer_url)
            if isinstance(html, str):
                m = re.search(r"https?://[^\"'<>]+/manifest\.json", html)
                if m:
                    alt_manifest = m.group(0)
                    logger.info("BL fallback: found manifest URL %s", alt_manifest)
                    manifest = make_request(alt_manifest)
                    if manifest:
                        manifest_url = alt_manifest
        except Exception:
            logger.exception("BL: error while attempting viewer-based manifest discovery for %s", identifier)
    if not isinstance(manifest, dict):
        return False

    # Save manifest for reproducibility
    save_json(manifest, output_folder, f"bl_{identifier}_manifest")

    # Prefer manifest-level PDF/EPUB renderings if available
    try:
        renders = download_iiif_renderings(manifest, output_folder, filename_prefix=f"bl_{identifier}_")
        if renders > 0 and prefer_pdf_over_images():
            logger.info("British Library: downloaded %d rendering(s); skipping image downloads per config.", renders)
            return True
    except Exception:
        logger.exception("BL: error while downloading manifest renderings for %s", identifier)

    # Extract IIIF Image API service bases from v2 or v3
    service_bases = extract_image_service_bases(manifest)

    if not service_bases:
        logger.info("No IIIF image services found in BL manifest for %s", identifier)
        return True

    # Use shared helper to attempt per-canvas image downloads

    max_pages = get_max_pages("british_library")
    total = len(service_bases)
    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
    logger.info("British Library: downloading %d/%d page images for %s", len(to_download), total, identifier)
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        try:
            fname = f"bl_{identifier}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                ok_any = True
            else:
                logger.warning("Failed to download BL image from %s", svc)
        except Exception:
            logger.exception("Error downloading BL image for %s from %s", identifier, svc)
    return ok_any
