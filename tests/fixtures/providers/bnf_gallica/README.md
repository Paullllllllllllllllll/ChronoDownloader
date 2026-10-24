# Gallica fixtures

Gallica answered every request with HTTP 403 on 19.10.2026, so no real search
or manifest response could be recorded.

- `sru_cuisinier_royal.xml`: synthetic; SRU oai_dc answer reconstructed from the
  v1.30.3 test mocks (`TestGallicaSearchParsing`).
- `manifest_bpt6k12345q.json`: synthetic; IIIF Presentation v2 manifest in
  Gallica's shape (Image API service per canvas).
- `sru_403_challenge.html`: live, 19.10.2026; the body of the SRU's HTTP 403
  (a Cloudflare managed challenge), trimmed, with tokens replaced by `REDACTED`.
