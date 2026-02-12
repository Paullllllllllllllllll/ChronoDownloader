"""Connector for the Staatsbibliothek zu Berlin digital collections.

Search uses the GBV SRU endpoint for StaBiKat and downloads via the
METS resolver (digitized collections) that exposes direct image/PDF URLs.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from .model import SearchResult, convert_to_searchresult, resolve_item_id, resolve_item_field
from .query_helpers import escape_sru_literal
from .core.budget import budget_exhausted
from .core.config import get_max_pages, prefer_pdf_over_images
from .core.network import make_request
from .utils import download_file, save_json

logger = logging.getLogger(__name__)

SRU_URL = "https://sru.gbv.de/stabikat"
METS_URL = "https://digital.staatsbibliothek-berlin.de/dms/metsresolver/?PPN={ppn}"
ITEM_URL = "https://digital.staatsbibliothek-berlin.de/werkansicht?PPN={ppn}"

def _candidate_queries(title: str, creator: str | None) -> list[str]:
    q_title = escape_sru_literal(title)
    q_creator = escape_sru_literal(creator) if creator else None
    queries = []
    if q_creator:
        queries.append(f'pica.tit="{q_title}" AND pica.aut="{q_creator}"')
        queries.append(f'dc.title="{q_title}" AND dc.creator="{q_creator}"')
    queries.append(f'pica.tit="{q_title}"')
    queries.append(f'dc.title="{q_title}"')
    queries.append(f'"{q_title}"')
    return [q for q in queries if q]

def search_sbb_digital(title: str, creator: str | None = None, max_results: int = 3) -> list[SearchResult]:
    """Search StaBiKat via SRU for potential digitized items."""
    results: list[SearchResult] = []
    for query in _candidate_queries(title, creator):
        params = {
            "version": "1.2",
            "operation": "searchRetrieve",
            "query": query,
            "maximumRecords": str(max_results),
            "recordSchema": "mods",
        }
        logger.info("Searching StaBiKat SRU for: %s", title)
        response_text = make_request(SRU_URL, params=params, headers={"Accept": "application/xml"})
        if not isinstance(response_text, str):
            continue

        try:
            ns = {
                "srw": "http://www.loc.gov/zing/srw/",
                "mods": "http://www.loc.gov/mods/v3",
            }
            root = ET.fromstring(response_text)
            for record in root.findall(".//srw:record", ns):
                mods = record.find(".//mods:mods", ns)
                if mods is None:
                    continue

                title_el = mods.find(".//mods:titleInfo/mods:title", ns)
                subtitle_el = mods.find(".//mods:titleInfo/mods:subTitle", ns)
                item_title = title_el.text.strip() if title_el is not None and title_el.text else "N/A"
                if subtitle_el is not None and subtitle_el.text:
                    item_title = f"{item_title} {subtitle_el.text.strip()}"

                creator_el = mods.find(".//mods:name/mods:displayForm", ns)
                if creator_el is None:
                    creator_el = mods.find(".//mods:name/mods:namePart", ns)
                item_creator = creator_el.text.strip() if creator_el is not None and creator_el.text else "N/A"

                record_id = None
                for rec_id in mods.findall(".//mods:recordInfo/mods:recordIdentifier", ns):
                    if rec_id is None or not rec_id.text:
                        continue
                    if rec_id.get("source") and "ppn" in (rec_id.get("source") or "").lower():
                        record_id = rec_id.text.strip()
                        break
                    if not record_id:
                        record_id = rec_id.text.strip()

                if not record_id:
                    continue

                if not record_id.upper().startswith("PPN"):
                    record_id = f"PPN{record_id}"

                raw = {
                    "title": item_title,
                    "creator": item_creator,
                    "id": record_id,
                    "item_url": ITEM_URL.format(ppn=record_id),
                    "mets_url": METS_URL.format(ppn=record_id),
                }
                results.append(convert_to_searchresult("SBB Digital Collections", raw))
                if len(results) >= max_results:
                    break

            if results:
                return results
        except ET.ParseError as e:
            logger.error("StaBiKat SRU XML parse error: %s", e)
        except Exception:
            logger.exception("Unexpected error during StaBiKat SRU parsing")

    return results

def _collect_mets_urls(mets_xml: str) -> tuple[list[str], list[str]]:
    ns = {
        "mets": "http://www.loc.gov/METS/",
        "xlink": "http://www.w3.org/1999/xlink",
    }
    root = ET.fromstring(mets_xml)
    pdf_urls: list[str] = []
    image_urls: list[str] = []

    for file_el in root.findall(".//mets:file", ns):
        mimetype = (file_el.get("MIMETYPE") or "").lower()
        flocat = file_el.find("mets:FLocat", ns)
        if flocat is None:
            continue
        href = flocat.get("{http://www.w3.org/1999/xlink}href")
        if not href:
            continue
        href_lower = href.lower()
        if "pdf" in mimetype or href_lower.endswith(".pdf"):
            pdf_urls.append(href)
            continue
        if "image" in mimetype or href_lower.endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".jp2")):
            image_urls.append(href)

    return pdf_urls, image_urls

def download_sbb_digital_work(item_data: SearchResult | dict, output_folder: str) -> bool:
    """Download PDF/images using METS resolver URLs."""
    ppn = resolve_item_id(item_data)
    mets_url = resolve_item_field(item_data, "mets_url")

    if not ppn:
        logger.warning("No PPN found for SBB digital item.")
        return False
    if not str(ppn).upper().startswith("PPN"):
        ppn = f"PPN{ppn}"

    if not mets_url:
        mets_url = METS_URL.format(ppn=ppn)

    logger.info("Fetching SBB METS: %s", mets_url)
    mets_xml = make_request(mets_url, headers={"Accept": "application/xml"})
    if not isinstance(mets_xml, str):
        logger.warning("Failed to fetch METS for %s", ppn)
        return False

    try:
        save_json({"mets_xml": mets_xml}, output_folder, f"sbb_{ppn}_mets")
    except Exception:
        pass

    try:
        pdf_urls, image_urls = _collect_mets_urls(mets_xml)
    except Exception:
        logger.exception("Failed to parse METS for %s", ppn)
        return False

    ok_any = False
    for url in pdf_urls:
        if download_file(url, output_folder, f"sbb_{ppn}_content"):
            ok_any = True
            if prefer_pdf_over_images():
                return True

    max_pages = get_max_pages("sbb_digital")
    to_download = image_urls[:max_pages] if max_pages and max_pages > 0 else image_urls
    for idx, url in enumerate(to_download, start=1):
        if budget_exhausted():
            logger.warning(
                "Download budget exhausted; stopping SBB downloads at %d/%d pages for %s",
                idx - 1,
                len(to_download),
                ppn,
            )
            break
        if download_file(url, output_folder, f"sbb_{ppn}_p{idx:05d}"):
            ok_any = True

    return ok_any
