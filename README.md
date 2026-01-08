# ChronoDownloader

A Python tool for discovering and downloading digitized historical sources from major digital libraries worldwide. ChronoDownloader automates searching, selecting, and downloading historical books, manuscripts, and documents from 14+ digital library providers.

Designed to integrate with [ChronoTranscriber](https://github.com/Paullllllllllllllllll/ChronoTranscriber) and [ChronoMiner](https://github.com/Paullllllllllllllllll/ChronoMiner) for a complete document retrieval, transcription, and data extraction pipeline.

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Supported Providers](#supported-providers)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
  - [First-Time Setup](#first-time-setup)
  - [Your First Download](#your-first-download)
  - [Common Workflows](#common-workflows)
- [Configuration](#configuration)
- [Usage](#usage)
- [Advanced Features](#advanced-features)
- [Architecture](#architecture)
- [Frequently Asked Questions](#frequently-asked-questions)
- [Contributing](#contributing)
- [Development](#development)
- [License](#license)

## Overview

ChronoDownloader enables researchers and archivists to discover and download historical materials from multiple digital libraries at scale. The tool provides intelligent candidate selection, fuzzy matching, and robust download management while respecting provider terms of service.

### Execution Modes

ChronoDownloader supports two execution modes:

- **Interactive Mode**: Guided workflow with colored console UI, provider status display, CSV discovery, navigation options (back/quit), and input validation. Ideal for exploratory work and first-time users.
- **CLI Mode**: Command-line arguments for automation, scripting, and batch jobs. Set `interactive_mode: false` in `config.json` or use `--cli` flag.

### Key Capabilities

- **Multi-Provider Search**: Query 14 major digital libraries with configurable parallel searches
- **Parallel Downloads**: Concurrent download workers with per-provider concurrency limits
- **Intelligent Selection**: Automatic fuzzy matching and scoring to select best candidate
- **Flexible Strategies**: Download PDFs, EPUBs, or high-resolution page images
- **IIIF Support**: Native support for IIIF Presentation and Image APIs
- **Budget Management**: Content-type download budgets (images, PDFs, metadata) with GB-based limits
- **Rate Limiting**: Built-in per-provider rate limiting with exponential backoff
- **Adaptive Circuit Breaker**: Automatically pauses providers hitting repeated 429s
- **Robust Error Handling**: Automatic retries, fallback providers, comprehensive logging
- **Batch Processing**: Process CSV files with hundreds or thousands of works efficiently
- **Metadata Preservation**: Save search results, manifests, selection decisions for auditing
- **Resume Modes**: Continue interrupted downloads (skip completed, skip if has objects, reprocess all)
- **Unified CSV System**: Single CSV file as both input and output, tracking download status

## Key Features

### Multi-Provider Search

- **14 Supported Providers**: Internet Archive, BnF Gallica, Library of Congress, Google Books, Europeana, DPLA, DDB, British Library, MDZ, Polona, BNE, HathiTrust, Wellcome Collection, Anna's Archive
- **Parallel Searches**: Configurable concurrent provider searches (up to 5x faster)
- **Unified Results**: SearchResult objects for consistent handling across providers

### Download Management

- **Parallel Downloads**: Concurrent workers (default: 4) with per-provider semaphores
- **Multiple Formats**: PDFs, EPUBs, page images (JPG, PNG, JP2, TIFF)
- **IIIF Integration**: Native IIIF Presentation v2/v3 manifest parsing and Image API
- **Smart Strategies**: PDF-first with IIIF fallback for 60% faster downloads

### Selection and Matching

- **Fuzzy Matching**: Token-set ratio matching for spelling variations, word order, punctuation
- **Configurable Thresholds**: Adjustable min_title_score (50-85) for different collections
- **Provider Hierarchy**: Ordered list of preferred providers
- **Quality Signals**: IIIF availability, item URL presence weighted in scoring

### Budget and Rate Limiting

- **Multi-Level Budgets**: Total and per-work limits for images, PDFs, metadata
- **Per-Provider Rate Limits**: Configurable delays, jitter, backoff, timeouts
- **Circuit Breaker**: Automatic provider pause after consecutive 429s with cooldown
- **Exponential Backoff**: Intelligent retry logic for transient errors

### Reliability Features

- **Resume Modes**: Skip completed, skip if has objects, resume from CSV, reprocess all
- **Fallback Providers**: Automatic fallback to next-best candidate on failure
- **Metadata Preservation**: Save candidates, scores, selection reasoning even on failure
- **Thread-Safe Operations**: Protected index.csv writes, deferred downloads

## Supported Providers

ChronoDownloader supports 14 digital library providers:

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

## System Requirements

### Software Dependencies

- **Python**: 3.10 or higher (recommended: 3.11+)
- **pip** package manager
- Internet connection for API access

### Python Packages

All dependencies in `requirements.txt` with pinned versions:

- HTTP: `requests`, `urllib3`
- Data: `pandas`, `beautifulsoup4`

Install with `pip install -r requirements.txt`. Exact versions ensure reproducible environments.

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
$env:ANNAS_ARCHIVE_API_KEY="your_annas_archive_key"  # Optional

# Linux/macOS
export EUROPEANA_API_KEY=your_europeana_key
export DDB_API_KEY=your_ddb_key
export DPLA_API_KEY=your_dpla_key
export GOOGLE_BOOKS_API_KEY=your_google_books_key
export ANNAS_ARCHIVE_API_KEY=your_annas_archive_key  # Optional
```

For persistent configuration, add to system environment variables or shell profile.

### Getting API Keys

- **Europeana**: Register at [Europeana Pro](https://pro.europeana.eu/page/get-api)
- **DPLA**: Request at [DPLA API](https://pro.dp.la/developers/api-codex)
- **DDB**: Apply at [Deutsche Digitale Bibliothek](https://www.deutsche-digitale-bibliothek.de/content/api)
- **Google Books**: Get from [Google Cloud Console](https://console.cloud.google.com/apis/library/books.googleapis.com)
- **Anna's Archive** (Optional): Become a member at [Anna's Archive](https://annas-archive.org) for fast downloads

### Verify Installation

```bash
python main/downloader.py --help
```

## Quick Start

### First-Time Setup

**Step 1: Install Dependencies**

```bash
git clone <repository-url>
cd ChronoDownloader

python -m venv .venv
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # Linux/macOS

pip install -r requirements.txt
```

**Step 2: Set Up API Keys (optional)**

```bash
# Windows PowerShell
$env:EUROPEANA_API_KEY="your_key_here"

# Linux/macOS
export EUROPEANA_API_KEY="your_key_here"
```

Many providers work without API keys (Internet Archive, BnF Gallica, Library of Congress).

**Step 3: Prepare CSV File**

Create CSV with required columns:

```csv
entry_id,short_title,main_author
1,Tractatus de vino,Anonymous
2,De re coquinaria,Apicius
```

**Step 4: Configure (optional)**

Edit `config.json` to enable/disable providers, set rate limits, configure parallel downloads.

### Your First Download

**Interactive Mode (Recommended)**

```bash
python main/downloader.py
```

The interface guides you through:
1. Mode selection (CSV batch, single work, predefined collection)
2. Source configuration (CSV path, work details, collection selection)
3. Output settings (directory configuration)
4. Options (dry-run, logging level)
5. Confirmation
6. Processing summary

**CLI Mode (Automation)**

```bash
# Process CSV file
python main/downloader.py sample_works.csv

# Custom output directory
python main/downloader.py my_books.csv --output_dir ./historical_sources

# Dry run to preview
python main/downloader.py my_books.csv --dry-run

# Custom configuration
python main/downloader.py my_books.csv --config config_small.json
```

### Common Workflows

**Workflow 1: Quick Test with Single Work**

Interactive mode → Single Work → Enter title and creator → Process

**Workflow 2: Large-Scale CSV Processing**

```bash
# Step 1: Process CSV (parallel downloads enabled in config)
python main/downloader.py large_collection.csv --output_dir ./downloads

# Step 2: Monitor progress via index.csv
cat ./downloads/index.csv

# Step 3: Resume if interrupted (automatically skips completed works)
python main/downloader.py large_collection.csv --output_dir ./downloads
```

**Workflow 3: Dry Run Analysis**

```bash
# Preview what would be downloaded
python main/downloader.py sample_works.csv --dry-run

# Analyze results from work.json files
ls downloaded_works/*/work.json
```

**Workflow 4: Provider-Specific Configuration**

Create specialized configs:
- `config_fast.json`: Internet Archive + Google Books only
- `config_quality.json`: European libraries with high-quality scans
- `config_public.json`: No API key providers only

```bash
python main/downloader.py books.csv --config config_fast.json
```

## Configuration

ChronoDownloader uses a JSON configuration file (`config.json` by default). Specify alternative config with `--config` flag or `CHRONO_CONFIG_PATH` environment variable.

### Configuration Structure

Seven main sections:
1. `general`: Global settings including interactive/CLI mode toggle
2. `providers`: Enable/disable specific providers
3. `provider_settings`: Per-provider rate limiting and behavior
4. `download`: Download preferences, parallel settings, resume modes
5. `download_limits`: Budget constraints
6. `selection`: Candidate selection and matching strategy
7. `naming`: Output folder and file naming conventions

### 1. General Settings

```json
{
  "general": {
    "interactive_mode": true,
    "default_output_dir": "downloaded_works",
    "default_csv_path": "sample_works.csv"
  }
}
```

**Parameters**:
- `interactive_mode`: `true` = interactive workflow, `false` = CLI mode
- `default_output_dir`: Default output directory (interactive mode)
- `default_csv_path`: Default CSV file (interactive mode)

### 2. Enable/Disable Providers

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
    "annas_archive": false
  }
}
```

### 3. Provider Settings and Rate Limiting

Per-provider rate limiting, retry policies, download limits, and quota management:

```json
{
  "provider_settings": {
    "mdz": {
      "max_pages": 0,
      "_quota_note": "No daily quota - unlimited downloads with rate limiting only",
      "network": {
        "delay_ms": 300,
        "jitter_ms": 150,
        "max_attempts": 25,
        "base_backoff_s": 1.5,
        "backoff_multiplier": 1.5,
        "max_backoff_s": 60.0,
        "timeout_s": 30
      }
    },
    "gallica": {
      "max_pages": 15,
      "_quota_note": "No daily quota - unlimited downloads with rate limiting only",
      "network": {
        "delay_ms": 1500,
        "jitter_ms": 400,
        "max_attempts": 25,
        "base_backoff_s": 1.5,
        "backoff_multiplier": 1.6,
        "timeout_s": 30
      }
    },
    "annas_archive": {
      "max_pages": 0,
      "quota": {
        "enabled": true,
        "daily_limit": 875,
        "reset_hours": 24,
        "wait_for_reset": true,
        "_note": "Anna's Archive fast download API has a daily quota (member feature)"
      },
      "network": {
        "delay_ms": 800,
        "jitter_ms": 300,
        "max_attempts": 5,
        "base_backoff_s": 2.0,
        "backoff_multiplier": 2.0,
        "max_backoff_s": 45.0,
        "timeout_s": 45,
        "_note": "Rate limiting delays (applies to all requests, separate from quota)"
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

#### Quota vs Rate Limiting

ChronoDownloader distinguishes between two types of download restrictions:

**Quota Limits** (Daily/Hourly Hard Caps):
- **Anna's Archive**: 875 fast downloads per day (member API feature)
- When quota is exhausted, downloads are **deferred** to a persistent queue
- Background scheduler automatically retries when quota resets
- Deferred queue automatically cleans up completed/failed items older than 7 days
- Configure with `quota` section in provider settings
- Use `--quota-status` to check current quota usage and deferred items

**Rate Limiting** (Request Delays):
- **All providers**: Configurable delays, jitter, exponential backoff
- **MDZ, Gallica, etc.**: Unlimited downloads with rate limiting only
- No deferrals - downloads continue with appropriate delays
- Configure with `network` section in provider settings

**Quota Parameters** (for quota-limited providers only):
- `quota.enabled`: Enable quota tracking for this provider
- `quota.daily_limit`: Maximum downloads per reset period
- `quota.reset_hours`: Hours until quota resets (typically 24)
- `quota.wait_for_reset`: If true, defer downloads when quota exhausted; if false, fallback to alternative download method

**Network Parameters** (all providers):
- `delay_ms`: Minimum delay between requests (milliseconds)
- `jitter_ms`: Random jitter added to delay
- `max_attempts`: Maximum retry attempts
- `base_backoff_s`: Initial backoff duration (seconds)
- `backoff_multiplier`: Multiplier for exponential backoff
- `timeout_s`: Request timeout
- `max_backoff_s`: Upper bound for exponential backoff
- `circuit_breaker_enabled`: Enable/disable circuit breaker
- `circuit_breaker_threshold`: Consecutive failures before disabling provider
- `circuit_breaker_cooldown_s`: Cooldown before testing provider again

**Provider-Specific Limits**:
- `max_pages`: Maximum page images per work
- `max_images`: Alternative name for max_pages
- `max_files`: Maximum files per work
- `free_only`: Only download free/public domain works
- `prefer`: Preferred format (pdf or images)
- `allow_drm`: Whether to allow DRM-protected content

### 4. Download Preferences

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

**Download Parameters**:
- `resume_mode`: How to handle previously processed works
  - `skip_completed`: Skip if work.json exists
  - `skip_if_has_objects`: Skip if objects/ folder has files
  - `resume_from_csv`: Skip if retrievable=True in CSV
  - `reprocess_all`: Always reprocess
- `prefer_pdf_over_images`: Skip page images when PDF/EPUB available (recommended: true)
- `download_manifest_renderings`: Download PDFs/EPUBs linked in IIIF manifests
- `max_renderings_per_manifest`: Maximum rendering files per manifest
- `rendering_mime_whitelist`: Allowed MIME types for renderings
- `overwrite_existing`: Overwrite existing files
- `include_metadata`: Save metadata JSON files
- `allowed_object_extensions`: File extensions to download

**Parallel Download Parameters**:
- `max_parallel_downloads`: Concurrent download workers (default: 1 sequential, set to 4 for parallel)
- `provider_concurrency`: Per-provider concurrent download limits
  - `default`: Default limit for unlisted providers
  - Provider-specific limits prevent overwhelming rate-limited APIs
- `worker_timeout_s`: Maximum seconds per download (default: 600)

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

**Budget Parameters**:
- `total.images_gb`: Maximum combined GB of page images (0 = unlimited)
- `total.pdfs_gb`: Maximum combined GB of PDFs
- `total.metadata_gb`: Maximum combined GB of metadata
- `per_work.images_gb`: Maximum GB of images per work
- `per_work.pdfs_gb`: Maximum GB of PDFs per work
- `per_work.metadata_mb`: Maximum metadata size per work (MB)
- `on_exceed`: Action when limit exceeded (`skip`: skip and continue; `stop`: abort)

### 6. Selection Strategy and Fuzzy Matching

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

**Selection Parameters**:
- `strategy`: Selection strategy
  - `collect_and_select`: Search all providers, score, rank, select best (recommended)
  - `sequential_first_hit`: Search in order, stop at first match (faster but may miss better matches)
- `max_parallel_searches`: Concurrent provider searches (1 = sequential, >1 = parallel)
- `provider_hierarchy`: Ordered list of preferred providers
- `min_title_score`: Minimum fuzzy match score (0-100) to accept candidate
- `creator_weight`: Weight of creator match in scoring (0.0-1.0)
- `year_tolerance`: Reserved for future date-based matching
- `max_candidates_per_provider`: Limit search results per provider
- `download_strategy`: Download mode (`selected_only` or `all`)
- `keep_non_selected_metadata`: Save metadata for non-selected candidates

**Fuzzy Matching**: Token-set ratio matching handles spelling variations, word order, punctuation, subtitles. Adjustable `min_title_score`: use lower (50-60) for multilingual/historical collections, higher (70-85) for modern English collections.

**Parallel Search Performance**: With `max_parallel_searches: 5`, searching 5 providers completes in ~1 second vs ~5 seconds sequential. Significantly speeds up large CSV processing while respecting per-provider rate limits.

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

**Naming Parameters**:
- `include_creator_in_work_dir`: Include creator in folder name
- `include_year_in_work_dir`: Include publication year in folder name
- `title_slug_max_len`: Maximum title slug length

## Usage

### CSV Input Format

ChronoDownloader uses unified CSV format compatible with sampling notebooks. CSV serves as both input and output, tracking download status.

**Required columns**:
- `entry_id`: Unique identifier (must be present)
- `short_title`: Title to search for

**Optional columns**:
- `main_author`: Creator/author name (improves matching)
- `retrievable`: Download status (True/False/empty, automatically updated)
- `link`: Item URL (automatically populated after download)

Additional columns preserved but not used.

**Example CSV**:

```csv
entry_id,short_title,main_author,retrievable,link
6106,Tractatus de uino,Anonymous,,
12495,Tractatus de praeparandis,Hier. Emser,,
12613,Hydrenogamia triumphans,Hippolyto Guarinonio,,
```

After processing:

```csv
entry_id,short_title,main_author,retrievable,link
6106,Tractatus de uino,Anonymous,True,https://archive.org/details/...
12495,Tractatus de praeparandis,Hier. Emser,False,
12613,Hydrenogamia triumphans,Hippolyto Guarinonio,True,https://gallica.bnf.fr/ark:/...
```

### Basic Usage

```bash
# Process CSV file
python main/downloader.py your_works.csv

# Custom output directory
python main/downloader.py my_books.csv --output_dir ./historical_sources

# Dry run to preview
python main/downloader.py my_books.csv --dry-run

# Verbose logging
python main/downloader.py my_books.csv --log-level DEBUG

# Custom configuration
python main/downloader.py my_books.csv --config config_small.json
```

### Command-Line Options

**Positional**:
- `csv_file`: Path to CSV file (required in CLI mode, optional in interactive)

**Optional**:
- `--output_dir DIR`: Output directory (default: downloaded_works)
- `--dry-run`: Search and select, create folders and work.json, skip downloads
- `--log-level LEVEL`: Logging verbosity (DEBUG, INFO, WARNING, ERROR, CRITICAL; default: INFO)
- `--config PATH`: Path to configuration JSON file (default: config.json)
- `--interactive`: Force interactive mode
- `--cli`: Force CLI mode
- `--quota-status`: Display quota usage and deferred queue status, then exit
- `--cleanup-deferred`: Remove completed items from deferred queue, then exit

### Interactive Mode

When `interactive_mode: true` in config, `python main/downloader.py` launches guided workflow:

**Workflow Steps**:
1. **Welcome Screen**: Tool banner, enabled/disabled providers
2. **Mode Selection**: CSV Batch, Single Work, or Predefined Collection
3. **Source Configuration**: CSV path, work details, or collection selection
4. **Output Settings**: Configure output directory
5. **Options**: Dry-run, logging level
6. **Confirmation**: Review settings
7. **Processing Summary**: Success/failure counts

**Interactive Features**:
- **Colored Console UI**: ANSI color support (Windows 10+)
- **Provider Status Display**: Visual indicator of enabled/disabled providers
- **CSV Discovery**: Lists available CSV files
- **CSV Validation**: Validates format and required columns
- **Navigation**: Back/quit options at each step
- **Input Validation**: Helpful error messages
- **Default Values**: Uses config defaults

**Force Interactive**:
```bash
python main/downloader.py --interactive
```

### CLI Mode

When `interactive_mode: false` or using `--cli` flag:

```bash
# Basic usage
python main/downloader.py --cli sample_works.csv

# With options
python main/downloader.py --cli works.csv --output_dir results --dry-run --log-level DEBUG
```

### Quota and Deferred Queue Management

ChronoDownloader provides built-in tools for monitoring quota usage and managing deferred downloads.

**Check Quota Status**:

```bash
python main/downloader.py --quota-status
```

Displays:
- Current quota usage for quota-limited providers (e.g., Anna's Archive: 48/875 used)
- Deferred queue statistics (pending, completed, failed items)
- Background scheduler status
- Next retry time for deferred downloads

**Clean Up Deferred Queue**:

```bash
python main/downloader.py --cleanup-deferred
```

Removes completed items from the deferred queue. The queue automatically cleans items older than 7 days.

**How Deferred Downloads Work**:

1. When a quota-limited provider (e.g., Anna's Archive) exhausts its daily quota, downloads are deferred instead of failing
2. Deferred items are saved to persistent queue (survives script restarts)
3. Background scheduler automatically retries when quotas reset
4. You can continue processing other works while deferred downloads wait
5. Run `--quota-status` to see when deferred items will be retried

**State Persistence**:

All quota and deferred queue state is stored in `.downloader_state.json`:
- Quota usage tracking per provider
- Deferred download queue with retry metadata
- Automatic migration from legacy state files on first run

### Programmatic Usage

```python
from main import pipeline
from main.unified_csv import load_works_csv, get_pending_works
from api.core.config import get_config

# Load providers
providers = pipeline.load_enabled_apis("config.json")
providers = pipeline.filter_enabled_providers_for_keys(providers)
pipeline.ENABLED_APIS = providers

# Process single work
pipeline.process_work(
    title="Tractatus de uino",
    creator="Anonymous",
    entry_id="6106",
    base_output_dir="downloaded_works",
    dry_run=False,
)

# Process CSV
from main.execution import run_batch_downloads

csv_path = "sampled_books.csv"
works_df = load_works_csv(csv_path)
pending_df = get_pending_works(works_df)

stats = run_batch_downloads(
    works_df=pending_df,
    output_dir="downloaded_works",
    config=get_config(),
    dry_run=False,
    csv_path=csv_path,
)
```

### Output Structure

**Folder Structure**:

```
downloaded_works/<entry_id>_<work_name>/
  work.json
  metadata/
    <entry_id>_<work_name>_<provider>.json
  objects/
    <entry_id>_<work_name>_<provider>.pdf
    <entry_id>_<work_name>_<provider>_image_001.jpg
```

**File Naming**:
- `<entry_id>_<work_name>_<provider>.<ext>` for single files
- `<entry_id>_<work_name>_<provider>_image_001.jpg` for page images
- `<entry_id>_<work_name>_<provider>_2.pdf` for multiple files

All names use strict snake_case. Images have 3-digit counters. Non-image files get numeric suffixes when multiple exist.

**Index File** (`downloaded_works/index.csv`):

Columns:
- `work_id`: Stable hash-based identifier
- `entry_id`: Your CSV entry ID
- `work_dir`: Path to work folder
- `title`: Work title
- `creator`: Creator/author
- `selected_provider`: Provider name
- `selected_provider_key`: Provider key
- `selected_source_id`: Provider's identifier
- `selected_dir`: Download directory
- `work_json`: Path to work.json
- `item_url`: Public URL for item (landing page or IIIF manifest)
- `status`: Final status (`completed`, `failed`, `deferred`, `no_match`)

Thread-safe writes, schema-tolerant appending.

**Metadata Files**:

`work.json` contains:
- Input parameters (title, creator, entry_id)
- Current status (pending → completed/failed/deferred/no_match)
- All search candidates from all providers
- Fuzzy match scores
- Selection decision and reasoning
- Timestamps (created_at, updated_at)
- Download summary (provider/source_id)

Provider metadata files (in metadata/) contain original API responses, IIIF manifests, search result details.

## Advanced Features

### Parallel Downloads

Parallel downloads significantly speed up batch processing. When `max_parallel_downloads > 1`, downloads run concurrently across multiple works.

**How it works**:
1. Main thread searches providers and selects candidates sequentially
2. Download tasks queued and executed by worker thread pool
3. Per-provider semaphores limit concurrent downloads per provider
4. Thread-safe operations protect shared resources (index.csv, deferred downloads)

Provider searches within a single work can run in parallel via `selection.max_parallel_searches`.

**Configuration example**:

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

**Performance tips**:
- Start with 4 workers, adjust based on network/provider mix
- Lower concurrency for rate-limited providers (Anna's Archive, BnF Gallica)
- Higher limits for providers with generous rate limits (Internet Archive)
- Monitor logs for 429 errors, adjust `provider_concurrency` accordingly
- Use `max_parallel_downloads: 1` for sequential mode

**Expected speedup**: 2-4x faster for typical runs with 10+ works and multiple providers.

### Large-Scale Processing

For very large jobs (thousands of works):

**1. Split CSV into batches**:

```python
import pandas as pd

df = pd.read_csv("large_works.csv")
batch_size = 100

for i in range(0, len(df), batch_size):
    batch = df.iloc[i:i+batch_size]
    batch.to_csv(f"batch_{i//batch_size:03d}.csv", index=False)
```

**2. Run multiple processes in parallel**:

```bash
# Terminal 1
python main/downloader.py batch_000.csv --output_dir output_batch_0

# Terminal 2
python main/downloader.py batch_001.csv --output_dir output_batch_1
```

**3. Monitor progress**:

```python
import pandas as pd
import glob

indices = [pd.read_csv(f) for f in glob.glob("output_*/index.csv")]
combined = pd.concat(indices, ignore_index=True)
print(f"Total works processed: {len(combined)}")
print(f"By provider: {combined['selected_provider'].value_counts()}")
```

### Custom Provider Configuration

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

```bash
python main/downloader.py sample_works.csv --dry-run --log-level INFO > analysis.log
```

Analyze results:

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
│   ├── core/                     # Core infrastructure
│   │   ├── config.py            # Configuration management
│   │   ├── network.py           # HTTP session, rate limiting, retries
│   │   ├── context.py           # Thread-local work/provider tracking
│   │   ├── naming.py            # Filename sanitization
│   │   └── budget.py            # Download budget enforcement
│   ├── providers.py             # Provider registry
│   ├── model.py                 # SearchResult dataclass
│   ├── matching.py              # Fuzzy matching algorithms
│   ├── iiif.py                  # IIIF manifest parsing
│   ├── download_helpers.py      # Shared download patterns
│   ├── utils.py                 # File download and utilities
│   ├── query_helpers.py         # Query string escaping
│   └── <provider>_api.py        # Individual provider connectors
├── main/                         # CLI and orchestration
│   ├── pipeline.py              # Core orchestration
│   ├── selection.py             # Candidate collection, scoring, selection
│   ├── download_scheduler.py    # Parallel download scheduler
│   ├── mode_selector.py         # Mode detection
│   ├── interactive.py           # Interactive workflow UI
│   └── downloader.py            # Unified entry point
├── config.json                   # Main configuration
├── requirements.txt              # Python dependencies
└── README.md                     # This file
```

### Key Components

**Provider Connectors** (`api/*_api.py`):
- Dedicated module per provider
- Implements `search_<provider>()` and `download_<provider>_work()`
- Returns SearchResult objects

**Core Infrastructure** (`api/core/`):
- `network.py`: Centralized HTTP with per-provider rate limiting, exponential backoff, retry logic
- `config.py`: Configuration loading with caching, environment variable support
- `budget.py`: Multi-level download budget tracking
- `context.py`: Thread-local state for work/provider tracking
- `naming.py`: Consistent filename sanitization, snake_case conversion

**Orchestration** (`main/`):
- `pipeline.py`: Provider loading, API key validation, work directory creation, download coordination
- `selection.py`: Candidate collection strategies, fuzzy matching scoring, best candidate selection
- `download_scheduler.py`: ThreadPoolExecutor-based parallel scheduler with per-provider semaphores
- `mode_selector.py`: Dual-mode detection (interactive vs CLI)
- `interactive.py`: Interactive workflow UI
- `downloader.py`: Unified entry point

**Data Models** (`api/model.py`):
- SearchResult: Unified search result format
- Conversion utilities for legacy dict-based results

**Shared Utilities**:
- `iiif.py`: IIIF Presentation v2/v3 parsing, Image API URL generation
- `download_helpers.py`: Common download patterns
- `matching.py`: Token-set ratio fuzzy matching, text normalization
- `utils.py`: Backward-compatible facade, file download with budget checks

### Workflow

**Sequential Mode** (max_parallel_downloads = 1):
```
1. Load CSV → Parse rows
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
   │   ├─→ Download PDFs/EPUBs
   │   ├─→ Download page images
   │   └─→ Save metadata
   └─→ Update index.csv
3. Complete → Summary report
```

**Parallel Mode** (max_parallel_downloads > 1):
```
1. Load CSV → Parse rows
2. SEARCH PHASE (main thread):
   For each work:
   ├─→ Search all enabled providers
   ├─→ Collect and score candidates
   ├─→ Select best candidate
   ├─→ Create work directory + work.json
   └─→ Queue DownloadTask to worker pool
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
4. Complete → Summary report with stats
```

### Scalability and Reliability

**Rate Limiting**:
- Per-provider rate limiters with delays and jitter
- Per-provider semaphores limit concurrent downloads
- Prevents overwhelming providers
- Respects API quotas and terms of service

**Retry Logic**:
- Exponential backoff for transient errors (429, 5xx)
- Configurable max attempts per provider
- Explicit handling of Retry-After headers

**Budget Management**:
- Pre-flight checks before downloads
- Real-time tracking of files and bytes
- Multi-level limits (global, per-work, per-provider)
- Configurable policies (skip vs stop)

**Error Handling**:
- Graceful degradation with fallback providers
- Comprehensive logging
- Non-blocking errors (continue processing)
- Metadata preservation on download failure

## Frequently Asked Questions

### General Questions

**Q: Which providers should I enable?**

A: Depends on your collection:
- **General historical materials**: Internet Archive, BnF Gallica, Library of Congress
- **European sources**: BnF Gallica, Europeana, MDZ, British Library
- **German materials**: MDZ, DDB
- **Fast downloads**: Internet Archive, Google Books (with API key)
- **No API key needed**: Internet Archive, BnF Gallica, LOC, British Library, MDZ, Wellcome

**Q: How much does it cost?**

A: Most providers are free. API keys required for:
- Europeana (free registration)
- DPLA (free registration)
- DDB (free registration)
- Google Books (Google Cloud account required)
- Anna's Archive (optional membership for fast downloads)

**Q: Should I use parallel downloads?**

A: Use parallel for:
- Large CSV files (50+ works)
- Mix of fast and slow providers
- Good internet connection

Use sequential for:
- Small CSV files (<20 works)
- Strict rate limit concerns
- Testing new providers

**Q: Can I process multiple CSVs simultaneously?**

A: Yes, run multiple instances with different output directories. Each instance maintains its own index.csv and progress tracking.

### Configuration Questions

**Q: How do I adjust for slow providers?**

A: Increase delays in provider settings:

```json
"bnf_gallica": {
  "network": {
    "delay_ms": 2000,
    "jitter_ms": 500,
    "max_attempts": 5
  }
}
```

**Q: How do I handle 429 (Too Many Requests) errors?**

A: Enable circuit breaker:

```json
"google_books": {
  "network": {
    "circuit_breaker_enabled": true,
    "circuit_breaker_threshold": 2,
    "circuit_breaker_cooldown_s": 600.0
  }
}
```

Provider automatically pauses for 10 minutes after 2 consecutive 429s.

**Q: How do I prioritize PDF downloads?**

A: Set in download configuration:

```json
"download": {
  "prefer_pdf_over_images": true
}
```

This skips page images when PDF/EPUB is available.

**Q: How do I limit download size?**

A: Configure budget limits:

```json
"download_limits": {
  "per_work": {
    "images_gb": 2,
    "pdfs_gb": 1
  }
}
```

**Q: How do I resume interrupted downloads?**

A: Set resume mode:

```json
"download": {
  "resume_mode": "skip_if_has_objects"
}
```

Options: `skip_completed`, `skip_if_has_objects`, `resume_from_csv`, `reprocess_all`

### Processing Questions

**Q: No items found for my works?**

A: Possible causes:
- API keys missing for enabled providers
- Network issues
- Provider API changes
- Titles don't match (try lowering `min_title_score`)

Solution: Test with `--log-level DEBUG` to see detailed API responses.

**Q: Downloads are very slow?**

A: Solutions:
- Enable parallel downloads: `max_parallel_downloads: 4`
- Reduce `delay_ms` in provider settings (respect terms of service)
- Use `sequential_first_hit` strategy for faster selection
- Enable only fast providers (Internet Archive, Google Books)
- Increase `provider_concurrency` for high-throughput providers

**Q: How do I handle partial failures?**

A: The tool automatically:
- Saves metadata even on download failure
- Tries fallback providers
- Marks status in work.json and index.csv
- Allows reprocessing with different settings

**Q: What if fuzzy matching is too strict?**

A: Lower `min_title_score`:
- 50-60: Multilingual/historical collections with variant spellings
- 70-85: Modern English collections with exact titles

```json
"selection": {
  "min_title_score": 60
}
```

**Q: How do I analyze download results?**

A: Check `downloaded_works/index.csv` for summary. Check individual `work.json` files for detailed candidate information, scores, and selection reasoning.

**Q: I'm experiencing issues not covered here**

A: Enable debug logging (`--log-level DEBUG`), check provider API status, verify API keys, test with single provider, review logs for specific error messages. For persistent issues, please open a GitHub issue.

## Contributing

Contributions are welcome!

### Reporting Issues

Include:
- Clear description
- Steps to reproduce
- Expected vs actual behavior
- Environment (OS, Python version, package versions)
- Relevant config sections (remove sensitive info)
- Log excerpts

### Suggesting Features

Provide:
- Use case description
- Proposed solution
- Alternatives considered
- Impact assessment

### Code Contributions

1. Fork repository and create feature branch
2. Follow existing code style and architecture
3. Add tests for new functionality
4. Update documentation
5. Test with multiple providers
6. Submit pull request with clear description

### Development Guidelines

- **Modularity**: Keep functions focused, modules organized
- **Error Handling**: Use try-except with informative messages
- **Logging**: Use logger for debugging information
- **Configuration**: Use JSON files, avoid hardcoding
- **User Experience**: Clear prompts and feedback
- **Documentation**: Update docstrings and README

### Areas for Contribution

- New providers (additional digital libraries)
- Enhanced matching algorithms
- Performance optimization for large datasets
- Testing (unit and integration tests)
- Provider-specific documentation
- UI (web interface or GUI)

## Development

### Recent Updates

For complete release history and detailed documentation, see WORKFLOW_GUIDE.md and CONFIG_GUIDE.md.

**Latest**: Multi-provider support, parallel downloads, circuit breaker, unified CSV system, interactive mode, fuzzy matching improvements.

## License

MIT License

Copyright (c) 2025 Paul Goetz

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
