import logging
import urllib.parse
from typing import List, Union

from .utils import (
    save_json,
    download_file,
    make_request,
    download_iiif_renderings,
    prefer_pdf_over_images,
    get_provider_setting,
    budget_exhausted,
)
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

SEARCH_API_URL = "https://archive.org/advancedsearch.php"
METADATA_API_URL = "https://archive.org/metadata/{identifier}"


def _ia_max_pages() -> int | None:
    """Read max pages from config provider_settings.internet_archive.max_pages (0/None = all).

    Optional env override: IA_MAX_PAGES
    """
    val = get_provider_setting("internet_archive", "max_pages", None)
    if isinstance(val, int):
        return val
    try:
        import os

        v = int(os.getenv("IA_MAX_PAGES", "0"))
        return v
    except Exception:
        return 0


def search_internet_archive(title, creator=None, max_results=3) -> List[SearchResult]:
    """Search Internet Archive using the Advanced Search API and return SearchResult list."""
    query_parts = [f'title:("{title}")']
    if creator:
        query_parts.append(f'creator:("{creator}")')
    query_parts.append('mediatype:(texts)')
    query = " AND ".join(query_parts)
    params = {
        "q": query,
        "fl[]": "identifier,title,creator,mediatype,year",
        "rows": str(max_results),
        "page": "1",
        "output": "json",
    }
    logger.info("Searching Internet Archive for: %s", title)
    data = make_request(SEARCH_API_URL, params=params)
    results: List[SearchResult] = []
    if data and data.get("response") and data["response"].get("docs"):
        for item in data["response"]["docs"]:
            # Build a normalized SearchResult, keep raw for downloads
            raw = {
                "title": item.get("title", "N/A"),
                "creator": ", ".join(item.get("creator", ["N/A"])),
                "identifier": item.get("identifier"),
                "year": item.get("year", "N/A"),
            }
            sr = convert_to_searchresult("Internet Archive", raw)
            results.append(sr)
    return results


def download_ia_work(item_data: Union[SearchResult, dict], output_folder) -> bool:
    """Download available objects for an Internet Archive item.

    Order of preference:
      1) Manifest-level renderings (PDF/EPUB) when present in IIIF manifest
      2) Direct files listed in metadata (PDF first, then EPUB, then DjVu)
      3) Page images via IIIF Image API (subject to max_pages)
      4) Cover/thumbnail images from metadata
    """
    identifier = None
    if isinstance(item_data, SearchResult):
        identifier = item_data.source_id or item_data.raw.get("identifier")
    else:
        identifier = item_data.get("identifier") or item_data.get("id")
    if not identifier:
        logger.warning("No identifier found in item data.")
        return False
    metadata_url = METADATA_API_URL.format(identifier=identifier)
    logger.info("Fetching Internet Archive metadata: %s", metadata_url)
    metadata = make_request(metadata_url)
    if metadata:
        save_json(metadata, output_folder, f"ia_{identifier}_metadata")

        any_object_downloaded = False
        primary_obtained = False

        # Resolve IIIF manifest URL candidates
        iiif_manifest_url = None
        if metadata.get("misc") and metadata["misc"].get("ia_iiif_url"):
            iiif_manifest_url = metadata["misc"]["ia_iiif_url"]
        if not iiif_manifest_url:
            # Try common IIIF endpoints in order
            candidates = [
                f"https://iiif.archivelab.org/iiif/{identifier}/manifest.json",
                f"https://iiif.archive.org/iiif/{identifier}/manifest.json",
                f"http://iiif.archivelab.org/iiif/{identifier}/manifest.json",
            ]
        else:
            candidates = [iiif_manifest_url]

        iiif_manifest_data = None
        for url in candidates:
            logger.info("Attempting to fetch IA IIIF manifest: %s", url)
            iiif_manifest_data = make_request(url)
            if iiif_manifest_data:
                iiif_manifest_url = url
                break
        if iiif_manifest_data:
            save_json(iiif_manifest_data, output_folder, f"ia_{identifier}_iiif_manifest")
            # Try to download manifest-level renderings (PDF/EPUB) if present
            try:
                renders = download_iiif_renderings(iiif_manifest_data, output_folder, filename_prefix=f"ia_{identifier}_")
                if renders > 0:
                    any_object_downloaded = True
                    primary_obtained = True
                    if prefer_pdf_over_images():
                        logger.info(
                            "Internet Archive: downloaded %d rendering(s); skipping image downloads per config.",
                            renders,
                        )
                        return True
            except Exception:
                logger.exception("IA: error while downloading manifest renderings for %s", identifier)

        # Attempt direct file downloads from metadata (PDF > EPUB > DjVu)
        try:
            files = metadata.get("files")
            preferred_exts = [".pdf", ".epub", ".djvu"]
            # List handling
            def _download_from_list(fl: list) -> tuple[bool, bool]:
                ok = False
                got_primary = False
                # Try preferred formats first
                for ext in preferred_exts:
                    for f in fl:
                        name = (f.get("name") or f.get("file") or "").strip()
                        fmt = str(f.get("format") or "").lower()
                        if not name:
                            continue
                        if name.lower().endswith(ext) or ext.lstrip(".") in fmt:
                            file_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(name)}"
                            if download_file(file_url, output_folder, f"ia_{identifier}_content"):
                                ok = True
                                got_primary = True
                                # Do not download multiple of the same primary type
                                break
                    if ok and prefer_pdf_over_images():
                        # If we got a primary object and prefer that, we can return early
                        return True, got_primary
                return ok, got_primary

            if isinstance(files, list):
                ok, got_primary = _download_from_list(files)
                if ok:
                    any_object_downloaded = True
                if got_primary:
                    primary_obtained = True
                # Thumbnails and covers
                thumb_got = False
                for f in files:
                    name = f.get("name") or f.get("file")
                    fmt = f.get("format")
                    if fmt == "Thumbnail" and name:
                        thumb_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(name)}"
                        if download_file(thumb_url, output_folder, f"ia_{identifier}_thumbnail.jpg"):
                            any_object_downloaded = True
                            thumb_got = True
                            break
                if not thumb_got:
                    for f in files:
                        name = f.get("name") or f.get("file")
                        if name and (name.endswith("_thumb.jpg") or name.endswith("_thumb.png")):
                            thumb_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(name)}"
                            if download_file(thumb_url, output_folder, f"ia_{identifier}_thumbnail.jpg"):
                                any_object_downloaded = True
                                break
            elif isinstance(files, dict):
                # Backward compatibility if dict mapping name -> info
                for fname, finfo in files.items():
                    if isinstance(finfo, dict) and finfo.get("format") == "Thumbnail":
                        thumb_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(fname)}"
                        if download_file(thumb_url, output_folder, f"ia_{identifier}_thumbnail.jpg"):
                            any_object_downloaded = True
                        break
        except Exception:
            logger.exception("IA: error while processing file list for %s", identifier)

        # If allowed, download page images via IIIF Image API after trying to get a primary object
        # Skip if we successfully obtained a primary object and config prefers PDFs/EPUBs over images
        if not (primary_obtained and prefer_pdf_over_images()) and 'iiif_manifest_data' in locals() and iiif_manifest_data:
            try:
                bases = extract_image_service_bases(iiif_manifest_data)
                if bases:
                    max_pages = _ia_max_pages()
                    to_dl = bases[:max_pages] if max_pages and max_pages > 0 else bases
                    logger.info(
                        "Internet Archive: downloading %d/%d page images for %s",
                        len(to_dl), len(bases), identifier,
                    )
                    for idx, svc in enumerate(to_dl, start=1):
                        if budget_exhausted():
                            logger.warning(
                                "Download budget exhausted; stopping IA downloads at %d/%d pages for %s",
                                idx - 1,
                                len(to_dl),
                                identifier,
                            )
                            break
                        fname = f"ia_{identifier}_p{idx:05d}.jpg"
                        if download_one_from_service(svc, output_folder, fname):
                            any_object_downloaded = True
            except Exception:
                logger.exception("IA: error while downloading IIIF images for %s", identifier)

        # Cover image present in metadata.misc.image (useful preview)
        try:
            if metadata.get("misc") and metadata["misc"].get("image"):
                cover_image_url = metadata["misc"]["image"]
                if not cover_image_url.startswith("http"):
                    cover_image_url = f"https://archive.org{cover_image_url}"
                if download_file(cover_image_url, output_folder, f"ia_{identifier}_cover.jpg"):
                    any_object_downloaded = True
        except Exception:
            logger.exception("IA: error while downloading cover for %s", identifier)

        return any_object_downloaded
    return False
