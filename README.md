# ChronoDownloader v1.0.0

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
- [Migration](#migration)
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

**Requirements**: Python 3.10+ (3.11+ recommended), pip, internet
connection.

```bash
git clone https://github.com/Paullllllllllllllllll/ChronoDownloader.git
cd ChronoDownloader

# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux / macOS

pip install -r requirements.txt
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

### Verify Installation

```bash
python -m main.cli --help
python -m main.cli --list-providers
```


## Quick Start

### Interactive Mode

```bash
python -m main.cli
```

The guided workflow walks through mode selection (CSV batch, single
work, predefined collection, or direct IIIF), source configuration,
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
Force it explicitly with `--cli` or `interactive_mode: false` in
config.

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

- `main_author`: creator/author name (improves matching)
- `direct_link`: IIIF manifest URL for direct download (bypasses
  search)
- `retrievable`: download status (`True`/`False`/empty,
  automatically updated)
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
- `--interactive` / `--cli` -- force execution mode

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

- `--quota-status` -- display quota usage, deferred queue, and
  background scheduler status
- `--cleanup-deferred` -- remove completed items from deferred
  queue

**Processing scope**:

- `--pending-mode {all,new,failed}` -- which rows to process
- `--entry-ids IDS` -- restrict to specific entry IDs
  (repeatable, comma-separated)
- `--limit N` -- process at most N filtered rows

**Runtime config overrides**:

- `--resume-mode {skip_completed,reprocess_all,skip_if_has_objects,resume_from_csv}`
- `--selection-strategy {collect_and_select,sequential_first_hit}`
- `--min-title-score FLOAT` -- fuzzy match threshold (0--100)
- `--creator-weight FLOAT` -- author match weight (0.0--1.0)
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

ChronoDownloader uses a JSON configuration file (`config.json` by
default). Override with `--config` or the `CHRONO_CONFIG_PATH`
environment variable.

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
  (0 = unlimited)
- `max_files`: maximum files per work
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
    "creator_weight": 0.2,
    "year_tolerance": 2,
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
- `creator_weight`: weight of creator match in scoring
  (0.0--1.0)
- `year_tolerance`: reserved for future date-based matching
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
    "include_creator_in_work_dir": true,
    "include_year_in_work_dir": true,
    "title_slug_max_len": 80
  }
}
```

- `include_creator_in_work_dir`: include creator in folder name
- `include_year_in_work_dir`: include publication year
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

`index.csv` is a thread-safe ledger tracking all processed works:

| Column | Description |
|--------|-------------|
| `work_id` | Stable hash-based identifier |
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
| `status` | `completed`, `failed`, `deferred`, `no_match` |

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
exhausted. Deferred items are persisted to
`.downloader_state.json` and retried automatically by a background
scheduler when quotas reset.

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

The `python main/downloader.py` entry point and all pre-refactor
import paths were removed on 2026-04-24. Use
`python -m main.cli` instead. See
[MIGRATION.md](MIGRATION.md) for the complete old-to-new
import-path table.


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

This project uses semantic versioning. The commit history was
squashed to a single baseline commit at v1.0.0 on 25 April 2026.
All prior development history was consolidated; version numbers
before v1.0.0 do not exist.


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

MIT License. Copyright (c) 2025 Paul Goetz. See
[LICENSE](LICENSE) for the full text.
