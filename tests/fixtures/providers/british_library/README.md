# british_library fixtures

All three are synthetic, reconstructed from the v1.30.3 test mocks: on 19.10.2026
`sru.bl.uk` and `api.bl.uk` did not resolve (NXDOMAIN) and `bnb.data.bl.uk/sparql`
timed out.

- `sru_search.xml`: SRU 1.2 response with an `srw_dc` record, an `oai_dc` record
  whose ARK sits in a manifest URL, and a record without an ARK.
- `bnb_sparql.json`: BNB SPARQL result with an ARK in `?ident`, one in `?same` on a
  binding without a title, and a binding without an ARK.
- `manifest.json`: IIIF Presentation 2 manifest with two service-backed canvases.
