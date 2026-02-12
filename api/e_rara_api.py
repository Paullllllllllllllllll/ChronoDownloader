"""Connector for the e-rara.ch SRU + IIIF APIs.

Searches e-rara via its SRU endpoint (MODS schema) and downloads via IIIF
manifests published for each VLID (Visual Library ID).
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from .download_helpers import download_iiif_manifest_and_images
from .model import SearchResult, convert_to_searchresult, resolve_item_id, resolve_item_field
from .query_helpers import escape_sru_literal
from .core.network import make_request

logger = logging.getLogger(__name__)

SRU_URL = "https://www.e-rara.ch/sru"
IIIF_MANIFEST_URL = "https://www.e-rara.ch/i3f/v20/{vlid}/manifest"
ITEM_URL = "https://www.e-rara.ch/{prefix}/{vlid}"

def _build_query(title: str, creator: str | None) -> str:
    parts: list[str] = []
    if title:
        parts.append(f'"{escape_sru_literal(title)}"')
    if creator:
        parts.append(f'"{escape_sru_literal(creator)}"')
    return " ".join(parts).strip()

def search_e_rara(title: str, creator: str | None = None, max_results: int = 3) -> list[SearchResult]:
    """Search e-rara via SRU (MODS schema)."""
    query = _build_query(title, creator)
    if not query:
        return []

    params = {
        "version": "1.2",
        "operation": "searchRetrieve",
        "query": query,
        "maximumRecords": str(max_results),
        "recordSchema": "mods",
    }
    logger.info("Searching e-rara for: %s", title)
    response_text = make_request(SRU_URL, params=params)
    if not isinstance(response_text, str):
        return []

    results: list[SearchResult] = []
    try:
        ns = {
            "srw": "http://www.loc.gov/zing/srw/",
            "mods": "http://www.loc.gov/mods/v3",
            "vl": "http://visuallibrary.net/vl",
        }
        root = ET.fromstring(response_text)
        for record in root.findall(".//srw:record", ns):
            mods = record.find(".//mods:mods", ns)
            extra = record.find(".//srw:extraRecordData", ns)
            if mods is None:
                continue

            title_el = mods.find(".//mods:titleInfo/mods:title", ns)
            item_title = title_el.text.strip() if title_el is not None and title_el.text else "N/A"

            creator_el = mods.find(".//mods:name/mods:displayForm", ns)
            if creator_el is None:
                creator_el = mods.find(".//mods:name/mods:namePart", ns)
            item_creator = creator_el.text.strip() if creator_el is not None and creator_el.text else "N/A"

            vlid = None
            prefix = None
            if extra is not None:
                vlid_el = extra.find("vl:id", ns)
                prefix_el = extra.find("vl:prefix", ns)
                if vlid_el is not None and vlid_el.text:
                    vlid = vlid_el.text.strip()
                if prefix_el is not None and prefix_el.text:
                    prefix = prefix_el.text.strip()

            if not vlid:
                continue

            raw = {
                "title": item_title,
                "creator": item_creator,
                "id": vlid,
                "prefix": prefix,
                "item_url": ITEM_URL.format(prefix=prefix, vlid=vlid) if prefix else None,
                "iiif_manifest": IIIF_MANIFEST_URL.format(vlid=vlid),
            }
            results.append(convert_to_searchresult("e-rara", raw))
            if len(results) >= max_results:
                break
    except ET.ParseError as e:
        logger.error("e-rara SRU XML parse error: %s", e)
    except Exception:
        logger.exception("Unexpected error during e-rara SRU parsing")

    return results

def download_e_rara_work(item_data: SearchResult | dict, output_folder: str) -> bool:
    """Download via e-rara IIIF manifest."""
    vlid = resolve_item_id(item_data)
    manifest_url = resolve_item_field(item_data, "iiif_manifest", attr="iiif_manifest")

    if not vlid and not manifest_url:
        logger.warning("No e-rara VLID or manifest URL provided.")
        return False

    if not manifest_url and vlid:
        manifest_url = IIIF_MANIFEST_URL.format(vlid=vlid)

    if not manifest_url:
        logger.warning("No IIIF manifest URL for e-rara item: %s", vlid)
        return False

    return download_iiif_manifest_and_images(
        manifest_url=manifest_url,
        output_folder=output_folder,
        provider_key="e_rara",
        item_id=str(vlid or "erara"),
    )
