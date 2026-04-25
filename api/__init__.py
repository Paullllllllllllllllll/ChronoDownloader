"""ChronoDownloader API package.

Public structure:

- :mod:`api.core` -- foundational primitives (config, network, context,
  naming, budget, download).
- :mod:`api.providers` -- provider connectors and the central PROVIDERS
  registry.
- :mod:`api.iiif` -- IIIF manifest parsing, download strategies, direct
  manifest flow.
- :mod:`api.identifier_resolver` -- provider + identifier -> manifest URL.
- :mod:`api.matching` -- fuzzy matching and scoring.
- :mod:`api.model` -- SearchResult and related data classes.
- :mod:`api.query_helpers` -- SRU / SPARQL escaping.
"""
from __future__ import annotations

from .model import QuotaDeferredException, SearchResult, convert_to_searchresult
from .providers import PROVIDERS

__all__ = [
    "PROVIDERS",
    "SearchResult",
    "QuotaDeferredException",
    "convert_to_searchresult",
]
