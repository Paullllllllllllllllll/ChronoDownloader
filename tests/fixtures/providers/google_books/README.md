# Google Books fixtures

Signatures (`sig=`) and image tokens (`imgtk=`) are replaced with `REDACTED`.

- `volume_zGlhAAAAcAAJ.json`: baseline, the volume response saved by the
  v1.30.3 live baseline (`--id zGlhAAAAcAAJ`) on 19.10.2026.
- `search.json`: synthetic, a search envelope around the baseline volume (the
  baseline kept no raw search response).
- `search_empty.json`: live, the strict `free-ebooks` search for work 6206 on
  19.10.2026 (no hits).
- `search_503.json`: live, the 503 body the unfiltered search answered on
  19.10.2026.
