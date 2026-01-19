import logging
import urllib.parse
from typing import List, Union

from .utils import (
    save_json,
    download_file,
    make_request,
    get_max_pages,
    download_iiif_renderings,
    prefer_pdf_over_images,
)
from .iiif import extract_image_service_bases, download_one_from_service
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

LOC_API_BASE_URL = "https://www.loc.gov/"


def search_loc(title: str, creator: str | None = None, max_results: int = 3) -> List[SearchResult]:
    """Search Library of Congress for works.
    
    Args:
        title: Work title to search for
        creator: Optional creator/author name
        max_results: Maximum number of results to return
        
    Returns:
        List of SearchResult objects
    """
    query_parts = [title]
    if creator:
        query_parts.append(creator)
    search_query = " ".join(query_parts)
    params = {
        "q": search_query,
        "fo": "json",
        "c": str(max_results),
    }
    headers = {"Accept": "application/json"}
    logger.info("Searching Library of Congress for: %s", title)
    # Prefer the Books collection endpoint first; fall back to the generic search if needed
    books_url = urllib.parse.urljoin(LOC_API_BASE_URL, "books/")
    data = make_request(books_url, params=params, headers=headers)
    if not isinstance(data, dict) or not (data.get("results") or (data.get("content") and data["content"].get("results"))):
        search_url = urllib.parse.urljoin(LOC_API_BASE_URL, "search/")
        data = make_request(search_url, params=params, headers=headers)
    
    if not isinstance(data, dict):
        return []
    
    results: List[SearchResult] = []

    def _extract_iiif_manifest(item: dict) -> str | None:
        iiif_manifest = item.get("iiif_manifest_url")
        if iiif_manifest:
            return iiif_manifest
        resources = item.get("resources") or []
        if isinstance(resources, list):
            for res in resources:
                if isinstance(res, dict) and res.get("iiif_manifest"):
                    return res.get("iiif_manifest")
        elif isinstance(resources, dict):
            return resources.get("iiif_manifest") or iiif_manifest
        return None

    def _item_to_search_result(item: dict) -> SearchResult:
        item_id = item.get("id")
        if item_id:
            item_id = item_id.strip("/").split("/")[-1]
        raw = {
            "title": item.get("title", "N/A"),
            "creator": item.get("contributor_names", ["N/A"])[0] if item.get("contributor_names") else "N/A",
            "id": item_id,
            "item_url": item.get("url"),
            "iiif_manifest": _extract_iiif_manifest(item),
        }
        return convert_to_searchresult("Library of Congress", raw)

    items = None
    if data.get("results"):
        items = data.get("results")
    elif data.get("content") and data["content"].get("results"):
        items = data["content"].get("results")

    if items:
        for item in items:
            results.append(_item_to_search_result(item))
    return results

def download_loc_work(item_data: Union[SearchResult, dict], output_folder: str) -> bool:
    """Download a Library of Congress work.
    
    Args:
        item_data: SearchResult or dict containing item data
        output_folder: Folder to download files to
        
    Returns:
        True if download was successful, False otherwise
    """
    if isinstance(item_data, SearchResult):
        item_url = item_data.item_url or item_data.raw.get("url")
        item_id = item_data.source_id or item_data.raw.get("id") or item_data.title or "unknown_item"
        iiif_manifest_hint = item_data.iiif_manifest or item_data.raw.get("iiif_manifest")
    else:
        item_url = item_data.get("item_url")
        item_id = item_data.get("id", item_data.get("title", "unknown_item"))
        iiif_manifest_hint = item_data.get("iiif_manifest")
    if not item_url:
        logger.warning("No item URL found for LOC item: %s", item_id)
        return False
    item_json_url = f"{item_url}?fo=json" if not item_url.endswith("?fo=json") else item_url
    logger.info("Fetching LOC item JSON: %s", item_json_url)
    headers = {"Accept": "application/json"}
    item_full_json = make_request(item_json_url, headers=headers)
    if not isinstance(item_full_json, dict):
        logger.error("Failed to fetch item JSON from %s", item_json_url)
        return False
    
    save_json(item_full_json, output_folder, f"loc_{item_id}_item_details")
    iiif_manifest_url = iiif_manifest_hint
    if not iiif_manifest_url and item_full_json.get("item") and item_full_json["item"].get("resources"):
        for res in item_full_json["item"]["resources"]:
            if res.get("iiif_manifest"):
                iiif_manifest_url = res.get("iiif_manifest")
                break
    if not iiif_manifest_url and item_full_json.get("resources"):
        for res in item_full_json["resources"]:
            if isinstance(res, dict) and res.get("iiif_manifest"):
                iiif_manifest_url = res.get("iiif_manifest")
                break
    if iiif_manifest_url:
        logger.info("Fetching LOC IIIF manifest: %s", iiif_manifest_url)
        iiif_manifest_data = make_request(iiif_manifest_url)
        if isinstance(iiif_manifest_data, dict):
            save_json(iiif_manifest_data, output_folder, f"loc_{item_id}_iiif_manifest")

            # Prefer manifest-level renderings (PDF/EPUB) when available
            try:
                renders = download_iiif_renderings(iiif_manifest_data, output_folder, filename_prefix=f"loc_{item_id}_")
                if renders > 0 and prefer_pdf_over_images():
                    logger.info("LOC: downloaded %d rendering(s); skipping image downloads per config.", renders)
                    return True
            except Exception:
                logger.exception("LOC: error while downloading manifest renderings for %s", item_id)

            # Extract IIIF Image service bases (v2/v3)
            service_bases = extract_image_service_bases(iiif_manifest_data)

            if service_bases:
                max_pages = get_max_pages("loc")
                total = len(service_bases)
                to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
                logger.info("LOC: downloading %d/%d page images for %s", len(to_download), total, item_id)
                ok_any = False
                for idx, svc in enumerate(to_download, start=1):
                    try:
                        fname = f"loc_{item_id}_p{idx:05d}.jpg"
                        if download_one_from_service(svc, output_folder, fname):
                            ok_any = True
                        else:
                            logger.warning("Failed to download LOC image from %s", svc)
                    except Exception:
                        logger.exception("Error downloading LOC image for %s from %s", item_id, svc)
                return ok_any
            else:
                logger.info("No IIIF image services in manifest for LOC item %s; falling back to single image if available.", item_id)
        else:
            logger.warning("Failed to fetch IIIF manifest from %s", iiif_manifest_url)
    else:
        logger.info("No IIIF manifest URL found for LOC item: %s", item_id)
    
    # Fallback: try downloading a single representative image
    image_url = None
    if item_full_json.get("item") and item_full_json["item"].get("image_url"):
        if isinstance(item_full_json["item"]["image_url"], dict):
            image_url = item_full_json["item"]["image_url"].get("medium") or item_full_json["item"]["image_url"].get("full")
        elif isinstance(item_full_json["item"]["image_url"], str):
            image_url = item_full_json["item"]["image_url"]
    if image_url:
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        elif not image_url.startswith("http"):
            image_url = "https://www.loc.gov" + image_url
        download_file(image_url, output_folder, f"loc_{item_id}_sample_image.jpg")
        return True
    return False
