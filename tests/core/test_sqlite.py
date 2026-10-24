"""The SQLite helper: transactions, pragmas, busy bounds and robustness.

Ported from ChronoTranscriber's ``tests/common/test_sqlite.py`` and
``test_sqlite_robustness.py`` for the synchronous helper. The subprocess
tests run ``tests/core/sqlite_child.py``.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from chronodownloader import sqlite as sqlite_module
from chronodownloader.sqlite import (
    DatabaseBusyError,
    DatabaseFullError,
    TableSpec,
    connect,
    is_busy,
    read,
    read_rows,
    table_names,
    transact,
)
from tests.core.spawn import CHILD_TIMEOUT, child, expect, send

DOCUMENTS = TableSpec(
    name="documents",
    ddl=(
        "CREATE TABLE IF NOT EXISTS documents ("
        "doc_hash TEXT PRIMARY KEY, path TEXT, model TEXT)"
    ),
    key=("doc_hash",),
)
ROWS = TableSpec(
    name="rows",
    ddl=(
        "CREATE TABLE IF NOT EXISTS rows ("
        "id INTEGER PRIMARY KEY, label TEXT NOT NULL, seq INTEGER NOT NULL, "
        "payload BLOB)"
    ),
    key=("id",),
)
TABLES = (DOCUMENTS, ROWS)
PRAGMAS = ("journal_mode", "synchronous", "foreign_keys", "busy_timeout")


def _pragmas(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        name: connection.execute(f"PRAGMA {name}").fetchone()[0] for name in PRAGMAS
    }


def _assert_pragmas(values: dict[str, Any]) -> None:
    assert values["journal_mode"] == "delete"
    assert values["synchronous"] == 2
    assert values["foreign_keys"] == 1
    assert values["busy_timeout"] > 0


def _insert(label: str, seq: int) -> Any:
    def write(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO rows (label, seq) VALUES (?, ?)", (label, seq))

    return write


def _labels(db: Path) -> list[tuple[str, int]]:
    return [
        (row["label"], row["seq"]) for row in read_rows(db, "rows", order_by=["id"])
    ]


def _integrity(db: Path) -> str:
    return str(read(db, lambda c: c.execute("PRAGMA integrity_check").fetchone()[0]))


@contextlib.contextmanager
def _writer(db: Path, **kwargs: Any) -> Any:
    connection = connect(db, TABLES, **kwargs)
    try:
        yield connection
    finally:
        connection.close()


def _seed(db: Path, label: str, count: int) -> None:
    with _writer(db) as connection:
        for seq in range(count):
            transact(connection, _insert(label, seq))


def test_transaction_returns_the_result_of_the_function(tmp_path: Path) -> None:
    db = tmp_path / "out" / "tool.sqlite"

    def insert(connection: sqlite3.Connection) -> int:
        cursor = connection.execute(
            "INSERT INTO documents (doc_hash, path) VALUES ('a', 'a.pdf')"
        )
        assert connection.in_transaction
        return int(cursor.rowcount)

    with _writer(db) as connection:
        assert transact(connection, insert) == 1
        assert not connection.in_transaction

    assert read_rows(db, "documents")[0]["path"] == "a.pdf"


def test_transaction_rolls_back_and_reraises(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"

    def fail(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO documents (doc_hash) VALUES ('lost')")
        raise LookupError("no such page")

    with _writer(db) as connection:
        with pytest.raises(LookupError, match="no such page"):
            transact(connection, fail)
        transact(
            connection,
            lambda c: c.execute("INSERT INTO documents (doc_hash) VALUES ('kept')"),
        )

    assert [row["doc_hash"] for row in read_rows(db, "documents")] == ["kept"]


def test_writer_connection_uses_the_rollback_journal(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"

    with _writer(db, busy_timeout=2.0) as connection:
        values = transact(connection, _pragmas)

    _assert_pragmas(values)
    assert values["busy_timeout"] == 2000


def test_read_helper_opens_read_only_connections_with_the_pragmas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "a", 1)
    targets: list[tuple[Any, bool]] = []
    real_connect = sqlite3.connect

    def spy(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        targets.append((database, kwargs.get("uri", False)))
        connection: sqlite3.Connection = real_connect(database, *args, **kwargs)
        return connection

    monkeypatch.setattr(sqlite3, "connect", spy)

    _assert_pragmas(read(db, _pragmas))
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        read(db, lambda c: c.execute("INSERT INTO documents (doc_hash) VALUES ('x')"))
    read_rows(db, "rows")
    table_names(db)

    assert len(targets) == 4
    for database, uri in targets:
        assert uri is True
        assert str(database).startswith("file:")
        assert str(database).endswith("?mode=ro")


def test_connect_switches_a_wal_database_to_the_rollback_journal(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "out"
    folder.mkdir()
    db = folder / "tool.sqlite"
    with contextlib.closing(sqlite3.connect(db)) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.execute(DOCUMENTS.ddl)
        connection.execute("INSERT INTO documents (doc_hash) VALUES ('old')")
        connection.commit()
    assert db.read_bytes()[18:20] == b"\x02\x02"

    with _writer(db) as connection:
        transact(
            connection,
            lambda c: c.execute("INSERT INTO documents (doc_hash) VALUES ('new')"),
        )
        _assert_pragmas(transact(connection, _pragmas))

    assert db.read_bytes()[18:20] == b"\x01\x01"
    assert [path.name for path in folder.iterdir()] == ["tool.sqlite"]
    assert [row["doc_hash"] for row in read_rows(db, "documents")] == ["old", "new"]


def test_read_helper_switches_an_idle_wal_database(tmp_path: Path) -> None:
    folder = tmp_path / "out"
    folder.mkdir()
    db = folder / "legacy.sqlite"
    with contextlib.closing(sqlite3.connect(db)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(DOCUMENTS.ddl)
        connection.execute("INSERT INTO documents (doc_hash) VALUES ('old')")
        connection.commit()

    assert read(db, _pragmas)["journal_mode"] == "delete"
    assert [path.name for path in folder.iterdir()] == ["legacy.sqlite"]


def test_recovery_connection_uses_the_rollback_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "a", 1)
    modes: list[str] = []
    real_configure = sqlite_module.configure

    def spy(connection: sqlite3.Connection, busy_timeout: float) -> None:
        real_configure(connection, busy_timeout)
        modes.append(connection.execute("PRAGMA journal_mode").fetchone()[0])

    monkeypatch.setattr(sqlite_module, "configure", spy)
    sqlite_module._recover(db, 1.0)

    assert modes == ["delete"]


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"
    children = TableSpec(
        name="children",
        ddl=(
            "CREATE TABLE IF NOT EXISTS children (id INTEGER PRIMARY KEY, "
            "doc_hash TEXT NOT NULL REFERENCES documents (doc_hash))"
        ),
        key=("id",),
    )

    connection = connect(db, (*TABLES, children))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            transact(
                connection,
                lambda c: c.execute(
                    "INSERT INTO children (id, doc_hash) VALUES (1, 'none')"
                ),
            )
    finally:
        connection.close()

    assert read_rows(db, "children") == []


def test_a_failed_commit_rolls_back_and_later_writes_work(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"
    children = TableSpec(
        name="children",
        ddl=(
            "CREATE TABLE IF NOT EXISTS children (id INTEGER PRIMARY KEY, "
            "doc_hash TEXT NOT NULL REFERENCES documents (doc_hash) "
            "DEFERRABLE INITIALLY DEFERRED)"
        ),
        key=("id",),
    )

    connection = connect(db, (*TABLES, children))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            transact(
                connection,
                lambda c: c.execute(
                    "INSERT INTO children (id, doc_hash) VALUES (1, 'none')"
                ),
            )
        assert not connection.in_transaction
        transact(
            connection,
            lambda c: c.execute("INSERT INTO documents (doc_hash) VALUES ('kept')"),
        )
    finally:
        connection.close()

    assert read_rows(db, "children") == []
    assert [row["doc_hash"] for row in read_rows(db, "documents")] == ["kept"]


def test_table_spec_validation() -> None:
    with pytest.raises(ValueError):
        TableSpec(name="pages", ddl="", key=())
    with pytest.raises(ValueError):
        TableSpec(name="bad name", ddl="", key=("id",))
    with pytest.raises(ValueError):
        read_rows(Path("unused.sqlite"), 'x"; --')


def test_writing_and_reading_leave_only_the_database_file(tmp_path: Path) -> None:
    folder = tmp_path / "out"
    db = folder / "tool.sqlite"
    _seed(db, "a", 1)

    assert [path.name for path in folder.iterdir()] == ["tool.sqlite"]
    assert _labels(db) == [("a", 0)]
    assert table_names(db) == ["documents", "rows"]
    assert [path.name for path in folder.iterdir()] == ["tool.sqlite"]
    with pytest.raises(FileNotFoundError):
        read_rows(folder / "missing.sqlite", "rows")
    assert not (folder / "missing.sqlite").exists()


def test_reopening_keeps_rows_and_lists_tables(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "a", 1)
    _seed(db, "b", 1)

    assert table_names(db) == ["documents", "rows"]
    assert _labels(db) == [("a", 0), ("b", 0)]


def test_two_connections_in_threads_share_one_file(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "seed", 0)

    def write(label: str) -> None:
        with _writer(db, busy_timeout=10.0) as connection:
            for seq in range(50):
                transact(connection, _insert(label, seq), busy_timeout=10.0)

    threads = [threading.Thread(target=write, args=(label,)) for label in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(_labels(db)) == sorted(
        [(label, seq) for label in "ab" for seq in range(50)]
    )
    assert read(db, lambda c: c.execute("PRAGMA journal_mode").fetchone()[0]) == (
        "delete"
    )


def test_a_busy_reader_delays_the_commit_without_error(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "before", 1)
    reader = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM rows").fetchone()[0] == 1

    def finish() -> None:
        reader.execute("COMMIT")
        reader.close()

    timer = threading.Timer(1.0, finish)
    with _writer(db, busy_timeout=0.2) as connection:
        timer.start()
        started = time.monotonic()
        transact(connection, _insert("during", 0), busy_timeout=10.0)
        waited = time.monotonic() - started
    timer.join()

    assert waited >= 0.8
    assert _labels(db) == [("before", 0), ("during", 0)]


def _hold_read(db: Path) -> sqlite3.Connection:
    reader = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM rows").fetchone()
    return reader


def test_another_writer_proceeds_during_the_commit_backoff(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "before", 1)
    reader = _hold_read(db)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            with _writer(db, busy_timeout=0.1) as connection:
                transact(connection, _insert("a", 0), busy_timeout=30.0)
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            errors.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    time.sleep(0.3)
    other = sqlite3.connect(db, isolation_level=None, timeout=5.0)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO rows (label, seq) VALUES ('b', 0)")
        reader.execute("COMMIT")
        reader.close()
        other.execute("COMMIT")
    finally:
        other.close()
    thread.join(60)

    assert errors == []
    assert sorted(_labels(db)) == [("a", 0), ("b", 0), ("before", 0)]


def test_an_interrupt_during_the_backoff_leaves_no_open_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "before", 1)
    reader = _hold_read(db)

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    try:
        with _writer(db, busy_timeout=0.05) as connection:
            monkeypatch.setattr(time, "sleep", interrupt)
            with pytest.raises(KeyboardInterrupt):
                transact(connection, _insert("lost", 0), busy_timeout=10.0)
            monkeypatch.undo()
            assert not connection.in_transaction
            reader.execute("COMMIT")
            other = sqlite3.connect(db, isolation_level=None, timeout=0.1)
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            other.close()
    finally:
        reader.close()

    assert _labels(db) == [("before", 0)]


def test_busy_retries_end_after_the_total_wait(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "before", 1)
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with _writer(db, busy_timeout=0.3) as connection:
            started = time.monotonic()
            with pytest.raises(DatabaseBusyError) as raised:
                transact(connection, _insert("blocked", 0), busy_timeout=0.3)
            waited = time.monotonic() - started
            assert not connection.in_transaction
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert 0.25 <= waited < 3.0
    assert raised.value.waited >= 0.25
    assert isinstance(raised.value, sqlite3.OperationalError)
    assert not is_busy(raised.value)
    assert _labels(db) == [("before", 0)]


def test_busy_open_ends_after_the_total_wait(tmp_path: Path) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "before", 1)
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(DatabaseBusyError):
            read(db, _pragmas, busy_timeout=0.2)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_a_full_database_raises_the_typed_error_and_keeps_earlier_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "tool.sqlite"

    def overflow(connection: sqlite3.Connection) -> None:
        (pages,) = connection.execute("PRAGMA page_count").fetchone()
        connection.execute(f"PRAGMA max_page_count = {pages + 2}")
        for seq in range(200):
            connection.execute(
                "INSERT INTO rows (label, seq, payload) "
                "VALUES ('overflow', ?, randomblob(4000))",
                (seq,),
            )

    with _writer(db) as connection:
        for seq in range(10):
            transact(connection, _insert("kept", seq))
        with pytest.raises(DatabaseFullError) as raised:
            transact(connection, overflow)
        assert isinstance(raised.value, sqlite3.OperationalError)
        assert isinstance(raised.value.__cause__, sqlite3.Error)

    assert _labels(db) == [("kept", seq) for seq in range(10)]
    assert _integrity(db) == "ok"


def test_two_processes_commit_400_transactions_into_one_database(
    tmp_path: Path,
) -> None:
    db = tmp_path / "race.sqlite"
    with child("sqlite_child.py", "race", str(db), "a", "200") as first:
        expect(first, "READY")
        with child("sqlite_child.py", "race", str(db), "b", "200") as second:
            expect(second, "READY")
            send(first)
            send(second)
            expect(first, "DONE")
            expect(second, "DONE")
            assert first.wait(CHILD_TIMEOUT) == 0
            assert second.wait(CHILD_TIMEOUT) == 0

    rows = _labels(db)
    assert len(rows) == 400
    assert sorted(rows) == sorted(
        [("a", seq) for seq in range(200)] + [("b", seq) for seq in range(200)]
    )
    assert _integrity(db) == "ok"
    assert read(db, lambda c: c.execute("PRAGMA journal_mode").fetchone()[0]) == (
        "delete"
    )


def _kill_inside_transaction(db: Path) -> Path:
    journal = db.with_name(db.name + "-journal")
    with child("sqlite_child.py", "hot", str(db)) as process:
        expect(process, "READY")
        process.kill()
        process.wait(CHILD_TIMEOUT)
    return journal


def test_connect_rolls_back_the_hot_journal_of_a_killed_child(
    tmp_path: Path,
) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "before", 10)
    size_before_child = db.stat().st_size

    journal = _kill_inside_transaction(db)

    assert journal.is_file()
    assert db.stat().st_size > size_before_child
    with _writer(db) as connection:
        assert _pragmas(connection)["journal_mode"] == "delete"

    assert not journal.exists()
    assert _integrity(db) == "ok"
    rows = _labels(db)
    assert [label for label, _ in rows] == ["before"] * 10 + ["committed"] * 5


def test_read_helper_recovers_the_hot_journal_of_a_killed_child(
    tmp_path: Path,
) -> None:
    db = tmp_path / "tool.sqlite"
    _seed(db, "before", 3)

    journal = _kill_inside_transaction(db)

    assert journal.is_file()
    rows = _labels(db)
    assert [label for label, _ in rows] == ["before"] * 3 + ["committed"] * 5
    assert not journal.exists()
    assert _integrity(db) == "ok"
