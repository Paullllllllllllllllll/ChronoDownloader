"""SQLite connections on the rollback journal, with bounded busy retries.

Every connection this module opens uses the rollback journal
(``journal_mode=DELETE``, verified) with ``synchronous=FULL``, foreign keys
on and a busy timeout; WAL is never used, because the SQLite bundled with
current Python releases carries the WAL-reset corruption bug. A database
file found in WAL mode is switched to the rollback journal when a
read-write connection opens it.

Writers open a connection with :func:`connect` and run each write through
:func:`transact`: a function run inside ``BEGIN IMMEDIATE ... COMMIT`` that
commits or rolls back as a whole. With the rollback journal a commit waits
until every open read has ended. A transaction that finds the database busy,
at its ``COMMIT`` too, is rolled back and run again after a backoff from
0.05 to 2 s; no lock is held during the backoff. The retries of one call
end after a total wait of *busy_timeout* seconds with
:class:`DatabaseBusyError`; since every statement also waits up to
*busy_timeout* inside SQLite, one call can wait somewhat longer in total.
Any other error (a full disk, a constraint) propagates at once, a full
database as :class:`DatabaseFullError`.

Readers open short read-only connections (``file:...?mode=ro``) through
:func:`read_only`. A hot journal left by a killed writer cannot be rolled
back through such a connection; the helper then opens a read-write
connection once, whose first read rolls the journal back, and reads again.
A busy open is retried under the same bound. The connection then holds one
read transaction until it closes, so its queries see one state and are not
refused later.

Copied from ChronoTranscriber v1.0.0 ``common/sqlite.py`` on 19.10.2026 and
made synchronous: the writer thread, its queue, ``DocumentTransaction``, the
upsert helpers and the busy callbacks are removed; busy retries are bounded
by a total wait instead of running without limit, and a busy ``COMMIT`` is
rolled back and replayed instead of retried while holding the write lock.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

__all__ = [
    "DEFAULT_BUSY_TIMEOUT",
    "DatabaseBusyError",
    "DatabaseFullError",
    "TableSpec",
    "configure",
    "connect",
    "is_busy",
    "read",
    "read_only",
    "read_rows",
    "table_names",
    "transact",
]

DEFAULT_BUSY_TIMEOUT = 30.0
RETRY_DELAY_MIN = 0.05
RETRY_DELAY_MAX = 2.0

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
_READONLY_ROLLBACK = 776
_WAL_HEADER_OFFSET = 18

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DatabaseFullError(sqlite3.OperationalError):
    """The database or its disk is full (``SQLITE_FULL``)."""


class DatabaseBusyError(sqlite3.OperationalError):
    """The database stayed busy past the total wait of one call.

    ``waited`` holds the seconds waited. The error carries no SQLite error
    code, so :func:`is_busy` does not match it and no retry loop repeats it.
    """

    def __init__(self, message: str, waited: float) -> None:
        super().__init__(message)
        self.waited = waited


@dataclass(frozen=True)
class TableSpec:
    """A table: its name, its DDL and the key columns that identify a row.

    The DDL must declare a primary key or unique constraint on exactly the key
    columns, and should use ``IF NOT EXISTS`` so that reopening a database
    works.
    """

    name: str
    ddl: str
    key: tuple[str, ...]

    def __post_init__(self) -> None:
        _quote(self.name)
        if not self.key:
            raise ValueError(f"Table {self.name!r} declares no key columns.")
        for column in self.key:
            _quote(column)


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _error_code(exc: BaseException) -> int:
    return int(getattr(exc, "sqlite_errorcode", 0) or 0)


def is_busy(exc: BaseException) -> bool:
    """Return True for ``SQLITE_BUSY`` and ``SQLITE_LOCKED`` errors."""
    return isinstance(exc, sqlite3.OperationalError) and (
        (_error_code(exc) & 0xFF) in _BUSY_CODES
    )


def _full_error(exc: BaseException) -> DatabaseFullError | None:
    if not isinstance(exc, sqlite3.Error) or isinstance(exc, DatabaseFullError):
        return None
    if (_error_code(exc) & 0xFF) != sqlite3.SQLITE_FULL:
        return None
    full = DatabaseFullError(str(exc))
    full.sqlite_errorcode = _error_code(exc)
    full.sqlite_errorname = getattr(exc, "sqlite_errorname", "SQLITE_FULL")
    return full


def configure(connection: sqlite3.Connection, busy_timeout: float) -> None:
    """Apply the busy timeout, rollback journal, full sync and foreign keys.

    Switching a WAL database to the rollback journal needs every other
    connection to the file closed; otherwise SQLite reports it busy.
    """
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
    (mode,) = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
    if str(mode).lower() != "delete":
        raise sqlite3.OperationalError(
            f"Journal mode stays {mode!r}; close other connections to the file."
        )
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA foreign_keys = ON")


class _Backoff:
    """Waits between busy retries of one call, bounded by a total wait."""

    def __init__(self, what: str, limit: float) -> None:
        self.what = what
        self.limit = limit
        self.started = time.monotonic()
        self.delay = RETRY_DELAY_MIN
        self.warned = False

    def wait(self, exc: BaseException) -> None:
        """Sleep before the next retry; raise once the total wait is spent.

        Raises:
            DatabaseBusyError: *limit* seconds have passed since the start.
        """
        waited = time.monotonic() - self.started
        if waited >= self.limit:
            raise DatabaseBusyError(
                f"Database {self.what} stayed busy for {waited:.1f} s "
                "(another connection holds a transaction).",
                waited,
            ) from exc
        if not self.warned:
            self.warned = True
            logger.warning(
                "Database %s is busy (another connection holds a transaction); "
                "retrying for up to %.0f s.",
                self.what,
                self.limit,
            )
        time.sleep(min(self.delay, self.limit - waited))
        self.delay = min(self.delay * 2, RETRY_DELAY_MAX)


def _database_name(connection: sqlite3.Connection) -> str:
    with contextlib.suppress(sqlite3.Error):
        for _seq, name, file in connection.execute("PRAGMA database_list"):
            if name == "main":
                return str(file)
    return "(unknown file)"


def connect(
    path: Path,
    tables: Sequence[TableSpec] = (),
    *,
    busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open *path* for writing, set its pragmas and create the declared tables.

    The connection runs in autocommit mode (``isolation_level=None``); write
    through :func:`transact`. The parent folder is created when missing.
    *check_same_thread* False lets another thread close the connection.

    Raises:
        DatabaseBusyError: The database stayed busy for *busy_timeout*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        timeout=busy_timeout,
        isolation_level=None,
        check_same_thread=check_same_thread,
    )
    backoff = _Backoff(str(path), busy_timeout)
    try:
        while True:
            try:
                configure(connection, busy_timeout)
                for spec in tables:
                    connection.executescript(spec.ddl)
                break
            except sqlite3.OperationalError as exc:
                if not is_busy(exc):
                    raise
                backoff.wait(exc)
    except BaseException:  # cleanup on every exit, interrupts too; re-raised
        connection.close()
        raise
    return connection


def transact(
    connection: sqlite3.Connection,
    func: Callable[[sqlite3.Connection], T],
    *,
    busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
) -> T:
    """Run *func* with *connection* inside one transaction; return its result.

    ``BEGIN IMMEDIATE`` takes the write lock first, then *func* runs and the
    transaction commits; an error rolls it back and is re-raised, a full
    database as :class:`DatabaseFullError`. When a statement inside *func*
    finds the database busy, the transaction is rolled back and *func* runs
    again, so *func* must act only through the connection. A busy ``COMMIT``
    is handled the same way: the transaction is rolled back before the
    backoff, so no lock is held while waiting, and *func* is replayed. Every
    exceptional exit, an interrupt during the backoff included, leaves no
    transaction open.

    Raises:
        DatabaseBusyError: The retries waited *busy_timeout* seconds in total.
    """
    backoff = _Backoff(_database_name(connection), busy_timeout)
    try:
        while True:
            busy = _attempt(connection, func)
            if not isinstance(busy, _Busy):
                return busy
            backoff.wait(busy.error)
    except BaseException:  # no transaction stays open on any exit; re-raised
        _rollback(connection)
        raise


@dataclass(frozen=True)
class _Busy:
    error: BaseException


def _attempt(
    connection: sqlite3.Connection, func: Callable[[sqlite3.Connection], T]
) -> T | _Busy:
    """Run one transaction; return :class:`_Busy` after a busy rollback."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = func(connection)
        connection.execute("COMMIT")
    # Rolls back on every exit; only a busy error is not re-raised.
    except BaseException as exc:
        _rollback(connection)
        if is_busy(exc):
            return _Busy(exc)
        full = _full_error(exc)
        if full is not None:
            raise full from exc
        raise
    return result


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")


def _is_wal_file(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(_WAL_HEADER_OFFSET + 2)
    return header[_WAL_HEADER_OFFSET : _WAL_HEADER_OFFSET + 2] == b"\x02\x02"


def _open_read_only(path: Path, busy_timeout: float) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri, uri=True, timeout=busy_timeout, isolation_level=None
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except BaseException:  # cleanup on every exit, interrupts too; re-raised
        connection.close()
        raise
    return connection


def _open_recovering(path: Path, busy_timeout: float) -> sqlite3.Connection:
    try:
        return _open_read_only(path, busy_timeout)
    except sqlite3.OperationalError as exc:
        if _error_code(exc) != _READONLY_ROLLBACK:
            raise
    _recover(path, busy_timeout)
    return _open_read_only(path, busy_timeout)


def _recover(path: Path, busy_timeout: float) -> None:
    """Roll back a hot journal and leave WAL mode through a read-write open."""
    connection = sqlite3.connect(path, timeout=busy_timeout, isolation_level=None)
    try:
        configure(connection, busy_timeout)
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        connection.close()


@contextlib.contextmanager
def read_only(
    path: Path, *, busy_timeout: float = DEFAULT_BUSY_TIMEOUT
) -> Iterator[sqlite3.Connection]:
    """Open an existing database through a ``mode=ro`` connection; close it after.

    A hot journal left by a killed writer makes the read-only open fail with
    ``SQLITE_READONLY_ROLLBACK``; a read-write connection then opens once,
    rolls the journal back on its first read and closes, and the read-only
    open is repeated. A file still in WAL mode is switched to the rollback
    journal first when no other connection holds it, and read as it is
    otherwise.

    An open still busy (a writer waiting to commit refuses new reads) is
    retried with backoff for up to *busy_timeout* seconds in total. The
    yielded connection holds a read transaction until it closes, so its
    queries see one state and are never refused as busy.

    Raises:
        FileNotFoundError: No database exists at *path*.
        DatabaseBusyError: The open stayed busy for *busy_timeout*.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No database at {path}")
    if _is_wal_file(path):
        with contextlib.suppress(sqlite3.OperationalError):
            _recover(path, 0.0)
    backoff = _Backoff(str(path), busy_timeout)
    while True:
        try:
            connection = _open_recovering(path, busy_timeout)
            break
        except sqlite3.OperationalError as exc:
            if not is_busy(exc):
                raise
            backoff.wait(exc)
    try:
        yield connection
    finally:
        connection.close()


def read(
    path: Path,
    queries: Callable[[sqlite3.Connection], T],
    *,
    busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
) -> T:
    """Run *queries* on a short read-only connection and return their result.

    The queries run in one read transaction (see :func:`read_only`).
    """
    with read_only(path, busy_timeout=busy_timeout) as connection:
        return queries(connection)


def table_names(path: Path) -> list[str]:
    """Return the names of the tables in the database at *path*, sorted."""
    rows = read(
        path,
        lambda connection: connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall(),
    )
    return [row[0] for row in rows]


def read_rows(
    path: Path, table: str, *, order_by: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """Return the rows of *table* as dicts, ordered by *order_by* or rowid."""
    order = ", ".join(_quote(column) for column in order_by) if order_by else "rowid"
    sql = f"SELECT * FROM {_quote(table)} ORDER BY {order}"

    def rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        cursor = connection.execute(sql)
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    return read(path, rows)
