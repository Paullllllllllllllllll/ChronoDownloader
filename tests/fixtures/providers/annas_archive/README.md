# Anna's Archive fixtures

The configured mirror `annas-archive.li` is parked (live baseline, 19.10.2026), so
the search, MD5-page and fast-download responses are reconstructed.

- `search_table.html`: synthetic; the table layout of the v1.30.3 parsing mocks
  (`tests/unit/test_provider_parsing.py`), with a duplicate, a linkless and a
  query-carrying row added.
- `md5_page.html`: synthetic; the links of the v1.30.3 link-filter mock, plus a
  member link, a mirror, a direct EPUB and a script URL.
- `fast_download.json`, `fast_download_no_offer.json`, `fast_download_quota.json`:
  synthetic; the documented `/dyn/api/fast_download.json` answer shape (no
  v1.30.3 test mocked it); no key anywhere.
- `login_page.html`: synthetic; carries the v1.30.3 login marker
  `<title>log in / register`.
- `search_parked.html`: trimmed from a live response of
  `annas-archive.li/search` on 19.10.2026; the parking script tag is kept, the
  tracking script with client parameters is removed.
