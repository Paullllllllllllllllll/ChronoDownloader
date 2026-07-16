# ChronoDownloader v1.12.2

A Python tool for discovering and downloading digitized historical
sources from major digital libraries worldwide.
ChronoDownloader automates searching, selecting, and downloading
historical books, manuscripts, and documents from 17 digital
library providers. It integrates with
[ChronoTranscriber](https://github.com/Paullllllllllllllllll/ChronoTranscriber)
and
[ChronoMiner](https://github.com/Paullllllllllllllllll/ChronoMiner)
for a complete document retrieval, transcription, and data
extraction pipeline.

> **Work in Progress** -- ChronoDownloader is under active
> development. If you encounter any issues, please report them on
> [GitHub Issues](https://github.com/Paullllllllllllllllll/ChronoDownloader/issues).

## Table of Contents

- [Features](#features)
- [Supported Providers](#supported-providers)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [CSV Format](#csv-format)
- [Command-Line Reference](#command-line-reference)
- [Configuration](#configuration)
- [Output Structure](#output-structure)
- [Advanced Usage](#advanced-usage)
- [Architecture](#architecture)
- [FAQ](#faq)
- [Versioning](#versioning)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Multi-provider search** across 17 digital libraries with
  configurable parallel searches (up to 5 concurrent)
- **Two execution modes**: interactive guided workflow with colored
  console UI, or scriptable CLI for automation and batch jobs
- **IIIF support**: native IIIF Presentation v2/v3 manifest
  parsing and Image API; direct manifest downloads via `--iiif`,
  interactive mode, or CSV `direct_link` column
- **Parallel downloads** with per-provider concurrency limits and
  thread-safe operations
- **Intelligent selection**: fuzzy token-set matching with
  configurable thresholds, creator weighting, and provider
  hierarchy ranking
- **Flexible formats**: PDFs, EPUBs, and high-resolution page
  images (JPG, PNG, JP2, TIFF); PDF-first strategy with IIIF
  fallback
- **Budget management**: content-type download budgets (images,
  PDFs, metadata) at global and per-work levels
- **Rate limiting**: per-provider delays, jitter, exponential
  backoff, and adaptive circuit breaker for repeated 429s
- **Quota system**: daily quota tracking with deferred download
  queue and automatic background retries
- **Resume modes**: skip completed, skip if objects exist, resume
  from CSV status, or reprocess all
- **Unified CSV**: single file as both input and output, tracking
  download status and item URLs
- **Metadata preservation**: search results, IIIF manifests,
  selection decisions, and scores saved for auditing

## Supported Providers

| Provider | Region | API Key | IIIF |
|----------|--------|---------|------|
| Internet Archive | US | No | Yes |
| BnF Gallica | France | No | Yes |
| Library of Congress | US | No | Yes |
| Google Books | Global | Yes | No |
| Europeana | EU (Aggregator) | Yes | Yes |
| DPLA | US (Aggregator) | Yes | Yes |
| Deutsche Digitale Bibliothek | Germany | Yes | Yes |
| British Library | UK | No | Yes |
| MDZ | Germany | No | Yes |
| Polona | Poland | No | Yes |
| Biblioteca Nacional de Espana | Spain | No | Yes |
| HathiTrust | US | Optional | Yes |
| Wellcome Collection | UK | No | Yes |
| Anna's Archive | Global (Aggregator) | Optional* | No |
| SLUB Dresden | Germany | No | Yes |
| e-rara | Switzerland | No | Yes |
| SBB Digital Collections | Germany | No | No (METS) |

\* Anna's Archive works without an API key using public download
links. A member API key enables faster downloads with daily quota
tracking (875/day).

## Installation

**Requirements**: Python 3.10+ (3.11+ recommended),
[uv](https://docs.astral.sh/uv/), internet connection.

```bash
git clone https://github.com/Paullllllllllllllllll/ChronoDownloader.git
cd ChronoDownloader

uv sync
```

For development (includes type checkers and test tools):

```bash
uv sync --extra dev
```

Alternatively, with pip:

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux / macOS

pip install -e .
pip install -e ".[dev]"       # development dependencies
```

### API Keys

Several providers require API keys set as environment variables.
Many providers (Internet Archive, BnF Gallica, Library of Congress,
British Library, MDZ, Polona, Wellcome, SLUB, e-rara, BNE, SBB)
work without any keys.

```bash
# Windows PowerShell
$env:EUROPEANA_API_KEY="your_key"
$env:DDB_API_KEY="your_key"
$env:DPLA_API_KEY="your_key"
$env:GOOGLE_BOOKS_API_KEY="your_key"
$env:ANNAS_ARCHIVE_API_KEY="your_key"     # optional

# Linux / macOS
export EUROPEANA_API_KEY=your_key
export DDB_API_KEY=your_key
export DPLA_API_KEY=your_key
export GOOGLE_BOOKS_API_KEY=your_key
export ANNAS_ARCHIVE_API_KEY=your_key     # optional
```

For persistent configuration, add these to your system environment
variables or shell profile.

**Where to register**:

- [Europeana Pro](https://pro.europeana.eu/page/get-api) (free)
- [DPLA API](https://pro.dp.la/developers/api-codex) (free)
- [Deutsche Digitale Bibliothek](https://www.deutsche-digitale-bibliothek.de/content/api) (free)
- [Google Cloud Console](https://console.cloud.google.com/apis/library/books.googleapis.com)
- [Anna's Archive](https://annas-archive.org) membership (optional,
  for fast downloads)

### Remapping Key Environment Variables (optional)

By default each provider reads its API key from the environment variable
named above. To swap keys between runs without editing the environment,
place an `api_keys.json` next to your `config.json` (the file resolved
via `--config` or `CHRONO_CONFIG_PATH`). It maps each key-bearing
provider to the name of the environment variable holding its key. A
tracked `api_keys.example.json` ships with the default mapping as a
starting template; copy it to `api_keys.json` to customize.

```json
{
  "europeana": "EUROPEANA_API_KEY_2",
  "dpla": "DPLA_API_KEY",
  "ddb": "DDB_API_KEY",
  "google_books": "GOOGLE_BOOKS_API_KEY",
  "hathitrust": "HATHI_API_KEY",
  "annas_archive": "ANNAS_ARCHIVE_API_KEY"
}
```

The file is fully optional. A missing file or a missing provider entry
falls back to the default variable name shown above, so existing setups
are unaffected.

### Verify Installation

```bash
uv run python -m main.cli --help
uv run python -m main.cli --list-providers
```

## Quick Start

### Interactive Mode

```bash
python -m main.cli
```

The guided workflow walks through mode selection (CSV batch, single
work, predefined collection, direct IIIF, or search only), source
configuration,
output settings, and processing options. Colored console UI with
provider status display, CSV discovery, back/quit navigation, and
input validation.

### CLI Mode

```bash
# Process a CSV file
python -m main.cli sample_works.csv

# Custom output directory
python -m main.cli my_books.csv --output_dir ./historical_sources

# Dry run (search and select without downloading)
python -m main.cli my_books.csv --dry-run

# Search only: print structured candidate metadata, download nothing
python -m main.cli --search "Le Viandier" --creator "Taillevent" --json
python -m main.cli my_books.csv --search-only --json

# Direct IIIF manifest download (bypass search)
python -m main.cli --cli \
  --iiif https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb11280551/manifest \
  --name "Kochbuch"

# Multiple IIIF manifests
python -m main.cli --cli --iiif URL1 --iiif URL2

# Specific configuration
python -m main.cli my_books.csv --config config_small.json
```

CLI mode activates automatically when positional CSV, `--iiif`,
`--id`, `--dry-run`, or other CLI-style arguments are present.
Force it explicitly with `--cli` / `--non-interactive`, or
`interactive_mode: false` in config. Interactive mode requires a
TTY: if it is requested without one, the tool exits with code `2`
instead of blocking on a prompt.

`--dry-run` has no side effects: it performs discovery, resume
classification, and candidate matching, but writes no work
directories, `work.json` files, or `index.csv` rows and makes no
downloads.

### Search-Only Mode

Search-only mode runs the same discovery and matching pipeline as a
download run but stops before the download phase and prints one
structured result per work. Like `--dry-run` it is fully
side-effect-free; unlike `--dry-run` it returns the full ranked
candidate list instead of a one-line log, and it ignores resume
status (completed works are searched again, since searching is free
and idempotent).

```bash
# Ad hoc search, human-readable ranked table
python -m main.cli --search "Le Viandier" --creator "Taillevent"

# Machine-readable: one JSON line (NDJSON) per work on stdout;
# logs move to stderr so stdout stays parseable
python -m main.cli --search "Le Viandier" --creator "Taillevent" --json

# Search every row of a CSV (all rows with a title, regardless of
# status; --entry-ids and --limit narrow the set)
python -m main.cli my_books.csv --search-only --json --limit 10
```

Each JSON line carries `entry_id`, `query`, `status` (`match`,
`no_match`, or `no_candidates`), the `selected` candidate, and all
`candidates` with `provider_key`, `source_id`, `item_url`,
`iiif_manifest`, and matching scores. Feed a chosen candidate to a
deterministic download via `--id SOURCE_ID --provider PROVIDER_KEY`
(or `--iiif MANIFEST_URL`), giving a search, review, then targeted
download workflow. Exit codes: `0` when every queried work produced
a confident match, `1` when at least one did not, `2` on usage
errors. Searches run in parallel but results print after selection, so
a slow provider would otherwise gate the whole fan-out; the per-provider
`selection.search_timeout_seconds` (default 60, overridable per run with
`--search-timeout`) drops any provider that exceeds it, and `--providers`
scopes the run to a chosen subset.

Downloads that hit a provider quota are recorded as `deferred` in
the works CSV (retriable, distinct from `failed`). Ready deferred
items are retried automatically and synchronously at the start of
the next run; there is no long-lived background daemon.

### Common Workflows

**Large-scale CSV processing**:

```bash
# Process (parallel downloads enabled in config)
python -m main.cli large_collection.csv --output_dir ./downloads

# Resume after interruption (skips completed works)
python -m main.cli large_collection.csv --output_dir ./downloads
```

**Dry-run analysis**:

```bash
python -m main.cli sample_works.csv --dry-run --log-level INFO
# Inspect work.json files for candidate scores and selection
```

**Provider-specific configs**: create specialized JSON configs
(e.g., `config_fast.json` with Internet Archive + Google Books
only, `config_quality.json` with European libraries and high
`min_title_score`) and pass via `--config`.

## CSV Format

ChronoDownloader uses a unified CSV as both input and output.

**Required columns**:

- `entry_id`: unique identifier (always required)
- `short_title`: title to search for (optional if `direct_link`
  is provided)

**Optional columns**:

- `main_author`: creator/author name (improves ranking)
- `direct_link`: IIIF manifest URL for direct download (bypasses
  search)
- `retrievable`: download status (`True`/`False`/`deferred`/empty,
  automatically updated). `deferred` marks a quota-deferred work
  that will be retried automatically at the start of the next run;
  it is distinct from `False` (failed) and counts as pending for
  resume purposes.
- `link`: item URL (automatically populated after download)

Additional columns are preserved but not used.

**Example** (before processing):

```csv
entry_id,short_title,main_author,retrievable,link
6106,Tractatus de uino,Anonymous,,
12495,Tractatus de praeparandis,Hier. Emser,,
```

**After processing**:

```csv
entry_id,short_title,main_author,retrievable,link
6106,Tractatus de uino,Anonymous,True,https://archive.org/details/...
12495,Tractatus de praeparandis,Hier. Emser,False,
```

**IIIF-only CSV**: for direct IIIF downloads, `short_title` can be
omitted. Rows with a `direct_link` value download directly from
the manifest URL without provider search.

```csv
entry_id,direct_link
1,https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb11280551/manifest
2,https://gallica.bnf.fr/iiif/ark:/12148/bpt6k1510024v/manifest.json
```

## Command-Line Reference

```
python -m main.cli [csv_file] [options]
```

**Positional**: `csv_file` -- path to CSV (required in CLI mode,
optional in interactive).

**Core options**:

- `--output_dir DIR` -- output directory
  (default: `downloaded_works`)
- `--dry-run` -- search and select only, skip downloads
- `--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}` -- logging
  verbosity (default: `INFO`)
- `--config PATH` -- configuration JSON file
  (default: `config.json`)
- `--interactive` / `--cli` / `--non-interactive` -- force execution
  mode (override the config-file mode)
- `--json` -- emit one machine-readable JSON summary line on stdout
  at exit (files processed / succeeded / failed / deferred / skipped,
  output paths)
- `--verify` -- verify the works already downloaded under
  `--output_dir` (non-empty objects, PDF/EPUB magic bytes, and
  recorded page counts) and flip any incomplete work to `partial`
  for re-download, then exit

**Search only** (no downloads, no side effects):

- `--search TITLE` -- search all enabled providers for a title and
  print structured candidate metadata
- `--creator NAME` -- creator/author for `--search` (improves match
  scoring)
- `--search-only` -- with a CSV file: search and match every row
  with a title, print one result per work (NDJSON with `--json`)

**Direct downloads** (bypass search):

- `--iiif URL` -- IIIF manifest URL (repeatable)
- `--id IDENTIFIER` -- provider-specific identifier
  (e.g., `bsb11280551` for MDZ)
- `--provider KEY` -- provider key for `--id` lookup
  (auto-detected when possible)
- `--name STEM` -- custom naming stem for `--iiif`/`--id`
  downloads

**Provider control**:

- `--providers KEYS` -- comma-separated provider list (replaces
  config)
- `--enable-provider KEYS` -- add providers to active set
- `--disable-provider KEYS` -- remove providers from active set
- `--list-providers` -- list available provider keys and exit

**Quota utilities**:

- `--quota-status` -- display quota usage and deferred queue status
- `--status` -- display works-CSV progress (pass the csv_file) plus
  quota and deferred queue status
- `--cleanup-deferred` -- remove completed items from deferred
  queue

**Exit codes** (the CLI agent contract):

- `0` -- full success
- `1` -- one or more works failed or are partial
- `2` -- usage or configuration error (missing/invalid CSV, no
  providers enabled, or interactive mode requested without a TTY)
- `130` -- interrupted by the user (Ctrl-C)

**Processing scope**:

- `--pending-mode {all,new,failed}` -- which rows to process
- `--entry-ids IDS` -- restrict to specific entry IDs
  (repeatable, comma-separated)
- `--limit N` -- process at most N filtered rows

**Runtime config overrides**:

- `--resume-mode {skip_completed,reprocess_all,skip_if_has_objects,resume_from_csv}`
  -- `resume_from_csv` is a row-filter mode: it relies solely on the
  `retrievable` column of the source CSV to decide which rows to
  process (completed rows are filtered out before processing); it
  performs no per-work-directory status check.
- `--selection-strategy {collect_and_select,sequential_first_hit}`
- `--min-title-score FLOAT` -- minimum PURE title-match score to
  accept a candidate (0--100). Creator similarity never lowers this
  gate; missing creator metadata is not penalized.
- `--search-timeout SECONDS` -- per-provider search timeout (float);
  overrides `selection.search_timeout_seconds` for this run. A provider
  whose search exceeds it is dropped so one slow provider cannot stall the
  fan-out; `0` disables the timeout
- `--creator-weight FLOAT` -- author match weight (0.0--1.0), applied
  as a positive ranking bonus only
- `--max-candidates-per-provider INT`
- `--download-strategy {selected_only,all}`
- `--[no-]keep-non-selected-metadata`
- `--[no-]prefer-pdf-over-images`
- `--[no-]download-manifest-renderings`
- `--max-renderings-per-manifest INT`
- `--rendering-mime-whitelist MIMES` -- comma-separated, repeatable
- `--[no-]overwrite-existing`
- `--[no-]include-metadata`

## Configuration

ChronoDownloader ships a tracked template `config.example.json` with
conservative defaults. Your personal `config.json` is gitignored and
machine-local. The loader resolves them in this order:

1. `config.json` at the path set by `--config` or `CHRONO_CONFIG_PATH`
   (or `config.json` in the current directory when neither is set).
2. `config.example.json` in the same directory, if `config.json` is
   absent (logs one INFO line with copy instructions).
3. `FileNotFoundError` when neither file is present.

To customize: copy `config.example.json` to `config.json` and edit it.
A fresh clone runs on the example defaults without any setup.

Override the config file at runtime with `--config` or the
`CHRONO_CONFIG_PATH` environment variable.

### 1. General

```json
{
  "general": {
    "interactive_mode": true,
    "default_output_dir": "downloaded_works",
    "default_csv_path": "sample_works.csv"
  }
}
```

- `interactive_mode`: `true` launches guided workflow; `false`
  runs in CLI mode
- `default_output_dir`: default output directory (interactive
  mode)
- `default_csv_path`: default CSV file (interactive mode)

### 2. Providers

Enable or disable individual providers:

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
    "wellcome": true,
    "annas_archive": false,
    "slub": false,
    "e_rara": true,
    "sbb_digital": false
  }
}
```

### 3. Provider Settings

Per-provider rate limiting, retry policies, download limits, and
quota management. Each provider key maps to a settings object:

```json
{
  "provider_settings": {
    "mdz": {
      "max_pages": 0,
      "network": {
        "delay_ms": 200,
        "jitter_ms": 100,
        "max_attempts": 25,
        "base_backoff_s": 1.5,
        "backoff_multiplier": 1.5,
        "max_backoff_s": 60.0,
        "timeout_s": 30
      }
    },
    "gallica": {
      "max_pages": 0,
      "network": {
        "delay_ms": 1500,
        "jitter_ms": 500,
        "max_attempts": 25,
        "base_backoff_s": 3.0,
        "backoff_multiplier": 1.8,
        "timeout_s": 30
      }
    },
    "annas_archive": {
      "max_pages": 0,
      "quota": {
        "enabled": true,
        "daily_limit": 875,
        "reset_hours": 24,
        "wait_for_reset": true
      },
      "network": {
        "delay_ms": 800,
        "jitter_ms": 300,
        "max_attempts": 5,
        "base_backoff_s": 2.0,
        "backoff_multiplier": 2.0,
        "max_backoff_s": 45.0,
        "timeout_s": 45
      }
    },
    "google_books": {
      "free_only": true,
      "prefer": "pdf",
      "allow_drm": false,
      "max_files": 3,
      "max_pages": 50,
      "network": {
        "delay_ms": 200,
        "jitter_ms": 100,
        "max_attempts": 25,
        "base_backoff_s": 1.2,
        "backoff_multiplier": 1.5,
        "timeout_s": 30,
        "circuit_breaker_enabled": true,
        "circuit_breaker_threshold": 2,
        "circuit_breaker_cooldown_s": 600.0
      }
    }
  }
}
```

**Network parameters** (all providers):

- `delay_ms`: minimum delay between requests (ms)
- `jitter_ms`: random jitter added to delay
- `max_attempts`: maximum retry attempts
- `base_backoff_s`: initial backoff duration (seconds)
- `backoff_multiplier`: multiplier for exponential backoff
- `max_backoff_s`: upper bound for backoff
- `timeout_s`: request timeout

**Circuit breaker** (optional, per-provider): automatically pauses
a provider after consecutive failures, then retries after a
cooldown period.

- `circuit_breaker_enabled`: enable/disable
- `circuit_breaker_threshold`: consecutive failures before pause
- `circuit_breaker_cooldown_s`: seconds before testing again

To handle repeated 429 errors from a provider, enable the circuit
breaker and set the threshold to 2--3 with a cooldown of
600--3,600 seconds.

**Quota parameters** (quota-limited providers only):

- `quota.enabled`: enable quota tracking
- `quota.daily_limit`: maximum downloads per reset period
- `quota.reset_hours`: hours until quota resets
- `quota.wait_for_reset`: if `true`, defer downloads when quota
  is exhausted; if `false`, fall back to alternative method

**Provider-specific limits**:

- `max_pages` / `max_images`: maximum page images per work
  (0 = unlimited). For Google Books, `max_pages` bounds the
  page-by-page image extraction fallback (default 50).
- `max_files`: maximum files per work (for Google Books, the
  direct PDF/EPUB download cap, distinct from `max_pages`)
- `free_only`: only download free/public domain works
- `prefer`: preferred format (`pdf` or `images`)
- `allow_drm`: whether to allow DRM-protected content

To adjust for slow providers, increase `delay_ms` and
`jitter_ms` (e.g., `delay_ms: 2000`, `jitter_ms: 500`).

### 4. Download Preferences

```json
{
  "download": {
    "resume_mode": "skip_if_has_objects",
    "prefer_pdf_over_images": true,
    "download_manifest_renderings": true,
    "max_renderings_per_manifest": 1,
    "rendering_mime_whitelist": [
      "application/pdf",
      "application/epub+zip"
    ],
    "overwrite_existing": false,
    "include_metadata": true,
    "allowed_object_extensions": [
      ".pdf", ".epub", ".jpg", ".jpeg",
      ".png", ".jp2", ".tif", ".tiff"
    ],
    "max_parallel_downloads": 4,
    "provider_concurrency": {
      "default": 2,
      "annas_archive": 1,
      "bnf_gallica": 1,
      "google_books": 1,
      "internet_archive": 3,
      "mdz": 2
    },
    "worker_timeout_s": 600
  }
}
```

- `resume_mode`: how to handle previously processed works
  - `skip_completed`: skip if `work.json` exists
  - `skip_if_has_objects`: skip if `objects/` has files
  - `resume_from_csv`: skip if `retrievable=True` in CSV
  - `reprocess_all`: always reprocess
- `prefer_pdf_over_images`: skip page images when PDF/EPUB
  available
- `download_manifest_renderings`: download PDFs/EPUBs linked in
  IIIF manifests
- `max_renderings_per_manifest`: maximum rendering files per
  manifest
- `rendering_mime_whitelist`: allowed MIME types for renderings
- `overwrite_existing`: overwrite existing files
- `include_metadata`: save metadata JSON files
- `allowed_object_extensions`: file extensions to download
- `max_parallel_downloads`: concurrent download workers
  (1 = sequential)
- `provider_concurrency`: per-provider concurrent download
  limits; prevents overwhelming rate-limited APIs
- `worker_timeout_s`: maximum seconds per download task

### 5. Download Budget Limits

```json
{
  "download_limits": {
    "total": {
      "images_gb": 100,
      "pdfs_gb": 50,
      "metadata_gb": 1
    },
    "per_work": {
      "images_gb": 5,
      "pdfs_gb": 3,
      "metadata_mb": 10
    },
    "on_exceed": "stop"
  }
}
```

- `total.*_gb`: maximum combined GB across all works
  (0 = unlimited)
- `per_work.*`: maximum per individual work
- `on_exceed`: action when limit exceeded (`skip`: skip file and
  continue; `stop`: abort processing)

### 6. Selection Strategy

```json
{
  "selection": {
    "strategy": "collect_and_select",
    "max_parallel_searches": 5,
    "provider_hierarchy": [
      "mdz", "bnf_gallica", "e_rara", "slub",
      "internet_archive", "annas_archive",
      "google_books", "wellcome", "loc", "europeana"
    ],
    "min_title_score": 85,
    "search_timeout_seconds": 60,
    "creator_weight": 0.2,
    "max_candidates_per_provider": 5,
    "download_strategy": "selected_only",
    "keep_non_selected_metadata": true
  }
}
```

- `strategy`:
  - `collect_and_select`: search all providers, score, rank,
    select best (recommended)
  - `sequential_first_hit`: search in order, stop at first
    match above threshold (faster)
- `max_parallel_searches`: concurrent provider searches
  (1 = sequential)
- `provider_hierarchy`: ordered list of preferred providers
- `min_title_score`: minimum fuzzy match score (0--100); use
  50--60 for multilingual/historical collections, 70--85 for
  modern English collections
- `search_timeout_seconds`: per-provider search timeout in
  seconds (default 60); a provider whose search exceeds it is
  logged at WARNING and dropped so one slow provider cannot stall
  the fan-out. `0` or `null` disables it (unbounded wait). Override
  per provider via `provider_settings.<key>.search_timeout_seconds`.
  Applies to both CLI and interactive runs (the config value is read
  live at search time); `--search-timeout` is a CLI-only per-run
  override
- `creator_weight`: weight of creator match in scoring
  (0.0--1.0)
- `max_candidates_per_provider`: limit search results per
  provider
- `download_strategy`: `selected_only` or `all`
- `keep_non_selected_metadata`: save metadata for non-selected
  candidates

With `max_parallel_searches: 5`, searching 5 providers completes
in approximately 1 second versus approximately 5 seconds
sequentially.

### 7. Naming Conventions

```json
{
  "naming": {
    "include_creator_in_work_dir": false,
    "include_year_in_work_dir": false,
    "title_slug_max_len": 80
  }
}
```

- `include_creator_in_work_dir`: include creator in folder name (opt-in;
  enabling it changes work-directory names for new runs, so directories of
  works downloaded earlier will not be matched by resume/skip-existing)
- `include_year_in_work_dir`: include publication year (opt-in; same caveat)
- `title_slug_max_len`: maximum title slug length

### 8. Direct IIIF Settings

```json
{
  "direct_iiif": {
    "enabled": true,
    "link_column": "direct_link",
    "check_link_column": true,
    "naming_template": "{provider}_{item_id}"
  }
}
```

- `enabled`: enable direct IIIF manifest downloads from CSV
  `direct_link` column
- `link_column`: CSV column containing IIIF manifest URLs
- `check_link_column`: also check the `link` column for IIIF
  manifest URLs
- `naming_template`: template for output file naming; available
  placeholders: `{entry_id}`, `{name}`, `{provider}`,
  `{item_id}`; page suffix (`_p00001.jpg`) appended
  automatically

## Output Structure

### Folder Layout

```
downloaded_works/
  index.csv
  <entry_id>_<work_name>/
    work.json
    metadata/
      <entry_id>_<work_name>_<provider>.json
      <entry_id>_<work_name>_<provider>_manifest.json
    objects/
      <entry_id>_<work_name>_<provider>.pdf
      <entry_id>_<work_name>_<provider>_image_001.jpg
```

**File naming**: strict `snake_case`. Images have 3-digit counters
(`_image_001.jpg`). Non-image files get numeric suffixes when
multiple exist (`_2.pdf`).

### Index File

`index.csv` is a thread-safe ledger with one row per work, keyed by
`work_id`: re-processing a work updates its row in place (upsert)
instead of appending duplicates, and the full column set is always
written.

| Column | Description |
|--------|-------------|
| `work_id` | Stable hash-based identifier (upsert key) |
| `entry_id` | Your CSV entry ID |
| `work_dir` | Path to work folder |
| `title` | Work title |
| `creator` | Creator/author |
| `selected_provider` | Provider display name |
| `selected_provider_key` | Provider key |
| `selected_source_id` | Provider's identifier |
| `selected_dir` | Download directory |
| `work_json` | Path to work.json |
| `item_url` | Public URL (landing page or manifest) |
| `status` | `completed`, `partial`, `failed`, `deferred`, `no_match` |
| `pages_expected` | Page count expected from the IIIF manifest |
| `pages_downloaded` | Pages actually downloaded |

A work is `completed` only when every expected page arrived;
page-level gaps yield `partial`, which is not treated as
retrievable and is picked up again on the next run (and by
`--verify`).

### work.json

Each work directory contains a `work.json` with input parameters,
current status, all search candidates with fuzzy match scores,
selection decision and reasoning, timestamps, and download
summary.

## Advanced Usage

### Parallel Downloads

When `max_parallel_downloads > 1`, downloads run concurrently
across works. The main thread searches providers and selects
candidates sequentially; worker threads execute downloads with
per-provider semaphores.

```json
{
  "download": {
    "max_parallel_downloads": 4,
    "provider_concurrency": {
      "default": 2,
      "annas_archive": 1,
      "internet_archive": 3
    }
  }
}
```

Start with 4 workers and adjust based on your provider mix. Lower
concurrency for rate-limited providers (Anna's Archive, BnF
Gallica), higher for generous ones (Internet Archive). Monitor
logs for 429 errors and adjust `provider_concurrency` accordingly.
Expected speedup: 2--4x for runs with 10+ works and multiple
providers.

### Large-Scale Processing

| Scale | Time | Success Rate | Approach |
|-------|------|-------------|----------|
| 1--10 items | 2--10 min | 70--80% | Quick validation |
| 11--25 items | 10--30 min | 65--75% | Single batch |
| 26--50 items | 30--90 min | 60--70% | Consider splitting |
| 51--100 items | 1--3 hours | 60--70% | Multiple batches |
| 100+ items | 3+ hours | 60--70% | Run overnight |

Test with 5--10 items first. Use `--dry-run` to verify matching
before downloading. Enable `skip_if_has_objects` resume mode for
safe interruption handling.

For very large jobs, split the CSV into batches and run multiple
instances with separate output directories:

```bash
python -m main.cli batch_000.csv --output_dir output_batch_0
python -m main.cli batch_001.csv --output_dir output_batch_1
```

### Quota and Deferred Downloads

Quota-limited providers (currently Anna's Archive, 875 fast
downloads/day) automatically defer downloads when the quota is
exhausted -- both when the local counter is spent and when the
provider's API itself reports a quota error. Deferred works are
marked `deferred` in the works CSV and persisted to the unified
state file; ready items are retried synchronously at the start of
the next run, and a successful retry updates the works CSV,
`work.json`, and `index.csv`. Quota units are only recorded when
the fast-download API was actually used (public-scraping fallbacks
do not consume quota).

The state file lives at `~/.chronodownloader/.downloader_state.json`
by default, so quota counters persist across working directories. A
legacy `.downloader_state.json` in the current directory is adopted
(copied) once automatically. Override the location with
`deferred.state_dir` (directory) or `deferred.state_file` (full
path) in the config.

```bash
# Check quota usage and deferred queue
python -m main.cli --quota-status

# Clean up completed items
python -m main.cli --cleanup-deferred
```

### Direct IIIF Downloads

Three entry points for downloading directly from IIIF manifest
URLs without provider search:

1. **CLI flag**: `--iiif URL` (repeatable for multiple manifests)
2. **CSV column**: `direct_link` with manifest URLs per row
3. **Interactive mode**: "Direct IIIF Download" option

### Identifier Lookup

Download by provider-specific identifier:

```bash
python -m main.cli --cli --id bsb11280551 --provider mdz
python -m main.cli --cli --id bsb11280551  # auto-detects MDZ
```

### Programmatic Usage

```python
from main.orchestration import (
    process_work,
    run_batch_downloads,
    load_enabled_apis,
    filter_enabled_providers_for_keys,
)
from main.data import load_works_csv, get_pending_works
from api.core.config import get_config

providers = load_enabled_apis("config.json")
providers = filter_enabled_providers_for_keys(providers)

# Single work
process_work(
    title="Tractatus de uino",
    creator="Anonymous",
    entry_id="6106",
    base_output_dir="downloaded_works",
    dry_run=False,
)

# Batch from CSV
works_df = load_works_csv("sampled_books.csv")
pending_df = get_pending_works(works_df)
stats = run_batch_downloads(
    works_df=pending_df,
    output_dir="downloaded_works",
    config=get_config(),
    dry_run=False,
    csv_path="sampled_books.csv",
)
```

### Monitoring and Recovery

```bash
# Check progress
cat ./downloads/index.csv

# Identify failures (PowerShell)
Import-Csv output/index.csv |
  Where-Object { $_.status -eq "failed" } |
  Select-Object entry_id, title
```

Recovery: check `index.csv` for failed items, review `work.json`
for failure reasons, adjust configuration (lower
`min_title_score`, change provider hierarchy), and re-run with the
same CSV. Resume mode automatically skips completed items.

## Architecture

```
ChronoDownloader/
|-- api/
|   |-- core/                  # Config, HTTP, rate limiting,
|   |   |                      # budget, context, naming,
|   |   |                      # file download + validation
|   |-- providers/             # 17 provider connectors
|   |   |-- _registry.py      # Provider dispatch table
|   |   |-- internet_archive.py
|   |   |-- bnf_gallica.py
|   |   `-- ...
|   |-- iiif/                  # IIIF manifest parsing,
|   |   |                      # strategies, renderings,
|   |   |                      # direct download
|   |-- model.py               # SearchResult, QuotaDeferredException
|   |-- matching.py            # Fuzzy matching / scoring
|   |-- query_helpers.py       # SRU / SPARQL escaping
|   `-- identifier_resolver.py # ID -> manifest URL mapping
|-- main/
|   |-- cli/                   # Entry point, argparse,
|   |   |                      # dispatch, subcommands
|   |-- ui/                    # Interactive workflow,
|   |   |                      # console output, mode detection
|   |-- orchestration/         # Pipeline, selection,
|   |   |                      # execution, scheduler
|   |-- state/                 # State store, quota tracking,
|   |   |                      # deferred queue, background retry
|   `-- data/                  # CSV I/O, index ledger,
|                              # work directory management
|-- tests/
|   |-- unit/
|   |-- integration/
|   `-- staging/
|-- config.json
|-- requirements.txt
`-- README.md
```

### Workflow

**Sequential mode** (`max_parallel_downloads = 1`):

```
Load CSV -> Parse rows
For each work:
  Search enabled providers -> Collect candidates
  Score (fuzzy matching) -> Rank (hierarchy + scores)
  Select best -> Create work dir -> Save work.json
  Download (budget check -> rate limit -> files -> metadata)
  Update index.csv
Summary report
```

**Parallel mode** (`max_parallel_downloads > 1`):

```
Load CSV -> Parse rows
SEARCH PHASE (main thread):
  For each work: search -> score -> select -> queue task
DOWNLOAD PHASE (worker threads):
  Acquire per-provider semaphore
  Download (rate limit -> budget check -> files)
  Update index.csv (thread-safe)
Summary report
```

## Migration

## FAQ

**Which providers should I enable?**

| Content Type | Primary | Secondary |
|-------------|---------|-----------|
| German | MDZ, DDB | Internet Archive, Google Books |
| French | BnF Gallica | Internet Archive, Google Books |
| Italian | MDZ, Internet Archive | Google Books |
| English | Internet Archive, Google Books | LoC, HathiTrust |
| Medical/Scientific | Wellcome | Internet Archive, MDZ |
| General/Mixed | Internet Archive, Google Books | MDZ, Gallica, LoC |

**How much does it cost?**

Most providers are free. API keys for Europeana, DPLA, DDB, and
Google Books require free registration. Anna's Archive membership
is optional and the only paid component.

**No items found for my works?**

Possible causes: missing API keys, network issues, provider API
changes, or titles that do not match. Lower `min_title_score`
(try 60) and test with `--log-level DEBUG` to inspect API
responses.

**Downloads are very slow?**

Enable parallel downloads (`max_parallel_downloads: 4`), use
`sequential_first_hit` strategy, enable only fast providers, or
increase `provider_concurrency` for high-throughput providers.

**Fuzzy matching is too strict or too loose?**

Adjust `min_title_score`: 50--60 for multilingual/historical
collections with variant spellings, 70--85 for modern English
collections with exact titles.

**BnF Gallica returns HTTP 403 on IIIF manifests?**

Gallica's IIIF manifest endpoint periodically blocks automated
requests server-side, returning 403 regardless of User-Agent. The
SRU search API continues to work. Retry later or use the standard
search-based workflow (omit `direct_link`), which falls back
gracefully when IIIF images are unavailable.

## Versioning

This project follows semantic versioning (`MAJOR.MINOR.PATCH`). The version in
`pyproject.toml` is the single source of truth; it is mirrored in the title
heading above and tagged in git as `vX.Y.Z`. The commit history was squashed to
a single baseline commit at v1.0.0 on 25 April 2026; version numbers before
v1.0.0 do not exist.

## Changelog

- **v1.12.2** (16 July 2026) -- Follow-up patch closing the low-severity
  items deferred from the v1.12.1 audit. `DownloadScheduler` no longer
  mutates the caller's `provider_limits` dict (it popped the `"default"` key
  in place; the mapping is now copied internally). The Library of Congress
  connector appends `fo=json` with the correct separator, so an item URL
  that already carries a query string no longer receives a second `?` that
  made LoC serve HTML instead of JSON. The interactive config-file picker
  reads the real `download_limits` config key (the former `budget` key never
  existed, so its note never displayed), and the interactive CSV wizard now
  accepts direct-link-only CSVs (no title column), matching the CLI batch
  handler and the interactive execution path itself. The budget byte
  pre-check against the encoded Content-Length was re-reviewed and left
  as-is: it is advisory only, and enforcement happens per decoded chunk
  during streaming. Five regression tests added (1,125 total).
- **v1.12.1** (16 July 2026) -- Bug-fix release from an automated audit,
  closing two defects. Content-encoded downloads (`Content-Encoding: gzip`/
  `deflate`/`br`) are no longer discarded as "incomplete": the byte-count
  completeness check compared the decoded stream length against the encoded
  wire `Content-Length`, so any compressed-and-length-declared response was
  rejected on every attempt; the check now applies only to identity-encoded
  responses. `escape_sparql_string` additionally escapes double quotes, so
  titles or creators containing `"` no longer break the British Library BNB
  SPARQL fallback query (which embeds them in double-quoted literals). Also
  strips a UTF-8 BOM from `.gitattributes` that made Git misparse the leading
  comment and warn `policy: is not a valid attribute name` on every
  attribute-touching operation. Two regression tests added (1,120 total).
- **v1.12.0** (16 July 2026) -- Per-provider search timeout. A slow provider
  can no longer stall the search fan-out: each provider search is bounded by
  `selection.search_timeout_seconds` (default 60; `0`/`null` disables),
  overridable per provider via
  `provider_settings.<key>.search_timeout_seconds` and per run via the new
  CLI flag `--search-timeout SECONDS`. The config value applies to CLI and
  interactive runs alike and is resolved live at search time. Providers that
  exceed their deadline are logged at WARNING and dropped; the parallel
  fan-out is bounded by the largest per-provider timeout, and both the fan-out
  and the sequential search paths now run provider searches on daemon worker
  threads (concurrency still capped by `max_parallel_searches`), so an
  abandoned search blocked in a slow HTTP call can never pin process exit. In
  a live test against 10 providers with an 8-second timeout, a query that
  previously took 651.6 seconds (SBB's SRU endpoint stalling in
  multi-minute retries) completed in 8.7 seconds with identical selection.
  Fifteen tests added, including subprocess-based regressions proving prompt
  interpreter exit (1,118 total).
- **v1.11.0** (16 July 2026) -- Search-only mode. New `--search TITLE`
  (with optional `--creator NAME`) runs the full discovery and matching
  pipeline for an ad hoc query and prints structured candidate metadata
  without downloading; new `--search-only` does the same for every titled row
  of a works CSV (ignoring resume status; `--entry-ids`/`--limit` narrow the
  set). With `--json`, output is one NDJSON line per work (query, status,
  selected candidate, and the full ranked candidate list with provider keys,
  source ids, IIIF manifests, and matching scores) with logs moved to stderr
  so stdout stays parseable; candidates chain directly into deterministic
  `--id`/`--iiif` downloads. Both forms are fully side-effect-free. Exit
  codes: 0 all matched, 1 some unmatched, 2 usage. Interactive mode gains a
  matching "Search Only" menu entry with a ranked candidate table.
  Implemented via a new `pipeline.search_work()`; 19 tests added (1,103
  total).
- **v1.10.0** (12 July 2026) -- Bug-fix release from an automated audit, closing
  seven defects. Anna's Archive quota tracking now activates whenever the
  provider is API-backed (config-based and remapped env-var keys included)
  instead of probing only the hardcoded `ANNAS_ARCHIVE_API_KEY` variable.
  Parallel-mode metadata writes no longer overwrite a candidate's
  `search_result` JSON after a worker-thread counter reset; `save_json` bumps
  the sequence until the path is free. `--config` defaults to the documented
  `CHRONO_CONFIG_PATH` environment variable before falling back to
  `config.json`, so CLI runs no longer clobber the override or load providers
  from the wrong file. Numeric `retrievable` CSV values (int64/float64 1/0)
  are classified completed/failed instead of pending, restoring resume
  behavior. The deferred queue no longer collapses distinct works that share
  an empty or default `entry_id`; deduplication now also matches on
  `source_id`. `QuotaManager.has_quota` honors legacy `daily_download_limit`
  configs whose `quota` block is absent. Internet Archive, Europeana, DDB, and
  Google Books strip embedded double quotes from titles and creators before
  building quoted query phrases. Nineteen regression tests added (1,084 total).
- **v1.9.1** (5 July 2026) -- Follow-up patch to v1.9.0. The
  `include_creator_in_work_dir` and `include_year_in_work_dir` naming options,
  made effective in v1.9.0, are now opt-in (default `false`) across code
  defaults, `config.example.json`, and the staging config: with the previous
  default of `true`, fresh runs would have produced creator-/year-suffixed
  work directories that resume and skip-existing checks could not match
  against corpora downloaded earlier. README documents the caveat. Also
  absorbs a pre-existing formatting reflow in `main/cli/entry.py` and syncs
  the `uv.lock` self-version. Full test suite green (1,065 tests).
- **v1.9.0** (5 July 2026) -- Bug-fix and quality release from a full code audit.
  Headline fix: providers can no longer report success with zero downloaded
  content. HathiTrust, Gallica, MDZ, British Library, BNE, DDB, Polona, and LOC
  previously returned `True` on metadata-only or failed downloads, permanently
  marking empty works "completed" in the ledger; success now reflects actual
  downloads, and thumbnail/cover-only results (Internet Archive, Google Books,
  Wellcome) no longer count as completed either. Further fixes: single-string
  Internet Archive creators are no longer character-split in ranking; parallel
  mode now leaves direct-IIIF "partial" results pending (matching sequential
  mode) and sequential mode now marks deferred works in the CSV; deferred-queue
  retries respect the fast-API quota guard and reuse the original work naming
  context; the deferred queue dedupes against "retrying" items; malformed
  `Content-Length` headers no longer crash downloads; Google Books page
  extraction uses its own `max_pages` setting (default 50) instead of
  `max_files`. Improvements: `include_creator_in_work_dir` and the
  `direct_iiif` link-column settings are now honored; `load_enabled_apis`
  falls back to `config.example.json` like the config loader; quota-deferred
  primaries now try ranked fallbacks before deferring; parallel fallback uses
  the hierarchy-ordered provider list; interactive mode accepts
  `direct_link`-only CSVs and reports single-work failures honestly; the dead
  background-scheduler daemon and the unused `year_tolerance`/`queue_size`
  config keys were removed; long Windows paths trigger an advisory warning.
  Full test suite green (1,065 tests); ruff and mypy clean.
- **v1.8.0** (4 July 2026) -- Dependency sweep: raise the urllib3 floor to >=2.7 and
  refresh the locked toolchain (6 packages upgraded). Full test suite green (1,065
  tests).
- **v1.7.1** (3 July 2026) -- CLI bug-fix release. `--non-interactive`
    without `--cli` no longer falls through to the interactive
    config-selection wizard (the flag now implies CLI mode at entry).
    Repeated `--iiif URL --name NAME` pairs are zipped positionally, so
    each manifest receives its own name instead of the last name winning
    for all; a mismatched count of names and URLs exits with a usage
    error. The Internet Archive identifier path resolves manifests via
    the live `iiif.archive.org` endpoint first, keeping the deprecated
    `iiif.archivelab.org` (broken TLS certificate) only as a fallback.

- **v1.7.0** (2 July 2026) -- Hardening release closing the data-integrity
    defects found in a full production audit. Downloads now stream to a
    `.part` file and are promoted atomically only after validation, so
    truncated transfers are discarded instead of being recorded as complete;
    `.pdf`/`.epub` files must carry correct magic bytes, and extension
    inference prefers the Content-Type header. The works CSV becomes the
    authoritative ledger: quota-deferred works are recorded with a new
    `deferred` status instead of `failed`, ready deferred items are retried
    synchronously at the start of every run (replacing the dead background
    daemon and its no-op prompt), and retry successes write through to
    work.json, index.csv, and the CSV. The state file and works CSV write
    atomically (corrupt state is preserved as `.corrupt` rather than silently
    resetting quota counters), and the state file moves to a user-level
    directory with a `deferred.state_dir` override and one-time legacy
    adoption. IIIF works record `pages_expected`/`pages_downloaded` and are
    marked `partial` unless complete, and a new `--verify` command audits
    existing corpora (size, magic bytes, page counts) and flips failures to
    `partial` for re-download. Matching semantics change deliberately:
    `min_title_score` now gates the pure title score, with creator similarity
    as a ranking bonus only, so perfect title matches lacking creator
    metadata are no longer rejected (recall-improving). The rate limiter and
    circuit breaker are thread-safe, downloads and 5xx/connection failures
    feed the breaker, fallback downloads acquire the actual provider's
    semaphore, and urllib3-internal status retries are removed. Anna's
    Archive gains the active `.gl` domain, host-relative link rebuilding,
    server-side quota deferral, and honest quota accounting. index.csv
    becomes an upsert-by-work_id snapshot with a fixed 14-column schema;
    budget accounting classifies bytes by type; `worker_timeout_s: 0` truly
    waits indefinitely. The CLI adopts the agent contract: exit codes
    0/1/2/130, a `--json` run summary, a non-TTY guard, `--non-interactive`,
    a side-effect-free `--dry-run`, and `--status`/`--quota-status` folded
    into argparse. Windows reserved device names are guarded, CSV reads use
    explicit UTF-8, ruff and mypy run clean with `verification/` excluded and
    pandas-stubs/types-requests added as dev dependencies.

- **v1.6.0** (28 June 2026) -- Move config.json to a user-local
    example/real split: a tracked `config.example.json` template ships
    with conservative default concurrency (3 parallel downloads, 2
    default provider slots), `config.json` is now gitignored and
    user-local, and the loader falls back to `config.example.json`
    automatically when `config.json` is absent, printing a one-line
    message. A fresh clone is immediately runnable on the example
    defaults; copy `config.example.json` to `config.json` to customize.
    `api_keys.example.json` ships alongside it with the default
    provider-to-env-var mapping.

- **v1.5.0** (28 June 2026) -- Add optional api_keys.json for per-provider
    API-key environment-variable remapping (backward-compatible). The file sits
    next to the resolved config.json and maps each key-bearing provider to the
    name of the environment variable holding its key; a missing file or entry
    falls back to the historical default name, so existing setups are unchanged.

- **v1.4.0** (21 June 2026) -- Adopted pandas 3.x and dropped Python 3.10 support.
    Raised `requires-python` to `>=3.11` (pandas 3.0 requires 3.11+), relaxed the
    pin from `pandas>=2.3,<3.0` to `pandas>=3.0` (`pandas` 2.3.3 -> 3.0.3), and set
    the ruff target to `py311`. The CSV index/works I/O paths pass all 1,020 tests
    under pandas 3.0. Note: Python 3.10 is no longer supported.

- **v1.3.0** (20 June 2026) -- Consolidated five within-module duplications behind
    new private helpers without changing any public interface or runtime behavior. In
    `selection.py` a `_search_provider_logged` helper now carries the shared search
    banner, search call, and empty/hit-count logging for both the sequential first-hit
    and exhaustive collectors. In `pipeline.py` the per-candidate search_result
    persistence loop moved into `_save_candidate_search_results`, called from both
    `_persist_candidates_metadata` and `process_work`. In `execution.py` the row field
    extraction and skip validation moved into `_parse_work_row`, shared by the
    sequential and parallel runners. In `iiif/_parsing.py` the v2 and v3 manifest
    traversals moved into `_iter_v2_resources` and `_iter_v3_bodies`, shared by
    `extract_image_service_bases` and `extract_direct_image_urls`. In
    `annas_archive.py` the raw-dict build, `SearchResult` conversion, and score
    attachment moved into `_build_annas_result`, used by both search strategies. No
    dead code was removed: the only `queue_file` occurrences are backward-compatible
    public parameters.

- **v1.2.0** (20 June 2026) -- Removed three confirmed-unused dev dependencies
    (`mypy`, `pandas-stubs`, and `types-requests`), which also dropped their
    transitive packages from the lockfile. Upgraded `requests` (2.33.1 to 2.34.2),
    `beautifulsoup4` (4.14.3 to 4.15.0), and `pytest` (9.0.3 to 9.1.1) to the latest
    stable releases within their current majors, raising each lower-bound floor
    accordingly. Held `pandas` at the 2.x major (current 2.3.3, latest 3.0.3) under
    the conservative majors-gated policy, retaining its `<3.0` pin; `urllib3` was
    already current within its major.

- **v1.1.1** (19 May 2026) -- Dependency refresh from environment-wide CVE audit:
    bump `urllib3` 2.6.3 -> 2.7.0 (audit-surface consolidation).

- **v1.1.0** (4 May 2026) -- Migrated dependency management to `pyproject.toml`
    and `uv`; added ruff linter and formatter configuration.

- **v1.0.0** (25 April 2026) -- Initial public release; squashed baseline.

## Contributing

Contributions are welcome. When reporting issues, include a clear
description, steps to reproduce, expected versus actual behavior,
environment details (OS, Python version), relevant config sections
(remove sensitive data), and log excerpts.

For code contributions:

1. Fork the repository and create a feature branch
2. Follow the existing code style and architecture
3. Add tests for new functionality
4. Update documentation
5. Test with multiple providers
6. Submit a pull request with a clear description

## License

MIT License. Copyright (c) 2025 Paul Goetz.
