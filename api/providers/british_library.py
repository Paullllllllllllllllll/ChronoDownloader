"""Connector for the British Library SRU and IIIF APIs."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

from ..core.budget import budget_exhausted
from ..core.config import get_max_pages, prefer_pdf_over_images
from ..core.download import save_json
from ..core.network import make_request
from ..iiif import (
    download_iiif_renderings,
    download_one_from_service,
    extract_image_service_bases,
)
from ..model import SearchResult, convert_to_searchresult, resolve_item_id
from ..query_helpers import escape_sparql_string, escape_sru_literal

logger = logging.getLogger(__name__)

SRU_BASE_URL = "https://sru.bl.uk/SRU"
IIIF_MANIFEST_BASE = (
    "https://api.bl.uk/metadata/iiif/ark:/81055/{identifier}/manifest.json"
)
BNB_SPARQL_URL = "https://bnb.data.bl.uk/sparql"


def _search_bnb_sparql(
    title: str, creator: str | None, max_results: int
) -> list[SearchResult]:
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
        f'  FILTER(CONTAINS(LCASE(STR(?title)), LCASE("{t}")))\n'
        "  OPTIONAL {\n"
        "    ?work dct:creator ?creator .\n"
        "    OPTIONAL { ?creator foaf:name ?creatorName }\n"
        "    OPTIONAL { ?creator rdfs:label ?creatorName }\n"
        "  }\n"
        "  OPTIONAL { ?work owl:sameAs ?same }\n"
        "  OPTIONAL { ?work rdfs:seeAlso ?same }\n"
        "  OPTIONAL { ?work dct:identifier ?ident }\n"
        + (
            '  FILTER(CONTAINS(LCASE(COALESCE(STR(?creatorName), "")), '
            f'LCASE("{c}")))\n'
            if c
            else ""
        )
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
        logger.warning("BNB SPARQL request failed", exc_info=True)
        data = None
    results: list[SearchResult] = []
    try:
        bindings = (
            (data if isinstance(data, dict) else {})
            .get("results", {})
            .get("bindings", [])
        )
        for b in bindings:

            def _val(name: str) -> str | None:
                v = b.get(name)  # noqa: B023
                if isinstance(v, dict):
                    return v.get("value")
                return None

            # Never substitute the query here: a binding that carries no
            # dct:title used to be handed the searched-for title, which
            # scored a perfect 100 against it and let an arbitrary work
            # sail through the min_title_score gate. An empty title
            # scores 0 and is filtered like any other untitled record.
            title_v = _val("title")
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
                    "title": title_v or "",
                    "creator": creator_v or None,
                    "identifier": ark,
                    "source": "bnb_sparql",
                }
                results.append(convert_to_searchresult("British Library", raw))
                if len(results) >= max_results:
                    break
    except Exception:
        logger.exception("BNB SPARQL fallback parsing error")
    return results


def search_british_library(
    title: str, creator: str | None = None, max_results: int = 3
) -> list[SearchResult]:
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
    response_text = make_request(
        SRU_BASE_URL, params=params, headers={"Accept": "application/xml,text/xml"}
    )

    results: list[SearchResult] = []
    if isinstance(response_text, str):
        namespaces = {
            "srw": "http://www.loc.gov/zing/srw/",
            "dc": "http://purl.org/dc/elements/1.1/",
        }
        root = None
        try:
            root = ET.fromstring(response_text)
        except ET.ParseError as e:
            logger.error("Error parsing BL SRU XML: %s", e)
        if root is not None:
            for record in root.findall(".//srw:recordData", namespaces):
                try:
                    # The DCMES namespace bound to "dc" below defines the
                    # fifteen elements and no container, so no conformant
                    # server ever sends a {dc}dc wrapper: SRU wraps the
                    # payload in {info:srw/schema/1/dc-v1.1}dc and OAI-PMH
                    # style responses in {...oai_dc/}dc. Take whatever
                    # single element recordData carries and read the
                    # dc:-prefixed children out of it.
                    dc = next(iter(record), None)
                    if dc is None:
                        continue
                    title_el = dc.find("dc:title", namespaces)
                    creator_el = dc.find("dc:creator", namespaces)
                    date_el = dc.find("dc:date", namespaces)
                    identifier_el = dc.find("dc:identifier", namespaces)
                    identifier = None
                    if identifier_el is not None and identifier_el.text:
                        # Stop at the first separator: a manifest URL would
                        # otherwise carry "/manifest.json" and a prose
                        # identifier its trailing gloss into the persisted
                        # ARK and into the viewer fallback URL built from it.
                        match = re.search(r"ark:/81055/([^\s/?#]+)", identifier_el.text)
                        if match:
                            identifier = match.group(1)

                    # Without an ARK the record cannot be downloaded; keeping it
                    # would also suppress the BNB SPARQL fallback below.
                    if not identifier:
                        logger.debug(
                            "BL: skipping SRU record without an ARK identifier (%s)",
                            title_el.text if title_el is not None else "unknown title",
                        )
                        continue

                    raw = {
                        "title": title_el.text if title_el is not None else "",
                        "creator": creator_el.text if creator_el is not None else None,
                        "date": date_el.text if date_el is not None else None,
                        "identifier": identifier,
                    }
                    results.append(convert_to_searchresult("British Library", raw))
                except Exception:
                    logger.warning("BL: skipping malformed SRU record", exc_info=True)
                    continue

    if results:
        return results

    logger.info("BL SRU returned no results; trying BNB SPARQL fallback for: %s", title)
    try:
        sparql_results = _search_bnb_sparql(title, creator, max_results)
    except Exception:
        logger.exception("BNB SPARQL fallback failed")
        sparql_results = []
    return sparql_results


def download_british_library_work(
    item_data: SearchResult | dict[str, Any], output_folder: str
) -> bool:
    """Download IIIF manifest and page images for a British Library item."""

    identifier = resolve_item_id(item_data, "identifier")
    if not identifier:
        logger.warning("No BL identifier provided for download.")
        return False

    # Normalize identifier: viewer ARKs often include a ".0x..." suffix which is
    # not present in the manifest path
    id_for_manifest = identifier.split(".")[0] if "." in identifier else identifier

    manifest_url = IIIF_MANIFEST_BASE.format(identifier=id_for_manifest)
    logger.info("Fetching BL IIIF manifest: %s", manifest_url)
    manifest = make_request(manifest_url)

    # Fallback: if direct manifest fetch failed, try discovering it from the
    # public viewer page. The test is on the type, not on truthiness:
    # make_request returns a str for an HTML body, and api.bl.uk answers with
    # an error or maintenance page rather than a status code, so a truthiness
    # test skipped the fallback in exactly the situation it exists for.
    if not isinstance(manifest, dict):
        try:
            viewer_url = f"https://access.bl.uk/item/viewer/ark:/81055/{identifier}"
            logger.info(
                "BL fallback: attempting to discover manifest from %s", viewer_url
            )
            html = make_request(viewer_url)
            if isinstance(html, str):
                m = re.search(r"https?://[^\"'<>]+/manifest\.json", html)
                if m:
                    alt_manifest = m.group(0)
                    logger.info("BL fallback: found manifest URL %s", alt_manifest)
                    alt = make_request(alt_manifest)
                    if isinstance(alt, dict):
                        manifest = alt
                        manifest_url = alt_manifest
        except Exception:
            logger.exception(
                "BL: error while attempting viewer-based manifest discovery for %s",
                identifier,
            )
    if not isinstance(manifest, dict):
        return False

    # Save manifest for reproducibility
    save_json(manifest, output_folder, f"bl_{identifier}_manifest")

    # Prefer manifest-level PDF/EPUB renderings if available
    renders = 0
    try:
        renders = download_iiif_renderings(manifest, output_folder)
        if renders > 0 and prefer_pdf_over_images():
            logger.info(
                "British Library: downloaded %d rendering(s); skipping image "
                "downloads per config.",
                renders,
            )
            return True
    except Exception:
        logger.exception(
            "BL: error while downloading manifest renderings for %s", identifier
        )

    # Extract IIIF Image API service bases from v2 or v3
    service_bases = extract_image_service_bases(manifest)

    if not service_bases:
        logger.info("No IIIF image services found in BL manifest for %s", identifier)
        return renders > 0

    # Use shared helper to attempt per-canvas image downloads

    max_pages = get_max_pages("british_library")
    total = len(service_bases)
    to_download = (
        service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
    )
    logger.info(
        "British Library: downloading %d/%d page images for %s",
        len(to_download),
        total,
        identifier,
    )
    ok_any = False
    for idx, svc in enumerate(to_download, start=1):
        if budget_exhausted():
            logger.warning(
                "Download budget exhausted; stopping British Library downloads "
                "at %d/%d pages for %s",
                idx - 1,
                len(to_download),
                identifier,
            )
            break
        try:
            fname = f"bl_{identifier}_p{idx:05d}.jpg"
            if download_one_from_service(svc, output_folder, fname):
                ok_any = True
            else:
                logger.warning("Failed to download BL image from %s", svc)
        except Exception:
            logger.exception(
                "Error downloading BL image for %s from %s", identifier, svc
            )
    return ok_any or renders > 0
