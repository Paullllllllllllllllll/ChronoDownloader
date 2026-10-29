# Changelog

- **v1.1.3** (29 October 2026) -- The decision-model judge builds its
  questions and reads its answers through the OpenRouter dialect of the
  decision modules shared with ChronoTranscriber, ChronoMiner and
  AutoExcerpter, vendored unchanged in `chronodownloader/common/`. The work
  request keeps its measured state object and the page check its parts, and
  every recorded jev_eval answer parses as before. A choice answer now needs
  probabilities that sum to at most 1, a declined question is a malformed
  answer, and some malformed-answer messages read differently.
- **v1.1.2** (27 October 2026) -- The cross-process pacing test no longer
  fails under `pytest -n auto`. Its lower bounds on the arrival gap and span
  equaled the lateness the coordinator allows and left no budget for connect
  and accept latency at the test server; both asserts now subtract a 0.05 s
  `ARRIVAL_SLACK`. No change to the package.
- **v1.1.1** (27 October 2026) -- Ruff now checks docstrings with its
  pydocstyle rules under the Google convention, outside tests. Every
  docstring opens with a one-line summary, the provider, event-protocol and
  parser methods that lacked a docstring have one, and docstrings with
  backslashes are raw strings. Behavior is unchanged.
- **v1.1.0** (24 October 2026) -- A provider block takes `min_search_timeout_s`,
  which raises the run's `--search-timeout` for that provider unless the run sets
  0. Gallica's built-in profile sets 180 s, since the searches of several runs queue
  behind its 8 s pacing and exceeded the 60 s default. A page-image size the server
  refuses with HTTP 400, 404 or 410 is now an expected answer, logged at INFO while
  another size remains, so Polona no longer logs one warning per page; a page that
  no size delivers still warns.
- **v1.0.1** (24 October 2026) -- Security patch: the lockfile moves certifi to
  2026.10.23, which closes GHSA-6q48-6c7w-9gw2, and urllib3 to 2.8.0, which closes
  GHSA-8988-9cw3-xx77, GHSA-vxq7-64xx-v4gw and GHSA-gh4c-6fx4-qh6g. Dependency versions
  only; no code changes.
- **v1.0.0** (24 October 2026) -- Rewrite of ChronoDownloader as one `chronodownloader`
  package on the architecture of ChronoTranscriber, ChronoMiner and AutoExcerpter,
  extended for agents. It replaces the `api/` and `main/` packages of the 1.x line with
  their `config.json`, `api_keys.json` and JSON state file; old state and old output
  folders are not migrated. The core in `run.py` plans and runs nine commands,
  `download`, `fetch`, `search`, `inspect`, `verify`, `status`, `drop`, `providers` and
  `ui`, each built from an option table in `spec.py`, from which the argparse parser,
  the wizard steps of the guided UI and the README tables follow. The core never prints,
  prompts or exits; every network command offers `--dry-run` and a one-line `--json`
  summary, and the exit codes are 0, 1, 2 and 130, with works left `no_match`,
  `needs_review`, `deferred` or `blocked` counted as open rather than failed. One YAML
  settings file (`config/settings.yaml`, `--settings FILE`, else the packaged example)
  holds the key variables, the judge, the coordination limits, one block per provider,
  size limits, naming, the state and log folders and a `defaults:` block of run options;
  unknown keys are errors. The 17 providers are classes behind one `Provider` protocol
  with declared capabilities (IIIF, required key, image size cap, HathiTrust
  metadata-only, Anna's Archive quota) and explicit completeness, so a work is complete
  only when every expected page or file within the page limit is present. Provider
  changes: MDZ highlight tags stripped, Wellcome images capped at 1,024 px, refusals and
  timeouts of Gallica, the Library of Congress, BNE, the British Library and SBB
  reported with their cause instead of as empty results, Internet Archive pages from its
  page service, item and viewer URLs resolved for 16 providers, DPLA records without a
  manifest reported as metadata-only with the holding library's page, Gallica and BNE
  requests sent with a Chrome fingerprint through curl_cffi (now a required dependency)
  past Cloudflare, Gallica searched by `dc.title`, Europeana's Gallica records
  downloaded through Gallica's own manifest, BNE digital copies read from the current
  `OP3001` link, SBB searched on `sru.k10plus.de` with its word indexes, and a creator
  search without records repeated without the creator. Matching has three modes: `judge`
  (default), in which the OpenRouter decision model `typesafe/jev-1.14` answers
  `same_work` and a five-way edition relation for every candidate, accepting at .9 and
  rejecting at .1; `fuzzy`, the text score alone, which accepts nothing on its own and
  sends its matches to review; and `hybrid`, which drops low text scores before the
  judge. The edition policies `any_edition`, `same_edition` and `any_language` decide
  which relations pass without review, a preference order (relation, banded probability,
  IIIF, provider order) picks the copy, and the next acceptable candidate is downloaded
  when the preferred one yields no file. A judge outage defers works with `retry_after`,
  and `inspect` and `verify --pages` ask the page model `openai/gpt-6-luna-decisions`
  about the title page and imprint year over the first pages. Each output root holds
  `chronodownloader.sqlite` (schema 1: runs, works, candidates with their judgments,
  attempts with their completeness, files), whose views `work_status` and
  `work_candidates` are the read contract and from which `index.csv` is rendered after
  each run. Per-work leases, a lease-checked commit of staged downloads, reconciliation
  when a store opens and the release of leases held by dead processes let several runs
  share one root, and `<state_dir>/coordination.sqlite` keeps pacing, download slots,
  circuit breakers and quota across all processes of a machine, the judge endpoint
  included. Metadata files are named by item (`metadata/{stem}_{slug}_{item}.json`);
  work folder, page image and rendering names are unchanged. Logs are redacted (stderr
  at WARNING, the log file at INFO), and `download`, `search` and `fetch` write the
  launcher heartbeat. Verification: in the live round of 23.-24.10.2026 from one
  machine, Internet Archive, MDZ, DDB, Polona, Wellcome, SLUB, e-rara, SBB, Gallica,
  Europeana and BNE completed downloads; the Library of Congress, the British Library,
  Google Books and Anna's Archive met external blocks; HathiTrust returned metadata and
  a clean metadata-only outcome; on 294 evaluation pairs the default judge reached .975
  accuracy with 193 automatic decisions and no error under the production request;
  `--matching fuzzy` ran without a key and accepted nothing; two concurrent processes on
  one root kept the pacing and slot limits and downloaded no page image twice; and four
  agents running `download`, `search`, `inspect`, `fetch`, `verify` and `drop` on one
  root at once, and a fetch killed mid-download and resumed, left no lease held, no item
  recorded twice, no staged leftover and no recorded file missing.
