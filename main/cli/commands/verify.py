"""Corpus verification command (``--verify``).

Walks the works already on disk under an output directory and checks each for
integrity: at least one non-empty object, valid magic bytes for ``.pdf`` /
``.epub`` payloads, and (where recorded) a complete expected-vs-downloaded page
count. Works that fail are flipped to ``partial`` status in both ``work.json``
and ``index.csv`` so a later run re-downloads them. This command is read-mostly:
it never deletes files, only reclassifies incomplete works.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from api.core.download import _validate_file_magic_bytes
from main.data.index import read_index_csv, update_index_csv
from main.data.work import update_work_status

logger = logging.getLogger(__name__)

_VALIDATED_EXTS = (".pdf", ".epub")

# Statuses whose work directory is expected to hold objects. Anything else
# (``no_match``, ``failed``, ``deferred``) was never downloaded, so its empty
# directory must not be reported as an incomplete work.
_VERIFIABLE_STATUSES = frozenset({"completed", "partial", ""})
_UNDOWNLOADED_STATUSES = frozenset({"no_match", "failed", "deferred"})


def _object_files(work_dir: str) -> list[str]:
    objects_dir = os.path.join(work_dir, "objects")
    if not os.path.isdir(objects_dir):
        return []
    return [
        os.path.join(objects_dir, name)
        for name in os.listdir(objects_dir)
        if os.path.isfile(os.path.join(objects_dir, name))
        and not name.endswith(".part")
    ]


def _read_work_json(work_dir: str) -> dict[str, Any]:
    path = os.path.join(work_dir, "work.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def verify_work(work_dir: str) -> tuple[bool, str]:
    """Verify a single work directory.

    Returns ``(ok, reason)``. ``ok`` is False when the work is incomplete or
    corrupt; ``reason`` explains the first problem found.
    """
    files = _object_files(work_dir)
    if not files:
        return False, "no downloaded objects"

    nonempty = [p for p in files if _safe_size(p) > 0]
    if not nonempty:
        return False, "all objects are zero bytes"

    for path in files:
        ext = os.path.splitext(path)[1].lower()
        if ext in _VALIDATED_EXTS:
            ok, msg = _validate_file_magic_bytes(path, ext)
            if not ok:
                return False, msg

    meta = _read_work_json(work_dir)
    expected = meta.get("pages_expected")
    downloaded = meta.get("pages_downloaded")
    if (
        isinstance(expected, int)
        and expected > 0
        and isinstance(downloaded, int)
        and downloaded < expected
    ):
        return False, f"incomplete pages ({downloaded}/{expected})"

    return True, "ok"


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _cell_text(value: Any) -> str | None:
    """Return a CSV cell as text, or ``None`` when missing (pandas NaN)."""
    if value is None or value != value:  # NaN never equals itself
        return None
    text = str(value).strip()
    return text or None


def _iter_work_dirs(output_dir: str) -> list[tuple[str, str | None, str | None]]:
    """Return ``(work_dir, work_id, entry_id)`` triples to verify.

    Unions the index.csv rows (authoritative, and the only source of work_id
    and entry_id) with a scan of the output directory. The scan is not a
    fallback: direct-IIIF downloads deliberately write no index row, so a
    mixed corpus would otherwise hide every one of them behind a non-empty
    index. Rows that were never downloaded are skipped, and a work_dir
    recorded relative to a different working directory is retried under
    ``output_dir``.
    """
    triples: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()

    def _key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    df = read_index_csv(output_dir)
    if df is not None and "work_dir" in df.columns:
        for _, row in df.iterrows():
            wd = row.get("work_dir")
            if not isinstance(wd, str) or not wd:
                continue
            status = (_cell_text(row.get("status")) or "").lower()
            if status not in _VERIFIABLE_STATUSES:
                continue
            if not os.path.isdir(wd):
                # Stored paths are relative when --output_dir was relative;
                # re-anchor them on the output directory being verified.
                relocated = os.path.join(output_dir, os.path.basename(wd.rstrip("/\\")))
                if os.path.isdir(relocated):
                    wd = relocated
            if _key(wd) in seen:
                continue
            seen.add(_key(wd))
            triples.append(
                (wd, _cell_text(row.get("work_id")), _cell_text(row.get("entry_id")))
            )

    if os.path.isdir(output_dir):
        for name in sorted(os.listdir(output_dir)):
            wd = os.path.join(output_dir, name)
            if not os.path.isdir(wd) or _key(wd) in seen:
                continue
            if not (
                os.path.isdir(os.path.join(wd, "objects"))
                or os.path.exists(os.path.join(wd, "work.json"))
            ):
                continue
            status = str(_read_work_json(wd).get("status") or "").strip().lower()
            if status in _UNDOWNLOADED_STATUSES:
                continue
            triples.append((wd, None, None))
    return triples


def run_verify(output_dir: str) -> dict[str, int]:
    """Verify every work under ``output_dir`` and flip failures to ``partial``.

    Returns a stats dict with ``total``, ``ok``, and ``partial`` counts.
    """
    triples = _iter_work_dirs(output_dir)
    stats = {"total": 0, "ok": 0, "partial": 0}

    for work_dir, work_id, entry_id in triples:
        stats["total"] += 1
        ok, reason = verify_work(work_dir)
        if ok:
            stats["ok"] += 1
            continue

        stats["partial"] += 1
        logger.warning("VERIFY: '%s' -> partial (%s)", work_dir, reason)

        work_json_path = os.path.join(work_dir, "work.json")
        if os.path.exists(work_json_path):
            update_work_status(work_json_path, "partial")

        if work_id:
            meta = _read_work_json(work_dir)
            update_index_csv(
                output_dir,
                {
                    "work_id": work_id,
                    "entry_id": entry_id,
                    "work_dir": work_dir,
                    "status": "partial",
                    "pages_expected": meta.get("pages_expected"),
                    "pages_downloaded": meta.get("pages_downloaded"),
                },
            )

    logger.info(
        "Verify complete: %d work(s) checked, %d ok, %d flipped to partial",
        stats["total"],
        stats["ok"],
        stats["partial"],
    )
    return stats


__all__ = ["run_verify", "verify_work"]
