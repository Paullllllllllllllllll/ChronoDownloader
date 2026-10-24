"""Child process for the store's multi-process and crash tests.

Usage, from the repository root; ROOT is the output root. Every mode first
prints ``READY``.

    python tests/output/store_child.py serve ROOT RUN_ID
        Answer one command per stdin line until ``exit``: ``acquire ENTRY``
        prints ``LEASE <generation>`` or ``BLOCKED``; ``release ENTRY``
        prints ``RELEASED``. ``exit`` closes the store, which releases the
        leases.

    python tests/output/store_child.py crash ROOT RUN_ID ENTRY RENAMES
        Lease ENTRY, stage the pages p00001 to p00003 (content ``new``) and
        commit them to ``objects/<item>.jpg``; the process ends with exit code
        3 right after the RENAMES-th rename of the commit, before COMMIT.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chronodownloader.output import _promotion  # noqa: E402
from chronodownloader.output.store import (  # noqa: E402
    Promotion,
    Store,
    WorkInput,
)
from chronodownloader.providers.base import (  # noqa: E402
    Completeness,
    DownloadResult,
    StagedFile,
)

CRASH_EXIT = 3


def _say(text: str) -> None:
    print(text, flush=True)  # noqa: T201 - the parent reads stdout


def folder_of(entry_id: str) -> str:
    """Return the work folder the tests use for *entry_id*."""
    return f"{entry_id}_work"


def serve(argv: list[str]) -> None:
    root, run_id = argv
    leases = {}
    with Store(Path(root)) as store:
        store.begin_run("download", run_id=run_id)
        _say("READY")
        for raw in sys.stdin:
            words = raw.split()
            if not words or words[0] == "exit":
                break
            verb, entry_id = words
            if verb == "acquire":
                lease = store.acquire(
                    WorkInput(entry_id, f"Title {entry_id}"), folder_of(entry_id)
                )
                if lease is None:
                    _say("BLOCKED")
                else:
                    leases[entry_id] = lease
                    _say(f"LEASE {lease.generation}")
            elif verb == "release":
                store.release(leases.pop(entry_id))
                _say("RELEASED")
            else:
                raise SystemExit(f"unknown command {verb!r}")


def crash(argv: list[str]) -> None:
    root, run_id, entry_id, renames = argv
    store = Store(Path(root))
    store.begin_run("download", run_id=run_id)
    lease = store.acquire(WorkInput(entry_id, f"Title {entry_id}"), folder_of(entry_id))
    if lease is None:
        raise SystemExit("work is leased by another run")
    staging = store.staging_dir(lease)
    items = ("p00001", "p00002", "p00003")
    files = []
    for item in items:
        (staging / f"{item}.jpg").write_bytes(b"new")
        files.append(StagedFile(f"{item}.jpg", "image", item, 3, "image/jpeg", ""))
    result = DownloadResult(
        "mdz",
        "bsb1",
        "images",
        (Completeness("images", items, items),),
        tuple(files),
    )
    done = 0
    rename = _promotion.rename

    def crashing(source: Path, target: Path) -> None:
        nonlocal done
        rename(source, target)
        done += 1
        if done >= int(renames):
            os._exit(CRASH_EXIT)

    _promotion.rename = crashing
    _say("READY")
    store.commit_download(
        lease,
        result,
        [Promotion(file, f"objects/{file.item}.jpg") for file in files],
        status="complete",
    )
    _say("COMMITTED")


def main(argv: list[str]) -> None:
    modes = {"serve": serve, "crash": crash}
    mode = argv[0]
    if mode not in modes:
        raise SystemExit(f"unknown mode {mode!r}")
    modes[mode](argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
