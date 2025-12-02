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
- Intelligent Selection: Automatic fuzzy matching and scoring to select the best candidate from multiple sources with configurable thresholds for multilingual collections
- Flexible Download Strategies: Download PDFs, EPUBs, or high-resolution page images based on availability and preferences
- IIIF Support: Native support for IIIF Presentation and Image APIs with optimized performance for faster downloads
- Budget Management: Content-type download budgets (images, PDFs, metadata) with simple GB-based limits
- Rate Limiting: Built-in per-provider rate limiting with exponential backoff to respect API quotas
- Adaptive Circuit Breaker: Automatically pauses providers that hit repeated 429s and retries after a cooldown
- Robust Error Handling: Automatic retries, fallback providers, and comprehensive logging
- Batch Processing: Process CSV files with hundreds or thousands of works efficiently with proven workflows for large-scale operations
- Dual-Mode Operation: Interactive guided workflow or CLI automation via configuration toggle
- Metadata Preservation: Save search results, manifests, and selection decisions for auditing
- Performance Optimizations: Internet Archive PDF-first strategy with IIIF fallback for 60% faster downloads

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
4. `download`: Download preferences (PDF vs images, metadata, etc.)
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

### 3. Download Preferences

```json
{
  "download": {
    "prefer_pdf_over_images": true,
    "download_manifest_renderings": true,
    "max_renderings_per_manifest": 1,
    "rendering_mime_whitelist": ["application/pdf", "application/epub+zip"],
    "overwrite_existing": false,
    "include_metadata": true
  }
}
```

Download Preference Parameters:

- `prefer_pdf_over_images`: Skip page images when PDF/EPUB is available (recommended: true for faster downloads, especially with Internet Archive)
- `download_manifest_renderings`: Download PDFs/EPUBs linked in IIIF manifests
- `max_renderings_per_manifest`: Maximum number of rendering files per manifest
- `rendering_mime_whitelist`: Allowed MIME types for renderings
- `overwrite_existing`: Whether to overwrite existing files
- `include_metadata`: Save metadata JSON files alongside downloads

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

### 6. Naming Conventions

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

The input CSV must contain at minimum a `Title` column. Additional columns enhance matching accuracy:

Required:
- `Title`: The title of the work to search for

Optional but recommended:
- `entry_id`: Unique identifier for each work (e.g., E0001, BOOK_001). If missing, auto-generated.
- `Creator`: Author or creator name(s). Improves matching accuracy.

Example CSV (`sample_works.csv`):

```csv
entry_id,Title,Creator
E0001,"Le Morte d'Arthur","Thomas Malory"
E0002,"The Raven","Edgar Allan Poe"
E0003,"Philosophiæ Naturalis Principia Mathematica","Isaac Newton"
E0004,"De revolutionibus orbium coelestium","Nicolaus Copernicus"
E0005,"Discours de la méthode","René Descartes"
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

When `interactive_mode` is `true` in config (default), running `python main/downloader.py` launches a guided workflow:

1. **Mode Selection**: Choose between CSV batch, single work, or predefined collection
2. **Source Configuration**: Specify CSV path or enter work details manually
3. **Output Settings**: Configure output directory
4. **Options**: Set dry-run, logging level
5. **Confirmation**: Review and confirm before processing

Interactive mode features:
- Visual display of enabled/disabled providers
- Navigation with back/quit options at each step
- Input validation with helpful error messages
- Processing summary on completion

Force interactive mode with `--interactive` flag even when config says CLI:
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
from api.core.config import get_config

# Load and configure providers
providers = pipeline.load_enabled_apis("config.json")
providers = pipeline.filter_enabled_providers_for_keys(providers)
pipeline.ENABLED_APIS = providers

# Process a single work
pipeline.process_work(
    title="Le Morte d'Arthur",
    creator="Thomas Malory",
    entry_id="E0001",
    base_output_dir="downloaded_works",
    dry_run=False,
)

# Process multiple works
works = [
    {"title": "The Raven", "creator": "Edgar Allan Poe", "entry_id": "E0002"},
    {"title": "Divina Commedia", "creator": "Dante Alighieri", "entry_id": "E0003"},
]

for work in works:
    pipeline.process_work(
        title=work["title"],
        creator=work.get("creator"),
        entry_id=work.get("entry_id"),
        base_output_dir="my_output",
        dry_run=False,
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
│   ├── pipeline.py              # Core orchestration logic
│   ├── selection.py             # Candidate collection, scoring, and selection
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
- `pipeline.py`: Provider loading, API key validation, work directory creation, metadata persistence, download coordination with fallback
- `selection.py`: Candidate collection strategies (sequential/collect-and-select), fuzzy matching scoring, best candidate selection
- `mode_selector.py`: Dual-mode detection (interactive vs CLI) based on config and CLI flags
- `interactive.py`: Interactive workflow UI with guided prompts, navigation, and session management
- `downloader.py`: Unified entry point routing to interactive or CLI handlers

**Data Models** (`api/model.py`):
- SearchResult: Unified search result format with provider metadata
- Conversion utilities for legacy dict-based results

**Shared Utilities**:
- `iiif.py`: IIIF Presentation v2/v3 manifest parsing, Image API URL generation, image download helpers
- `download_helpers.py`: Common download patterns (PDF-first strategies, IIIF manifest + images)
- `matching.py`: Token-set ratio fuzzy matching, text normalization, combined scoring
- `utils.py`: Backward-compatible facade re-exporting core functionality, file download with budget checks, JSON persistence

### Workflow

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

### Scalability and Reliability

**Rate Limiting:**
- Per-provider rate limiters with configurable delays and jitter
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

Cause: Conservative rate limiting or slow providers

Solution:

- Reduce `delay_ms` in provider settings (respect terms of service)
- Use `sequential_first_hit` strategy for faster selection
- Enable only fast providers (Internet Archive, Google Books)

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