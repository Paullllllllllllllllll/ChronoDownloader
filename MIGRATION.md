# Migration Guide: Pre-Refactor -> Deep-Module Layout

ChronoDownloader's `api/` and `main/` packages were reorganized into deep
modules on 2026-04-24. All previous compatibility shims were removed
("clean break"). This document maps every old import path to its new
location so external scripts, notebooks, and sibling projects can migrate
mechanically.

If you invoke the CLI from the shell, change:

```
python main/downloader.py ...    -> python -m main.cli ...
```

The argument surface is unchanged (29 flags, same help text beyond the
program-name line).

## Import-Path Table

### CLI / entry points

| Old | New |
|-----|-----|
| `python main/downloader.py ...` | `python -m main.cli ...` |
| `from main.downloader import main` | `from main.cli import main` |
| `from main.downloader import create_cli_parser` | `from main.cli import create_cli_parser` |
| `from main.downloader import run_cli` | `from main.cli import run_cli` |
| `from main.downloader import show_quota_status` | `from main.cli.commands.quota import show_quota_status` |
| `from main.downloader import cleanup_deferred_queue` | `from main.cli.commands.quota import cleanup_deferred_queue` |
| `from main.downloader import _run_identifier_cli` | `from main.cli.commands.identifier import run_identifier_cli` |
| `from main.downloader import _run_direct_iiif_cli` | `from main.cli.commands.direct_iiif import run_direct_iiif_cli` |
| `from main.downloader import _looks_like_cli_invocation` | `from main.cli.overrides import _looks_like_cli_invocation` |
| `from main.downloader import _apply_runtime_config_overrides` | `from main.cli.overrides import _apply_runtime_config_overrides` |
| `from main.downloader import _apply_provider_cli_overrides` | `from main.cli.overrides import _apply_provider_cli_overrides` |
| `from main.downloader import _filter_pending_rows` | `from main.cli.overrides import _filter_pending_rows` |

### `api/` layer

| Old | New |
|-----|-----|
| `from api.utils import download_file, save_json` | `from api.core.download import download_file, save_json` |
| `from api.utils import download_iiif_renderings` | `from api.iiif import download_iiif_renderings` |
| `from api.utils import get_config, get_download_config, ...` | `from api.core.config import get_config, get_download_config, ...` |
| `from api.utils import make_request, get_session, RateLimiter` | `from api.core.network import make_request, get_session, RateLimiter` |
| `from api.utils import set_current_work, provider_context, ...` | `from api.core.context import set_current_work, provider_context, ...` |
| `from api.utils import sanitize_filename, to_snake_case, get_provider_slug` | `from api.core.naming import sanitize_filename, to_snake_case, get_provider_slug` |
| `from api.utils import budget_exhausted, get_budget` | `from api.core.budget import budget_exhausted, get_budget` |
| `from api.iiif import ...` (old single-file module) | `from api.iiif import ...` (now a package; same public symbols) |
| `from api.direct_iiif_api import ...` | `from api.iiif import ...` |
| `from api.download_helpers import ...` | `from api.iiif import ...` |
| `from api.providers import PROVIDERS` | `from api.providers import PROVIDERS` (unchanged) |
| `from api.<name>_api import search_<name>, download_<name>_work` | `from api.providers.<name> import search_<name>, download_<name>_work` |
| `import api.utils` | (removed; import the specific submodule directly) |

Provider renames (`api/<name>_api.py` -> `api/providers/<name>.py`), all
17 connectors:

- annas_archive_api -> providers/annas_archive
- bne_api -> providers/bne
- bnf_gallica_api -> providers/bnf_gallica
- british_library_api -> providers/british_library
- ddb_api -> providers/ddb
- dpla_api -> providers/dpla
- e_rara_api -> providers/e_rara
- europeana_api -> providers/europeana
- google_books_api -> providers/google_books
- hathitrust_api -> providers/hathitrust
- internet_archive_api -> providers/internet_archive
- loc_api -> providers/loc
- mdz_api -> providers/mdz
- polona_api -> providers/polona
- sbb_digital_api -> providers/sbb_digital
- slub_api -> providers/slub
- wellcome_api -> providers/wellcome

### `main/` layer

| Old | New |
|-----|-----|
| `from main.pipeline import process_work` | `from main.orchestration import process_work` |
| `from main.pipeline import search_and_select` | `from main.orchestration import search_and_select` |
| `from main.pipeline import execute_download` | `from main.orchestration import execute_download` |
| `from main.pipeline import load_enabled_apis` | `from main.orchestration import load_enabled_apis` |
| `from main.pipeline import filter_enabled_providers_for_keys` | `from main.orchestration import filter_enabled_providers_for_keys` |
| `from main.selection import collect_candidates_all, ...` | `from main.orchestration import collect_candidates_all, ...` |
| `from main.execution import run_batch_downloads` | `from main.orchestration import run_batch_downloads` |
| `from main.execution import process_direct_iiif` | `from main.orchestration import process_direct_iiif` |
| `from main.execution import create_interactive_callbacks` | `from main.orchestration import create_interactive_callbacks` |
| `from main.download_scheduler import DownloadTask, DownloadScheduler` | `from main.orchestration import DownloadTask, DownloadScheduler` |
| `from main.unified_csv import load_works_csv, get_pending_works, get_stats` | `from main.data import load_works_csv, get_pending_works, get_stats` |
| `from main.unified_csv import mark_success, mark_failed, mark_deferred` | `from main.data import mark_success, mark_failed, mark_deferred` |
| `from main.unified_csv import TITLE_COL, ENTRY_ID_COL, ...` | `from main.data import TITLE_COL, ENTRY_ID_COL, ...` |
| `from main.index_manager import update_index_csv, build_index_row, ...` | `from main.data import update_index_csv, build_index_row, ...` |
| `from main.work_manager import compute_work_id, compute_work_dir, ...` | `from main.data import compute_work_id, compute_work_dir, ...` |
| `from main.state_manager import StateManager, get_state_manager` | `from main.state import StateManager, get_state_manager` |
| `from main.quota_manager import QuotaManager, get_quota_manager` | `from main.state import QuotaManager, get_quota_manager` |
| `from main.deferred_queue import DeferredQueue, DeferredItem, get_deferred_queue` | `from main.state import DeferredQueue, DeferredItem, get_deferred_queue` |
| `from main.background_scheduler import BackgroundRetryScheduler, ...` | `from main.state import BackgroundRetryScheduler, ...` |
| `from main.interactive import InteractiveWorkflow, run_interactive` | `from main.ui import InteractiveWorkflow, run_interactive` |
| `from main.console_ui import ConsoleUI, DownloadConfiguration` | `from main.ui import ConsoleUI, DownloadConfiguration` |
| `from main.mode_selector import run_with_mode_detection, get_general_config` | `from main.ui import run_with_mode_detection, get_general_config` |

## Behavioral Changes

### Provider-level quota handling (Anna's Archive)

Before the refactor, `api/annas_archive_api.py` did a lazy
`from main.quota_manager import get_quota_manager` to pre-flight check
the fast-download quota and record consumption. This violated clean
layering (provider reaching into main orchestration).

After the refactor:

- `api/providers/annas_archive.py` is quota-agnostic. It no longer
  imports from `main/` in any form. It attempts fast download first
  when an API key is present, then falls back to public scraping.
- `main/orchestration/pipeline.py` performs the quota pre-flight
  (`_quota_preflight`) for providers in `QUOTA_LIMITED_PROVIDERS`
  immediately before invoking `download_func`. If the provider's
  quota is exhausted and `wait_for_reset` is true, the pipeline
  raises `QuotaDeferredException` itself; the existing deferred-queue
  handler picks it up.
- `main/orchestration/pipeline.py` records successful consumption
  (`_quota_record`) immediately after a successful download from a
  quota-backed provider.

The provider public interface (`download_annas_archive_work`) keeps
the same signature and return type. The helper functions
`is_quota_available()` and `get_quota_reset_time()` are removed from
the provider module. If you consumed those helpers directly, use
`main.state.quota.get_quota_manager().get_quota_status(...)` instead.

### Monkeypatching in tests

Tests that previously patched `api.utils.X` or `main.downloader.X`
must target the new modules where the symbols are actually defined
and looked up. See the table above for the exact new paths.

For helpers previously underscore-prefixed inside `main/downloader.py`,
the new handlers drop the leading underscore (they are the package's
public API now):

- `_run_identifier_cli` -> `run_identifier_cli`
- `_run_direct_iiif_cli` -> `run_direct_iiif_cli`

The internal-helpers in `main/cli/overrides.py` keep their underscore
prefix (`_split_csv_values`, `_dedupe_keep_order`, etc.) to signal
they are not part of the public CLI surface.

## Verification

To verify your migration locally:

1. `python -m main.cli --help` -- argument list matches the pre-refactor
   help text (only the `usage:` program name differs).
2. `python -m main.cli --list-providers` -- 17 providers listed in the
   same order as before.
3. Grep your scripts for the Old patterns in the table above; each match
   has a New counterpart.
4. Your pytest suite: update import statements and `@patch` targets per
   the table; tests should pass without behavior changes.
