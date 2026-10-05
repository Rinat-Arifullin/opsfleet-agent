"""DDL of the saved-report table (migration 3). A leaf module: no store imports.

``store.db`` imports it into ``MIGRATIONS`` and ``store.reports`` re-applies it idempotently,
so neither imports the other (the same split as ``audit_schema``).

The table is ``saved_report`` (not ``report``: the audit tests own a ``report`` fixture table).
``report_id`` is a 32-hex uuid4 (the audit ``target_id`` shape); ``idempotency_key`` is TEXT
UNIQUE with no COLLATE, and no trigger is defined (D-80/81).
"""

from __future__ import annotations

from typing import Final

REPORTS_MIGRATION: Final[tuple[str, ...]] = (
    "CREATE TABLE IF NOT EXISTS saved_report ("
    "report_id TEXT PRIMARY KEY, "
    "owner_user_id TEXT NOT NULL, "
    "session_id TEXT NOT NULL, "
    "turn_id TEXT NOT NULL, "
    "title TEXT NOT NULL, "
    "body_markdown TEXT NOT NULL, "
    "sections_json TEXT NOT NULL, "
    "sql_used TEXT NOT NULL, "
    "scope_snapshot TEXT NOT NULL, "
    "data_window TEXT NOT NULL, "
    "tags TEXT NOT NULL DEFAULT '[]', "
    "model_used TEXT NOT NULL DEFAULT '', "
    "persona_version TEXT NOT NULL DEFAULT '', "
    "draft_hash TEXT NOT NULL, "
    "idempotency_key TEXT NOT NULL UNIQUE, "
    "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
    "CREATE INDEX IF NOT EXISTS saved_report_owner ON saved_report (owner_user_id, created_at)",
)
