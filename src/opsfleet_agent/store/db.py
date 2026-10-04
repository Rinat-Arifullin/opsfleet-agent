"""App SQLite store: WAL, secure_delete, foreign keys, versioned migrations.

Checkpoints use a separate file (HLD §8, §7.2); never mix them into this one.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_MS = 5000

# Version 1 is the meta/version table only; feature tables arrive in later iterations.
MIGRATIONS: Sequence[tuple[int, Sequence[str]]] = (
    (1, ("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",)),
)


class StoreError(Exception):
    pass


class SchemaTooNewError(StoreError):
    """The database schema is newer than this code knows."""


def _secure_files(path: str | Path) -> None:
    """DB, WAL and SHM files are owner-only (0600)."""
    base = str(path)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(base + suffix, 0o600)
        except FileNotFoundError:
            pass


def connect(path: str | Path, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a connection with the required pragmas, verified by reading them back."""
    # Pre-create the main file as 0600: SQLite gives -wal/-shm the same permissions.
    os.close(os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600))
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        if str(mode).lower() != "wal":
            raise StoreError("journal_mode WAL not enabled")
        if conn.execute("PRAGMA secure_delete").fetchone()[0] != 1:
            raise StoreError("secure_delete not enabled")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StoreError("foreign_keys not enabled")
        _secure_files(path)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Single-writer transaction: BEGIN IMMEDIATE (waits up to the busy timeout)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def checkpoint_truncate(conn: sqlite3.Connection) -> None:
    """Fold the WAL into the main file and truncate it (so deleted data leaves no WAL copy)."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


def migrate(
    conn: sqlite3.Connection, migrations: Sequence[tuple[int, Sequence[str]]] = MIGRATIONS
) -> int:
    """Apply pending migrations in order, each in its own transaction. Returns the version."""
    with write_tx(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
    known = max((v for v, _ in migrations), default=0)
    found = current_version(conn)
    if found > known:
        raise SchemaTooNewError(f"database schema version {found} is newer than supported {known}")
    for version, statements in sorted(migrations, key=lambda m: m[0]):
        with write_tx(conn):
            if version <= current_version(conn):
                continue
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    return current_version(conn)


def open_store(path: str | Path) -> sqlite3.Connection:
    """Connect and migrate to the latest schema."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    _secure_files(path)
    return conn
