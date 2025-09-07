- The core HTTP stack (`api/utils.py`) centralizes retries, pacing, and download budgeting. Use `download_file()` for all network file downloads and `make_request()` for API calls.
- When working with IIIF, save manifests and consider calling `utils.download_iiif_renderings(manifest, out_dir, prefix)` to automatically pick up PDFs/EPUBs exposed via manifest-level `rendering` entries.

## Scalability and Reliability

- The session layer uses `requests.Session` with limited urllib3 retries and explicit handling of 429/5xx with exponential backoff. Per-provider pacing is configured in `provider_settings.*.network`.
- A global download budget guards against runaway jobs (see "Download limits and preferences"). The main loop will stop early when the budget is exhausted.
- For very large jobs, consider splitting input CSVs and running multiple processes. Per-provider limits and budget checks will throttle IO; keep an eye on provider terms of use.
# Historical Sources Downloader

This project provides a Python-based tool to search for and download digitized historical sources (books, manuscripts, etc.) from various European and American digital libraries and platforms.

## Project Structure

- `api/`: Contains modules for interacting with specific digital library APIs.
  - `__init__.py`
  - `utils.py`: Core HTTP, retries/backoff, pacing, download budgeting, file helpers.
  - `iiif.py`: Shared helpers to parse IIIF manifests and download representative images (DRY across providers).
  - `providers.py`: Central registry that maps provider keys to their search/download functions.
  - `[library_name]_api.py`: Individual modules for each API (e.g., `bnf_gallica_api.py`, `internet_archive_api.py`, etc.).
- `main/`: CLI and orchestration layer.
  - `__init__.py`
  - `pipeline.py`: Core orchestration pipeline (search, select, download). Reusable from code.
  - `downloader.py`: Thin CLI wrapper that parses arguments and delegates to `pipeline.py`.
- `sample_works.csv`: An example CSV file format.
- `requirements.txt`: Python dependencies.
- `README.md`: This file.

## Setup

1. **Clone the repository (or create the files as listed).**
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **API keys**
   Some providers require API keys. Set them as environment variables before running. On Windows PowerShell:
   ```powershell
   $env:EUROPEANA_API_KEY = "<your_key>"
   $env:DDB_API_KEY = "<your_key>"
   $env:DPLA_API_KEY = "<your_key>"
   $env:GOOGLE_BOOKS_API_KEY = "<your_key>"
   $env:HATHI_API_KEY = "<your_key>"   # optional; needed only for page-image API
   ```
   Required/optional keys by provider:
   - Europeana: EUROPEANA_API_KEY (required)
   - Deutsche Digitale Bibliothek (DDB): DDB_API_KEY (required)
   - Digital Public Library of America (DPLA): DPLA_API_KEY (required)
   - Google Books: GOOGLE_BOOKS_API_KEY (required)
   - HathiTrust: HATHI_API_KEY (optional)

   Preflight API key checks:
   - At startup, the CLI enforces environment-only configuration for required providers.
   - If a required environment variable is missing for any enabled provider, an ERROR is logged and that provider is skipped for the run.
   - Required provider env vars: `EUROPEANA_API_KEY`, `DDB_API_KEY`, `DPLA_API_KEY`, `GOOGLE_BOOKS_API_KEY`.
   - Optional: `HATHI_API_KEY` enables page-image API for HathiTrust but is not required for searching/saving metadata.
   - Others (BnF Gallica, British Library, MDZ, Library of Congress, Polona, BNE, Internet Archive, Wellcome Collection): no key required

## Usage

1. **Prepare your input CSV file.** It must contain a column `Title`. You can also include an optional `Creator` and a recommended `entry_id` column to uniquely identify each row. If `entry_id` is missing, the tool will generate a fallback like `E0001`, `E0002`, ... for the current run.

   Example (`sample_works.csv`):
   ```csv
   entry_id,Title,Creator
   E0001,"Le Morte d'Arthur","Thomas Malory"
   E0002,"The Raven","Edgar Allan Poe"
   E0003,"Philosophiæ Naturalis Principia Mathematica","Isaac Newton"
   E0004,"De revolutionibus orbium coelestium","Nicolaus Copernicus"
   E0005,"Discours de la méthode","René Descartes"
   E0006,"On the Origin of Species","Charles Darwin"
   E0007,"De Magnete","William Gilbert"
   E0008,"Principia Ethica","G. E. Moore"
   E0009,"Dialogo sopra i due massimi sistemi del mondo","Galileo Galilei"
   E0010,"De humani corporis fabrica","Andreas Vesalius"
   E0011,"Historia regum Britanniae","Geoffrey of Monmouth"
   E0012,"Divina Commedia","Dante Alighieri"
   ```
2. **Run the downloader script:**
   ```bash
   # Basic run
   python main/downloader.py your_input_file.csv --config config.json --output_dir downloaded_works --log-level INFO

   # Dry run (search + scoring + metadata only, no downloads)
   python main/downloader.py your_input_file.csv --config config.json --dry-run
   ```
  The CLI resolves providers from the config, searches across them, selects the best match per row, writes metadata, and downloads according to your strategy. Internally it delegates to `main/pipeline.py`, which encapsulates the orchestration logic for easier reuse and testing.

### CLI options

- `csv_file`: Path to the CSV file. Must have a `Title` column; optional `Creator` column.
- `--output_dir`: Output directory (default: `downloaded_works`).
- `--dry-run`: Skip downloads; still writes selection metadata and folders.
- `--log-level`: `DEBUG` | `INFO` | `WARNING` | `ERROR` | `CRITICAL` (default: `INFO`).
- `--config`: Path to JSON config (default: `config.json`). Alternatively set the `CHRONO_CONFIG_PATH` environment variable.

Example (PowerShell) to point to a non-default config path:

```powershell
$env:CHRONO_CONFIG_PATH = "C:\path\to\config.json"
python .\main\downloader.py .\sample_works.csv
```

## Configuration

Use `config.json` (or `CHRONO_CONFIG_PATH`) to enable/disable providers and tune per-provider behavior.

### Enable/disable providers

```json
{
  "providers": {
    "internet_archive": false,
    "bnf_gallica": true,
    "loc": true,
    "europeana": true,
    "dpla": false,
    "ddb": true,
    "british_library": true,
    "mdz": true,
    "polona": false,
    "bne": true,
    "google_books": true,
    "hathitrust": false,
    "wellcome": true
  }
}
```

### Provider settings and rate limiting

Each provider can define `max_pages`/`max_images` and network/backoff policies. Example:

```json
{
  "provider_settings": {
    "gallica": {
      "max_pages": 15,
      "network": {
        "delay_ms": 1500,
        "jitter_ms": 400,
        "max_attempts": 25,
        "base_backoff_s": 1.5,
        "backoff_multiplier": 1.6,
        "timeout_s": 30
      }
    },
    "google_books": {
      "free_only": true,
      "prefer": "pdf",
      "allow_drm": false,
      "max_files": 3,
      "network": {
        "delay_ms": 200,
        "jitter_ms": 100,
        "max_attempts": 25,
        "base_backoff_s": 1.2,
        "backoff_multiplier": 1.5,
        "timeout_s": 30
      }
    }
  }
}
```

Optional environment overrides used by some connectors:
- `MDZ_MAX_PAGES`, `DDB_MAX_PAGES`, `DPLA_MAX_PAGES`, `WELLCOME_MAX_IMAGES`.

### Download limits and preferences (centralized)

The downloader enforces optional global/per-work/per-provider download budgets and can prefer bundled PDFs/EPUBs from IIIF manifests over page images. Configure in `config.json`:

```json
{
  "download": {
    "prefer_pdf_over_images": true,
    "download_manifest_renderings": true,
    "max_renderings_per_manifest": 1,
    "rendering_mime_whitelist": ["application/pdf", "application/epub+zip"],
    "overwrite_existing": false,
    "include_metadata": true  // set to false to download only objects (skip saving metadata files)
  },
  "download_limits": {
    "max_total_files": 0,
    "max_total_bytes": 0,
    "per_work": { "max_files": 0, "max_bytes": 0 },
    "per_provider": {
      "mdz": { "max_files": 0, "max_bytes": 0 },
      "gallica": { "max_files": 0, "max_bytes": 0 }
    },
    "on_exceed": "skip"  // or "stop" to abort processing immediately
  }
}
```

Notes:
- Per-provider keys use host groups detected from download URLs: `gallica`, `british_library`, `mdz`, `europeana`, `wellcome`, `loc`, `ddb`, `polona`, `bne`, `dpla`, `internet_archive`, `google_books`.
- If a manifest includes top-level `rendering` entries (PDF/EPUB), they are downloaded first. When `prefer_pdf_over_images` is true, page-image downloads are skipped if at least one rendering was saved.
- Budgets are enforced centrally in `api/utils.py::download_file()` with pre-checks and dynamic byte counting; policy `on_exceed` can be `skip` or `stop`.

### Selection, Fuzzy Matching, and Provider Hierarchy

The downloader now performs a pre-download selection across all enabled providers to avoid downloading duplicates from multiple sources. Configure selection behavior in `config.json`:

```
{
  "selection": {
    "strategy": "collect_and_select",              // or "sequential_first_hit"
    "provider_hierarchy": ["mdz", "bnf_gallica", "loc", "british_library", "internet_archive", "europeana"],
    "min_title_score": 85,                          // 0..100 strictness for title match
    "creator_weight": 0.2,                          // 0..1 weight to include creator similarity
    "year_tolerance": 2,                            // reserved for future date-based matching
    "max_candidates_per_provider": 5,               // limit results per provider
    "download_strategy": "selected_only",          // "selected_only" | "all"
    "keep_non_selected_metadata": true              // persist JSON metadata for non-selected candidates
  },
  "naming": {
    "include_creator_in_work_dir": true,
    "include_year_in_work_dir": true,
    "title_slug_max_len": 80
  }
}
```

- In `collect_and_select`, the tool searches all providers, scores candidates (title + optional creator), then picks the best according to `provider_hierarchy` and quality signals (e.g., IIIF availability).
- In `sequential_first_hit`, the tool searches providers in `provider_hierarchy` order and selects the first acceptable match.

### Output Folder Structure and Naming

For each CSV row, a stable work directory is created in strict snake_case using the `entry_id` and `Title`:

```
downloaded_works/<entry_id>_<work_name>/
  work.json     // unified metadata: inputs (includes entry_id), candidates, scores, selection decision
  metadata/     // downloaded metadata (manifests, API responses), if enabled in config
    <entry_id>_<work_name>_<provider>.json
    <entry_id>_<work_name>_<provider>_2.json
    ...
  objects/      // all downloaded objects (images, PDFs, EPUBs)
    <entry_id>_<work_name>_<provider>.pdf
    <entry_id>_<work_name>_<provider>_2.pdf
    <entry_id>_<work_name>_<provider>_image_001.jpg
    <entry_id>_<work_name>_<provider>_image_002.jpg
    ...
```

Notes:
- `entry_id`, `work_name`, and `provider` are always included in file names, all in snake_case.
- Images are always numbered with a 3-digit counter (`_image_001`, `_image_002`, ...).
- Non-image objects (PDF/EPUB/etc.) get a number only when multiple files of the same type exist for the same provider (e.g., `_2`).

One summary file is maintained under `downloaded_works/`:
- `index.csv` — appended per row

It includes columns: `work_id`, `entry_id`, `work_dir`, `title`, `creator`, `selected_provider`, `selected_provider_key`, `selected_source_id`, `selected_dir`, `work_json`.

## Implemented APIs
The downloader currently supports connectors for:

- **BnF Gallica** (France)
- **Internet Archive** (US)
- **Library of Congress** (US)
- **Europeana** (EU aggregator)
- **Digital Public Library of America**
- **Deutsche Digitale Bibliothek**
- **British Library**
- **Münchener DigitalisierungsZentrum**
- **Polona** (Polish National Library)
- **Biblioteca Nacional de España**
- **Google Books**
- **HathiTrust**
- **Wellcome Collection**

## Extending the Tool
- Implement more APIs in `api/`.
- Enhance search capabilities by using more fields from the CSV.
- IIIF parsing and per-canvas downloads are centralized in `api/iiif.py` to reduce duplication across providers.
- Adjust fuzzy matching or selection rules via `api/matching.py` and `config.json`.
 - Programmatic use (advanced): import and call the pipeline directly from your own Python code:

   ```python
   from main import pipeline

   providers = pipeline.load_enabled_apis("config.json")
   providers = pipeline.filter_enabled_providers_for_keys(providers)
   pipeline.ENABLED_APIS = providers

   pipeline.process_work(
       title="Le Morte d'Arthur",
       creator="Thomas Malory",
       entry_id="E0001",
       base_output_dir="downloaded_works",
       dry_run=False,
   )
   ```

## Changelog

Recent maintenance and bug fixes:

- Network core (`api/utils.py`):
  - Fixed provider host matching to match exact domains or subdomains and avoid false positives (e.g., `notarchive.org` no longer matches `archive.org`).
  - Corrected `Retry-After` HTTP-date parsing to use `datetime.now(tz)` for accurate backoff durations.
  - Moved the success log for downloads to occur only after confirming the file was not truncated by budget limits.
  - Made urllib3 `Retry.allowed_methods` a `frozenset` for broader compatibility.
  - Added thread-local `entry_id` support and automatic filename suffixing with `entry_id` and provider abbreviation for all downloads.
- Main downloader (`main/downloader.py`):
  - Parent folder is now named `<entry_id>_<title_slug>[_creator][_YYYY]` for easier browsing.
  - `work.json` now includes `entry_id` in the `input` block.
  - The run writes `index.csv` (CSV only) including an `entry_id` column.
- Samples: `sample_works.csv` now includes an `entry_id` column and additional sample rows.
- Requirements: Removed `openpyxl`; indexing is now CSV-only.
- Gallica connector (`api/bnf_gallica_api.py`): Removed manual `time.sleep()` pacing; rate limiting is now handled centrally per-provider.
- BNE connector (`api/bne_api.py`): Hardened IIIF manifest resolution by trying both `/manifest` and `/manifest.json` patterns.
- Requirements: Pinned versions in `requirements.txt` for stability (requests/urllib3/pandas/beautifulsoup4).
- Repo hygiene: Added a top-level `.gitignore` and included `sample_works.csv` referenced in this README.

Architecture and code quality:

- Introduced `main/pipeline.py` to encapsulate the orchestration flow (search → select → persist → download). `main/downloader.py` is now a thin CLI wrapper calling the pipeline. This improves modularity, readability, and reuse.
- Centralized selection and naming helpers in the pipeline; simplified the CLI and reduced duplication.
- Minor cleanup: removed unused imports in `main/downloader.py`, improved error messages, and kept backward-compatible shims for legacy imports.
