"""The folder listing behind reconciliation and ``verify``: one listing per
folder, every answer the one ``Path.is_file`` and ``stat`` give."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chronodownloader.output._disk import FolderListing


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    objects = tmp_path / "work" / "objects"
    objects.mkdir(parents=True)
    (objects / "p00001.jpg").write_bytes(b"page")
    (objects / "p00002.jpg").mkdir()
    (objects / "Upper.jpg").write_bytes(b"case")
    (tmp_path / "work" / "metadata.json").write_bytes(b"{}")
    return tmp_path / "work"


PATHS = (
    "objects/p00001.jpg",
    "objects/p00002.jpg",
    "objects/p00003.jpg",
    "objects/upper.jpg",
    "objects/Upper.jpg",
    "metadata.json",
    "missing/p00001.jpg",
)


@pytest.mark.parametrize("path", PATHS)
def test_a_listed_answer_is_the_one_of_the_file_itself(
    work_dir: Path, path: str
) -> None:
    listing = FolderListing(work_dir)
    target = work_dir / path
    expected = target.stat().st_size if target.is_file() else None

    assert listing.is_file(path) == target.is_file()
    assert listing.size(path) == expected
    assert listing.size(path, fresh=True) == expected


def test_each_folder_is_listed_once(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[str] = []
    scandir = os.scandir

    def counted(path: Path) -> object:
        listed.append(Path(path).name)
        return scandir(path)

    monkeypatch.setattr(os, "scandir", counted)
    listing = FolderListing(work_dir)
    assert listing.is_file("objects/p00001.jpg")
    assert listing.size("objects/Upper.jpg") == 4
    assert listing.is_file("metadata.json")
    assert sorted(listed) == ["objects", "work"]


def test_a_size_comes_from_the_listing_unless_fresh(work_dir: Path) -> None:
    listing = FolderListing(work_dir)
    assert listing.size("objects/p00001.jpg") == 4
    (work_dir / "objects" / "p00001.jpg").write_bytes(b"longer page")

    assert listing.size("objects/p00001.jpg", fresh=True) == 11


def test_may_hold_is_false_only_without_an_entry_of_the_name(work_dir: Path) -> None:
    listing = FolderListing(work_dir)
    assert not listing.may_hold("objects/.staging")
    assert listing.may_hold("objects/UPPER.JPG")
    assert listing.may_hold("objects/p00002.jpg")
    (work_dir / "objects" / ".Staging").mkdir()

    assert FolderListing(work_dir).may_hold("objects/.staging")
    assert not FolderListing(work_dir).may_hold("absent/.staging")


def test_a_folder_that_cannot_be_listed_is_unknown_not_empty(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scandir = os.scandir

    def refusing(path: Path) -> object:
        if Path(path).name == "objects":
            raise PermissionError(13, "listing refused", str(path))
        return scandir(path)

    monkeypatch.setattr(os, "scandir", refusing)
    listing = FolderListing(work_dir)

    assert listing.may_hold("objects/.staging")
    assert listing.is_file("objects/p00001.jpg")
    assert not listing.is_file("objects/p00002.jpg")
    assert listing.size("objects/p00001.jpg") == 4
