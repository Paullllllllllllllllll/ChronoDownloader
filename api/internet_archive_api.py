import logging
import urllib.parse
from typing import List, Union

from .utils import save_json, download_file, make_request
from .model import SearchResult, convert_to_searchresult

logger = logging.getLogger(__name__)

SEARCH_API_URL = "https://archive.org/advancedsearch.php"
METADATA_API_URL = "https://archive.org/metadata/{identifier}"


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


def download_ia_work(item_data: Union[SearchResult, dict], output_folder):
    """Download metadata and IIIF manifest for an Internet Archive item."""
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
        # Cover image present in metadata.misc.image
        if metadata.get("misc") and metadata["misc"].get("image"):
            cover_image_url = metadata["misc"]["image"]
            if not cover_image_url.startswith("http"):
                cover_image_url = f"https://archive.org{cover_image_url}"
            download_file(cover_image_url, output_folder, f"ia_{identifier}_cover.jpg")
        elif metadata.get("files"):
            files = metadata.get("files")
            # IA often returns a list of file dicts with 'name' and 'format'
            if isinstance(files, list):
                for f in files:
                    name = f.get("name") or f.get("file")
                    fmt = f.get("format")
                    # Prefer explicit Thumbnail format
                    if fmt == "Thumbnail" and name:
                        thumb_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(name)}"
                        download_file(thumb_url, output_folder, f"ia_{identifier}_thumbnail.jpg")
                        break
                else:
                    # Fallback: look for typical thumb filenames
                    for f in files:
                        name = f.get("name") or f.get("file")
                        if name and (name.endswith("_thumb.jpg") or name.endswith("_thumb.png")):
                            thumb_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(name)}"
                            download_file(thumb_url, output_folder, f"ia_{identifier}_thumbnail.jpg")
                            break
            elif isinstance(files, dict):
                # Backward compatibility if dict mapping name -> info
                for fname, finfo in files.items():
                    if isinstance(finfo, dict) and finfo.get("format") == "Thumbnail":
                        thumb_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(fname)}"
                        download_file(thumb_url, output_folder, f"ia_{identifier}_thumbnail.jpg")
                        break
        return True
    return False
