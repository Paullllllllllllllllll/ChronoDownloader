# bne fixtures

On 19.10.2026 `datos.bne.es/sparql` and `bnedigital.bne.es/bd/es/pdf` both
answered HTTP 403, so nothing could be recorded live.

- `sparql_search.json`, `sparql_see_also.json`: trimmed live responses of
  `datos.bne.es/sparql` recorded on 01.08.2026 by the v1.30.3 tests
  (`tests/unit/test_bne.py`).
- `pdf_range.pdf`: synthetic, a one-page PDF standing in for a BNE Digital page
  range.
