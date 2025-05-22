"""Connector for the Biblioteca Nacional de España (BNE) APIs."""

from .utils import save_json, make_request


SEARCH_API_URL = "https://datos.bne.es/sparql"
IIIF_MANIFEST_URL = "https://iiif.bne.es/{item_id}/manifest"


def search_bne(title, creator=None, max_results=3):
    """Search the BNE SPARQL endpoint for works by title and creator."""

    query = f"""
        SELECT ?id ?title ?creator WHERE {{
            ?id <http://www.w3.org/2000/01/rdf-schema#label> ?title .
            FILTER(CONTAINS(LCASE(?title), LCASE('{title}')))
            OPTIONAL {{ ?id <http://dbpedia.org/ontology/author> ?creator . }}
        }} LIMIT {max_results}
    """

    params = {
        "query": query,
        "format": "json",
    }

    print(f"Searching BNE for: {title}")
    data = make_request(SEARCH_API_URL, params=params)

    results = []
    if data and data.get("results") and data["results"].get("bindings"):
        for b in data["results"]["bindings"]:
            item_id = b.get("id", {}).get("value")
            item_title = b.get("title", {}).get("value")
            item_creator = b.get("creator", {}).get("value")
            if item_id:
                results.append(
                    {
                        "title": item_title or "N/A",
                        "creator": item_creator or "N/A",
                        "id": item_id,
                        "source": "BNE",
                    }
                )

    return results


def download_bne_work(item_data, output_folder):
    """Download the IIIF manifest for a BNE item."""

    item_id = item_data.get("id")
    if not item_id:
        print("No BNE item id provided.")
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
