import logging
import urllib.parse
from typing import List, Union
from .utils import save_json, download_file, make_request, get_provider_setting
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

LOC_API_BASE_URL = "https://www.loc.gov/"


def _loc_max_pages() -> int | None:
    """Read max pages from config provider_settings.loc.max_pages (0/None = all)."""
    val = get_provider_setting("loc", "max_pages", None)
    if isinstance(val, int):
        return val
    return 0

def search_loc(title, creator=None, max_results=3) -> List[SearchResult]:
    query_parts = [title]
    if creator:
        query_parts.append(creator)
    search_query = " ".join(query_parts)
    params = {
        "q": search_query,
        "fo": "json",
        "c": str(max_results),
    }
    search_url = urllib.parse.urljoin(LOC_API_BASE_URL, "search/")
    logger.info("Searching Library of Congress for: %s", title)
    headers = {"Accept": "application/json"}
    data = make_request(search_url, params=params, headers=headers)
    if not data or not (data.get("results") or (data.get("content") and data["content"].get("results"))):
        # Fallback to Books collection endpoint
        books_url = urllib.parse.urljoin(LOC_API_BASE_URL, "books/")
        data = make_request(books_url, params=params, headers=headers)
    results: List[SearchResult] = []
    if data and data.get("results"):
        for item in data["results"]:
            item_id = item.get("id")
            if item_id:
                item_id = item_id.strip('/').split('/')[-1]
            iiif_manifest = item.get("iiif_manifest_url", item.get("resources", [{}])[0].get("iiif_manifest"))
            raw = {
                "title": item.get("title", "N/A"),
                "creator": item.get("contributor_names", ["N/A"])[0] if item.get("contributor_names") else "N/A",
                "id": item_id,
                "item_url": item.get("url"),
                "iiif_manifest": iiif_manifest,
            }
            sr = convert_to_searchresult("Library of Congress", raw)
            results.append(sr)
    elif data and data.get("content") and data["content"].get("results"):
        for item in data["content"]["results"]:
            item_id = item.get("id")
            if item_id:
                item_id = item_id.strip('/').split('/')[-1]
            iiif_manifest = item.get("iiif_manifest_url", item.get("resources", [{}])[0].get("iiif_manifest"))
            raw = {
                "title": item.get("title", "N/A"),
                "creator": item.get("contributor_names", ["N/A"])[0] if item.get("contributor_names") else "N/A",
                "id": item_id,
                "item_url": item.get("url"),
                "iiif_manifest": iiif_manifest,
            }
            results.append(convert_to_searchresult("Library of Congress", raw))
    return results

def download_loc_work(item_data: Union[SearchResult, dict], output_folder):
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
    if item_full_json:
        save_json(item_full_json, output_folder, f"loc_{item_id}_item_details")
        iiif_manifest_url = iiif_manifest_hint
        if not iiif_manifest_url and item_full_json.get("item") and item_full_json["item"].get("resources"):
            for res in item_full_json["item"]["resources"]:
                if res.get("iiif_manifest"):
                    iiif_manifest_url = res["iiif_manifest"]
                    break
        elif not iiif_manifest_url and item_full_json.get("resources"):
            for res in item_full_json["resources"]:
                if isinstance(res, dict) and res.get("iiif_manifest"):
                    iiif_manifest_url = res["iiif_manifest"]
                    break
        if iiif_manifest_url:
            logger.info("Fetching LOC IIIF manifest: %s", iiif_manifest_url)
            iiif_manifest_data = make_request(iiif_manifest_url)
            if iiif_manifest_data:
                save_json(iiif_manifest_data, output_folder, f"loc_{item_id}_iiif_manifest")

                # Extract IIIF Image service bases (v2/v3)
                service_bases: List[str] = []
                # v2
                try:
                    sequences = iiif_manifest_data.get("sequences") or []
                    if sequences:
                        canvases = sequences[0].get("canvases", [])
                        for canvas in canvases:
                            try:
                                images = canvas.get("images", [])
                                if not images:
                                    continue
                                res = images[0].get("resource", {})
                                service = res.get("service", {})
                                svc_id = service.get("@id") or service.get("id")
                                if not svc_id:
                                    img_id = res.get("@id") or res.get("id")
                                    if img_id and "/full/" in img_id:
                                        svc_id = img_id.split("/full/")[0]
                                if svc_id:
                                    service_bases.append(svc_id)
                            except Exception:
                                continue
                except Exception:
                    pass

                # v3
                if not service_bases and iiif_manifest_data.get("items"):
                    try:
                        for canvas in iiif_manifest_data.get("items", []):
                            try:
                                anno_pages = canvas.get("items", [])
                                if not anno_pages:
                                    continue
                                annos = anno_pages[0].get("items", [])
                                if not annos:
                                    continue
                                body = annos[0].get("body", {})
                                if isinstance(body, list) and body:
                                    body = body[0]
                                service = body.get("service") or body.get("services")
                                svc_obj = None
                                if isinstance(service, list) and service:
                                    svc_obj = service[0]
                                elif isinstance(service, dict):
                                    svc_obj = service
                                svc_id = None
                                if svc_obj:
                                    svc_id = svc_obj.get("@id") or svc_obj.get("id")
                                if not svc_id:
                                    body_id = body.get("id")
                                    if body_id and "/full/" in body_id:
                                        svc_id = body_id.split("/full/")[0]
                                if svc_id:
                                    service_bases.append(svc_id)
                            except Exception:
                                continue
                    except Exception:
                        pass

                if service_bases:
                    def _candidates(base: str) -> List[str]:
                        b = base.rstrip('/')
                        return [
                            f"{b}/full/full/0/default.jpg",
                            f"{b}/full/max/0/default.jpg",
                            f"{b}/full/full/0/native.jpg",
                        ]

                    def _download_one(base: str, filename: str) -> bool:
                        for u in _candidates(base):
                            if download_file(u, output_folder, filename):
                                return True
                        return False

                    max_pages = _loc_max_pages()
                    total = len(service_bases)
                    to_download = service_bases[:max_pages] if max_pages and max_pages > 0 else service_bases
                    logger.info("LOC: downloading %d/%d page images for %s", len(to_download), total, item_id)
                    ok_any = False
                    for idx, svc in enumerate(to_download, start=1):
                        try:
                            fname = f"loc_{item_id}_p{idx:05d}.jpg"
                            if _download_one(svc, fname):
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
    else:
        logger.error("Failed to fetch item JSON from %s", item_json_url)
    return False
