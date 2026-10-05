"""Ranked full-text search over saved reports: SQLite FTS5 with bm25 (iteration 37, AC-21.13).

A leaf module (stdlib only): ``store.db`` takes the migration from here and ``store.reports``
the sync and query helpers, so neither imports the other.

Rules enforced here, in code:

* **No FTS syntax from the user.** :func:`build_match` keeps only the letters and digits of the
  (already canonical) query, wraps every term in double quotes and joins them with spaces
  (implicit AND). ``"``, ``*``, ``^``, ``-``, ``:``, parentheses, column filters and the words
  ``NEAR``/``AND``/``OR``/``NOT`` all end up as literal terms or separators, never operators.
  At most :data:`MAX_TERMS` terms.
* **A regular FTS5 table, kept in sync by code, no triggers** (D-199). ``report_id`` is an
  UNINDEXED column; the index is written in the same transaction as the report insert
  (:meth:`ReportStore.save`), the title update (:meth:`ReportStore.rename`) and the audited
  delete (``store.audit`` ``fts_dependents``). ``audited_delete`` refuses any DELETE trigger on a
  touched table, which is why the HLD's "maintained by triggers" is done in code here.
* **No residue.** The table is created with FTS5's ``secure-delete`` option, and the audited
  delete also runs ``optimize`` before COMMIT, so a deleted report's tokens leave no copy in
  the ``*_data``/``*_idx``/``*_content``/``*_docsize`` shadow tables. A SQLite without FTS5
  or without ``secure-delete`` (older than 3.44) gets no table at all, and search falls back
  to the substring path (:func:`fts5_supported`).
* **Owner first.** The table holds no owner column: every query joins ``saved_report`` on the
  key and filters by owner in SQL, over the same bounded row source as the substring search
  (the owner's newest ``MAX_LIST`` reports).
"""

from __future__ import annotations

import functools
import re
import sqlite3
from collections.abc import Sequence
from typing import Final

__all__ = [
    "FTS_KEY",
    "FTS_MIGRATION",
    "FTS_TABLE",
    "MAX_TERMS",
    "FtsQueryError",
    "build_match",
    "fts5_supported",
    "has_index",
    "index_report",
    "migration",
    "reindex_title",
    "tags_text",
]

FTS_TABLE: Final = "report_fts"
FTS_KEY: Final = "report_id"
MAX_TERMS: Final = 16
# bm25 column weights (report_id, title, body, tags): a title hit counts most, then tags.
BM25_WEIGHTS: Final = (0.0, 4.0, 1.0, 2.0)

FTS_DDL: Final = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
    f"{FTS_KEY} UNINDEXED, title, body, tags, "
    "tokenize = 'porter unicode61 remove_diacritics 2')"
)
FTS_SECURE_DELETE: Final = f"INSERT INTO {FTS_TABLE}({FTS_TABLE}, rank) VALUES('secure-delete', 1)"
# Backfill of reports saved before the index existed; a re-run adds nothing (NOT IN).
FTS_BACKFILL: Final = (
    f"INSERT INTO {FTS_TABLE} ({FTS_KEY}, title, body, tags) "
    "SELECT s.report_id, s.title, s.body_markdown, "
    "CASE WHEN json_valid(s.tags) "
    "THEN COALESCE((SELECT group_concat(value, ' ') FROM json_each(s.tags)), '') "
    "ELSE '' END "
    f"FROM saved_report s WHERE s.report_id NOT IN (SELECT {FTS_KEY} FROM {FTS_TABLE})"
)
FTS_MIGRATION: Final[tuple[str, ...]] = (FTS_DDL, FTS_SECURE_DELETE, FTS_BACKFILL)

_TERM_RE: Final = re.compile(r"[^\W_]+")


class FtsQueryError(ValueError):
    """An unusable ranked query. The message is static and never echoes the query."""


@functools.cache
def fts5_supported() -> bool:
    """True when this process's SQLite has FTS5 with the ``secure-delete`` option."""
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        conn.execute("INSERT INTO probe(probe, rank) VALUES('secure-delete', 1)")
        conn.execute("SELECT bm25(probe) FROM probe WHERE probe MATCH '\"x\"'").fetchall()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def migration() -> tuple[str, ...]:
    """Migration 5: the FTS table and its backfill, or nothing when FTS5 is unavailable
    (``ReportStore`` creates it later if a newer SQLite appears; the statements are idempotent)."""
    return FTS_MIGRATION if fts5_supported() else ()


def has_index(conn: sqlite3.Connection) -> bool:
    """True when the FTS table exists in ``main`` as an FTS5 virtual table."""
    row = conn.execute(
        "SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?", (FTS_TABLE,)
    ).fetchone()
    return row is not None and "USING FTS5" in str(row[0]).upper()


def tags_text(tags: Sequence[str]) -> str:
    return " ".join(str(t) for t in tags)


def index_report(
    conn: sqlite3.Connection, report_id: str, title: str, body: str, tags: Sequence[str]
) -> None:
    """Add one report to the index. The caller holds the write transaction."""
    conn.execute(
        f"INSERT INTO {FTS_TABLE} ({FTS_KEY}, title, body, tags) VALUES (?, ?, ?, ?)",
        (report_id, title, body, tags_text(tags)),
    )


def reindex_title(conn: sqlite3.Connection, report_id: str, title: str) -> int:
    """Replace the indexed title (rename). Returns the number of index rows changed."""
    return conn.execute(
        f"UPDATE {FTS_TABLE} SET title = ? WHERE {FTS_KEY} = ?", (title, report_id)
    ).rowcount


def build_match(needle: str) -> str:
    """An FTS5 MATCH expression from a canonical query: every letter/digit run becomes one
    double-quoted term (implicit AND). Nothing the user typed is FTS syntax."""
    terms = list(dict.fromkeys(_TERM_RE.findall(str(needle))))
    if not terms:
        raise FtsQueryError("the search text needs some letters or digits")
    if len(terms) > MAX_TERMS:
        raise FtsQueryError(f"use at most {MAX_TERMS} words in a search")
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


def ranked_sql(columns: str) -> str:
    """The owner-filtered, bm25-ordered query over the owner's newest ``limit`` reports.
    Parameters: (match, owner, owner, limit)."""
    weights = ", ".join(str(w) for w in BM25_WEIGHTS)
    return (
        f"SELECT {columns} FROM {FTS_TABLE} JOIN saved_report s "
        f"ON s.report_id = {FTS_TABLE}.{FTS_KEY} "
        f"WHERE {FTS_TABLE} MATCH ? AND s.owner_user_id = ? "
        "AND s.report_id IN (SELECT report_id FROM saved_report WHERE owner_user_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?) "
        f"ORDER BY bm25({FTS_TABLE}, {weights}), s.created_at DESC, s.rowid DESC"
    )
