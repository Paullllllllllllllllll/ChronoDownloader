# Library of Congress fixtures

All files are synthetic: loc.gov answered HTTP 403 to search and manifest
requests in the 19.10.2026 baseline and to a fresh search the same day.

- `search.json`: synthetic; `books/` answer in the shape of the v1.30.3 test
  mocks, with the baseline LCCNs 65078367 and 75320945.
- `item.json`: synthetic; item JSON (`fo=json`) with `resources` and a list of
  `image_url` entries, as in the v1.30.3 test mocks.
- `manifest.json`: synthetic; IIIF v2 manifest with two Image API canvases.
