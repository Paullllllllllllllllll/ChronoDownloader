"""The works CSV: columns, aliases, warnings and refusals."""

from __future__ import annotations

from pathlib import Path

import pytest

from chronodownloader.inputs import InputError, read_works
from chronodownloader.output.store import WorkInput


def csv_file(tmp_path: Path, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / "works.csv"
    path.write_bytes(text.encode(encoding))
    return path


def test_every_column_is_read_and_empty_cells_are_none(tmp_path: Path) -> None:
    path = csv_file(
        tmp_path,
        "entry_id,title,creator,year,place,publisher,language,edition,link\n"
        "E1, Le cuisinier françois ,La Varenne,1651,Paris,,fr,2e,\n",
    )

    works = read_works(path)

    assert works.works == (
        WorkInput(
            "E1",
            "Le cuisinier françois",
            creator="La Varenne",
            year="1651",
            place="Paris",
            language="fr",
            edition="2e",
        ),
    )
    assert works.warnings == ()


def test_aliases_fill_their_columns_and_a_bom_is_accepted(tmp_path: Path) -> None:
    path = csv_file(
        tmp_path,
        "Entry_ID,short_title,main_author\nE1,Kochbuch,Rumpolt\n",
        "utf-8-sig",
    )

    (work,) = read_works(path).works

    assert (work.entry_id, work.title, work.creator) == ("E1", "Kochbuch", "Rumpolt")


def test_the_canonical_column_wins_over_its_alias(tmp_path: Path) -> None:
    path = csv_file(tmp_path, "entry_id,title,short_title\nE1,Full,Short\nE2,,Only\n")

    titles = [work.title for work in read_works(path).works]

    assert titles == ["Full", "Only"]


def test_unknown_columns_are_named_in_a_warning(tmp_path: Path) -> None:
    path = csv_file(tmp_path, "entry_id,title,notes,shelfmark\nE1,T,x,y\n")

    works = read_works(path)

    assert len(works.works) == 1
    assert works.warnings == (f"{path}: unknown columns ignored: notes, shelfmark",)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("title\nT\n", "missing required column(s): entry_id"),
        ("entry_id,creator\nE1,C\n", "missing required column(s): title"),
        ("entry_id,title\nE1,A\nE2,B\nE1,C\n", "duplicate entry_id values: E1"),
        ("entry_id,title\nE1,\n", "line 2: the row has no title"),
        ("entry_id,title\n,T\n", "line 2: the row has no entry_id"),
        ("", "has no header row"),
    ],
)
def test_unusable_files_raise_an_input_error(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(
        InputError, match=message.replace("(", r"\(").replace(")", r"\)")
    ):
        read_works(csv_file(tmp_path, text))


def test_a_missing_or_non_utf8_file_raises(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="not found"):
        read_works(tmp_path / "none.csv")
    latin1 = csv_file(tmp_path, "entry_id,title\nE1,Kochbücher\n", "latin-1")
    with pytest.raises(InputError, match="not UTF-8"):
        read_works(latin1)


def test_the_file_is_never_rewritten(tmp_path: Path) -> None:
    path = csv_file(tmp_path, "entry_id,title,extra\nE1,T,x\n")
    before = path.read_bytes()

    read_works(path)

    assert path.read_bytes() == before
    assert InputError("x").exit_code == 2
