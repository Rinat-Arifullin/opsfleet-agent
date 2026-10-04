"""Per-user aggregate fingerprints for the differencing guard (FR-70, AC-08.7, AC-08.15; HLD §5.5).

One row per released QI aggregate, keyed by user, kept 30 days and read across all sessions of
that user. A row holds **no values**: the scope key (already a hash, or ``all``) and keyed
digests (HMAC-SHA256) of the cell's dimensions, the columns each dimension derives from, the
value of each dimension fixed by an equality filter, the columns of every other filter, each
filter predicate (audit only) and each result cell's identity, plus each cell's
distinct-customer count and row count (round 2, R2-H1). No literal, no group value, no
result figure is stored in clear (owner item: the HLD says "literals included"; this store
keeps only their digests, see ``guards/differencing.py``).

The HMAC key is generated once per app DB and kept in ``meta``. It only stops precomputed
dictionaries across installations: whoever can read this DB can also read the key and test
guesses, so the digests are pseudonyms, not encryption (the DB file is owner-only, §7.2).

Fails closed: every SQLite error, a missing table, an outdated table layout, a closed
connection or bad input raises :class:`FingerprintStoreError`, and the guard turns that into a
refusal (SEC-12).

**Check and record are one transaction** (:meth:`FingerprintStore.check_and_record`, review
M-3): the candidates are read and the new row is inserted under one ``BEGIN IMMEDIATE``, so two
concurrent releases of one user cannot both pass against a state that lacks the other.

Schema: created idempotently by :func:`ensure_schema` (``CREATE ... IF NOT EXISTS``) like
``store/feedback.py``, so it does not collide with other iterations' numbered migrations.
``FINGERPRINT_MIGRATION`` holds the same statements for folding into ``store.db.MIGRATIONS``
at integration. A table with the pre-review layout (``group_key``, no ``dims``) is not
migrated: the store refuses to open (owner item; delete the dev DB or the table).

Round 2 (re-review): the per-cell row count rides in the existing ``cells`` JSON as a third
element of each entry (``[key, customers, rows]``; ``null`` or a two-element entry means the
rows are unknown), so the table layout is unchanged. Recording a fingerprint identical to one
already stored for the same user and scope refreshes that row instead of adding another
(R2-L1), so repeats do not push towards ``MAX_CANDIDATES``; going over the cap raises
:class:`FingerprintCapError` (a distinct cause for the caller, G-2).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from opsfleet_agent.store.db import StoreError, write_tx

RETENTION_DAYS: Final = 30
RETENTION_SECONDS: Final = RETENTION_DAYS * 86_400
#: Upper bound on rows read per check. More rows than this fails closed (refusal).
MAX_CANDIDATES: Final = 1_000
#: Upper bound on cells stored per fingerprint; a larger result is stored with unknown counts.
MAX_CELLS: Final = 10_000
_MAX_ID: Final = 200
_KEY_NAME: Final = "fingerprint_hmac_key"
#: Marker for "derives from columns the guard could not resolve" (compares as overlapping all).
ANY_COLUMN: Final = "*"

FINGERPRINT_MIGRATION: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS aggregate_fingerprint ("
    "fingerprint_id TEXT PRIMARY KEY, "
    "user_id TEXT NOT NULL, "
    "session_id TEXT NOT NULL, "
    "scope_key TEXT NOT NULL, "
    "dims TEXT NOT NULL, "
    "filters TEXT NOT NULL, "
    "predicates TEXT NOT NULL, "
    "cells TEXT, "
    "created_at INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS aggregate_fingerprint_user_scope "
    "ON aggregate_fingerprint (user_id, scope_key, created_at)",
    "CREATE INDEX IF NOT EXISTS aggregate_fingerprint_age ON aggregate_fingerprint (created_at)",
)
_COLUMNS: Final = frozenset(
    {
        "fingerprint_id",
        "user_id",
        "session_id",
        "scope_key",
        "dims",
        "filters",
        "predicates",
        "cells",
        "created_at",
    }
)


class FingerprintStoreError(StoreError):
    """The fingerprint store is unavailable or a read or write failed (fail closed)."""


class FingerprintCapError(FingerprintStoreError):
    """More than ``MAX_CANDIDATES`` fingerprints to compare (fail closed, distinct cause)."""


@dataclass(frozen=True, slots=True)
class Dim:
    """One dimension of a cell (all digests).

    ``dim`` identifies the dimension (source column plus the canonical shape of the
    expression over it). ``cols`` are digests of the source columns it derives from
    (``ANY_COLUMN`` when unresolved). ``fixed`` is the digest of the value an equality filter
    pins it to, or ``""`` when the dimension is grouped (varies per row).
    """

    dim: str
    cols: frozenset[str]
    fixed: str = ""


CellKey = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """Value-free fingerprint of one aggregate (all strings are digests except ``scope_key``).

    ``filters`` holds the column digests of every predicate that is not an equality on a
    dimension (``ANY_COLUMN`` when unresolved). ``predicates`` is kept for audit only.
    ``cells`` maps a cell key (sorted ``(dim, value)`` digest pairs) to its distinct-customer
    count; ``None`` means the counts are unknown (the guard then refuses any comparable pair).
    ``rows`` maps the same keys to the cell's row count (``COUNT(*)``, R2-H1); ``None`` means
    unknown. When set it must have exactly the keys of ``cells``.
    """

    scope_key: str
    dims: tuple[Dim, ...]
    filters: frozenset[str]
    predicates: tuple[str, ...] = ()
    cells: Mapping[CellKey, int] | None = None
    rows: Mapping[CellKey, int] | None = None


@dataclass(frozen=True, slots=True)
class StoredFingerprint:
    fingerprint_id: str
    session_id: str
    created_at: int
    fingerprint: Fingerprint


def ensure_schema(conn: sqlite3.Connection) -> None:
    with write_tx(conn):
        for stmt in FINGERPRINT_MIGRATION:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            (_KEY_NAME, secrets.token_hex(32)),
        )
    present = {row[1] for row in conn.execute("PRAGMA table_info(aggregate_fingerprint)")}
    if present != _COLUMNS:
        raise FingerprintStoreError("fingerprint store layout outdated")


def _ident(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ID:
        raise FingerprintStoreError(f"{name} is required")
    return value.strip()


def _count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FingerprintStoreError("cell counts must be non-negative integers")
    return value


def _digest_text(value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_ID:
        raise FingerprintStoreError("malformed fingerprint")
    return value


def _encode(fp: Fingerprint) -> tuple[str, str, str, str | None]:
    if not isinstance(fp, Fingerprint):
        raise FingerprintStoreError("not a fingerprint")
    dims = json.dumps(
        sorted(
            [_digest_text(d.dim), sorted(_digest_text(c) for c in d.cols), _digest_text(d.fixed)]
            for d in fp.dims
        )
    )
    filters = json.dumps(sorted(_digest_text(f) for f in fp.filters))
    predicates = json.dumps(sorted(_digest_text(p) for p in fp.predicates))
    cells = None
    if fp.rows is not None and (fp.cells is None or set(fp.rows) != set(fp.cells)):
        raise FingerprintStoreError("row counts do not match the cells")
    if fp.cells is not None and len(fp.cells) <= MAX_CELLS:
        rows = fp.rows
        cells = json.dumps(
            sorted(
                [
                    [[_digest_text(a), _digest_text(b)] for a, b in key],
                    _count(n),
                    None if rows is None else _count(rows[key]),
                ]
                for key, n in fp.cells.items()
            )
        )
    return dims, filters, predicates, cells


def _decode(scope_key: str, row: tuple) -> StoredFingerprint:
    fid, session_id, created_at, dims, filters, predicates, cells = row
    cell_map: dict[CellKey, int] | None = None
    row_map: dict[CellKey, int] | None = None
    if cells is not None:
        cell_map, row_map = {}, {}
        for entry in json.loads(cells):
            if not isinstance(entry, list) or len(entry) not in (2, 3):
                raise FingerprintStoreError("malformed fingerprint")
            key, n = entry[0], entry[1]
            ck = tuple(sorted((_digest_text(a), _digest_text(b)) for a, b in key))
            if ck in cell_map:
                raise FingerprintStoreError("malformed fingerprint")
            cell_map[ck] = _count(n)
            r = entry[2] if len(entry) == 3 else None
            if r is None:
                row_map = None  # unknown for one cell: unknown for all (fail closed)
            elif row_map is not None:
                row_map[ck] = _count(r)
    fp = Fingerprint(
        scope_key=scope_key,
        dims=tuple(
            Dim(_digest_text(d), frozenset(_digest_text(c) for c in cols), _digest_text(fixed))
            for d, cols, fixed in json.loads(dims)
        ),
        filters=frozenset(_digest_text(f) for f in json.loads(filters)),
        predicates=tuple(_digest_text(p) for p in json.loads(predicates)),
        cells=cell_map,
        rows=row_map,
    )
    return StoredFingerprint(str(fid), str(session_id), int(created_at), fp)


class FingerprintStore:
    """SQLite store on the app DB. Every public method raises only FingerprintStoreError."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock
        try:
            ensure_schema(conn)
            row = conn.execute("SELECT value FROM meta WHERE key=?", (_KEY_NAME,)).fetchone()
            self._key = bytes.fromhex(row[0])
        except FingerprintStoreError:
            raise
        except Exception as err:  # noqa: BLE001 - sqlite3.Error, bad key, closed conn
            raise FingerprintStoreError("fingerprint store unavailable") from err
        if len(self._key) < 16:
            raise FingerprintStoreError("fingerprint store key invalid")

    def now(self) -> int:
        try:
            return int(self.clock())
        except Exception as err:  # noqa: BLE001
            raise FingerprintStoreError("clock unavailable") from err

    def digest(self, text: str) -> str:
        """Keyed digest of a canonical text (never stored or logged in clear)."""
        return hmac.new(self._key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:40]

    # -- reads

    def candidates(
        self, user_id: str, scope_key: str, *, now: int | None = None
    ) -> list[StoredFingerprint]:
        """This user's fingerprints, all sessions, last 30 days, same scope key (review B-1:
        every one is compared, not only those one predicate away)."""
        user_id = _ident("user_id", user_id)
        scope_key = _ident("scope_key", scope_key)
        try:
            cutoff = (self.now() if now is None else int(now)) - RETENTION_SECONDS
            rows = self.conn.execute(
                "SELECT fingerprint_id, session_id, created_at, dims, filters, predicates, cells "
                "FROM aggregate_fingerprint WHERE user_id=? AND scope_key=? AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, scope_key, cutoff, MAX_CANDIDATES + 1),
            ).fetchall()
            if len(rows) > MAX_CANDIDATES:
                raise FingerprintCapError("too many fingerprints to check")
            return [_decode(scope_key, r) for r in rows]
        except FingerprintStoreError:
            raise
        except Exception as err:  # noqa: BLE001 - sqlite3.Error, corrupt JSON: fail closed
            raise FingerprintStoreError("fingerprint read failed") from err

    # -- writes

    def record(
        self,
        user_id: str,
        session_id: str,
        fp: Fingerprint,
        *,
        now: int | None = None,
        _in_tx: bool = False,
    ) -> str:
        """Insert one fingerprint (its own transaction unless called from
        :meth:`check_and_record`). Prefer :meth:`check_and_record`.

        A fingerprint identical to a stored one of the same user and scope (same dims, filters,
        predicates, cells and rows) is not inserted again (R2-L1): that row's ``created_at``
        moves to the later of the two and its ``session_id`` to this one, and its id is
        returned."""
        user_id = _ident("user_id", user_id)
        session_id = _ident("session_id", session_id)
        if not isinstance(fp, Fingerprint):
            raise FingerprintStoreError("not a fingerprint")
        fid = uuid.uuid4().hex
        try:
            dims, filters, predicates, cells = _encode(fp)
            values = (
                fid,
                user_id,
                session_id,
                _ident("scope_key", fp.scope_key),
                dims,
                filters,
                predicates,
                cells,
                self.now() if now is None else int(now),
            )
            if _in_tx:
                fid = self._upsert(fid, values)
            else:
                with write_tx(self.conn):
                    fid = self._upsert(fid, values)
        except FingerprintStoreError:
            raise
        except Exception as err:  # noqa: BLE001
            raise FingerprintStoreError("fingerprint write failed") from err
        return fid

    def _upsert(self, fid: str, values: tuple) -> str:
        """Insert ``values`` or refresh the identical stored row (inside a transaction)."""
        _, user_id, session_id, scope_key, dims, filters, predicates, cells, at = values
        row = self.conn.execute(
            "SELECT fingerprint_id FROM aggregate_fingerprint WHERE user_id=? AND scope_key=? "
            "AND dims=? AND filters=? AND predicates=? AND cells IS ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, scope_key, dims, filters, predicates, cells),
        ).fetchone()
        if row is not None:
            self.conn.execute(
                "UPDATE aggregate_fingerprint SET created_at=MAX(created_at, ?), session_id=? "
                "WHERE fingerprint_id=?",
                (at, session_id, row[0]),
            )
            return str(row[0])
        self.conn.execute(
            "INSERT INTO aggregate_fingerprint (fingerprint_id, user_id, session_id, "
            "scope_key, dims, filters, predicates, cells, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            values,
        )
        return fid

    def check_and_record(
        self,
        user_id: str,
        session_id: str,
        fp: Fingerprint,
        allow: Callable[[list[StoredFingerprint]], bool],
        *,
        now: int | None = None,
    ) -> str | None:
        """Read this user's candidates and, only if ``allow(candidates)`` is True, insert ``fp``,
        all under one write transaction (review M-3). Returns the new id, or None when refused
        (nothing written). Any failure, including one raised by ``allow``, rolls back and
        raises FingerprintStoreError."""
        if not isinstance(fp, Fingerprint):
            raise FingerprintStoreError("not a fingerprint")
        try:
            at = self.now() if now is None else int(now)
            with write_tx(self.conn):
                stored = self.candidates(user_id, fp.scope_key, now=at)
                if allow(stored) is not True:
                    return None
                return self.record(user_id, session_id, fp, now=at, _in_tx=True)
        except FingerprintStoreError:
            raise
        except Exception as err:  # noqa: BLE001 - sqlite3.Error, a bug in allow: fail closed
            raise FingerprintStoreError("fingerprint check failed") from err

    def purge_expired(self, *, now: int | None = None) -> int:
        """Delete rows older than 30 days (run at startup in the prototype)."""
        try:
            cutoff = (self.now() if now is None else int(now)) - RETENTION_SECONDS
            with write_tx(self.conn):
                return self.conn.execute(
                    "DELETE FROM aggregate_fingerprint WHERE created_at < ?", (cutoff,)
                ).rowcount
        except Exception as err:  # noqa: BLE001
            raise FingerprintStoreError("fingerprint purge failed") from err

    def delete_user(self, user_id: str) -> int:
        """Erasure hook (FR-59, ``/erase``): remove every fingerprint of one user."""
        user_id = _ident("user_id", user_id)
        try:
            with write_tx(self.conn):
                return self.conn.execute(
                    "DELETE FROM aggregate_fingerprint WHERE user_id=?", (user_id,)
                ).rowcount
        except Exception as err:  # noqa: BLE001
            raise FingerprintStoreError("fingerprint delete failed") from err
