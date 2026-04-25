"""Works CSV, index, and per-work artifact I/O package.

Three cohesive concerns previously peer-level under ``main/``:

- :mod:`main.data.works_csv` -- input works CSV and per-row status tracking
- :mod:`main.data.index` -- summary ``index.csv`` ledger
- :mod:`main.data.work` -- per-work directory naming, work.json persistence,
  candidate/selection formatters, resume-mode decisions
"""
from __future__ import annotations

from .index import (
    build_index_row,
    get_processed_work_ids,
    read_index_csv,
    update_index_csv,
)
from .work import (
    check_work_status,
    compute_work_dir,
    compute_work_id,
    create_work_json,
    format_candidates_for_json,
    format_selected_for_json,
    get_naming_config,
    update_work_status,
)
from .works_csv import (
    CREATOR_COL,
    DIRECT_LINK_COL,
    ENTRY_ID_COL,
    LINK_COL,
    PROVIDER_COL,
    STATUS_COL,
    TIMESTAMP_COL,
    TITLE_COL,
    get_pending_works,
    get_stats,
    load_works_csv,
    mark_deferred,
    mark_failed,
    mark_success,
)

__all__ = [
    # works_csv
    "ENTRY_ID_COL",
    "TITLE_COL",
    "CREATOR_COL",
    "STATUS_COL",
    "LINK_COL",
    "PROVIDER_COL",
    "TIMESTAMP_COL",
    "DIRECT_LINK_COL",
    "load_works_csv",
    "get_pending_works",
    "get_stats",
    "mark_success",
    "mark_failed",
    "mark_deferred",
    # index
    "build_index_row",
    "update_index_csv",
    "read_index_csv",
    "get_processed_work_ids",
    # work
    "compute_work_id",
    "compute_work_dir",
    "check_work_status",
    "create_work_json",
    "update_work_status",
    "format_candidates_for_json",
    "format_selected_for_json",
    "get_naming_config",
]
