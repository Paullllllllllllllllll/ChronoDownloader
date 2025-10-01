# ChronoDownloader

**A comprehensive Python tool for discovering and downloading digitized historical sources from major digital libraries worldwide.**

ChronoDownloader automates the process of searching, selecting, and downloading historical books, manuscripts, and documents from 13+ digital library providers. It features intelligent candidate selection, fuzzy matching, configurable download strategies, and robust rate limiting to ensure reliable bulk downloads while respecting provider terms of service.

## Key Features

- **Multi-Provider Search**: Query 13 major digital libraries simultaneously including Internet Archive, BnF Gallica, Library of Congress, Google Books, and more
- **Intelligent Selection**: Automatic fuzzy matching and scoring to select the best candidate from multiple sources
- **Flexible Download Strategies**: Download PDFs, EPUBs, or high-resolution page images based on availability and preferences
- **IIIF Support**: Native support for IIIF Presentation and Image APIs for high-quality downloads
- **Budget Management**: Content-type download budgets (images, PDFs, metadata) with simple GB-based limits
- **Rate Limiting**: Built-in per-provider rate limiting with exponential backoff to respect API quotas
- **Robust Error Handling**: Automatic retries, fallback providers, and comprehensive logging
- **Batch Processing**: Process CSV files with hundreds or thousands of works efficiently
- **Metadata Preservation**: Save search results, manifests, and selection decisions for auditing

## Table of Contents

- [Key Features](#key-features)
- [Supported Providers](#supported-providers)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Output Structure](#output-structure)
- [Advanced Usage](#advanced-usage)
- [Project Architecture](#project-architecture)
- [Extending the Tool](#extending-the-tool)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

## Supported Providers

ChronoDownloader currently supports the following digital library providers:

| Provider | Region | API Key Required | IIIF Support |
|----------|--------|------------------|-------------|
| **Internet Archive** | US | No | Yes |
| **BnF Gallica** | France | No | Yes |
| **Library of Congress** | US | No | Yes |
| **Google Books** | Global | Yes | No |
| **Europeana** | EU (Aggregator) | Yes | Yes |
| **DPLA** | US (Aggregator) | Yes | Yes |
| **Deutsche Digitale Bibliothek** | Germany | Yes | Yes |
| **British Library** | UK | No | Yes |
| **MDZ** | Germany | No | Yes |
| **Polona** | Poland | No | Yes |
| **Biblioteca Nacional de España** | Spain | No | Yes |
| **HathiTrust** | US | Optional | Yes |
| **Wellcome Collection** | UK | No | Yes |

## Quick Start

```bash
# 1. Clone the repository
git clone <repository-url>
cd ChronoDownloader

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up API keys (if needed)
$env:EUROPEANA_API_KEY = "your_key_here"
$env:GOOGLE_BOOKS_API_KEY = "your_key_here"

# 4. Run with the sample CSV
python main/downloader.py sample_works.csv --output_dir my_downloads

# 5. Check results
ls my_downloads
```

## Installation

### Prerequisites

- **Python 3.8+** (recommended: Python 3.9 or higher)
- **pip** package manager
- Internet connection for API access

### Step-by-Step Installation

1. **Clone or download the repository:**
   ```bash
   git clone <repository-url>
   cd ChronoDownloader
   ```

2. **Create a virtual environment (recommended):**
   ```bash
   # Windows
   python -m venv venv
   .\venv\Scripts\activate
   
   # Linux/Mac
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install required dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   
   The tool requires:
   - `requests` - HTTP library for API calls
   - `pandas` - CSV processing and data handling
   - `beautifulsoup4` - HTML/XML parsing
   - `urllib3` - HTTP client with retry support

4. **Configure API keys (for providers that require them):**

   **Windows (PowerShell):**
   ```powershell
   $env:EUROPEANA_API_KEY = "your_europeana_key"
   $env:DDB_API_KEY = "your_ddb_key"
   $env:DPLA_API_KEY = "your_dpla_key"
   $env:GOOGLE_BOOKS_API_KEY = "your_google_books_key"
   ```
   
   **Linux/Mac (Bash):**
   ```bash
   export EUROPEANA_API_KEY="your_europeana_key"
   export DDB_API_KEY="your_ddb_key"
   export DPLA_API_KEY="your_dpla_key"
   export GOOGLE_BOOKS_API_KEY="your_google_books_key"
   ```

5. **Verify installation:**
   ```bash
   python main/downloader.py --help
   ```

### Getting API Keys

- **Europeana**: Register at [Europeana Pro](https://pro.europeana.eu/page/get-api)
- **DPLA**: Request at [DPLA API](https://pro.dp.la/developers/api-codex)
- **DDB**: Apply at [Deutsche Digitale Bibliothek](https://www.deutsche-digitale-bibliothek.de/content/api)
- **Google Books**: Get from [Google Cloud Console](https://console.cloud.google.com/apis/library/books.googleapis.com)

## Usage

### Basic Usage

The tool processes CSV files containing works to download. Each row should have at minimum a `Title` column.

**Minimal CSV format:**
```csv
Title
"Le Morte d'Arthur"
"The Raven"
"On the Origin of Species"
```

**Recommended CSV format:**
```csv
entry_id,Title,Creator
E0001,"Le Morte d'Arthur","Thomas Malory"
E0002,"The Raven","Edgar Allan Poe"
E0003,"On the Origin of Species","Charles Darwin"
```

**Run the downloader:**
```bash
python main/downloader.py your_works.csv
```

### Command-Line Options

```bash
python main/downloader.py <csv_file> [OPTIONS]
```

**Required:**
- `csv_file` - Path to CSV file with works to download (must have `Title` column)

**Optional:**
- `--output_dir DIR` - Output directory (default: `downloaded_works`)
- `--dry-run` - Search and score candidates without downloading
- `--log-level LEVEL` - Set logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (default: `INFO`)
- `--config PATH` - Path to configuration JSON file (default: `config.json`)

### Usage Examples

**1. Basic download with default settings:**
```bash
python main/downloader.py sample_works.csv
```

**2. Custom output directory:**
```bash
python main/downloader.py my_books.csv --output_dir ./historical_sources
```

**3. Dry run to preview results without downloading:**
```bash
python main/downloader.py my_books.csv --dry-run --log-level DEBUG
```

**4. Use custom configuration:**
```bash
python main/downloader.py my_books.csv --config config_small.json
```

**5. Verbose logging for debugging:**
```bash
python main/downloader.py my_books.csv --log-level DEBUG
```

**6. Using environment variable for config path:**
```powershell
$env:CHRONO_CONFIG_PATH = "C:\path\to\config.json"
python main/downloader.py my_books.csv
```

### CSV Input Format

The input CSV must contain at minimum a `Title` column. Additional columns enhance matching accuracy:

**Required columns:**
- `Title` - The title of the work to search for

**Optional but recommended columns:**
- `entry_id` - Unique identifier for each work (e.g., `E0001`, `BOOK_001`). If missing, auto-generated as `E0001`, `E0002`, etc.
- `Creator` - Author or creator name(s). Improves matching accuracy when specified.

**Example CSV** (`sample_works.csv`):
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

## Configuration

ChronoDownloader uses a JSON configuration file (`config.json` by default) to control all aspects of its behavior. You can specify an alternative config file using the `--config` flag or the `CHRONO_CONFIG_PATH` environment variable. For detailed guidance and sample configurations, see `CONFIG_GUIDE.md`.

### Configuration Overview

The configuration file has five main sections:
1. **`providers`** - Enable/disable specific providers
2. **`provider_settings`** - Per-provider rate limiting and behavior
3. **`download`** - Download preferences (PDF vs images, metadata, etc.)
4. **`selection`** - Candidate selection and matching strategy
5. **`download_limits`** - Budget constraints to prevent runaway downloads
6. **`naming`** - Output folder and file naming conventions

### 1. Enable/Disable Providers

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

### 2. Provider Settings and Rate Limiting

Each provider can have custom rate limiting, retry policies, and download limits. This ensures compliance with provider terms of service and prevents overwhelming their servers.

**Configuration structure:**

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

**Network policy parameters:**
- `delay_ms` - Minimum delay between requests in milliseconds
- `jitter_ms` - Random jitter added to delay (prevents thundering herd)
- `max_attempts` - Maximum retry attempts for failed requests
- `base_backoff_s` - Initial backoff duration for retries (seconds)
- `backoff_multiplier` - Multiplier for exponential backoff
- `timeout_s` - Request timeout in seconds
- `headers` - Custom HTTP headers for this provider

**Provider-specific limits:**
- `max_pages` - Maximum number of page images to download per work
- `max_images` - Alternative name for max_pages
- `max_files` - Maximum files to download per work (Google Books)
- `free_only` - Only download free/public domain works (Google Books)
- `prefer` - Preferred format: "pdf" or "images" (Google Books)
- `allow_drm` - Whether to allow DRM-protected content (Google Books)

**Environment variable overrides:**
Some providers support environment variables for quick adjustments:
- `MDZ_MAX_PAGES` - Override max pages for MDZ
- `DDB_MAX_PAGES` - Override max pages for DDB
- `DPLA_MAX_PAGES` - Override max pages for DPLA
- `WELLCOME_MAX_IMAGES` - Override max images for Wellcome

### 3. Download Preferences

The downloader enforces optional content-type download budgets and can prefer bundled PDFs/EPUBs from IIIF manifests over page images. Configure in `config.json`:

```json
{
  "download": {
    "prefer_pdf_over_images": true,
    "download_manifest_renderings": true,
    "max_renderings_per_manifest": 1,
    "rendering_mime_whitelist": ["application/pdf", "application/epub+zip"],
    "overwrite_existing": false,
    "include_metadata": true
  },
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

**Download preference parameters:**
- `prefer_pdf_over_images` - If true, skip page images when PDF/EPUB is available
- `download_manifest_renderings` - Download PDFs/EPUBs linked in IIIF manifests
- `max_renderings_per_manifest` - Maximum number of rendering files per manifest
- `rendering_mime_whitelist` - Allowed MIME types for renderings
- `overwrite_existing` - Whether to overwrite existing files
- `include_metadata` - Save metadata JSON files alongside downloads

**How it works:**
1. When a IIIF manifest includes `rendering` entries (PDF/EPUB), they are downloaded first
2. If `prefer_pdf_over_images` is true and at least one rendering was saved, page image downloads are skipped
3. This saves bandwidth and storage while providing the most useful format

### 4. Download Budget Limits

Download budgets prevent runaway jobs and help manage storage/bandwidth. Limits are defined per content type, expressed in GB (metadata per work uses MB for finer control):

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

**Budget parameters:**
- `total.images_gb` - Maximum combined GB of page images across all works (0 or missing = unlimited)
- `total.pdfs_gb` - Maximum combined GB of PDF downloads across all works
- `total.metadata_gb` - Maximum combined GB of metadata saved across all works
- `per_work.images_gb` - Maximum GB of images per individual work
- `per_work.pdfs_gb` - Maximum GB of PDFs per individual work
- `per_work.metadata_mb` - Maximum metadata size per work (in MB for finer granularity)
- `on_exceed` - Action when limit exceeded: `"skip"` (skip item, continue) or `"stop"` (abort immediately)

Legacy byte- and provider-based fields are still understood when present but new configurations should migrate to the simplified structure above.

### 5. Selection Strategy and Fuzzy Matching

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

**Selection parameters:**
- `strategy` - Selection strategy: `"collect_and_select"` or `"sequential_first_hit"`
- `provider_hierarchy` - Ordered list of preferred providers
- `min_title_score` - Minimum fuzzy match score (0-100) to accept a candidate
- `creator_weight` - Weight of creator match in scoring (0.0-1.0)
- `year_tolerance` - Reserved for future date-based matching
- `max_candidates_per_provider` - Limit search results per provider
- `download_strategy` - `"selected_only"` or `"all"` (download all candidates)
- `keep_non_selected_metadata` - Save metadata for non-selected candidates

**Selection strategies explained:**

**`collect_and_select` (recommended):**
1. Searches all enabled providers in parallel
2. Scores all candidates using fuzzy title/creator matching
3. Ranks candidates by provider hierarchy and quality signals (IIIF availability, etc.)
4. Selects the best match overall
5. Falls back to next-best if download fails

**`sequential_first_hit`:**
1. Searches providers in `provider_hierarchy` order
2. Stops at the first provider with an acceptable match (score ≥ `min_title_score`)
3. Faster but may miss better matches from lower-priority providers

**Fuzzy matching:**
The tool uses token-set ratio matching to handle:
- Minor spelling variations
- Different word orders
- Punctuation differences
- Subtitle variations

Scoring combines:
- Title similarity (primary)
- Creator similarity (weighted by `creator_weight`)
- Quality signals (+3 for IIIF manifest, +0.5 for item URL)

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

**Naming parameters:**
- `include_creator_in_work_dir` - Include creator in folder name
- `include_year_in_work_dir` - Include publication year in folder name
- `title_slug_max_len` - Maximum length of title slug in filenames

## Output Structure

ChronoDownloader creates a well-organized output structure for easy navigation and auditing.

### Folder Structure

For each work in your CSV, a dedicated folder is created:

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

### File Naming Conventions

**All filenames follow a consistent pattern:**
- `<entry_id>_<work_name>_<provider>.<ext>` for single files
- `<entry_id>_<work_name>_<provider>_image_001.jpg` for page images
- `<entry_id>_<work_name>_<provider>_2.pdf` for multiple files of same type

**Naming rules:**
- All names use strict `snake_case` formatting
- `entry_id`, `work_name`, and `provider` are always included
- Images always have 3-digit counters (`001`, `002`, ...)
- Non-image files get numeric suffixes only when multiple exist

### Index File

A master index is maintained at `downloaded_works/index.csv` with columns:
- `work_id` - Stable hash-based identifier
- `entry_id` - Your CSV entry ID
- `work_dir` - Path to work folder
- `title` - Work title
- `creator` - Creator/author
- `selected_provider` - Provider name used
- `selected_provider_key` - Provider key
- `selected_source_id` - Provider's identifier for the work
- `selected_dir` - Download directory
- `work_json` - Path to work.json metadata file

### Metadata Files

**`work.json`** contains:
- Input parameters (title, creator, entry_id)
- All search candidates from all providers
- Fuzzy match scores for each candidate
- Selection decision and reasoning
- Timestamp

**Provider metadata files** (in `metadata/`) contain:
- Original API responses
- IIIF manifests
- Search result details

This comprehensive metadata enables:
- Auditing selection decisions
- Debugging failed downloads
- Reprocessing with different strategies
- Academic citation and provenance tracking

## Advanced Usage

### Programmatic Usage

You can use ChronoDownloader as a Python library in your own code:

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

### Large-Scale Processing

For very large jobs (thousands of works):

1. **Split your CSV** into smaller batches:
   ```python
   import pandas as pd
   
   df = pd.read_csv("large_works.csv")
   batch_size = 100
   
   for i in range(0, len(df), batch_size):
       batch = df.iloc[i:i+batch_size]
       batch.to_csv(f"batch_{i//batch_size:03d}.csv", index=False)
   ```

2. **Run multiple processes** in parallel (different terminals):
   ```bash
   # Terminal 1
   python main/downloader.py batch_000.csv --output_dir output_batch_0
   
   # Terminal 2
   python main/downloader.py batch_001.csv --output_dir output_batch_1
   ```

3. **Monitor progress** using the index.csv files:
   ```python
   import pandas as pd
   import glob
   
   # Combine all index files
   indices = [pd.read_csv(f) for f in glob.glob("output_*/index.csv")]
   combined = pd.concat(indices, ignore_index=True)
   print(f"Total works processed: {len(combined)}")
   print(f"By provider: {combined['selected_provider'].value_counts()}")
   ```

### Custom Provider Configuration

Create specialized configs for different use cases:

**`config_fast.json`** - Prioritize speed:
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

**`config_quality.json`** - Prioritize quality:
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

# Load all work.json files
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

## Project Architecture

ChronoDownloader follows a modular architecture for maintainability and extensibility.

### Directory Structure

```
ChronoDownloader/
├── api/                          # Provider connectors and core utilities
│   ├── core/                     # Core infrastructure modules
│   │   ├── config.py            # Configuration management
│   │   ├── network.py           # HTTP session, rate limiting, retries
│   │   ├── context.py           # Thread-local work/provider tracking
│   │   ├── naming.py            # Filename sanitization
│   │   └── budget.py            # Download budget enforcement
│   ├── providers.py             # Provider registry
│   ├── model.py                 # SearchResult dataclass
│   ├── matching.py              # Fuzzy matching algorithms
│   ├── iiif.py                  # IIIF manifest parsing
│   ├── utils.py                 # File download and utilities
│   ├── query_helpers.py         # Query string escaping
│   ├── bnf_gallica_api.py       # BnF Gallica connector
│   ├── internet_archive_api.py  # Internet Archive connector
│   ├── loc_api.py               # Library of Congress connector
│   ├── europeana_api.py         # Europeana connector
│   ├── dpla_api.py              # DPLA connector
│   ├── ddb_api.py               # DDB connector
│   ├── british_library_api.py   # British Library connector
│   ├── mdz_api.py               # MDZ connector
│   ├── polona_api.py            # Polona connector
│   ├── bne_api.py               # BNE connector
│   ├── google_books_api.py      # Google Books connector
│   ├── hathitrust_api.py        # HathiTrust connector
│   └── wellcome_api.py          # Wellcome Collection connector
├── main/                         # CLI and orchestration
│   ├── pipeline.py              # Core orchestration logic
│   └── downloader.py            # CLI entry point
├── config.json                   # Main configuration file
├── requirements.txt              # Python dependencies
├── sample_works.csv              # Example input
└── README.md                     # This file
```

### Key Components

**1. Provider Connectors** (`api/*_api.py`)
- Each provider has a dedicated module
- Implements `search_<provider>()` and `download_<provider>_work()` functions
- Returns `SearchResult` objects for uniform handling

**2. Core Infrastructure** (`api/core/`)
- **`network.py`**: Centralized HTTP with per-provider rate limiting, exponential backoff, retry logic
- **`config.py`**: Configuration loading with caching and defaults
- **`budget.py`**: Download budget tracking (files, bytes) at multiple levels
- **`context.py`**: Thread-local state for work/provider tracking
- **`naming.py`**: Consistent filename sanitization

**3. Orchestration** (`main/pipeline.py`)
- Provider loading and API key validation
- Multi-provider search coordination
- Fuzzy matching and candidate scoring
- Selection strategy implementation
- Download coordination with fallback

**4. Data Models** (`api/model.py`)
- `SearchResult`: Unified search result format
- Conversion utilities for legacy dict-based results

**5. Utilities**
- **`iiif.py`**: IIIF Presentation v2/v3 manifest parsing, Image API URL generation
- **`matching.py`**: Token-set ratio fuzzy matching, text normalization
- **`utils.py`**: File download with budget checks, JSON persistence

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
- SSL and DNS error policies

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

1. **Create a new API module** (`api/my_provider_api.py`):

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
    # Build API query
    api_url = "https://api.myprovider.com/search"
    params = {"q": title, "limit": max_results}
    if creator:
        params["creator"] = creator
    
    # Make request
    data = make_request(api_url, params=params)
    if not data or "results" not in data:
        return []
    
    # Convert to SearchResult objects
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
        # Download PDF if available
        pdf_url = result.raw.get("pdf_url")
        if pdf_url:
            return download_file(pdf_url, output_dir, "document.pdf")
        
        # Fallback to images
        # ... implementation ...
        
        return True
    except Exception as e:
        logger.error("Download failed: %s", e)
        return False
```

2. **Register in `api/providers.py`:**

```python
from . import my_provider_api

PROVIDERS: Dict[str, Tuple[Any, Any, str]] = {
    # ... existing providers ...
    "my_provider": (
        my_provider_api.search_my_provider,
        my_provider_api.download_my_provider_work,
        "My Provider"
    ),
}
```

3. **Add to configuration** (`config.json`):

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

4. **Add host mapping** (if needed) in `api/core/network.py`:

```python
PROVIDER_HOST_MAP: Dict[str, tuple[str, ...]] = {
    # ... existing mappings ...
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
    # ...
```

### Adding CSV Columns

To use additional CSV columns (e.g., `Year`, `ISBN`):

1. **Update `main/downloader.py`** to read new columns:

```python
for index, row in works_df.iterrows():
    title = row["Title"]
    creator = row.get("Creator")
    year = row.get("Year")  # New column
    isbn = row.get("ISBN")  # New column
    # Pass to pipeline...
```

2. **Update provider search functions** to use new fields
3. **Update matching logic** to incorporate new fields

## Troubleshooting

### Common Issues

**1. "No providers are enabled"**
- **Cause**: All providers disabled in config or missing API keys
- **Solution**: Check `config.json` and ensure at least one provider is enabled. Set required API keys as environment variables.

**2. "SSL certificate verification failed"**
- **Cause**: SSL certificate issues with provider
- **Solution**: Add `"ssl_error_policy": "retry_insecure_once"` to provider's network config (use cautiously)

**3. "429 Too Many Requests"**
- **Cause**: Hitting provider rate limits
- **Solution**: Increase `delay_ms` and `jitter_ms` in provider settings. The tool will automatically back off, but higher initial delays prevent the issue.

**4. "No items found" for all works**
- **Cause**: API keys missing, network issues, or provider API changes
- **Solution**: 
  - Verify API keys are set correctly
  - Test with `--log-level DEBUG` to see detailed API responses
  - Check if provider APIs are accessible

**5. "Download budget exhausted"**
- **Cause**: Reached configured download limits
- **Solution**: Adjust limits in `config.json` under `download_limits`, or set to `0` for unlimited

**6. Downloads are very slow**
- **Cause**: Conservative rate limiting or slow providers
- **Solution**: 
  - Reduce `delay_ms` in provider settings (respect terms of service)
  - Use `sequential_first_hit` strategy for faster selection
  - Enable only fast providers (Internet Archive, Google Books)

**7. "Title" column not found in CSV**
- **Cause**: CSV missing required column or encoding issues
- **Solution**: Ensure CSV has a column named exactly `Title` (case-sensitive). Check file encoding (should be UTF-8).

### Debug Mode

Enable detailed logging:

```bash
python main/downloader.py sample_works.csv --log-level DEBUG > debug.log 2>&1
```

This captures:
- All API requests and responses
- Rate limiting delays
- Budget checks
- Matching scores
- Download attempts

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

Contributions are welcome! Areas for improvement:

- **New Providers**: Add connectors for additional digital libraries
- **Enhanced Matching**: Improve fuzzy matching algorithms
- **Performance**: Optimize for very large datasets
- **Testing**: Add unit and integration tests
- **Documentation**: Improve provider-specific documentation
- **UI**: Create a web interface or GUI

### Development Setup

```bash
# Clone and install in development mode
git clone <repository-url>
cd ChronoDownloader
pip install -e .

# Run tests (if available)
python -m pytest tests/

# Format code
black api/ main/
```

## License

Please check the repository for license information.

## Acknowledgments

This tool interfaces with the following digital libraries:
- Internet Archive
- Bibliothèque nationale de France (BnF Gallica)
- Library of Congress
- Google Books
- Europeana
- Digital Public Library of America (DPLA)
- Deutsche Digitale Bibliothek (DDB)
- British Library
- Münchener DigitalisierungsZentrum (MDZ)
- Polona (National Library of Poland)
- Biblioteca Nacional de España (BNE)
- HathiTrust Digital Library
- Wellcome Collection

Please respect each provider's terms of service and rate limits.
