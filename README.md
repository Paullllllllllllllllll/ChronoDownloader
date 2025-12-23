# ChronoDownloader

A comprehensive Python tool for discovering and downloading digitized historical sources from major digital libraries worldwide. ChronoDownloader automates the process of searching, selecting, and downloading historical books, manuscripts, and documents from 14+ digital library providers.

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Supported Providers](#supported-providers)
- [Quick Start](#quick-start)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Documentation](#documentation)
- [Output Structure](#output-structure)
- [Advanced Usage](#advanced-usage)
  - [Parallel Downloads](#parallel-downloads)
  - [Large-Scale Processing](#large-scale-processing)
- [Architecture](#architecture)
- [Extending the Tool](#extending-the-tool)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)

## Overview

ChronoDownloader is designed for researchers and archivists who need to discover and download historical materials from multiple digital libraries at scale. The tool provides intelligent candidate selection, fuzzy matching, and robust download management while respecting provider terms of service.
Meant to be used in conjunction with [ChronoMiner](https://github.com/Paullllllllllllllllll/ChronoMiner) and [ChronoTranscriber](https://github.com/Paullllllllllllllllll/ChronoTranscriber) for a full historical document retrieval, transcription and data extraction pipeline.

## Key Features

- Multi-Provider Search: Query 14 major digital libraries with configurable parallel searches (up to 5x faster) including Internet Archive, BnF Gallica, Library of Congress, Google Books, Anna's Archive, and more
- Parallel Downloads: Concurrent download workers with per-provider concurrency limits for 2-4x faster batch processing while respecting API rate limits
- Intelligent Selection: Automatic fuzzy matching and scoring to select the best candidate from multiple sources with configurable thresholds for multilingual collections
- Flexible Download Strategies: Download PDFs, EPUBs, or high-resolution page images based on availability and preferences
- IIIF Support: Native support for IIIF Presentation and Image APIs with optimized performance for faster downloads
- Budget Management: Content-type download budgets (images, PDFs, metadata) with simple GB-based limits
- Rate Limiting: Built-in per-provider rate limiting with exponential backoff to respect API quotas
- Adaptive Circuit Breaker: Automatically pauses providers that hit repeated 429s and retries after a configurable cooldown period
- Robust Error Handling: Automatic retries, fallback providers, and comprehensive logging
- Batch Processing: Process CSV files with hundreds or thousands of works efficiently with proven workflows for large-scale operations
- Dual-Mode Operation: Interactive guided workflow with colored console UI, or CLI automation for scripting and batch jobs
- Metadata Preservation: Save search results, manifests, and selection decisions for auditing
- Performance Optimizations: Internet Archive PDF-first strategy with IIIF fallback for 60% faster downloads
- Resume Modes: Continue interrupted downloads with configurable resume strategies (skip completed, skip if has objects, reprocess all)
- Unified CSV System: Single CSV file serves as both input and output, tracking download status and URLs for seamless integration with sampling workflows

## Supported Providers

ChronoDownloader currently supports the following digital library providers:

| Provider | Region | API Key Required | IIIF Support |
|----------|--------|------------------|-------------|
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
| Biblioteca Nacional de España | Spain | No | Yes |
| HathiTrust | US | Optional | Yes |
| Wellcome Collection | UK | No | Yes |
| Anna's Archive | Global (Aggregator) | Optional* | No |

\* Anna's Archive works without an API key using public download links. Member API key enables faster downloads.

## Quick Start

```bash
# Clone the repository
git clone <repository-url>
cd ChronoDownloader

# Install dependencies
pip install -r requirements.txt

# Set up API keys (if needed)
export EUROPEANA_API_KEY=your_key_here  # Linux/macOS
export GOOGLE_BOOKS_API_KEY=your_key_here

# Run in interactive mode (default)
python main/downloader.py

# Or run with a CSV file in CLI mode
python main/downloader.py --cli sample_works.csv --output_dir my_downloads

# Check results
ls my_downloads
```

For large-scale downloads (50+ items), see WORKFLOW_GUIDE.md for batch processing strategies, performance tuning, and quality control procedures.

## System Requirements

### Software Dependencies

- Python: 3.10 or higher (recommended: 3.11+)
- pip package manager
- Internet connection for API access

### Python Packages

All Python dependencies are listed in `requirements.txt` with pinned versions:

- `requests`: HTTP library for API calls
- `pandas`: CSV processing and data handling
- `beautifulsoup4`: HTML/XML parsing
- `urllib3`: HTTP client with retry support

Install all dependencies with `pip install -r requirements.txt`. The requirements file specifies exact versions to ensure reproducible environments.

## Installation

### Clone the Repository

```bash
git clone <repository-url>
cd ChronoDownloader
```

### Create a Virtual Environment

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux/macOS
python3 -m venv .venv
source .venv/bin/activate
```

### Install Python Dependencies

```bash
pip install -r requirements.txt
```

### Configure API Keys

Set API keys for providers that require them:

```bash
# Windows PowerShell
$env:EUROPEANA_API_KEY="your_europeana_key"
$env:DDB_API_KEY="your_ddb_key"
$env:DPLA_API_KEY="your_dpla_key"
$env:GOOGLE_BOOKS_API_KEY="your_google_books_key"
$env:ANNAS_ARCHIVE_API_KEY="your_annas_archive_key"  # Optional, for fast downloads

# Linux/macOS
export EUROPEANA_API_KEY=your_europeana_key
export DDB_API_KEY=your_ddb_key
export DPLA_API_KEY=your_dpla_key
export GOOGLE_BOOKS_API_KEY=your_google_books_key
export ANNAS_ARCHIVE_API_KEY=your_annas_archive_key  # Optional, for fast downloads
```

For persistent configuration, add environment variables to your system settings or shell profile.

### Getting API Keys

- **Europeana**: Register at [Europeana Pro](https://pro.europeana.eu/page/get-api)
- **DPLA**: Request at [DPLA API](https://pro.dp.la/developers/api-codex)
- **DDB**: Apply at [Deutsche Digitale Bibliothek](https://www.deutsche-digitale-bibliothek.de/content/api)
- **Google Books**: Get from [Google Cloud Console](https://console.cloud.google.com/apis/library/books.googleapis.com)
- **Anna's Archive** (Optional): Become a member at [Anna's Archive](https://annas-archive.org) for fast download access. Without an API key, the provider will use slower public download links.

### Verify Installation

```bash
python main/downloader.py --help
```

## Configuration

ChronoDownloader uses a JSON configuration file (`config.json` by default) to control all aspects of its behavior. You can specify an alternative config file using the `--config` flag or the `CHRONO_CONFIG_PATH` environment variable.

### Configuration Structure

The configuration file has seven main sections:

1. `general`: Global settings including interactive/CLI mode toggle
2. `providers`: Enable/disable specific providers
3. `provider_settings`: Per-provider rate limiting and behavior
4. `download`: Download preferences, parallel download settings, resume modes
5. `download_limits`: Budget constraints to prevent runaway downloads
6. `selection`: Candidate selection and matching strategy
7. `naming`: Output folder and file naming conventions

### 0. General Settings

```json
{
  "general": {
    "interactive_mode": true,
    "default_output_dir": "downloaded_works",
    "default_csv_path": "sample_works.csv"
  }
}
```

General Settings Parameters:

- `interactive_mode`: When `true`, launches interactive workflow with guided prompts. When `false`, uses CLI mode requiring command-line arguments.
- `default_output_dir`: Default output directory used in interactive mode
- `default_csv_path`: Default CSV file suggested in interactive mode

### 1. Enable/Disable Providers

```yaml
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

### 2. Provider Settings and Rate Limiting

Each provider can have custom rate limiting, retry policies, and download limits:

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

Network Policy Parameters:

- `delay_ms`: Minimum delay between requests in milliseconds
- `jitter_ms`: Random jitter added to delay (prevents thundering herd)
- `max_attempts`: Maximum retry attempts for failed requests
- `base_backoff_s`: Initial backoff duration for retries (seconds)
- `backoff_multiplier`: Multiplier for exponential backoff
- `timeout_s`: Request timeout in seconds
- `max_backoff_s`: Upper bound for exponential backoff (prevents multi-minute sleeps)
- `circuit_breaker_enabled`: Turn the circuit breaker on/off per provider (default: true)
- `circuit_breaker_threshold`: Consecutive failures (typically HTTP 429) before temporarily disabling the provider
- `circuit_breaker_cooldown_s`: Cooldown window before the provider is tested again in HALF_OPEN state

Provider-Specific Limits:

- `max_pages`: Maximum number of page images to download per work
- `max_images`: Alternative name for max_pages
- `max_files`: Maximum files to download per work
- `free_only`: Only download free/public domain works
- `prefer`: Preferred format (pdf or images)
- `allow_drm`: Whether to allow DRM-protected content
- `circuit_breaker_*`: Optional overrides for providers with strict quotas (e.g., Google Books)

#### Example: Google Books Hardened Settings

```json
"google_books": {
  "free_only": true,
  "prefer": "pdf",
  "allow_drm": false,
  "max_files": 1000,
  "network": {
    "delay_ms": 2000,
    "jitter_ms": 500,
    "max_attempts": 3,
    "base_backoff_s": 2.0,
    "backoff_multiplier": 2.0,
    "max_backoff_s": 30.0,
    "timeout_s": 30,
    "circuit_breaker_enabled": true,
    "circuit_breaker_threshold": 2,
    "circuit_breaker_cooldown_s": 600.0,
    "headers": { "Referer": "https://books.google.com/" }
  }
}
```

These values slow down request cadence, cap retries, and ensure the circuit breaker disables Google Books for ten minutes after two consecutive 429 responses, letting other providers finish uninterrupted.

#### Example: Anna's Archive with Quota Management

```json
"annas_archive": {
  "max_pages": 0,
  "daily_fast_download_limit": 10,
  "wait_for_quota_reset": true,
  "quota_reset_wait_hours": 24,
  "network": {
    "delay_ms": 800,
    "jitter_ms": 300,
    "max_attempts": 5,
    "base_backoff_s": 2.0,
    "backoff_multiplier": 2.0,
    "max_backoff_s": 45.0,
    "timeout_s": 45
  }
}
```

Anna's Archive supports two download modes: fast downloads with an API key (limited daily quota) and slower public scraping without a key. The `daily_fast_download_limit` tracks how many fast downloads have been used, and `wait_for_quota_reset` controls whether to defer or fall back to scraping when the quota is exhausted.

### 3. Download Preferences

```json
{
  "download": {
    "resume_mode": "skip_if_has_objects",
    "prefer_pdf_over_images": true,
    "download_manifest_renderings": true,
    "max_renderings_per_manifest": 1,
    "rendering_mime_whitelist": ["application/pdf", "application/epub+zip"],
    "overwrite_existing": false,
    "include_metadata": true,
    "allowed_object_extensions": [".pdf", ".epub", ".jpg", ".jpeg", ".png", ".jp2", ".tif", ".tiff"],
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

Download Preference Parameters:

- `resume_mode`: How to handle previously processed works. Options: `skip_completed` (skip if work.json exists), `skip_if_has_objects` (skip if objects/ folder has files), `resume_from_csv` (skip if retrievable=True in CSV), `reprocess_all` (always reprocess)
- `prefer_pdf_over_images`: Skip page images when PDF/EPUB is available (recommended: true for faster downloads, especially with Internet Archive)
- `download_manifest_renderings`: Download PDFs/EPUBs linked in IIIF manifests
- `max_renderings_per_manifest`: Maximum number of rendering files per manifest
- `rendering_mime_whitelist`: Allowed MIME types for renderings
- `overwrite_existing`: Whether to overwrite existing files
- `include_metadata`: Save metadata JSON files alongside downloads
- `allowed_object_extensions`: List of file extensions to download (e.g., [".pdf", ".epub", ".jpg"]). Files with other extensions are saved to metadata only if `save_disallowed_to_metadata` is true

Parallel Download Parameters:

- `max_parallel_downloads`: Number of concurrent download workers (default: 1 for sequential, set to 4 for parallel mode)
- `provider_concurrency`: Per-provider limits on concurrent downloads. Prevents overwhelming rate-limited APIs while maximizing throughput.
  - `default`: Default limit for providers not explicitly listed (default: 2)
  - Provider-specific limits: e.g., `annas_archive: 1` for strict quota, `internet_archive: 3` for higher throughput
- `worker_timeout_s`: Maximum seconds to wait for a single download to complete (default: 600)

### 4. Download Budget Limits

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

Budget Parameters:

- `total.images_gb`: Maximum combined GB of page images across all works (0 or missing = unlimited)
- `total.pdfs_gb`: Maximum combined GB of PDF downloads across all works
- `total.metadata_gb`: Maximum combined GB of metadata saved across all works
- `per_work.images_gb`: Maximum GB of images per individual work
- `per_work.pdfs_gb`: Maximum GB of PDFs per individual work
- `per_work.metadata_mb`: Maximum metadata size per work (in MB)
- `on_exceed`: Action when limit exceeded (skip: skip item and continue; stop: abort immediately)

### 5. Selection Strategy and Fuzzy Matching

```json
{
  "selection": {
    "strategy": "collect_and_select",
    "max_parallel_searches": 5,
    "provider_hierarchy": ["mdz", "bnf_gallica", "loc", "british_library", "internet_archive", "europeana"],
    "min_title_score": 85,
    "creator_weight": 0.2,
    "year_tolerance": 2,
    "max_candidates_per_provider": 5,
    "download_strategy": "selected_only",
    "keep_non_selected_metadata": true
  }
}
```

Selection Parameters:

- `strategy`: Selection strategy (collect_and_select or sequential_first_hit)
- `max_parallel_searches`: Number of concurrent provider searches (1 = sequential, >1 = parallel). Recommended: 3-6 for most setups. Set to 1 to disable parallel searches.
- `provider_hierarchy`: Ordered list of preferred providers
- `min_title_score`: Minimum fuzzy match score (0-100) to accept a candidate (configurable threshold for multilingual collections)
- `creator_weight`: Weight of creator match in scoring (0.0-1.0)
- `year_tolerance`: Reserved for future date-based matching
- `max_candidates_per_provider`: Limit search results per provider
- `download_strategy`: Download mode (selected_only or all)
- `keep_non_selected_metadata`: Save metadata for non-selected candidates

Selection Strategies:

**collect_and_select (recommended):**
1. Searches all enabled providers (in parallel when `max_parallel_searches > 1`)
2. Scores all candidates using fuzzy title/creator matching
3. Ranks candidates by provider hierarchy and quality signals
4. Selects the best match overall
5. Falls back to next-best if download fails

**sequential_first_hit:**
1. Searches providers in provider_hierarchy order (always sequential)
2. Stops at the first provider with an acceptable match
3. Faster but may miss better matches from lower-priority providers

**Parallel Search Performance:**
With `max_parallel_searches: 5`, searching 5 providers completes in ~1 second instead of ~5 seconds (sequential). This significantly speeds up processing for large CSV files while respecting per-provider rate limits. Each provider's backoff and retry logic operates independently.

Fuzzy Matching:

The tool uses token-set ratio matching to handle minor spelling variations, different word orders, punctuation differences, and subtitle variations. Scoring combines title similarity, creator similarity (weighted), and quality signals (IIIF availability, item URL). The min_title_score threshold is configurable and can be adjusted based on your collection: use lower values (50-60) for multilingual or historical collections with variant spellings, and higher values (70-85) for modern English-language collections where exact matching is expected.

### 6. CSV Format Requirements

ChronoDownloader expects CSV files in the sampling notebook format with fixed column names:

- `entry_id`: Unique identifier (required)
- `short_title`: Work title for search (required)
- `main_author`: Creator name (optional)
- `retrievable`: Download status - True/False/empty (automatically managed)
- `link`: Item URL (automatically populated)

Additional columns from the sampling notebook (e.g., full_title, primary_category, stratum_abbrev, selection_type, etc.) are preserved but not used by the downloader.

This standardized format enables seamless integration with the WhatForDinner bib_sampling.ipynb workflow, where the same CSV file can be used for sampling, downloading, and re-sampling failed items.

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

Naming Parameters:

- `include_creator_in_work_dir`: Include creator in folder name
- `include_year_in_work_dir`: Include publication year in folder name
- `title_slug_max_len`: Maximum length of title slug in filenames

## Documentation

ChronoDownloader includes comprehensive documentation for different use cases:

### Configuration Reference

**CONFIG_GUIDE.md** - Technical reference for all configuration options in config.json. Explains what each setting does, provides examples of common configurations, and details the download limits structure. Use this when you need to understand specific configuration parameters.

### Operational Workflows

**WORKFLOW_GUIDE.md** - Best practices guide for large-scale downloads (50+ items). Covers batch processing strategies, performance tuning, monitoring and recovery procedures, quality control, and storage management. Essential reading for production deployments and large collections.

### Quick Reference

For basic usage, see the Configuration and Usage sections in this README. For specialized use cases such as historical cookbooks or other domain-specific collections, check the respective directories for additional guides.

## Usage

### CSV Input Format

ChronoDownloader uses a unified CSV format compatible with sampling notebooks. The CSV serves as both input (defining works to download) and output (tracking download status).

Required columns:
- `entry_id`: Unique identifier for each work (must be present)
- `short_title`: Title of the work to search for

Optional columns:
- `main_author`: Creator/author name (improves matching accuracy)
- `retrievable`: Download status (True/False/empty). Automatically updated during downloads.
- `link`: Item URL (automatically populated after successful download)

The CSV may contain additional columns (e.g., full_title, primary_category, stratum_abbrev) which are preserved but not used by the downloader.

Example CSV (`sampled_books.csv`):

```csv
entry_id,short_title,main_author,retrievable,link
6106,Tractatus de uino,Anonymous,,
12495,Tractatus de praeparandis,Hier. Emser,,
12613,Hydrenogamia triumphans,Hippolyto Guarinonio,,
```

After processing, the CSV is updated in-place:

```csv
entry_id,short_title,main_author,retrievable,link
6106,Tractatus de uino,Anonymous,True,https://archive.org/details/...
12495,Tractatus de praeparandis,Hier. Emser,False,
12613,Hydrenogamia triumphans,Hippolyto Guarinonio,True,https://gallica.bnf.fr/ark:/...
```

### Basic Usage

```bash
# Process a CSV file
python main/downloader.py your_works.csv

# Custom output directory
python main/downloader.py my_books.csv --output_dir ./historical_sources

# Dry run to preview without downloading
python main/downloader.py my_books.csv --dry-run

# Verbose logging
python main/downloader.py my_books.csv --log-level DEBUG

# Custom configuration
python main/downloader.py my_books.csv --config config_small.json
```

### Command-Line Options

Positional:
- `csv_file`: Path to CSV file with works to download (required in CLI mode, optional in interactive mode)

Optional:
- `--output_dir DIR`: Output directory (default: downloaded_works)
- `--dry-run`: Search and score candidates without downloading
- `--log-level LEVEL`: Set logging verbosity (DEBUG, INFO, WARNING, ERROR, CRITICAL; default: INFO)
- `--config PATH`: Path to configuration JSON file (default: config.json)
- `--interactive`: Force interactive mode regardless of config setting
- `--cli`: Force CLI mode regardless of config setting

### Interactive Mode

When `interactive_mode` is `true` in config (default), running `python main/downloader.py` launches a guided workflow with a colored console interface:

**Workflow Steps:**

1. **Welcome Screen**: Displays tool banner and lists all enabled/disabled providers
2. **Mode Selection**: Choose between three download modes:
   - **CSV Batch**: Process multiple works from a CSV file
   - **Single Work**: Download a specific work by entering title and optional creator
   - **Predefined Collection**: Select from CSV files in the current directory or `collections/` folder
3. **Source Configuration**: Based on selected mode, specify CSV path, enter work details, or pick a collection
4. **Output Settings**: Configure output directory (defaults to `downloaded_works`)
5. **Options**: Set dry-run mode and logging level
6. **Confirmation**: Review all settings and confirm before processing
7. **Processing Summary**: View results with success/failure counts on completion

**Interactive Mode Features:**

- **Colored Console UI**: ANSI color support for better readability (Windows 10+ supported)
- **Provider Status Display**: Visual indicator of which providers are enabled/disabled
- **CSV Discovery**: Automatically lists available CSV files in the current directory
- **CSV Validation**: Validates CSV format and ensures required columns are present
- **Navigation**: Back/quit options at each step for easy workflow control
- **Input Validation**: Helpful error messages for invalid inputs (missing files, bad CSV format)
- **Default Values**: Uses `default_output_dir` and `default_csv_path` from config

**Force Interactive Mode:**
```bash
python main/downloader.py --interactive
```

### CLI Mode

When `interactive_mode` is `false` or using `--cli` flag, requires command-line arguments:

```bash
# Basic usage
python main/downloader.py --cli sample_works.csv

# With options
python main/downloader.py --cli works.csv --output_dir results --dry-run --log-level DEBUG
```

### Programmatic Usage

You can use ChronoDownloader as a Python library:

```python
from main import pipeline
from main.unified_csv import load_works_csv, get_pending_works
from api.core.config import get_config

# Load and configure providers
providers = pipeline.load_enabled_apis("config.json")
providers = pipeline.filter_enabled_providers_for_keys(providers)
pipeline.ENABLED_APIS = providers

# Process a single work
pipeline.process_work(
    title="Tractatus de uino",
    creator="Anonymous",
    entry_id="6106",
    base_output_dir="downloaded_works",
    dry_run=False,
)

# Process works from CSV (unified CSV system)
from main.execution import run_batch_downloads

csv_path = "sampled_books.csv"
works_df = load_works_csv(csv_path)
pending_df = get_pending_works(works_df)  # Only process pending items

stats = run_batch_downloads(
    works_df=pending_df,
    output_dir="downloaded_works",
    config=get_config(),
    dry_run=False,
    csv_path=csv_path,  # Updates status back to CSV
    )
```

## Output Structure

### Folder Structure

For each work in your CSV, a dedicated folder is created:

```
downloaded_works/<entry_id>_<work_name>/
  work.json
  metadata/
    <entry_id>_<work_name>_<provider>.json
    <entry_id>_<work_name>_<provider>_2.json
  objects/
    <entry_id>_<work_name>_<provider>.pdf
    <entry_id>_<work_name>_<provider>_2.pdf
    <entry_id>_<work_name>_<provider>_image_001.jpg
    <entry_id>_<work_name>_<provider>_image_002.jpg
```

### File Naming Conventions

All filenames follow a consistent pattern:

- `<entry_id>_<work_name>_<provider>.<ext>` for single files
- `<entry_id>_<work_name>_<provider>_image_001.jpg` for page images
- `<entry_id>_<work_name>_<provider>_2.pdf` for multiple files of same type

Naming Rules:

- All names use strict snake_case formatting
- entry_id, work_name, and provider are always included
- Images always have 3-digit counters (001, 002)
- Non-image files get numeric suffixes only when multiple exist

### Index File

A master index is maintained at `downloaded_works/index.csv` with columns:

- `work_id`: Stable hash-based identifier
- `entry_id`: Your CSV entry ID
- `work_dir`: Path to work folder
- `title`: Work title
- `creator`: Creator/author
- `selected_provider`: Provider name used
- `selected_provider_key`: Provider key
- `selected_source_id`: Provider's identifier for the work
- `selected_dir`: Download directory
- `work_json`: Path to work.json metadata file
 - `item_url`: Best-effort public URL for the selected item (typically a landing page; in some cases this may be an IIIF manifest URL)
 - `status`: Final status of the work (`completed`, `failed`, `deferred`)

If `item_url` is missing for a given row, check the corresponding `work_json` file. The detailed candidate list stored there typically contains `item_url` and/or `iiif_manifest` links for provenance and reproducibility.

#### Backfilling item_url for older index.csv files

If you have an `index.csv` created before `item_url` was added, you can backfill it from the per-work `work.json` files.

Run this from the repository root (with your virtual environment activated):

```bash
python -c "import json, os; import pandas as pd; p='downloaded_works/index.csv'; df=pd.read_csv(p); item_urls=[]; \
\nfor _, r in df.iterrows():\
    wjp=str(r.get('work_json') or ''); u=None;\
    try:\
        if wjp and os.path.exists(wjp):\
            w=json.load(open(wjp,'r',encoding='utf-8'));\
            sel=w.get('selected') or {};\
            sid=sel.get('source_id'); pk=sel.get('provider_key');\
            for c in w.get('candidates') or []:\
                if c.get('source_id')==sid and c.get('provider_key')==pk:\
                    u=c.get('item_url') or c.get('iiif_manifest');\
                    break\
    except Exception:\
        u=None\
    item_urls.append(u)\
\ndf['item_url']=item_urls; df.to_csv(p,index=False); print('Backfilled item_url for', sum(1 for x in item_urls if isinstance(x,str) and x.strip()), 'rows')"
```

### Metadata Files

**work.json** contains:

- Input parameters (title, creator, entry_id)
- All search candidates from all providers
- Fuzzy match scores for each candidate
- Selection decision and reasoning
- Timestamp

**Provider metadata files** (in metadata/) contain:

- Original API responses
- IIIF manifests
- Search result details

This comprehensive metadata enables auditing selection decisions, debugging failed downloads, reprocessing with different strategies, and academic citation and provenance tracking.

## Advanced Usage

### Parallel Downloads

ChronoDownloader supports parallel downloads to significantly speed up batch processing. When `max_parallel_downloads` is greater than 1, downloads run concurrently across multiple works while searches remain sequential.

**How it works:**
1. The main thread searches providers and selects candidates sequentially
2. Download tasks are queued and executed by a pool of worker threads
3. Per-provider semaphores limit concurrent downloads to each provider
4. Thread-safe operations protect shared resources (index.csv, deferred downloads)

**Configuration example for parallel downloads:**

```json
{
  "download": {
    "max_parallel_downloads": 4,
    "provider_concurrency": {
      "default": 2,
      "annas_archive": 1,
      "internet_archive": 3
    },
    "worker_timeout_s": 600
  }
}
```

**Performance tips:**
- Start with 4 workers and adjust based on your network and provider mix
- Set lower concurrency limits for rate-limited providers (Anna's Archive, BnF Gallica)
- Set higher limits for providers with generous rate limits (Internet Archive)
- Monitor logs for 429 errors and adjust `provider_concurrency` accordingly
- Use `max_parallel_downloads: 1` for sequential mode (original behavior)

**Expected speedup:** 2-4x faster for typical runs with 10+ works and multiple providers.

### Large-Scale Processing

For very large jobs (thousands of works):

1. Split your CSV into smaller batches:
   ```python
   import pandas as pd
   
   df = pd.read_csv("large_works.csv")
   batch_size = 100
   
   for i in range(0, len(df), batch_size):
       batch = df.iloc[i:i+batch_size]
       batch.to_csv(f"batch_{i//batch_size:03d}.csv", index=False)
   ```

2. Run multiple processes in parallel (different terminals):
   ```bash
   # Terminal 1
   python main/downloader.py batch_000.csv --output_dir output_batch_0
   
   # Terminal 2
   python main/downloader.py batch_001.csv --output_dir output_batch_1
   ```

3. Monitor progress using the index.csv files:
   ```python
   import pandas as pd
   import glob
   
   indices = [pd.read_csv(f) for f in glob.glob("output_*/index.csv")]
   combined = pd.concat(indices, ignore_index=True)
   print(f"Total works processed: {len(combined)}")
   print(f"By provider: {combined['selected_provider'].value_counts()}")
   ```

### Custom Provider Configuration

Create specialized configs for different use cases:

**config_fast.json** (prioritize speed):

```json
{
  "providers": {
    "internet_archive": true,
    "google_books": true,
    "bnf_gallica": false
  },
  "selection": {
    "strategy": "sequential_first_hit",
    "min_title_score": 75
  },
  "download": {
    "prefer_pdf_over_images": true
  }
}
```

**config_quality.json** (prioritize quality):

```json
{
  "providers": {
    "bnf_gallica": true,
    "loc": true,
    "british_library": true,
    "mdz": true
  },
  "selection": {
    "strategy": "collect_and_select",
    "min_title_score": 90,
    "provider_hierarchy": ["mdz", "bnf_gallica", "loc", "british_library"]
  },
  "download": {
    "prefer_pdf_over_images": false
  }
}
```

### Dry Run Analysis

Use dry runs to analyze what would be downloaded:

```bash
python main/downloader.py sample_works.csv --dry-run --log-level INFO > analysis.log
```

Then analyze the results:

```python
import pandas as pd
import json
import glob

work_files = glob.glob("downloaded_works/*/work.json")
results = []

for wf in work_files:
    with open(wf) as f:
        data = json.load(f)
        results.append({
            "title": data["input"]["title"],
            "selected_provider": data["selected"]["provider"] if data["selected"] else None,
            "num_candidates": len(data["candidates"]),
            "best_score": max([c["scores"]["total"] for c in data["candidates"]]) if data["candidates"] else 0
        })

df = pd.DataFrame(results)
print(df.describe())
print(df["selected_provider"].value_counts())
```

## Architecture

ChronoDownloader follows a modular architecture for maintainability and extensibility.

### Directory Structure

```
ChronoDownloader/
├── api/                          # Provider connectors and core utilities
│   ├── core/                     # Core infrastructure modules
│   │   ├── config.py            # Configuration management with caching
│   │   ├── network.py           # HTTP session, rate limiting, retries
│   │   ├── context.py           # Thread-local work/provider tracking
│   │   ├── naming.py            # Filename sanitization and work directory naming
│   │   └── budget.py            # Download budget enforcement
│   ├── providers.py             # Provider registry
│   ├── model.py                 # SearchResult dataclass
│   ├── matching.py              # Fuzzy matching algorithms
│   ├── iiif.py                  # IIIF manifest parsing and image downloads
│   ├── download_helpers.py      # Shared download patterns for providers
│   ├── utils.py                 # File download and utilities (backward-compatible facade)
│   ├── query_helpers.py         # Query string escaping for SRU/SPARQL
│   └── <provider>_api.py        # Individual provider connectors
├── main/                         # CLI and orchestration
│   ├── pipeline.py              # Core orchestration logic (search, select, download phases)
│   ├── selection.py             # Candidate collection, scoring, and selection
│   ├── download_scheduler.py    # Parallel download scheduler with per-provider limits
│   ├── mode_selector.py         # Mode detection (interactive vs CLI)
│   ├── interactive.py           # Interactive workflow UI
│   └── downloader.py            # Unified entry point (CLI + interactive)
├── config.json                   # Main configuration file
├── requirements.txt              # Python dependencies
├── sample_works.csv              # Example input
└── README.md                     # This file
```

### Key Components

**Provider Connectors** (`api/*_api.py`):
- Each provider has a dedicated module
- Implements `search_<provider>()` and `download_<provider>_work()` functions
- Returns SearchResult objects for uniform handling

**Core Infrastructure** (`api/core/`):
- `network.py`: Centralized HTTP with per-provider rate limiting, exponential backoff, retry logic
- `config.py`: Configuration loading with caching, environment variable support (CHRONO_CONFIG_PATH), and defaults
- `budget.py`: Download budget tracking at multiple levels (global, per-work, per-provider)
- `context.py`: Thread-local state for work/provider tracking and file sequencing
- `naming.py`: Consistent filename sanitization, snake_case conversion, and work directory naming

**Orchestration** (`main/`):
- `pipeline.py`: Provider loading, API key validation, work directory creation, metadata persistence, download coordination with fallback. Provides `search_and_select()` and `execute_download()` functions for parallel mode.
- `selection.py`: Candidate collection strategies (sequential/collect-and-select), fuzzy matching scoring, best candidate selection
- `download_scheduler.py`: ThreadPoolExecutor-based parallel download scheduler with per-provider semaphores, graceful shutdown, and progress callbacks
- `mode_selector.py`: Dual-mode detection (interactive vs CLI) based on config and CLI flags
- `interactive.py`: Interactive workflow UI with guided prompts, navigation, and session management
- `downloader.py`: Unified entry point routing to interactive or CLI handlers, supports both sequential and parallel download modes

**Data Models** (`api/model.py`):
- SearchResult: Unified search result format with provider metadata
- Conversion utilities for legacy dict-based results

**Shared Utilities**:
- `iiif.py`: IIIF Presentation v2/v3 manifest parsing, Image API URL generation, image download helpers
- `download_helpers.py`: Common download patterns (PDF-first strategies, IIIF manifest + images)
- `matching.py`: Token-set ratio fuzzy matching, text normalization, combined scoring
- `utils.py`: Backward-compatible facade re-exporting core functionality, file download with budget checks, JSON persistence

### Workflow

**Sequential Mode** (max_parallel_downloads = 1):
```
1. Load CSV → Parse rows
                ↓
2. For each work:
   ├─→ Search all enabled providers
   ├─→ Collect candidates (SearchResult objects)
   ├─→ Score candidates (fuzzy matching)
   ├─→ Rank by provider hierarchy + scores
   ├─→ Select best candidate
   ├─→ Create work directory
   ├─→ Save work.json metadata
   ├─→ Download from selected provider
   │   ├─→ Check budget limits
   │   ├─→ Apply rate limiting
   │   ├─→ Download PDFs/EPUBs (if available)
   │   ├─→ Download page images (if needed)
   │   └─→ Save metadata
   └─→ Update index.csv
                ↓
3. Complete → Summary report
```

**Parallel Mode** (max_parallel_downloads > 1):
```
1. Load CSV → Parse rows
                ↓
2. SEARCH PHASE (main thread):
   For each work:
   ├─→ Search all enabled providers
   ├─→ Collect and score candidates
   ├─→ Select best candidate
   ├─→ Create work directory + work.json
   └─→ Queue DownloadTask to worker pool
                ↓
3. DOWNLOAD PHASE (worker threads):
   Worker pool executes tasks concurrently:
   ├─→ Acquire per-provider semaphore
   ├─→ Set thread-local context
   ├─→ Download from selected provider
   │   ├─→ Apply rate limiting
   │   ├─→ Check budget limits
   │   └─→ Try fallback providers if needed
   ├─→ Update index.csv (thread-safe)
   └─→ Release semaphore
                ↓
4. Complete → Summary report with stats
```

### Scalability and Reliability

**Rate Limiting:**
- Per-provider rate limiters with configurable delays and jitter
- Per-provider semaphores limit concurrent downloads in parallel mode
- Prevents overwhelming provider servers
- Respects API quotas and terms of service

**Retry Logic:**
- Exponential backoff for transient errors (429, 5xx)
- Configurable max attempts per provider
- Explicit handling of Retry-After headers

**Budget Management:**
- Pre-flight checks before downloads
- Real-time tracking of files and bytes
- Multi-level limits (global, per-work, per-provider)
- Configurable policies (skip vs stop)

**Error Handling:**
- Graceful degradation with fallback providers
- Comprehensive logging at all levels
- Non-blocking errors (continue processing)
- Metadata preservation even on download failure

## Extending the Tool

### Adding a New Provider

1. Create a new API module (`api/my_provider_api.py`):

```python
from typing import List, Optional
from .model import SearchResult
from .utils import download_file, make_request
import logging

logger = logging.getLogger(__name__)

def search_my_provider(
    title: str,
    creator: Optional[str] = None,
    max_results: int = 5
) -> List[SearchResult]:
    """Search My Provider for works matching title/creator."""
    api_url = "https://api.myprovider.com/search"
    params = {"q": title, "limit": max_results}
    if creator:
        params["creator"] = creator
    
    data = make_request(api_url, params=params)
    if not data or "results" not in data:
        return []
    
    results = []
    for item in data["results"]:
        results.append(SearchResult(
            provider="My Provider",
            provider_key="my_provider",
            title=item.get("title", "N/A"),
            creators=item.get("authors", []),
            date=item.get("year"),
            source_id=item.get("id"),
            item_url=item.get("url"),
            iiif_manifest=item.get("manifest"),
            raw=item
        ))
    
    return results

def download_my_provider_work(
    result: SearchResult,
    output_dir: str
) -> bool:
    """Download a work from My Provider."""
    try:
        pdf_url = result.raw.get("pdf_url")
        if pdf_url:
            return download_file(pdf_url, output_dir, "document.pdf")
        return True
    except Exception as e:
        logger.error("Download failed: %s", e)
        return False
```

2. Register in `api/providers.py`:

```python
from . import my_provider_api

PROVIDERS: Dict[str, Tuple[Any, Any, str]] = {
    "my_provider": (
        my_provider_api.search_my_provider,
        my_provider_api.download_my_provider_work,
        "My Provider"
    ),
}
```

3. Add to configuration (`config.json`):

```json
{
  "providers": {
    "my_provider": true
  },
  "provider_settings": {
    "my_provider": {
      "max_pages": 100,
      "network": {
        "delay_ms": 500,
        "jitter_ms": 200,
        "max_attempts": 5,
        "base_backoff_s": 1.5,
        "backoff_multiplier": 1.5,
        "timeout_s": 30
      }
    }
  }
}
```

4. Add host mapping (if needed) in `api/core/network.py`:

```python
PROVIDER_HOST_MAP: Dict[str, tuple[str, ...]] = {
    "my_provider": ("api.myprovider.com", "myprovider.com"),
}
```

### Customizing Matching Logic

Edit `api/matching.py` to adjust fuzzy matching:

```python
def combined_match_score(
    query_title: str,
    item_title: str,
    query_creator: Optional[str] = None,
    creators: Optional[List[str]] = None,
    creator_weight: float = 0.2,
    method: str = "token_set"
) -> float:
    # Customize scoring logic here
```

## Troubleshooting

### Common Issues

#### No providers are enabled

Cause: All providers disabled in config or missing API keys

Solution: Check `config.json` and ensure at least one provider is enabled. Set required API keys as environment variables.

#### SSL certificate verification failed

Cause: SSL certificate issues with provider

Solution: Add `"ssl_error_policy": "retry_insecure_once"` to provider's network config (use cautiously)

#### 429 Too Many Requests

Cause: Hitting provider rate limits

Solution: Increase `delay_ms` and `jitter_ms` in provider settings. The tool will automatically back off, but higher initial delays prevent the issue.

#### No items found for all works

Cause: API keys missing, network issues, or provider API changes

Solution:

- Verify API keys are set correctly
- Test with `--log-level DEBUG` to see detailed API responses
- Check if provider APIs are accessible

#### Download budget exhausted

Cause: Reached configured download limits

Solution: Adjust limits in `config.json` under `download_limits`, or set to 0 for unlimited

#### Downloads are very slow

Cause: Conservative rate limiting, slow providers, or sequential mode

Solution:

- Enable parallel downloads: set `max_parallel_downloads: 4` in config
- Reduce `delay_ms` in provider settings (respect terms of service)
- Use `sequential_first_hit` strategy for faster selection
- Enable only fast providers (Internet Archive, Google Books)
- Increase `provider_concurrency` for providers with generous rate limits

#### Title column not found in CSV

Cause: CSV missing required column or encoding issues

Solution: Ensure CSV has a column named exactly `Title` (case-sensitive). Check file encoding (should be UTF-8).

### Debug Mode

Enable detailed logging:

```bash
python main/downloader.py sample_works.csv --log-level DEBUG > debug.log 2>&1
```

This captures all API requests and responses, rate limiting delays, budget checks, matching scores, and download attempts.

### Testing Individual Providers

Test a single provider:

```json
{
  "providers": {
    "internet_archive": true
  }
}
```

```bash
python main/downloader.py sample_works.csv --config test_config.json --dry-run
```

## Contributing

Contributions are welcome! Here's how you can help improve ChronoDownloader:

### Reporting Issues

When reporting bugs or issues, please include:

- Description: Clear description of the problem
- Steps to Reproduce: Detailed steps to reproduce the issue
- Expected Behavior: What you expected to happen
- Actual Behavior: What actually happened
- Environment: OS, Python version, relevant package versions
- Configuration: Relevant sections from your config files (remove sensitive information)
- Logs: Relevant log excerpts showing the error

### Suggesting Features

Feature suggestions are appreciated. Please provide:

- Use Case: Describe the problem or need
- Proposed Solution: Your idea for addressing it
- Alternatives: Other approaches you've considered
- Impact: Who would benefit and how

### Code Contributions

If you'd like to contribute code:

1. Fork the repository and create a feature branch
2. Follow the existing code style and architecture patterns
3. Add tests for new functionality where applicable
4. Update documentation including this README and inline comments
5. Test thoroughly with multiple providers
6. Submit a pull request with a clear description of your changes

### Development Guidelines

- Modularity: Keep functions focused and modules organized
- Error Handling: Use try-except blocks with informative error messages
- Logging: Use the logger for debugging information
- Configuration: Use JSON configuration files rather than hardcoding values
- User Experience: Provide clear prompts and feedback
- Documentation: Update docstrings and README for any interface changes

### Areas for Contribution

Potential areas where contributions would be valuable:

- New Providers: Add connectors for additional digital libraries
- Enhanced Matching: Improve fuzzy matching algorithms
- Performance: Optimize for very large datasets
- Testing: Add unit and integration tests
- Documentation: Improve provider-specific documentation
- UI: Create a web interface or GUI

## License

MIT License

Copyright (c) 2025 Paul Goetz

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Acknowledgments

This tool interfaces with the following digital libraries: Internet Archive, Bibliothèque nationale de France (BnF Gallica), Library of Congress, Google Books, Europeana, Digital Public Library of America (DPLA), Deutsche Digitale Bibliothek (DDB), British Library, Münchener DigitalisierungsZentrum (MDZ), Polona (National Library of Poland), Biblioteca Nacional de España (BNE), HathiTrust Digital Library, and Wellcome Collection.

Please respect each provider's terms of service and rate limits.