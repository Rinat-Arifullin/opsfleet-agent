"""Per-user preference store (iteration 39; R4.1, AC-24.1..24.4; HLD §4.3 "User level").

Implements the :class:`opsfleet_agent.graph.memory.PreferenceStore` seam. One row per user:
the validated preferences (format, depth, charts) and up to 5 sanitised notes, each with the
scope snapshot it was written under, as one JSON document. This store is the source of truth
across sessions: the graph reads it every turn, ``/prefs`` writes it.

Every write and every read goes through ``SessionMemory.from_state``, so only enumerated
values and notes that still pass ``sanitise_note`` get in or out (fail closed): a row edited
on disk cannot smuggle text into the prompt. Restatements and pending clarifications are
session-only and are never stored here.

Erasure and residue (iterations 23/35): ``ERASURE_TABLE`` names the table and its user key so
a per-user deletion or a residue enumeration can find it; :meth:`SQLitePreferenceStore.
delete_user` is the erasure hook. Schema is created idempotently by :func:`ensure_schema`;
``PREFERENCES_MIGRATION`` holds the same statements for folding into ``store.db.MIGRATIONS``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from opsfleet_agent.graph.memory import SessionMemory
from opsfleet_agent.store.db import StoreError, write_tx

TABLE = "user_preferences"
# (table, user-key column) for per-user erasure and residue enumeration (iterations 23/35).
ERASURE_TABLE: tuple[str, str] = (TABLE, "user_id")
MAX_DOC_BYTES = 8192  # 3 enum values + 5 notes of <=200 chars with snapshots fit easily

PREFERENCES_MIGRATION: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS user_preferences ("
    "user_id TEXT PRIMARY KEY, "
    "data TEXT NOT NULL, "
    "updated_at TEXT NOT NULL DEFAULT (datetime('now')))",
)


class PreferenceStoreError(StoreError):
    """Invalid preference input (no user id, or a document over the size bound)."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    with write_tx(conn):
        for stmt in PREFERENCES_MIGRATION:
            conn.execute(stmt)


def _user(user_id: Any) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise PreferenceStoreError("user_id is required")
    return user_id.strip()


def _clean(doc: Any) -> dict[str, Any]:
    """Only what ``SessionMemory.from_state`` accepts, as the persistable shape."""
    return SessionMemory.from_state(doc).persistable()


class SQLitePreferenceStore:
    """SQLite implementation of the ``PreferenceStore`` seam, keyed by user id."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_schema(conn)

    def save(self, user_id: str, preferences: Mapping[str, Any]) -> None:
        """Replace the user's stored preferences with the validated part of ``preferences``
        (the ``SessionMemory.persistable()`` shape). Empty preferences and notes delete the row."""
        uid = _user(user_id)
        doc = _clean(preferences)
        if not doc["preferences"] and not doc["notes"]:
            self.delete_user(uid)
            return
        data = json.dumps(doc, sort_keys=True, ensure_ascii=False)
        if len(data.encode("utf-8")) > MAX_DOC_BYTES:
            raise PreferenceStoreError("preferences document too large")
        with write_tx(self.conn):
            self.conn.execute(
                "INSERT INTO user_preferences (user_id, data) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, "
                "updated_at=datetime('now')",
                (uid, data),
            )

    def load(self, user_id: str) -> SessionMemory:
        """The user's stored preferences and notes, re-validated; empty when none or invalid."""
        row = self.conn.execute(
            "SELECT data FROM user_preferences WHERE user_id=?", (_user(user_id),)
        ).fetchone()
        if row is None:
            return SessionMemory()
        try:
            doc = json.loads(row[0])
        except (TypeError, ValueError):
            return SessionMemory()
        if not isinstance(doc, Mapping):
            return SessionMemory()
        return SessionMemory.from_state(
            {"preferences": doc.get("preferences"), "notes": doc.get("notes")}
        )

    def reset(self, user_id: str) -> None:
        """Clear the user's preferences and notes (``/prefs reset``, AC-24.1)."""
        self.delete_user(user_id)

    def delete_user(self, user_id: str) -> int:
        """Erasure hook for iteration 35: remove the user's preference row."""
        with write_tx(self.conn):
            return self.conn.execute(
                "DELETE FROM user_preferences WHERE user_id=?", (_user(user_id),)
            ).rowcount
