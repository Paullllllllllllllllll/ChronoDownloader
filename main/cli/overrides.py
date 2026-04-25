"""Pure helpers used by CLI dispatch for override and filter handling.

Kept free of orchestration imports so tests can exercise the override
and filter logic without pulling in the full pipeline.
"""
from __future__ import annotations

import argparse
import copy
import logging
from typing import Any

import pandas as pd

import api.core.config as core_config
from api.providers import PROVIDERS
from main.data.works_csv import ENTRY_ID_COL, STATUS_COL, get_pending_works

_TRUTHY = frozenset({"true", "1", "yes", "y"})
_FALSY = frozenset({"false", "0", "no", "n"})


def _split_csv_values(values: list[str] | None) -> list[str]:
    """Split comma-separated CLI values and strip whitespace."""
    if not values:
        return []
    result: list[str] = []
    for raw in values:
        if not raw:
            continue
        for part in str(raw).split(","):
            item = part.strip()
            if item:
                result.append(item)
    return result


def _dedupe_keep_order(values: list[str]) -> list[str]:
    """Deduplicate strings while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _classify_status(value: Any) -> str:
    """Classify CSV status cell as completed, failed, or pending."""
    if pd.isna(value):
        return "pending"
    if isinstance(value, bool):
        return "completed" if value else "failed"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            return "completed"
        if lowered in _FALSY:
            return "failed"
    return "pending"


def _apply_runtime_config_overrides(
    args: argparse.Namespace,
    config: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, Any]:
    """Apply CLI overrides to runtime config and refresh config cache."""
    merged = copy.deepcopy(config or {})
    merged.setdefault("download", {})
    merged.setdefault("selection", {})

    dl_cfg = dict(merged.get("download") or {})
    sel_cfg = dict(merged.get("selection") or {})

    resume_mode = getattr(args, "resume_mode", None)
    prefer_pdf = getattr(args, "prefer_pdf_over_images", None)
    manifest_renderings = getattr(args, "download_manifest_renderings", None)
    max_renderings = getattr(args, "max_renderings_per_manifest", None)
    rendering_mimes = getattr(args, "rendering_mime_whitelist", None)
    overwrite_existing = getattr(args, "overwrite_existing", None)
    include_metadata = getattr(args, "include_metadata", None)

    selection_strategy = getattr(args, "selection_strategy", None)
    min_title_score = getattr(args, "min_title_score", None)
    creator_weight = getattr(args, "creator_weight", None)
    max_candidates_per_provider = getattr(args, "max_candidates_per_provider", None)
    download_strategy = getattr(args, "download_strategy", None)
    keep_non_selected_metadata = getattr(args, "keep_non_selected_metadata", None)

    if resume_mode is not None:
        dl_cfg["resume_mode"] = resume_mode
    if prefer_pdf is not None:
        dl_cfg["prefer_pdf_over_images"] = bool(prefer_pdf)
    if manifest_renderings is not None:
        dl_cfg["download_manifest_renderings"] = bool(manifest_renderings)
    if max_renderings is not None:
        dl_cfg["max_renderings_per_manifest"] = int(max(0, max_renderings))
    if rendering_mimes:
        mime_values = _dedupe_keep_order(_split_csv_values(rendering_mimes))
        if mime_values:
            dl_cfg["rendering_mime_whitelist"] = mime_values
    if overwrite_existing is not None:
        dl_cfg["overwrite_existing"] = bool(overwrite_existing)
    if include_metadata is not None:
        dl_cfg["include_metadata"] = bool(include_metadata)

    if selection_strategy is not None:
        sel_cfg["strategy"] = selection_strategy
    if min_title_score is not None:
        sel_cfg["min_title_score"] = float(min_title_score)
    if creator_weight is not None:
        sel_cfg["creator_weight"] = float(creator_weight)
    if max_candidates_per_provider is not None:
        sel_cfg["max_candidates_per_provider"] = int(max(1, max_candidates_per_provider))
    if download_strategy is not None:
        sel_cfg["download_strategy"] = download_strategy
    if keep_non_selected_metadata is not None:
        sel_cfg["keep_non_selected_metadata"] = bool(keep_non_selected_metadata)

    merged["download"] = dl_cfg
    merged["selection"] = sel_cfg

    core_config._CONFIG_CACHE = merged
    logger.debug("Applied CLI runtime config overrides to in-memory config cache")
    return merged


def _apply_provider_cli_overrides(
    args: argparse.Namespace,
    providers: list[Any],
    logger: logging.Logger,
) -> list[Any]:
    """Apply provider selection overrides while preserving provider ordering."""
    explicit_keys = _dedupe_keep_order(_split_csv_values(getattr(args, "providers", None)))
    force_enable = _dedupe_keep_order(_split_csv_values(getattr(args, "enable_provider", None)))
    force_disable = set(
        _dedupe_keep_order(_split_csv_values(getattr(args, "disable_provider", None)))
    )

    if not explicit_keys and not force_enable and not force_disable:
        return providers

    available = set(PROVIDERS.keys())
    unknown = [
        k for k in explicit_keys + force_enable + list(force_disable) if k not in available
    ]
    if unknown:
        logger.warning("Ignoring unknown provider key(s): %s", ", ".join(sorted(set(unknown))))

    current_keys = [p[0] for p in providers if isinstance(p, tuple) and len(p) >= 4]

    if explicit_keys:
        ordered_keys = [k for k in explicit_keys if k in available]
    else:
        ordered_keys = list(current_keys)

    for key in force_enable:
        if key in available and key not in ordered_keys:
            ordered_keys.append(key)

    ordered_keys = [k for k in ordered_keys if k not in force_disable and k in available]

    overridden: list[Any] = []
    for key in ordered_keys:
        search_fn, download_fn, name = PROVIDERS[key]
        overridden.append((key, search_fn, download_fn, name))

    logger.info(
        "Provider override active. Effective providers: %s",
        ", ".join(ordered_keys) or "(none)",
    )
    return overridden


def _filter_pending_rows(
    works_df: pd.DataFrame, args: argparse.Namespace
) -> pd.DataFrame:
    """Apply pending-mode, entry-id, and limit filters to the work DataFrame."""
    pending_mode = getattr(args, "pending_mode", "all")
    if pending_mode == "all":
        pending_df = get_pending_works(works_df)
    else:
        status_series = (
            works_df[STATUS_COL]
            if STATUS_COL in works_df.columns
            else pd.Series([pd.NA] * len(works_df))
        )
        status_labels = status_series.apply(_classify_status)
        if pending_mode == "new":
            pending_df = works_df[status_labels == "pending"].copy()
        else:  # pending_mode == "failed"
            pending_df = works_df[status_labels == "failed"].copy()

    requested_ids = _dedupe_keep_order(_split_csv_values(getattr(args, "entry_ids", None)))
    if requested_ids:
        id_set = {str(i) for i in requested_ids}
        pending_df = pending_df[pending_df[ENTRY_ID_COL].astype(str).isin(id_set)].copy()

    limit = getattr(args, "limit", None)
    if limit is not None and limit >= 0:
        pending_df = pending_df.head(limit).copy()

    return pending_df


def _looks_like_cli_invocation(argv: list[str]) -> bool:
    """Heuristically detect CLI intent so automation need not toggle config first."""
    if not argv:
        return False

    if "--interactive" in argv:
        return False

    cli_flags = {
        "--cli",
        "--help",
        "-h",
        "--output_dir",
        "--dry-run",
        "--log-level",
        "--config",
        "--iiif",
        "--name",
        "--id",
        "--provider",
        "--providers",
        "--enable-provider",
        "--disable-provider",
        "--list-providers",
        "--pending-mode",
        "--entry-ids",
        "--limit",
        "--resume-mode",
        "--selection-strategy",
        "--min-title-score",
        "--creator-weight",
        "--max-candidates-per-provider",
        "--download-strategy",
        "--keep-non-selected-metadata",
        "--no-keep-non-selected-metadata",
        "--prefer-pdf-over-images",
        "--no-prefer-pdf-over-images",
        "--download-manifest-renderings",
        "--no-download-manifest-renderings",
        "--max-renderings-per-manifest",
        "--rendering-mime-whitelist",
        "--overwrite-existing",
        "--no-overwrite-existing",
        "--include-metadata",
        "--no-include-metadata",
    }

    for token in argv:
        if token in cli_flags:
            return True
        if not token.startswith("-"):
            return True
    return False
