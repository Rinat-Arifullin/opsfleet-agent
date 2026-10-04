"""Feedback store (FR-46, AC-25.1..25.3; HLD §6.4, data model FEEDBACK).

The comment is stored already redacted: the caller scrubs it (see `commands/feedback.py`),
and this store re-applies the shared secret scrub and the length bound as defence in depth.

Schema: the table is created idempotently by `ensure_schema` (CREATE TABLE IF NOT EXISTS), so
it does not collide with other iterations' numbered migrations. `FEEDBACK_MIGRATION` holds the
same statements for folding into `store.db.MIGRATIONS` as a numbered version at integration.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.store.db import StoreError, write_tx

RATINGS = ("up", "down")
REASONS = ("wrong_numbers", "misunderstood", "wrong_format", "slow", "other")
TRIAGE_STATES = ("new", "triaged", "promoted", "dismissed")
MAX_COMMENT = 500

FEEDBACK_MIGRATION: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS feedback ("
    "feedback_id TEXT PRIMARY KEY, "
    "user_id TEXT NOT NULL, "
    "session_id TEXT NOT NULL, "
    "turn_id TEXT NOT NULL, "
    "trace_id TEXT, "
    "rating TEXT NOT NULL CHECK (rating IN ('up','down')), "
    "comment TEXT, "
    "reason TEXT CHECK (reason IS NULL OR reason IN "
    "('wrong_numbers','misunderstood','wrong_format','slow','other')), "
    "triage_state TEXT NOT NULL DEFAULT 'new' CHECK (triage_state IN "
    "('new','triaged','promoted','dismissed')), "
    "created_at TEXT NOT NULL DEFAULT (datetime('now')))",
    "CREATE UNIQUE INDEX IF NOT EXISTS feedback_turn_user "
    "ON feedback (session_id, turn_id, user_id)",
    "CREATE INDEX IF NOT EXISTS feedback_user ON feedback (user_id)",
)


class FeedbackError(StoreError):
    """Invalid feedback input."""


@dataclass(frozen=True)
class FeedbackRecord:
    feedback_id: str
    user_id: str
    session_id: str
    turn_id: str
    trace_id: str | None
    rating: str
    comment: str | None
    reason: str | None
    triage_state: str
    created_at: str


_COLS = (
    "feedback_id, user_id, session_id, turn_id, trace_id, rating, comment, reason, "
    "triage_state, created_at"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    with write_tx(conn):
        for stmt in FEEDBACK_MIGRATION:
            conn.execute(stmt)


def _req(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeedbackError(f"{name} is required")
    return value.strip()


class FeedbackStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_schema(conn)

    def add(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        trace_id: str | None,
        rating: str,
        comment: str | None = None,
        reason: str | None = None,
    ) -> tuple[FeedbackRecord, bool]:
        """Insert, or replace the user's earlier rating of the same turn.

        Returns (record, replaced). `comment` must already be PII-scrubbed; secrets and the
        length bound are enforced here again.
        """
        user_id, session_id, turn_id = (
            _req("user_id", user_id),
            _req("session_id", session_id),
            _req("turn_id", turn_id),
        )
        if rating not in RATINGS:
            raise FeedbackError(f"rating must be one of: {', '.join(RATINGS)}")
        if reason is not None and reason not in REASONS:
            raise FeedbackError(f"reason must be one of: {', '.join(REASONS)}")
        if reason is not None and rating != "down":
            raise FeedbackError("a reason is only allowed with a down rating")
        if comment is not None:
            comment = tr.scrub_text(comment, MAX_COMMENT).strip() or None
        trace = tr.scrub_text(trace_id, 80) if trace_id else None
        fid = uuid.uuid4().hex
        with write_tx(self.conn):
            old = self.conn.execute(
                "SELECT feedback_id FROM feedback WHERE session_id=? AND turn_id=? AND user_id=?",
                (session_id, turn_id, user_id),
            ).fetchone()
            if old:
                self.conn.execute("DELETE FROM feedback WHERE feedback_id=?", (old[0],))
            self.conn.execute(
                "INSERT INTO feedback (feedback_id, user_id, session_id, turn_id, trace_id, "
                "rating, comment, reason) VALUES (?,?,?,?,?,?,?,?)",
                (fid, user_id, session_id, turn_id, trace, rating, comment, reason),
            )
        return self.get(fid), old is not None  # type: ignore[return-value]

    def get(self, feedback_id: str) -> FeedbackRecord | None:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM feedback WHERE feedback_id=?", (feedback_id,)
        ).fetchone()
        return FeedbackRecord(*row) if row else None

    def for_turn(self, session_id: str, turn_id: str) -> list[FeedbackRecord]:
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM feedback WHERE session_id=? AND turn_id=? ORDER BY created_at",
            (session_id, turn_id),
        ).fetchall()
        return [FeedbackRecord(*r) for r in rows]

    def counts(self, session_id: str | None = None) -> dict[str, Any]:
        """{'up', 'down', 'total', 'down_rate'}; `down_rate` is None with no feedback."""
        sql = "SELECT rating, COUNT(*) FROM feedback"
        args: tuple[str, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id=?"
            args = (session_id,)
        got = dict(self.conn.execute(sql + " GROUP BY rating", args).fetchall())
        up, down = int(got.get("up", 0)), int(got.get("down", 0))
        total = up + down
        return {
            "up": up,
            "down": down,
            "total": total,
            "down_rate": down / total if total else None,
        }

    def list_down(self, session_id: str | None = None, limit: int = 50) -> list[FeedbackRecord]:
        sql = f"SELECT {_COLS} FROM feedback WHERE rating='down'"
        args: list[Any] = []
        if session_id is not None:
            sql += " AND session_id=?"
            args.append(session_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [FeedbackRecord(*r) for r in self.conn.execute(sql, args).fetchall()]

    def delete_user(self, user_id: str) -> int:
        """Erasure hook for iteration 35: remove every feedback row of one user."""
        with write_tx(self.conn):
            return self.conn.execute("DELETE FROM feedback WHERE user_id=?", (user_id,)).rowcount
