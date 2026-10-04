"""Audit-log DDL as a leaf module (no store imports), so ``store.db`` can fold it verbatim.

``store.db.MIGRATIONS`` must import ``AUDIT_MIGRATION`` and ``AUDIT_MARKER_SQL`` from here at
integration and never re-type them: :func:`opsfleet_agent.store.audit.ensure_schema` compares
the live ``sqlite_master`` SQL text with these exact statements, so any drift fails closed.
"""

from __future__ import annotations

from typing import Final

# Mirrors store.audit.DELETE_FAILED (asserted there); kept literal so this module has no imports.
DELETE_FAILED_EVENT: Final = "delete.failed"
APPEND_ONLY_MSG: Final = "audit_event is append-only"

# Written in the same transaction that creates the table: if the table later disappears, the
# store was tampered with and the log refuses to recreate an empty one.
CREATED_MARKER: Final = "audit_event.created"
AUDIT_MARKER_SQL: Final = (
    f"INSERT OR IGNORE INTO meta (key, value) VALUES ('{CREATED_MARKER}', '1')"
)

AUDIT_MIGRATION: Final[tuple[str, ...]] = (
    "CREATE TABLE IF NOT EXISTS audit_event ("
    # seq > 0: a planted seq <= 0 row would otherwise make AUTOINCREMENT collide forever.
    "seq INTEGER PRIMARY KEY AUTOINCREMENT CHECK (seq > 0), "
    "event_id TEXT NOT NULL UNIQUE CHECK (length(event_id) BETWEEN 1 AND 128), "
    "ts TEXT NOT NULL CHECK (length(ts) BETWEEN 20 AND 32), "
    "actor_user_id TEXT NOT NULL CHECK (length(actor_user_id) BETWEEN 1 AND 128), "
    "session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128), "
    "turn_id TEXT NOT NULL CHECK (length(turn_id) BETWEEN 1 AND 128), "
    "pending_action_id TEXT CHECK (pending_action_id IS NULL "
    "OR length(pending_action_id) BETWEEN 1 AND 128), "
    "event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 64), "
    "target_ids TEXT CHECK (target_ids IS NULL OR (json_valid(target_ids) "
    "AND json_type(target_ids) = 'array')), "
    "count INTEGER CHECK (count IS NULL OR count >= 0), "
    "rule TEXT CHECK (rule IS NULL OR length(rule) BETWEEN 1 AND 64), "
    "outcome TEXT CHECK (outcome IS NULL OR length(outcome) BETWEEN 1 AND 32), "
    "details TEXT CHECK (details IS NULL OR (json_valid(details) "
    "AND json_type(details) = 'object' AND length(details) <= 2048)))",
    # One row per (pending_action_id, event_type), except delete.failed: every failure is kept.
    "CREATE UNIQUE INDEX IF NOT EXISTS audit_event_pending_type "
    "ON audit_event (pending_action_id, event_type) "
    f"WHERE pending_action_id IS NOT NULL AND event_type <> '{DELETE_FAILED_EVENT}'",
    "CREATE INDEX IF NOT EXISTS audit_event_session ON audit_event (session_id, seq)",
    "CREATE INDEX IF NOT EXISTS audit_event_actor ON audit_event (actor_user_id, seq)",
    "CREATE TRIGGER IF NOT EXISTS audit_event_no_update BEFORE UPDATE ON audit_event "
    f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY_MSG}'); END",
    "CREATE TRIGGER IF NOT EXISTS audit_event_no_delete BEFORE DELETE ON audit_event "
    f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY_MSG}'); END",
    # Insert-or-ignore on a duplicate; also stops INSERT OR REPLACE from overwriting a row.
    "CREATE TRIGGER IF NOT EXISTS audit_event_no_replace BEFORE INSERT ON audit_event "
    "WHEN EXISTS (SELECT 1 FROM audit_event WHERE event_id = NEW.event_id) "
    "OR EXISTS (SELECT 1 FROM audit_event WHERE seq = NEW.seq) "
    f"OR (NEW.pending_action_id IS NOT NULL AND NEW.event_type <> '{DELETE_FAILED_EVENT}' "
    "AND EXISTS (SELECT 1 FROM audit_event "
    "WHERE pending_action_id = NEW.pending_action_id AND event_type = NEW.event_type)) "
    "BEGIN SELECT RAISE(IGNORE); END",
)
