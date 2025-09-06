# Historical Sources Downloader

This project provides a Python-based tool to search for and download digitized historical sources (books, manuscripts, etc.) from various European and American digital libraries and platforms.

## Project Structure

- `api/`: Contains modules for interacting with specific digital library APIs.
  - `__init__.py`
  - `utils.py`: Utility functions (e.g., sanitizing filenames, downloading files).
  - `[library_name]_api.py`: Individual modules for each API (e.g., `bnf_gallica_api.py`, `internet_archive_api.py`, etc.).
- `main/`: Contains the main downloader script.
  - `__init__.py`
  - `downloader.py`: The main script to process a CSV input file and orchestrate downloads.
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
   - Others (BnF Gallica, British Library, MDZ, Library of Congress, Polona, BNE, Internet Archive, Wellcome Collection): no key required

## Usage

1. **Prepare your input CSV file.** It should contain at least a column named `Title`. Example (`sample_works.csv`):
   ```csv
   Title,Creator
   "Le Morte d'Arthur","Thomas Malory"
   "The Raven","Edgar Allan Poe"
   "Philosophiæ Naturalis Principia Mathematica","Isaac Newton"
   ```
2. **Run the downloader script:**
   ```bash
   # Basic run
   python main/downloader.py your_input_file.csv --config config.json --output_dir downloaded_works --log-level INFO

   # Dry run (search + scoring + metadata only, no downloads)
   python main/downloader.py your_input_file.csv --config config.json --dry-run
   ```
  The script resolves providers from the config, searches across them, selects the best match per row, writes metadata, and downloads according to your strategy.

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

### Output Folder Structure

For each CSV row, a stable work directory is created:

```
downloaded_works/<work_id>_<title_slug>[_<creator_slug>][_YYYY]/
  work.json             // unified metadata: inputs, candidates, scores, selection decision
  selected/<provider_key>/
    ...                 // downloaded files and manifests from the selected provider
  sources/<provider_key>/<source_id>/
    search_result.json  // (optional) raw candidate metadata for auditing/re-selection
```

Additionally an `index.csv` is appended under `downloaded_works/` summarizing processed works (work_id, title, creator, selected provider, paths).

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
- Improve download logic (IIIF parsing, etc.).
- Adjust fuzzy matching or selection rules via `api/matching.py` and `config.json`.
