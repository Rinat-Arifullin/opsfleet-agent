"""DDL of the per-user quota table (migration 4). A leaf module: no store imports.

``store.db`` imports it into ``MIGRATIONS`` and ``store.quota`` re-applies it idempotently
(``CREATE TABLE IF NOT EXISTS``), so neither imports the other (the same split as
``reports_schema``).
"""

from __future__ import annotations

from typing import Final

QUOTA_MIGRATION: Final[tuple[str, ...]] = (
    "CREATE TABLE IF NOT EXISTS user_quota ("
    "user_id TEXT NOT NULL, "
    "kind TEXT NOT NULL CHECK (kind IN ('llm_hour','llm_day','bq_bytes_day')), "
    "win TEXT NOT NULL, "
    "used INTEGER NOT NULL DEFAULT 0, "
    "PRIMARY KEY (user_id, kind, win))",
)
