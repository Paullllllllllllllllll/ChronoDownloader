"""Work directory and status management for ChronoDownloader.

This module encapsulates work directory creation, status checking,
work.json file management, and naming utilities extracted from pipeline.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from api.core.config import get_config, get_resume_mode
from api.core.naming import build_work_directory_name
from api.matching import normalize_text

logger = logging.getLogger(__name__)


def get_naming_config() -> Dict[str, Any]:
    """Get naming configuration with defaults.
    
    Returns:
        Dictionary with naming preferences for work directories and files
    """
    cfg = get_config()
    nm = dict(cfg.get("naming", {}) or {})
    
    nm.setdefault("include_creator_in_work_dir", True)
    nm.setdefault("include_year_in_work_dir", True)
    nm.setdefault("title_slug_max_len", 80)
    
    return nm


def compute_work_id(title: str, creator: str | None) -> str:
    """Generate a stable hash-based work ID from title and creator.
    
    Args:
        title: Work title
        creator: Optional creator name
        
    Returns:
        10-character hex hash identifier
    """
    norm = f"{normalize_text(title)}|{normalize_text(creator) if creator else ''}"
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
    return h


def compute_work_dir(
    base_output_dir: str,
    entry_id: str | None,
    title: str,
) -> Tuple[str, str]:
    """Compute the work directory path and name.
    
    Args:
        base_output_dir: Base directory for downloaded works
        entry_id: Optional entry identifier
        title: Work title
        
    Returns:
        Tuple of (work_dir_path, work_dir_name)
    """
    naming_cfg = get_naming_config()
    work_dir_name = build_work_directory_name(
        entry_id,
        title,
        max_len=int(naming_cfg.get("title_slug_max_len", 80))
    )
    work_dir = os.path.join(base_output_dir, work_dir_name)
    return work_dir, work_dir_name


def check_work_status(work_dir: str, resume_mode: str | None = None) -> Tuple[bool, str]:
    """Check if a work should be skipped based on resume mode and existing state.
    
    Args:
        work_dir: Path to the work directory
        resume_mode: Resume mode from config. If None, reads from config.
        
    Returns:
        Tuple of (should_skip, reason). If should_skip is True, the work should be skipped.
    """
    if resume_mode is None:
        resume_mode = get_resume_mode()
    
    if resume_mode == "reprocess_all":
        return False, ""
    
    if not os.path.isdir(work_dir):
        return False, ""
    
    work_json_path = os.path.join(work_dir, "work.json")
    objects_dir = os.path.join(work_dir, "objects")
    
    if resume_mode == "skip_completed":
        if os.path.exists(work_json_path):
            try:
                with open(work_json_path, "r", encoding="utf-8") as f:
                    work_meta = json.load(f)
                status = work_meta.get("status", "")
                if status == "completed":
                    return True, "status=completed in work.json"
            except Exception:
                pass
    
    elif resume_mode == "skip_if_has_objects":
        if os.path.isdir(objects_dir):
            try:
                files = [f for f in os.listdir(objects_dir) if os.path.isfile(os.path.join(objects_dir, f))]
                if files:
                    return True, f"objects/ contains {len(files)} file(s)"
            except Exception:
                pass
    
    return False, ""


def update_work_status(
    work_json_path: str,
    status: str,
    download_info: Dict[str, Any] | None = None,
) -> None:
    """Update the status field in work.json.
    
    Args:
        work_json_path: Path to work.json file
        status: New status ("completed", "partial", "failed", "deferred", "no_match")
        download_info: Optional dict with download details to merge
    """
    if not os.path.exists(work_json_path):
        return
    
    try:
        with open(work_json_path, "r", encoding="utf-8") as f:
            work_meta = json.load(f)
        
        work_meta["status"] = status
        work_meta["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        
        if download_info:
            work_meta["download"] = download_info
        
        with open(work_json_path, "w", encoding="utf-8") as f:
            json.dump(work_meta, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to update work.json status: %s", e)


def create_work_json(
    work_json_path: str,
    title: str,
    creator: str | None,
    entry_id: str | None,
    selection_config: Dict[str, Any],
    candidates: list,
    selected: Dict[str, Any] | None,
    status: str = "pending",
) -> None:
    """Create the initial work.json file.
    
    Args:
        work_json_path: Path to work.json file
        title: Work title
        creator: Optional creator name
        entry_id: Optional entry identifier
        selection_config: Selection configuration used
        candidates: List of candidate dictionaries
        selected: Selected candidate info or None
        status: Initial status (default: "pending")
    """
    work_meta: Dict[str, Any] = {
        "input": {"title": title, "creator": creator, "entry_id": entry_id},
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "selection": selection_config,
        "candidates": candidates,
        "selected": selected,
    }
    try:
        with open(work_json_path, "w", encoding="utf-8") as f:
            json.dump(work_meta, f, indent=2, ensure_ascii=False)
    except Exception:
        logger.exception("Failed to write work.json to %s", work_json_path)


def format_candidates_for_json(candidates: list) -> list:
    """Format SearchResult candidates for work.json storage.
    
    Args:
        candidates: List of SearchResult objects
        
    Returns:
        List of dictionaries suitable for JSON serialization
    """
    return [
        {
            "provider": sr.provider,
            "provider_key": sr.provider_key,
            "title": sr.title,
            "creators": sr.creators,
            "date": sr.date,
            "source_id": sr.source_id,
            "item_url": sr.item_url,
            "iiif_manifest": sr.iiif_manifest,
            "scores": sr.raw.get("__matching__", {}),
        }
        for sr in candidates
    ]


def format_selected_for_json(selected, source_id: str | None) -> Dict[str, Any] | None:
    """Format selected SearchResult for work.json storage.
    
    Args:
        selected: SearchResult or None
        source_id: Pre-computed source ID
        
    Returns:
        Dictionary or None
    """
    if not selected:
        return None
    return {
        "provider": selected.provider,
        "provider_key": selected.provider_key,
        "source_id": source_id,
        "title": selected.title,
    }


__all__ = [
    "get_naming_config",
    "compute_work_id",
    "compute_work_dir",
    "check_work_status",
    "update_work_status",
    "create_work_json",
    "format_candidates_for_json",
    "format_selected_for_json",
]
