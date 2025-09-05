"""Connector for the Biblioteca Nacional de España (BNE) APIs."""

import logging
from typing import List, Union

from .utils import save_json, make_request
from .model import SearchResult, convert_to_searchresult
from .query_helpers import escape_sparql_string

logger = logging.getLogger(__name__)


SEARCH_API_URL = "https://datos.bne.es/sparql"
IIIF_MANIFEST_URL = "https://iiif.bne.es/{item_id}/manifest"


def search_bne(title, creator=None, max_results=3) -> List[SearchResult]:
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


def download_bne_work(item_data: Union[SearchResult, dict], output_folder):
    """Download the IIIF manifest for a BNE item."""

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

    manifest_url = IIIF_MANIFEST_URL.format(item_id=item_identifier)
    manifest = make_request(manifest_url)

    if manifest:
        save_json(manifest, output_folder, f"bne_{item_identifier}_manifest")
        return True

    return False
