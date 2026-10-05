"""Saved-report store (FR-23, AC-06.1, AC-21.2, AC-21.8; HLD data model REPORT).

Rules enforced here, in code:

* every report stores its owner and its session (``owner_user_id``, ``session_id``);
* the body passes the output guard at the save boundary: :meth:`ReportStore.save` takes the
  guard as a required callable and stores only the text it allows, and refuses a body that
  lacks an AC-21.1 section (:func:`reports.schema.missing_sections`);
* saving is idempotent, atomic and concurrency-safe: one ``BEGIN IMMEDIATE`` transaction with
  ``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING``, so two writers (threads or
  processes) with the same key create exactly one row and both get it back;
* a stored body is untrusted on every read into a prompt (SEC-13): :meth:`get_fenced` and
  :meth:`store_items` hand out only fenced or fence-on-render forms. The raw body is for the
  owner's own display (iteration 19's view command), never for a prompt.

No delete method: deletion is iteration 22a's audited flow (audit record first). The only
update is :meth:`ReportStore.rename` (iteration 33), owner-checked and guarded.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from opsfleet_agent.graph.context import KIND_REPORT, StoreItem, fence_untrusted
from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.reports.schema import missing_sections
from opsfleet_agent.store.db import StoreError, write_tx
from opsfleet_agent.store.reports_schema import REPORTS_MIGRATION

__all__ = ["MAX_BODY_CHARS", "ReportError", "ReportStore", "SavedReport", "ensure_schema"]

MAX_BODY_CHARS: Final = 60_000
MAX_LIST: Final = 200
# guard(body) -> (allowed, text_to_store); the graph passes a closure over check_output
BodyGuard = Callable[[str], tuple[bool, str]]


class ReportError(StoreError):
    """Invalid report input or a body the guard refused."""


@dataclass(frozen=True)
class SavedReport:
    report_id: str
    owner_user_id: str
    session_id: str
    turn_id: str
    title: str
    body_markdown: str
    sections: dict[str, Any]
    sql_used: list[str]
    scope_snapshot: dict[str, Any]
    data_window: str
    tags: list[str]
    model_used: str
    persona_version: str
    draft_hash: str
    idempotency_key: str
    created_at: str


_COLS: Final = (
    "report_id, owner_user_id, session_id, turn_id, title, body_markdown, sections_json, "
    "sql_used, scope_snapshot, data_window, tags, model_used, persona_version, draft_hash, "
    "idempotency_key, created_at"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent (the same DDL as migration 3, for a connection not opened by open_store)."""
    with write_tx(conn):
        for stmt in REPORTS_MIGRATION:
            conn.execute(stmt)


def _req(name: str, value: Any, max_len: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"{name} is required")
    return value.strip()[:max_len]


def _row(row: Sequence[Any]) -> SavedReport:
    (rid, owner, sid, tid, title, body, sections, sql, scope, window, tags, model, persona,
     dhash, key, created) = row  # fmt: skip
    return SavedReport(
        rid, owner, sid, tid, title, body, json.loads(sections), json.loads(sql),
        json.loads(scope), window, json.loads(tags), model, persona, dhash, key, created,
    )  # fmt: skip


def _guarded(guard: BodyGuard, text: str, what: str, limit: int) -> str:
    """One stored text field through the output guard (redacted text), then the secret scrub."""
    if not text.strip():
        return text
    allowed, out = guard(text)
    if not allowed or not isinstance(out, str):
        raise ReportError(f"the output guard refused the report {what}")
    return tr.scrub_text(out, limit)


def _as_text(value: Any) -> str:
    """A section value as text: a string as is, anything else as its JSON text."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


class ReportStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_schema(conn)

    def save(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        turn_id: str,
        title: str,
        body_markdown: str,
        sections: Mapping[str, Any],
        sql_used: Sequence[str],
        scope_snapshot: Mapping[str, Any],
        data_window: str,
        draft_hash: str,
        idempotency_key: str,
        guard: BodyGuard,
        tags: Sequence[str] = (),
        model_used: str = "",
        persona_version: str = "",
    ) -> tuple[SavedReport, bool]:
        """Insert once per ``idempotency_key``. Returns (record, created).

        A repeat with the same key (a retry, a second process, a double "save") returns the
        existing record and ``created=False``; nothing is written twice. Atomic: the insert and
        the read-back run in one ``BEGIN IMMEDIATE`` transaction.
        """
        owner = _req("owner_user_id", owner_user_id)
        sid = _req("session_id", session_id)
        tid = _req("turn_id", turn_id)
        key = _req("idempotency_key", idempotency_key, 128)
        dhash = _req("draft_hash", draft_hash, 128)
        title = tr.scrub_text(_req("title", title, 200), 200)
        if not isinstance(body_markdown, str) or len(body_markdown) > MAX_BODY_CHARS:
            raise ReportError("report body is missing or too long")
        if not callable(guard):
            raise ReportError("a body guard is required")
        allowed, body = guard(body_markdown)
        if not allowed or not isinstance(body, str) or not body.strip():
            raise ReportError("the output guard refused the report body")
        body = tr.scrub_text(body, MAX_BODY_CHARS)  # secrets, defence in depth
        if missing_sections(body):
            raise ReportError("the report body lacks a required section")
        # B1: every stored free-text field passes the same guard as the body (PII redaction)
        # and the secret scrub: the title, section names and texts, the SQL, the data window,
        # tags and model/persona labels. A non-text section value is stored as its JSON text,
        # guarded like the rest (never stored raw).
        title = _guarded(guard, title, "title", 200)
        clean_sections = {
            _guarded(guard, str(k), "section name", 200): _guarded(
                guard, _as_text(v), "section", MAX_BODY_CHARS
            )
            for k, v in dict(sections).items()
        }
        clean_sql = [_guarded(guard, str(s), "SQL", MAX_BODY_CHARS) for s in sql_used]
        values = (
            uuid.uuid4().hex, owner, sid, tid, title, body,
            json.dumps(clean_sections, sort_keys=True),
            json.dumps(clean_sql),
            json.dumps(dict(scope_snapshot), sort_keys=True),
            _guarded(guard, str(data_window), "data window", 80),
            json.dumps([_guarded(guard, str(t), "tag", 40) for t in list(tags)[:8]]),
            _guarded(guard, str(model_used), "model label", 80),
            _guarded(guard, str(persona_version), "persona label", 80),
            dhash, key,
        )  # fmt: skip
        with write_tx(self.conn):
            cur = self.conn.execute(
                "INSERT INTO saved_report (report_id, owner_user_id, session_id, turn_id, title, "
                "body_markdown, sections_json, sql_used, scope_snapshot, data_window, tags, "
                "model_used, persona_version, draft_hash, idempotency_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                values,
            )
            created = cur.rowcount == 1
            row = self.conn.execute(
                f"SELECT {_COLS} FROM saved_report WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None or row[1] != owner:  # a key owned by someone else: never handed out
                raise ReportError("idempotency key conflict")
        return _row(row), created

    def get(self, report_id: str, owner_user_id: str) -> SavedReport | None:
        """The owner's report (another user's id returns None, never the row)."""
        row = self.conn.execute(
            f"SELECT {_COLS} FROM saved_report WHERE report_id=? AND owner_user_id=?",
            (str(report_id), str(owner_user_id)),
        ).fetchone()
        return _row(row) if row else None

    def get_by_key(self, idempotency_key: str, owner_user_id: str) -> SavedReport | None:
        """The owner's report saved under ``idempotency_key`` (a repeat save finds it first)."""
        row = self.conn.execute(
            f"SELECT {_COLS} FROM saved_report WHERE idempotency_key=? AND owner_user_id=?",
            (str(idempotency_key), str(owner_user_id)),
        ).fetchone()
        return _row(row) if row else None

    def rename(
        self, report_id: str, owner_user_id: str, title: str, guard: BodyGuard
    ) -> SavedReport | None:
        """Iteration 33 (AC-21.12): set the owner's report title. None when the id is not the
        owner's (another user's report is "not found", never touched). The title passes the
        same guard and secret scrub as at save time; the caller validates length and
        control characters first."""
        new_title = _guarded(guard, _req("title", title, 200), "title", 200)
        if not new_title.strip():
            raise ReportError("title is required")
        with write_tx(self.conn):
            cur = self.conn.execute(
                "UPDATE saved_report SET title=? WHERE report_id=? AND owner_user_id=?",
                (new_title, str(report_id), str(owner_user_id)),
            )
            if cur.rowcount != 1:
                return None
            row = self.conn.execute(
                f"SELECT {_COLS} FROM saved_report WHERE report_id=? AND owner_user_id=?",
                (str(report_id), str(owner_user_id)),
            ).fetchone()
        return _row(row) if row else None

    def get_fenced(self, report_id: str, owner_user_id: str) -> str | None:
        """The body as fenced untrusted data, for a prompt (SEC-13). PII-scrubbed and capped."""
        rec = self.get(report_id, owner_user_id)
        if rec is None:
            return None
        return fence_untrusted(KIND_REPORT, rec.body_markdown, item_id=rec.report_id)

    def list(self, owner_user_id: str, limit: int = 50) -> list[SavedReport]:
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM saved_report WHERE owner_user_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (str(owner_user_id), max(1, min(int(limit), MAX_LIST))),
        ).fetchall()
        return [_row(r) for r in rows]

    def store_items(self, owner_user_id: str, limit: int = 8) -> list[StoreItem]:
        """The owner's reports as context items; ``assemble_context`` fences and scope-filters
        them (a report saved under a wider scope is dropped for a narrower one, FR-76)."""
        return [
            StoreItem(KIND_REPORT, r.body_markdown, r.scope_snapshot, r.report_id)
            for r in reversed(self.list(owner_user_id, limit))
        ]

    def count(self, owner_user_id: str | None = None) -> int:
        if owner_user_id is None:
            return self.conn.execute("SELECT COUNT(*) FROM saved_report").fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM saved_report WHERE owner_user_id=?", (str(owner_user_id),)
        ).fetchone()[0]
