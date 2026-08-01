"""Connector for the Biblioteca Nacional de España (BNE).

Two BNE services are involved and they use *different* identifier
namespaces, which is the whole difficulty of this connector:

* ``datos.bne.es`` -- the linked-data catalogue, queried over SPARQL. Its
  resource identifiers cover authority records (``XX...``) just as much as
  bibliographic ones (``a...``, ``bimo...``), so an unconstrained query
  returns persons and subjects alongside editions.
* ``bnedigital.bne.es`` -- BNE Digital, the digitisation platform that
  superseded the Biblioteca Digital Hispánica in 2025. Its objects are
  addressed by UUID and delivered as PDF page ranges. The platform
  publishes no IIIF Presentation manifests, and the host ``iiif.bne.es``
  used by earlier revisions of this module no longer resolves at all.

The bridge between the two namespaces is ``rdfs:seeAlso`` on the catalogue
record, which points at the BNE Digital card URL carrying the UUID. The
search query below therefore constrains results to bibliographic
manifestations (``bne:C1003``) that actually carry such a link, and the
download path works exclusively in the BNE Digital namespace.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..core.budget import budget_exhausted
from ..core.config import get_max_pages
from ..core.download import download_file, save_json
from ..core.network import make_request
from ..model import (
    SearchResult,
    convert_to_searchresult,
    resolve_item_field,
    resolve_item_id,
)
from ..query_helpers import escape_sparql_string

logger = logging.getLogger(__name__)

SEARCH_API_URL = "https://datos.bne.es/sparql"
DIGITAL_BASE_URL = "https://bnedigital.bne.es"
DIGITAL_CARD_URL = DIGITAL_BASE_URL + "/bd/card?id={item_id}"
DIGITAL_PDF_URL = DIGITAL_BASE_URL + "/bd/es/pdf"

# datos.bne.es ontology terms used below.
MANIFESTATION_CLASS = "https://datos.bne.es/def/C1003"  # Manifestación (edition)
CREATOR_PROPERTY = "https://datos.bne.es/def/P1011"  # author, as a literal name
DATE_PROPERTY = "https://datos.bne.es/def/P3006"  # publication date
LABEL_PROPERTY = "http://www.w3.org/2000/01/rdf-schema#label"
SEE_ALSO_PROPERTY = "http://www.w3.org/2000/01/rdf-schema#seeAlso"

# BNE Digital rejects PDF requests spanning more than 25 pages (the viewer's
# own ``downloadPageLimit``) with HTTP 500, so whole works are fetched in
# chunks. MAX_PDF_CHUNKS is a safety stop, not an expected limit.
PDF_PAGE_CHUNK = 25
MAX_PDF_CHUNKS = 400

# Spanish orthography uses exactly these seven non-ASCII letters. The endpoint
# offers no accent-insensitive comparison, so an unaccented user query would
# never match an accented title ("novisimo" vs "novísimo"). Both sides of the
# CONTAINS filter are therefore folded onto ASCII through the same table: the
# query string in Python, the title literal by a REPLACE chain in SPARQL.
_ACCENT_FOLDING: tuple[tuple[str, str], ...] = (
    ("á", "a"),
    ("é", "e"),
    ("í", "i"),
    ("ó", "o"),
    ("ú", "u"),
    ("ü", "u"),
    ("ñ", "n"),
)

# The date property is free text, not a typed date: mostly a bare year
# ("1845"), but also "[1770]", "1886-1945", "[entre 1645 y 1668?]",
# "Anno 1762", century notation ("S.XVIII") and Roman numerals only
# ("A. MDCCLXII"). Nothing can be parsed into a number, so the constraint is a
# membership test: keep a record whose date literal mentions a year in
# 1000-1900. It fails open twice on purpose -- when the record carries no date
# at all (about 0.9% of digitised manifestations) and when the literal holds no
# four-digit year (century notation and Roman numerals, i.e. the oldest
# material) -- so no undated historical record is dropped. Both disjuncts are
# load-bearing, not defensive padding.
#
# The alternative, extracting the first four-digit run with a REPLACE capture
# group and comparing it, parses on the endpoint but is rejected by the
# Cloudflare WAF in front of it with an instant 403.
_HISTORICAL_YEAR_PATTERN = "(1[0-8][0-9][0-9]|1900)"

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# datos.bne.es resource ids: a letter prefix (a, bimo, XX, Mi ...) plus digits,
# optionally preceded by a parenthesised MARC organization code, as in
# "(CaPaEBR)a6232137". The pattern stays deliberately narrow because the id is
# interpolated unescaped into the seeAlso SPARQL query: no quotes, angle
# brackets, backslashes, or whitespace can pass.
_CATALOG_ID_RE = re.compile(r"^(?:\([A-Za-z]{1,10}\))?[A-Za-z]{0,4}\d[A-Za-z0-9]*$")


def _bindings(data: Any) -> list[dict[str, Any]]:
    """Extract the binding list from a SPARQL JSON result document."""

    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, dict):
        return []
    bindings = results.get("bindings")
    if not isinstance(bindings, list):
        return []
    return [b for b in bindings if isinstance(b, dict)]


def _binding_value(binding: dict[str, Any], name: str) -> str | None:
    """Return the string value of one SPARQL binding, if present."""

    cell = binding.get(name)
    if not isinstance(cell, dict):
        return None
    value = cell.get("value")
    return str(value) if value else None


def extract_digital_id(value: str | None) -> str | None:
    """Return the BNE Digital object UUID contained in *value*, if any.

    Accepts a bare UUID as well as any BNE Digital URL that carries one
    (``.../bd/card?id=<uuid>``, ``.../bd/es/viewer?id=<uuid>&page=1``).

    Args:
        value: Candidate identifier or URL.

    Returns:
        The lowercase UUID, or ``None`` when *value* holds none.
    """
    if not value:
        return None
    match = _UUID_RE.search(str(value))
    return match.group(0).lower() if match else None


def _catalog_resource_id(value: str) -> str | None:
    """Normalise a datos.bne.es resource URI or bare id to a bare id."""

    candidate = str(value).strip()
    if candidate.startswith("http"):
        if "datos.bne.es" not in candidate:
            return None
        candidate = candidate.rstrip("/").rsplit("/", 1)[-1]
    return candidate if _CATALOG_ID_RE.match(candidate) else None


def _lookup_digital_url(resource_id: str) -> str | None:
    """Follow ``rdfs:seeAlso`` from a catalogue record to its digital copy."""

    query = f"""
        SELECT ?digital WHERE {{
            <https://datos.bne.es/resource/{resource_id}>
                <{SEE_ALSO_PROPERTY}> ?digital .
            FILTER(STRSTARTS(STR(?digital), '{DIGITAL_BASE_URL}/'))
        }} LIMIT 1
    """
    data = make_request(SEARCH_API_URL, params={"query": query, "format": "json"})
    for binding in _bindings(data):
        url = _binding_value(binding, "digital")
        if url:
            return url
    return None


def _resolve_digital_id(value: str) -> str | None:
    """Map any accepted BNE identifier onto a BNE Digital object UUID.

    A UUID (or a BNE Digital URL containing one) is used directly. A
    datos.bne.es identifier belongs to the catalogue namespace and is
    translated by querying its ``rdfs:seeAlso`` link; authority records and
    editions without a digital copy legitimately resolve to ``None``.
    """
    digital_id = extract_digital_id(value)
    if digital_id:
        return digital_id

    resource_id = _catalog_resource_id(value)
    if not resource_id:
        return None

    logger.info("BNE: resolving catalogue record %s to its digital copy", resource_id)
    return extract_digital_id(_lookup_digital_url(resource_id))


def fold_accents(value: str) -> str:
    """Lowercase *value* and map Spanish accented letters onto plain ASCII.

    Args:
        value: Arbitrary text.

    Returns:
        The lowercased, accent-folded form used on the query side of the
        title filter.
    """
    folded = value.lower()
    for accented, plain in _ACCENT_FOLDING:
        folded = folded.replace(accented, plain)
    return folded


def _folded_sparql_expression(expression: str) -> str:
    """Wrap a SPARQL expression in the fold applied to *fold_accents*.

    The accented characters are literal single-character regexes, so the
    chain is a plain transliteration and carries no regex metacharacters.
    """
    folded = f"LCASE({expression})"
    for accented, plain in _ACCENT_FOLDING:
        folded = f"REPLACE({folded}, '{accented}', '{plain}')"
    return folded


def _build_search_query(title: str, limit: int) -> str:
    """Build the SPARQL query for digitised BNE editions matching *title*."""

    t = escape_sparql_string(fold_accents(title))
    folded_title = _folded_sparql_expression("?title")
    return f"""
        SELECT DISTINCT ?id ?title ?digital ?creator ?date WHERE {{
            ?id a <{MANIFESTATION_CLASS}> .
            ?id <{LABEL_PROPERTY}> ?title .
            ?id <{SEE_ALSO_PROPERTY}> ?digital .
            FILTER(CONTAINS({folded_title}, '{t}'))
            FILTER(STRSTARTS(STR(?digital), '{DIGITAL_BASE_URL}/'))
            OPTIONAL {{ ?id <{CREATOR_PROPERTY}> ?creator . }}
            OPTIONAL {{ ?id <{DATE_PROPERTY}> ?date . }}
            FILTER(
                !BOUND(?date)
                || !REGEX(STR(?date), '[0-9]{{4}}')
                || REGEX(STR(?date), '{_HISTORICAL_YEAR_PATTERN}')
            )
        }} LIMIT {limit}
    """


def search_bne(
    title: str, creator: str | None = None, max_results: int = 3
) -> list[SearchResult]:
    """Search the BNE linked-data catalogue for digitised editions.

    Results are restricted to bibliographic manifestations that link to a
    BNE Digital copy; the identifier carried in each result is the BNE
    Digital UUID, not the catalogue identifier, because only the former is
    downloadable. The *creator* argument is not pushed into the query --
    BNE records name authors both as literals and as authority links -- and
    is left to the pipeline's fuzzy matching stage.

    Args:
        title: Title substring to search for.
        creator: Accepted for interface parity; not used in the query.
        max_results: Maximum number of distinct editions to return.

    Returns:
        List of SearchResult objects, possibly empty.
    """
    # Optional clauses can repeat a record across bindings, so over-fetch and
    # de-duplicate rather than losing results to the LIMIT.
    limit = max(max_results, 1) * 4
    params = {
        "query": _build_search_query(title, limit),
        "format": "json",
    }

    logger.info("Searching BNE for: %s", title)
    data = make_request(SEARCH_API_URL, params=params)

    results: list[SearchResult] = []
    if not isinstance(data, dict):
        logger.warning(
            "BNE: no usable SPARQL response for %r. datos.bne.es sits behind a "
            "bot filter that rejects some non-browser HTTP clients.",
            title,
        )
        return results

    seen: set[str] = set()
    for binding in _bindings(data):
        digital_url = _binding_value(binding, "digital")
        digital_id = extract_digital_id(digital_url)
        if not digital_id or digital_id in seen:
            continue
        seen.add(digital_id)

        raw = {
            "title": _binding_value(binding, "title") or "",
            "creator": _binding_value(binding, "creator"),
            "date": _binding_value(binding, "date"),
            "id": digital_id,
            "catalog_id": _binding_value(binding, "id"),
            "item_url": digital_url,
        }
        results.append(convert_to_searchresult("BNE", raw))
        if len(results) >= max_results:
            break

    return results


def _download_pdf_pages(digital_id: str, output_folder: str, max_pages: int) -> int:
    """Download a BNE Digital object as consecutive PDF page ranges.

    Args:
        digital_id: BNE Digital object UUID.
        output_folder: Target directory.
        max_pages: Page ceiling from config; 0 means "the whole work".

    Returns:
        Number of PDF chunks successfully downloaded.
    """
    downloaded = 0
    start = 1

    for _ in range(MAX_PDF_CHUNKS):
        if max_pages > 0 and start > max_pages:
            break
        if budget_exhausted():
            logger.warning(
                "Download budget exhausted; stopping BNE downloads after "
                "%d PDF chunk(s) for %s",
                downloaded,
                digital_id,
            )
            break

        end = start + PDF_PAGE_CHUNK - 1
        if max_pages > 0:
            end = min(end, max_pages)
        page_range = str(start) if start == end else f"{start}-{end}"

        url = f"{DIGITAL_PDF_URL}?id={digital_id}&page={page_range}"
        filename = f"bne_{digital_id}_p{start:05d}_{end:05d}.pdf"
        logger.info("BNE: downloading pages %s of %s", page_range, digital_id)
        if not download_file(url, output_folder, filename):
            # BNE Digital answers with HTTP 500 once a range runs past the
            # last page, so a failed chunk marks the end of the work.
            logger.info(
                "BNE: no PDF for pages %s of %s; treating as end of work",
                page_range,
                digital_id,
            )
            break

        downloaded += 1
        start = end + 1

    return downloaded


def download_bne_work(
    item_data: SearchResult | dict[str, Any], output_folder: str
) -> bool:
    """Download a BNE Digital object as PDF page ranges.

    Args:
        item_data: SearchResult or raw dict identifying the item.
        output_folder: Target directory.

    Returns:
        ``True`` when at least one PDF chunk was written.
    """
    item_id = resolve_item_id(item_data, "digital_id", "id", "identifier")
    if not item_id:
        logger.warning("No BNE item id provided.")
        return False

    digital_id = _resolve_digital_id(item_id)
    if not digital_id:
        logger.warning(
            "BNE: cannot map identifier %r onto a BNE Digital object. Downloads "
            "need the bnedigital.bne.es UUID, or a datos.bne.es edition record "
            "that links to one via rdfs:seeAlso.",
            item_id,
        )
        return False

    save_json(
        {
            "digital_id": digital_id,
            "item_url": DIGITAL_CARD_URL.format(item_id=digital_id),
            "title": resolve_item_field(item_data, "title"),
            "catalog_id": resolve_item_field(item_data, "catalog_id"),
        },
        output_folder,
        f"bne_{digital_id}_metadata",
    )

    max_pages = get_max_pages("bne") or 0
    chunks = _download_pdf_pages(digital_id, output_folder, max_pages)
    if not chunks:
        logger.warning("BNE: no PDF pages downloaded for %s", digital_id)
        return False

    logger.info("BNE: downloaded %d PDF chunk(s) for %s", chunks, digital_id)
    return True
