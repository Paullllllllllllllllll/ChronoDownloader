"""ChronoDownloader API package.

This package provides the core infrastructure for searching and downloading
digitized historical sources from multiple digital library providers.

Key modules:
- core: Modular core utilities (config, network, context, naming, budget)
- utils: Backward-compatible façade re-exporting core functionality
- providers: Central registry of provider connectors
- model: SearchResult dataclass for unified provider responses
- matching: Fuzzy matching and scoring for candidate selection
- iiif: IIIF manifest parsing and image download helpers
- query_helpers: Query string escaping for SRU/SPARQL

Provider connectors:
- bnf_gallica_api: BnF Gallica (France)
- internet_archive_api: Internet Archive (US)
- loc_api: Library of Congress (US)
- europeana_api: Europeana (EU aggregator)
- dpla_api: Digital Public Library of America
- ddb_api: Deutsche Digitale Bibliothek (Germany)
- british_library_api: British Library (UK)
- mdz_api: Münchener DigitalisierungsZentrum (Germany)
- polona_api: Polona (Poland)
- bne_api: Biblioteca Nacional de España (Spain)
- google_books_api: Google Books
- hathitrust_api: HathiTrust Digital Library
- wellcome_api: Wellcome Collection (UK)

Usage:
    from api import utils
    from api.providers import PROVIDERS
    from api.model import SearchResult
"""

# Re-export commonly used symbols for convenience
from . import utils
from .model import SearchResult, convert_to_searchresult
from .providers import PROVIDERS

__all__ = [
    "utils",
    "SearchResult",
    "convert_to_searchresult",
    "PROVIDERS",
]
