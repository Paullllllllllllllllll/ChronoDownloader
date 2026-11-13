"""Checks of many recorded files of a work folder, one listing per folder.

:class:`FolderListing` lists each folder of a work once with
:func:`os.scandir` and looks a recorded path up by its exact name. A name the
listing lacks, or holds as anything but a regular file, is checked with its
own ``stat``, so every answer is the one :meth:`pathlib.Path.is_file` gives,
case-insensitive file systems (a name that differs in case only) and a file
replaced by a directory included.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["FolderListing"]


def _same_name(left: str, right: str) -> bool:
    """Return True when the names may be one on a case-insensitive file system."""
    return left.casefold() == right.casefold() or left.upper() == right.upper()


class FolderListing:
    """The entries of the folders of *work_dir*, each folder listed once.

    Paths are relative to *work_dir* with ``/`` between their parts, as the
    ``files`` rows record them. A listing is taken on first use and kept, so
    a change on disk after it is not seen; use one instance per check.
    """

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self._folders: dict[str, dict[str, os.DirEntry[str]] | None] = {}

    def _entries(self, folder: str) -> dict[str, os.DirEntry[str]] | None:
        """Return *folder*'s entries by name; None when it cannot be listed.

        A folder that does not exist has no entries.
        """
        if folder not in self._folders:
            entries: dict[str, os.DirEntry[str]] | None = None
            try:
                with os.scandir(self.work_dir / folder) as found:
                    entries = {entry.name: entry for entry in found}
            except FileNotFoundError:
                entries = {}
            except OSError:
                pass
            self._folders[folder] = entries
        return self._folders[folder]

    def _file_entry(self, path: str) -> os.DirEntry[str] | None:
        """Return the listed regular file of *path*'s exact name, or None."""
        folder, _, name = path.rpartition("/")
        entries = self._entries(folder)
        entry = None if entries is None else entries.get(name)
        try:
            return entry if entry is not None and entry.is_file() else None
        except OSError:
            return None

    def is_file(self, path: str) -> bool:
        """Return True when a regular file is at *path*."""
        if self._file_entry(path) is not None:
            return True
        return (self.work_dir / path).is_file()

    def size(self, path: str, *, fresh: bool = False) -> int | None:
        """Return the size of the file at *path*, None when no file is there.

        The size comes from the listing; *fresh* reads it with its own
        ``stat`` instead.
        """
        entry = None if fresh else self._file_entry(path)
        if entry is not None:
            try:
                return entry.stat().st_size
            except OSError:
                pass
        target = self.work_dir / path
        if not target.is_file():
            return None
        return target.stat().st_size

    def may_hold(self, path: str) -> bool:
        """Return False only when *path*'s folder has no entry of its name.

        Names are compared without case, so a False answer holds on every
        file system; a True one, also given for a folder that cannot be
        listed, needs a check of its own.
        """
        folder, _, name = path.rpartition("/")
        entries = self._entries(folder)
        if entries is None or name in entries:
            return True
        return any(_same_name(name, other) for other in entries)
