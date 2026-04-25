"""Step 12 README rewrite script."""
from pathlib import Path
import re

p = Path("README.md")
src = p.read_text(encoding="utf-8")

# 1. Replace CLI invocations
src = src.replace("python main/downloader.py", "python -m main.cli")

# 2. Rewrite the Directory Structure block.
dir_structure_re = re.compile(
    r"### Directory Structure\s*\n\s*```\s*\n.*?\n```",
    re.DOTALL,
)

new_tree = """### Directory Structure

```
ChronoDownloader/
|-- api/
|   |-- __init__.py
|   |-- core/                    # Foundational primitives
|   |   |-- config.py            # Configuration loader
|   |   |-- network.py           # HTTP, rate limiting, retries
|   |   |-- context.py           # Thread-local work/provider state
|   |   |-- naming.py            # Filename sanitization
|   |   |-- budget.py            # Download budget enforcement
|   |   `-- download.py          # Core file download + validation
|   |-- providers/               # 17 provider connectors
|   |   |-- __init__.py          # PROVIDERS registry export
|   |   |-- _registry.py         # Provider dispatch table
|   |   |-- annas_archive.py     # Anna's Archive
|   |   |-- bnf_gallica.py       # BnF Gallica
|   |   `-- ...                  # 15 more provider modules
|   |-- iiif/                    # IIIF manifest parsing + downloads
|   |   |-- __init__.py
|   |   |-- _parsing.py          # v2/v3 manifest parsing
|   |   |-- _direct.py           # Direct manifest download orchestrator
|   |   |-- _strategies.py       # PDF-first, page-image, combined
|   |   `-- _renderings.py       # Manifest rendering downloads
|   |-- model.py                 # SearchResult, QuotaDeferredException
|   |-- matching.py              # Fuzzy matching / scoring
|   |-- query_helpers.py         # SRU/SPARQL escaping
|   `-- identifier_resolver.py   # ID -> manifest URL mapping
|-- main/
|   |-- __init__.py
|   |-- cli/                     # Command-line entry point
|   |   |-- __init__.py
|   |   |-- __main__.py          # python -m main.cli entry
|   |   |-- entry.py             # main() wiring
|   |   |-- parser.py            # argparse definitions
|   |   |-- dispatch.py          # run_cli() router
|   |   |-- overrides.py         # override/filter helpers
|   |   `-- commands/            # per-subcommand handlers
|   |       |-- batch.py
|   |       |-- direct_iiif.py
|   |       |-- identifier.py
|   |       |-- providers.py
|   |       `-- quota.py
|   |-- ui/                      # Interactive + console UI
|   |   |-- __init__.py
|   |   |-- interactive.py
|   |   |-- console.py
|   |   `-- mode.py
|   |-- orchestration/           # Search-select-download pipeline
|   |   |-- __init__.py
|   |   |-- pipeline.py
|   |   |-- selection.py
|   |   |-- execution.py
|   |   `-- scheduler.py
|   |-- state/                   # Quota + deferred + state store
|   |   |-- __init__.py
|   |   |-- store.py
|   |   |-- quota.py
|   |   |-- deferred.py
|   |   `-- background.py
|   `-- data/                    # CSV + index + work metadata I/O
|       |-- __init__.py
|       |-- works_csv.py
|       |-- index.py
|       `-- work.py
|-- config.json                   # Main configuration
|-- requirements.txt              # Python dependencies
`-- README.md                     # This file
```"""
src = dir_structure_re.sub(new_tree, src, count=1)

# 3. Update Key Components
key_components_re = re.compile(
    r"### Key Components\s*\n.*?(?=\n### Workflow)",
    re.DOTALL,
)
new_key = """### Key Components

**Provider Connectors** (`api/providers/<name>.py`):
- Dedicated module per provider, 17 in total
- Implements `search_<provider>()` and `download_<provider>_work()`
- Returns `SearchResult` objects
- Quota-agnostic; pipeline performs pre-flight quota checks

**Core Infrastructure** (`api/core/`):
- `config.py`: Configuration loading with caching and environment variable support
- `network.py`: Centralized HTTP with per-provider rate limiting, exponential backoff, retry logic
- `budget.py`: Multi-level download budget tracking
- `context.py`: Thread-local state for work/provider tracking
- `naming.py`: Consistent filename sanitization and snake_case conversion
- `download.py`: File download with validation, magic-byte checks, HTML detection

**IIIF Handling** (`api/iiif/`):
- `_parsing.py`: IIIF Presentation v2/v3 parsing and Image API URL generation
- `_direct.py`: Direct manifest download (CLI `--iiif` and CSV `direct_link`)
- `_strategies.py`: Reusable patterns (page images, PDF-first, manifest + images)
- `_renderings.py`: Manifest-level rendering downloads (PDF, EPUB)

**CLI** (`main/cli/`):
- `entry.py`: `main()` supporting interactive and CLI modes
- `parser.py`: Argparse definition (29 options)
- `dispatch.py`: Routes to the correct subcommand after applying overrides
- `commands/`: Per-subcommand handlers (batch, direct_iiif, identifier, quota, providers)

**UI** (`main/ui/`):
- `interactive.py`: `InteractiveWorkflow` state machine
- `console.py`: ANSI console output and `DownloadConfiguration` dataclass
- `mode.py`: Dual-mode detection

**Orchestration** (`main/orchestration/`):
- `pipeline.py`: Per-work search-select-download orchestration; enforces quota pre-flight
- `selection.py`: Candidate scoring and best-match selection
- `execution.py`: Batch execution controller for sequential and parallel modes
- `scheduler.py`: ThreadPoolExecutor-based parallel download scheduler

**State** (`main/state/`):
- `store.py`: JSON-backed singleton state store
- `quota.py`: Provider quota tracking
- `deferred.py`: Deferred-download queue for quota-exhausted retries
- `background.py`: Retry daemon

**Data I/O** (`main/data/`):
- `works_csv.py`: Input CSV reader and status markers
- `index.py`: Thread-safe `index.csv` ledger
- `work.py`: Work directory naming and `work.json` persistence

**Data Models** (`api/model.py`):
- `SearchResult`: Unified search result format
- `QuotaDeferredException`: Raised when a quota-limited provider defers

**Shared Utilities**:
- `api/matching.py`: Token-set ratio fuzzy matching and text normalization
- `api/query_helpers.py`: SRU and SPARQL string escaping
- `api/identifier_resolver.py`: Provider identifier -> IIIF manifest URL mapping

"""
src = key_components_re.sub(new_key, src, count=1)

# 4. Update Programmatic Usage
prog_usage_re = re.compile(
    r"### Programmatic Usage\s*\n.*?(?=\n### Output Structure)",
    re.DOTALL,
)
new_prog = '''### Programmatic Usage

```python
from main.orchestration import (
    process_work,
    run_batch_downloads,
    load_enabled_apis,
    filter_enabled_providers_for_keys,
)
from main.data import load_works_csv, get_pending_works
from api.core.config import get_config

# Load providers
providers = load_enabled_apis("config.json")
providers = filter_enabled_providers_for_keys(providers)

# Process a single work
process_work(
    title="Tractatus de uino",
    creator="Anonymous",
    entry_id="6106",
    base_output_dir="downloaded_works",
    dry_run=False,
)

# Process a CSV in batch
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

'''
src = prog_usage_re.sub(new_prog, src, count=1)

p.write_text(src, encoding="utf-8")
print("README.md rewritten.")
