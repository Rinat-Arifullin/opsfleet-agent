"""Residue checks for user erasure (iteration 35; SEC-18, AC-28.6, FR-59; D-222..D-226).

Two synthetic users are seeded in EVERY per-user store (app.db tables, FTS, vectors,
checkpoints, traces, exports, golden candidates, audit rows), user A is erased through the
maintainer CLI, then:

* every app.db table is enumerated from ``sqlite_master``: a mapped table has zero rows of A;
  an unmapped table fails the test unless it is allowlisted below WITH a reason;
* A's id and content markers are absent from the raw bytes of app.db, its WAL and every file
  under the data dir (FTS and vector residue included);
* B's rows, files and checkpoint thread are intact.

All data is synthetic. No network.
"""

from __future__ import annotations

import io
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from opsfleet_agent.commands import erase as E
from opsfleet_agent.commands.report_actions import owner_folder_name
from opsfleet_agent.commands.triage import CANDIDATES_DIR
from opsfleet_agent.graph import graph as gr
from opsfleet_agent.reports import fts
from opsfleet_agent.store import audit as A
from opsfleet_agent.store import fingerprints
from opsfleet_agent.store.db import open_store
from opsfleet_agent.store.feedback import FeedbackStore
from opsfleet_agent.store.preferences import SQLitePreferenceStore
from opsfleet_agent.store.quota import QuotaStore
from opsfleet_agent.store.reports import ReportStore
from tests.unit.test_library import _add

MAINTAINER = "support_demo"
MAINTAINERS = frozenset({MAINTAINER})
KEY_ENV = {gr.AES_KEY_ENV: "k" * 32}  # synthetic test key
T0 = 1_900_000_000.0
USER_A, USER_B = "erasee_alpha", "keeper_beta"
MARK = {USER_A: "zqxalphamark", USER_B: "zqxbetamark"}  # content markers, never ids
SESSION = {USER_A: "a1" * 16, USER_B: "b2" * 16}

#: user-keyed tables -> the column naming the user (the erase deletes rows by it)
USER_COLUMNS: dict[str, str] = dict(A.ERASE_USER_TABLES)
#: tables checked another way, with the reason
SPECIAL: dict[str, str] = {
    fts.FTS_TABLE: "keyed by report id; checked against A's report ids",
    "audit_event": "append-only; A's rows are pseudonymised, not deleted (D-223)",
}
#: tables that hold no per-user rows, with the reason (a new table must be added somewhere)
ALLOWLIST: dict[str, str] = {
    "meta": "global key/value (schema version, HMAC keys); no per-user rows",
    "schema_migrations": "migration bookkeeping; no user data",
    "sqlite_sequence": "SQLite autoincrement counters; no user data",
}
FTS_SHADOW = re.compile(rf"{fts.FTS_TABLE}_(data|idx|content|docsize|config)")


@dataclass
class Seeded:
    report_id: str
    feedback_id: str
    files: dict[str, list[Path]]


@dataclass
class World:
    data_dir: Path
    cases_dir: Path
    conn: sqlite3.Connection
    users: dict[str, Seeded]

    def run(self, *argv: str, clock: float = T0) -> tuple[int, str]:
        out = io.StringIO()
        code = E.main(
            ["--as", MAINTAINER, "--data-dir", str(self.data_dir), *argv],
            conn=self.conn,
            maintainers=MAINTAINERS,
            env=KEY_ENV,
            clock=lambda: clock,
            cases_dir=self.cases_dir,
            out=out,
        )
        return code, out.getvalue()

    def erase(self, user: str) -> tuple[int, str]:
        code, text = self.run("--user", user)
        assert code == 0, text
        token = re.search(r"--confirm (\S+)", text).group(1)  # type: ignore[union-attr]
        return self.run("--user", user, "--confirm", token, "--retype", user)


def seed_user(conn: sqlite3.Connection, data_dir: Path, cases_dir: Path, user: str) -> Seeded:
    """One synthetic user in every per-user store."""
    mark, sess = MARK[user], SESSION[user]
    rid = _add(ReportStore(conn), f"{user}-k1", owner=user, extra=f"finding {mark}",
               session=sess)  # fmt: skip
    conn.execute(
        "INSERT INTO report_vector (report_id, owner_user_id, model, dims, vector, content_hash) "
        "VALUES (?,?,?,?,?,?)",
        (rid, user, "synthetic-embed", 3, mark.encode() * 4, "c" * 64),
    )
    SQLitePreferenceStore(conn).save(
        user,
        {
            "preferences": {},
            "notes": [{"text": f"note {mark}", "scope": {"all": True, "brands": []}}],
        },
    )
    rec, _ = FeedbackStore(conn).add(
        user_id=user, session_id=sess, turn_id="a1b2c3d4e5f6", trace_id=None,
        rating="down", comment=f"comment {mark}", reason="other",
    )  # fmt: skip
    QuotaStore(conn).record_calls(user, 2)
    fingerprints.ensure_schema(conn)
    conn.execute(
        "INSERT INTO aggregate_fingerprint (fingerprint_id, user_id, session_id, scope_key, "
        "dims, filters, predicates, cells, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"fp-{user}", user, sess, "scope", f'["{mark}"]', "[]", "[]", None, int(T0)),
    )
    log = A.AuditLog(conn)
    log.record(A.TOOL_RUN_SQL, actor_user_id=user, session_id=sess, turn_id="a1b2c3d4e5f6",
               outcome="ok")  # fmt: skip
    log.record(A.DELETE_PREVIEWED, actor_user_id=user, session_id=sess, turn_id="a1b2c3d4e5f6",
               pending_action_id=sess, target_ids=[rid], count=1, outcome="previewed",
               details={"kind": "report"})  # fmt: skip
    saver = gr.build_checkpointer(data_dir, KEY_ENV)
    cp = empty_checkpoint()
    cp["channel_values"] = {"owner": user, "question": f"question {mark}"}
    saver.put({"configurable": {"thread_id": sess, "checkpoint_ns": ""}}, cp, {}, {})
    saver.conn.close()
    files: dict[str, list[Path]] = {}
    trace = data_dir / E.TRACES_DIR / f"{sess}.jsonl"
    own = data_dir / E.EXPORTS_DIR / owner_folder_name(user)  # D-228 per-owner folder
    exports = [
        own / f"R-{rid}.md",
        own / f"custom-{user}.md",
        data_dir / E.EXPORTS_DIR / f"legacy-{user}.md",  # pre-D-228 flat file: by its header
    ]
    cand = data_dir / CANDIDATES_DIR / f"candidate-{user}.yaml"
    for p in (trace, *exports, cand):
        p.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(f'{{"event": "turn", "question": "{mark}"}}\n')
    for p in exports:
        p.write_text(f"# Synthetic\n\nReport R-{rid}, created 2026-01-01.\n\n{mark}\n")
    cand.write_text(f"# Golden candidate from feedback {rec.feedback_id} (iteration 36).\n{mark}\n")
    files.update(traces=[trace], exports=exports, golden_candidates=[cand])
    draft = cases_dir / f"triage_{rec.feedback_id[:12]}.yaml"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("synthetic draft\n")
    return Seeded(rid, rec.feedback_id, files)


def build_world(tmp: Path) -> World:
    data_dir, cases_dir = tmp / "data", tmp / "cases"
    data_dir.mkdir(parents=True)
    conn = open_store(data_dir / "app.db")
    users = {u: seed_user(conn, data_dir, cases_dir, u) for u in (USER_A, USER_B)}
    return World(data_dir, cases_dir, conn, users)


def user_rows(conn: sqlite3.Connection, user: str, report_id: str) -> dict[str, int]:
    """Rows of one user per table, every table in sqlite_master mapped or allowlisted."""
    out: dict[str, int] = {}
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall():
        if table in USER_COLUMNS:
            sql, args = f"SELECT COUNT(*) FROM {table} WHERE {USER_COLUMNS[table]} = ?", (user,)
        elif table == fts.FTS_TABLE:
            sql, args = f"SELECT COUNT(*) FROM {table} WHERE {fts.FTS_KEY} = ?", (report_id,)
        elif table == "audit_event":
            sql, args = "SELECT COUNT(*) FROM audit_event WHERE actor_user_id = ?", (user,)
        elif table in ALLOWLIST or FTS_SHADOW.fullmatch(table):
            continue
        else:
            raise AssertionError(
                f"table {table!r} is not mapped for erasure: add it to ERASE_USER_TABLES, "
                "or allowlist it in tests/unit/test_residue.py with a reason"
            )
        out[table] = conn.execute(sql, args).fetchone()[0]
    return out


def _files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


@pytest.fixture(scope="module")
def erased(tmp_path_factory: pytest.TempPathFactory) -> tuple[World, dict, dict, str]:
    world = build_world(tmp_path_factory.mktemp("residue"))
    before = {u: user_rows(world.conn, u, s.report_id) for u, s in world.users.items()}
    code, text = world.erase(USER_A)
    assert code == 0, text
    return world, before, {u: user_rows(world.conn, u, s.report_id)
                           for u, s in world.users.items()}, text  # fmt: skip


def test_seed_covers_every_user_table(erased) -> None:
    _, before, _, _ = erased
    for user in (USER_A, USER_B):
        assert all(n > 0 for n in before[user].values()), before[user]
    expected = set(USER_COLUMNS) | {"audit_event"} | ({fts.FTS_TABLE} if fts.fts5_supported()
                                                      else set())  # fmt: skip
    assert set(before[USER_A]) == expected


def test_unmapped_table_fails_the_residue_check(tmp_path) -> None:
    conn = open_store(tmp_path / "app.db")
    conn.execute("CREATE TABLE new_user_store (user_id TEXT)")
    with pytest.raises(AssertionError, match="new_user_store"):
        user_rows(conn, USER_A, "0" * 32)
    conn.close()


@pytest.mark.parametrize("table", sorted(set(USER_COLUMNS) | set(SPECIAL)))
def test_no_rows_of_erased_user(erased, table: str) -> None:
    _, before, after, _ = erased
    if table not in after[USER_A]:
        pytest.skip(f"{table} not present in this SQLite build")
    assert after[USER_A][table] == 0
    assert after[USER_B][table] == before[USER_B][table] > 0  # B intact


@pytest.mark.parametrize("store", ["traces", "exports", "golden_candidates"])
def test_files_of_erased_user_gone(erased, store: str) -> None:
    world, _, _, _ = erased
    assert all(not p.exists() for p in world.users[USER_A].files[store])
    assert all(p.exists() for p in world.users[USER_B].files[store])


def test_checkpoint_thread_of_erased_user_gone(erased) -> None:
    world, _, _, _ = erased
    saver = gr.build_checkpointer(world.data_dir, KEY_ENV)
    try:
        threads = {r[0] for r in saver.conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}
        assert threads == {SESSION[USER_B]}
        b = saver.get_tuple({"configurable": {"thread_id": SESSION[USER_B]}})
        assert b.checkpoint["channel_values"]["owner"] == USER_B
    finally:
        saver.conn.close()


def test_audit_rows_pseudonymised_and_erase_recorded(erased) -> None:
    world, before, _, text = erased
    log = A.AuditLog(world.conn)
    events = log.events()
    executed = [e for e in events if e.event_type == A.ERASE_EXECUTED]
    assert len(executed) == 1 and executed[0].actor_user_id == MAINTAINER
    pseudonym = executed[0].details["target_user"]
    assert A.PSEUDONYM_RE.fullmatch(pseudonym) and pseudonym in text
    assert sum(e.actor_user_id == pseudonym for e in events) == before[USER_A]["audit_event"]
    assert not any(e.event_type == A.ERASE_FAILED for e in events)
    # the erase record itself names nothing erased
    blob = repr(executed[0])
    assert USER_A not in blob and MARK[USER_A] not in blob
    assert world.users[USER_A].report_id not in blob


def test_listed_items_need_a_person(erased) -> None:
    _, _, _, text = erased
    assert "regression-case draft" in text and E.NOT_COVERED in text


def test_no_bytes_of_erased_user_on_disk(erased) -> None:
    """A's id and markers are absent from app.db, its WAL and every data-dir file (FTS and
    vector residue included); B's marker is still there (the scan reads the right bytes)."""
    world, _, _, _ = erased
    a = world.users[USER_A]
    needles = [USER_A.encode(), MARK[USER_A].encode()]
    # The random report id survives ONLY in audit target_ids (D-223: the audit trail keeps
    # object ids, which link to nothing once the user's rows are gone); nowhere else.
    assert [r[0] for r in world.conn.execute(
        "SELECT event_type FROM audit_event WHERE instr(target_ids, ?) > 0", (a.report_id,)
    )] == [A.DELETE_PREVIEWED]  # fmt: skip
    if fts.fts5_supported():  # the FTS index stores tokens: the marker must be gone from it
        assert not world.conn.execute(
            f"SELECT COUNT(*) FROM {fts.FTS_TABLE} WHERE {fts.FTS_TABLE} MATCH ?",
            (MARK[USER_A],),
        ).fetchone()[0]
    paths = _files_under(world.data_dir)
    assert world.data_dir / "app.db" in paths
    seen_b = False
    for p in paths:
        raw = p.read_bytes()
        for n in needles + ([a.report_id.encode()] if p.name != "app.db" else []):
            assert n not in raw, f"{n!r} left in {p.relative_to(world.data_dir)}"
        seen_b = seen_b or MARK[USER_B].encode() in raw
    assert seen_b


def test_second_erase_is_noop_but_audited(tmp_path) -> None:
    world = build_world(tmp_path)
    try:
        assert world.erase(USER_A)[0] == 0
        code, text = world.erase(USER_A)
        assert code == 0, text
        executed = sorted((e for e in A.AuditLog(world.conn).events()
                           if e.event_type == A.ERASE_EXECUTED), key=lambda e: e.seq)  # fmt: skip
        assert len(executed) == 2 and executed[-1].count == 0
        assert executed[0].details["target_user"] != executed[-1].details["target_user"]
        assert all(n == 0 for n in user_rows(world.conn, USER_A, "x").values())
    finally:
        world.conn.close()
