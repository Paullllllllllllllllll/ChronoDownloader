from .utils import save_json, make_request

# Biblioteca Nacional de España (BNE) SPARQL endpoint
SPARQL_ENDPOINT = "https://datos.bne.es/sparql"


def search_bne(title, creator=None, max_results=3):
    """Search BNE using a simple SPARQL query."""
    # Basic SPARQL query to match titles containing the search term
    query = (
        "SELECT ?item ?title WHERE {"
        "?item <http://www.w3.org/2000/01/rdf-schema#label> ?title ."
        f"FILTER(CONTAINS(LCASE(?title), LCASE(\"{title}\")))"
        "} LIMIT %d" % max_results
    )

    params = {"query": query, "format": "application/sparql-results+json"}
    print(f"Searching BNE for: {title}")
    data = make_request(SPARQL_ENDPOINT, params=params)

    results = []
    if data and data.get("results", {}).get("bindings"):
        for b in data["results"]["bindings"]:
            item_uri = b.get("item", {}).get("value")
            title_val = b.get("title", {}).get("value")
            results.append({
                "title": title_val,
                "id": item_uri,
                "source": "BNE",
            })
    return results


def download_bne_work(item_data, output_folder):
    """Download a IIIF manifest for a BNE item if available."""
    item_uri = item_data.get("id")
    if not item_uri:
        return False

    # Attempt to derive a IIIF manifest link from the item URI
    manifest_url = None
    if item_uri.startswith("http") and "/" in item_uri:
        identifier = item_uri.split("/")[-1]
        manifest_url = f"https://iiif.bne.es/iiif/{identifier}/manifest"

    if manifest_url:
        manifest_data = make_request(manifest_url)
        if manifest_data:
            save_json(manifest_data, output_folder, f"bne_{identifier}_manifest")
            return True
    return False
