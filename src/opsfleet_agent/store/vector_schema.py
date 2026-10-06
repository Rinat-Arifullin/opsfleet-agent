"""DDL of the per-report vector table (migration 6, iteration 38). A leaf module: no store imports.

``store.db`` imports it into ``MIGRATIONS``; ``store.reports.ensure_schema`` re-applies it
idempotently and ``reports.semantic`` reads and writes the table (the same split as
``reports_schema`` and ``quota_schema``). DDL only: the migration never embeds anything
(no network inside a migration); old reports are backfilled lazily by search (D-210).
No FK and no trigger: the audited delete removes rows as a declared, exact-counted dependent.
"""

from __future__ import annotations

from typing import Final

VECTOR_TABLE: Final = "report_vector"
VECTOR_KEY: Final = "report_id"

VECTOR_MIGRATION: Final[tuple[str, ...]] = (
    f"CREATE TABLE IF NOT EXISTS {VECTOR_TABLE} ("
    f"{VECTOR_KEY} TEXT PRIMARY KEY, "
    "owner_user_id TEXT NOT NULL, "
    "model TEXT NOT NULL, "
    "dims INTEGER NOT NULL, "
    "vector BLOB NOT NULL, "
    "content_hash TEXT NOT NULL)",
    f"CREATE INDEX IF NOT EXISTS {VECTOR_TABLE}_owner ON {VECTOR_TABLE} (owner_user_id)",
)
