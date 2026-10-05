"""Iteration 37 (AC-21.13): ranked full-text report search with SQLite FTS5 bm25.

Offline, synthetic data only. Covers ranking, inert query syntax, owner isolation, index sync
on save/rename/delete, delete residue in the FTS shadow tables and the raw DB/WAL bytes, the
migration backfill and the substring fallback."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from opsfleet_agent import commands
from opsfleet_agent.delete import flow
from opsfleet_agent.reports import fts, library
from opsfleet_agent.roles.library_agent import make_library_executors
from opsfleet_agent.store import audit as A
from opsfleet_agent.store import db
from opsfleet_agent.store.reports import ReportStore
from tests.unit.test_library import ACME, _add, _ctx

pytestmark = pytest.mark.skipif(not fts.fts5_supported(), reason="SQLite without FTS5")

TURN = "a1b2c3d4e5f6"
PA = "0a" * 16


class Recorder:
    def __init__(self) -> None:
        self.spans: list[tuple[str, str, dict[str, Any]]] = []

    def record(self, kind: str, name: str, **fields: Any) -> None:
        self.spans.append((kind, name, fields))


@pytest.fixture
def store(tmp_path) -> ReportStore:
    return ReportStore(db.open_store(tmp_path / "fts.db"))


@pytest.fixture
def denv(tmp_path):
    """A store with the audited delete registered (FTS dependent included)."""
    path = tmp_path / "del.db"
    conn = db.open_store(path)
    audit, store = A.AuditLog(conn), ReportStore(conn)
    svc = flow.setup_delete(conn, audit, store)
    assert svc is not None
    yield SimpleNamespace(conn=conn, audit=audit, store=store, path=path)
    A.unregister_deletable(flow.KIND)
    conn.close()


def _delete(d, rid: str, owner: str = "analyst_a") -> None:
    out = A.audited_delete(
        d.audit, kind=flow.KIND, actor_user_id=owner, session_id=uuid4().hex, turn_id=TURN,
        pending_action_id=PA, target_ids=[rid],
    )  # fmt: skip
    assert out.deleted == 1


def _fts_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(f"SELECT {fts.FTS_KEY} FROM {fts.FTS_TABLE}")}


def _ranked(store, text, owner="analyst_a", **kw) -> library.ListResult:
    return library.search_reports(store, owner, ACME, text=text, mode="ranked", **kw)


def test_search_ranked_fts_bm25(store) -> None:
    body = _add(store, "k1", title="Quarterly summary", extra="Notes on marmalade returns.")
    title = _add(store, "k2", title="Marmalade returns", extra="Plain synthetic notes.")
    tagged = _add(store, "k3", title="Other summary", tags=("marmalade",))
    _add(store, "k4", title="Unrelated", extra="Nothing here.")
    res = _ranked(store, "marmalade")
    assert res.path == "ranked"
    ids = [e.report_id for e in res.entries]
    assert set(ids) == {body, title, tagged} and ids[0] == title  # the title weighs most
    assert ids.index(tagged) < ids.index(body)  # a tag outranks a body-only hit
    # stemming: "returning" matches "returns"; every word must match (implicit AND)
    assert {e.report_id for e in _ranked(store, "returning marmalade").entries} == {body, title}
    out = commands.dispatch("/search marmalade", _ctx(store)).text
    assert "best match first" in out and title in out
    assert out.index(title) < out.index(body)


@pytest.mark.parametrize(
    "query",
    ['"', 'marmalade"', "marm*", "NEAR(marmalade notes)", "marmalade OR nothing",
     "-marmalade", "title:marmalade", "(marmalade)", "marmalade AND NOT x", "^marmalade",
     "{title body}: marmalade", "marmalade + notes"],
)  # fmt: skip
def test_search_fts_query_syntax_quoted(store, query) -> None:
    _add(store, "k1", title="Marmalade notes")
    try:
        match = fts.build_match(query)
    except fts.FtsQueryError:
        with pytest.raises(library.LibraryError):
            _ranked(store, query)
        return
    # every term is a quoted literal: nothing is left for FTS5 to parse as an operator
    assert all(t.startswith('"') and t.endswith('"') for t in match.split(" "))
    res = _ranked(store, query)  # never an FTS syntax error, never a fallback
    assert res.path == "ranked"
    words = {w.casefold() for w in fts._TERM_RE.findall(query)}
    expected = 1 if words <= {"marmalade", "notes"} else 0  # operators are literal words
    assert len(res.entries) == expected


def test_build_match_bounds() -> None:
    assert fts.build_match('a "b" a') == '"a" "b"'
    with pytest.raises(fts.FtsQueryError):
        fts.build_match('"*()-:')
    with pytest.raises(fts.FtsQueryError):
        fts.build_match(" ".join(f"w{i}" for i in range(fts.MAX_TERMS + 1)))
    assert len(fts.build_match(" ".join(f"w{i}" for i in range(fts.MAX_TERMS))).split()) == 16


def test_search_ranked_owner_isolation(store) -> None:
    mine = _add(store, "k1", title="Plum forecast")
    for i in range(5):
        _add(store, f"b{i}", owner="analyst_b", title=f"Plum plum plum {i}")
    res = _ranked(store, "plum")
    assert [e.report_id for e in res.entries] == [mine] and res.total == 1
    assert not res.truncated_scan
    assert len(store.ranked_search("analyst_a", '"plum"')) == 1
    assert store.ranked_search("analyst_c", '"plum"') == []
    out = commands.dispatch("/search plum", _ctx(store, "analyst_c")).text
    assert "Plum plum" not in out and "Plum forecast" not in out


def test_search_ranked_filters_and_limit(store) -> None:
    old = _add(store, "k1", title="Quince 1", tags=("ops",), created="2020-01-05T00:00:00+00:00")
    new = _add(store, "k2", title="Quince 2", created="2024-06-01T00:00:00+00:00")
    _add(store, "k3", title="Quince 3", brands=("Zenith",))  # outside the ACME scope
    assert {e.report_id for e in _ranked(store, "quince").entries} == {old, new}
    assert [e.report_id for e in _ranked(store, "quince", tags=["ops"]).entries] == [old]
    got = _ranked(store, "quince", date_from="2024-01-01")
    assert [e.report_id for e in got.entries] == [new]
    for i in range(library.MAX_RESULTS + 5):
        _add(store, f"m{i}", title=f"Quince many {i}")
    res = _ranked(store, "quince")
    assert len(res.entries) == library.MAX_RESULTS and res.total == library.MAX_RESULTS + 7


def test_rename_reindexes_title(store) -> None:
    rid = _add(store, "k1", title="Apricot outlook")
    assert [e.report_id for e in _ranked(store, "apricot").entries] == [rid]
    assert store.rename(rid, "analyst_a", "Nectarine outlook", guard=lambda b: (True, b))
    assert _ranked(store, "apricot").entries == ()
    assert [e.report_id for e in _ranked(store, "nectarine").entries] == [rid]
    assert store.rename(rid, "analyst_b", "Stolen", guard=lambda b: (True, b)) is None
    assert _ranked(store, "stolen").entries == ()


def test_index_rows_deleted_with_report(denv) -> None:
    keep = _add(denv.store, "k1", title="Damson keep")
    gone = _add(denv.store, "k2", title="Damson gone")
    assert _fts_ids(denv.conn) == {keep, gone}
    _delete(denv, gone)
    assert _fts_ids(denv.conn) == {keep}
    assert [e.report_id for e in _ranked(denv.store, "damson").entries] == [keep]
    assert [e.event_type for e in denv.audit.events(newest_first=False)][-1] == A.DELETE_EXECUTED


def test_delete_registers_fts_dependent(denv) -> None:
    kind = A._DELETABLE[flow.KIND][0]
    assert kind.fts_dependents == ((fts.FTS_TABLE, fts.FTS_KEY),)


MARKER = "Xylophonequartz"


@pytest.mark.parametrize("shadow", ["_data", "_idx", "_content", "_docsize", "raw_bytes"])
def test_delete_leaves_no_fts_residue(denv, shadow) -> None:
    keep = _add(denv.store, "k1", title="Residue keep", extra="Ordinary synthetic text.")
    gone = _add(denv.store, "k2", title=f"{MARKER} report", extra=f"{MARKER} body {MARKER}.",
                tags=(MARKER.lower(),))  # fmt: skip
    needle = MARKER.lower().encode()
    content = denv.conn.execute(f"SELECT * FROM {fts.FTS_TABLE}_content").fetchall()
    assert any(MARKER in str(c) for row in content for c in row)  # it was indexed
    _delete(denv, gone)
    db.checkpoint_truncate(denv.conn)
    if shadow == "raw_bytes":
        for p in (denv.path, denv.path.with_name(denv.path.name + "-wal")):
            if p.exists():
                assert needle not in p.read_bytes().lower()  # the id stays, in the audit log
        return
    rows = denv.conn.execute(f"SELECT * FROM {fts.FTS_TABLE}{shadow}").fetchall()
    for row in rows:
        for cell in row:
            blob = cell if isinstance(cell, bytes) else str(cell).encode()
            assert needle not in blob.lower() and gone.encode() not in blob
    if shadow == "_docsize":
        assert len(rows) == 1  # only the kept report
    assert _fts_ids(denv.conn) == {keep}


def test_audit_rejects_non_fts_dependent(tmp_path) -> None:
    conn = db.open_store(tmp_path / "k.db")
    ReportStore(conn)
    conn.execute("CREATE TABLE plain_idx (report_id TEXT)")
    conn.execute(
        "CREATE VIRTUAL TABLE ext_fts USING fts5(report_id, title, content='saved_report')"
    )
    try:
        for table in ("plain_idx", "ext_fts", "missing_fts"):
            kind = A.DeletableKind("fts_probe", "saved_report", "report_id",
                                   owner_column="owner_user_id",
                                   fts_dependents=((table, "report_id"),))  # fmt: skip
            with pytest.raises(A.AuditError):
                A.register_deletable(kind, conn=conn)
        with pytest.raises(A.AuditError):  # never the kind's own table
            A.DeletableKind("fts_probe", "saved_report", "report_id",
                            owner_column="owner_user_id",
                            fts_dependents=(("saved_report", "report_id"),))  # fmt: skip
        with pytest.raises(A.AuditError):  # missing column
            A.register_deletable(
                A.DeletableKind("fts_probe", "saved_report", "report_id",
                                owner_column="owner_user_id",
                                fts_dependents=((fts.FTS_TABLE, "nope"),)),  # fmt: skip
                conn=conn,
            )
    finally:
        A.unregister_deletable("fts_probe")
        conn.close()


def _no_fts_store(tmp_path, monkeypatch, name="old.db") -> sqlite3.Connection:
    """A database at schema 5 opened by a SQLite without FTS5 (migration 5 empty)."""
    monkeypatch.setattr(fts, "fts5_supported", lambda: False)
    conn = db.connect(tmp_path / name)
    db.migrate(conn, (*db.MIGRATIONS[:4], (5, ())))
    return conn


def test_migration_backfills_index(tmp_path, monkeypatch) -> None:
    conn = db.connect(tmp_path / "pre.db")
    db.migrate(conn, db.MIGRATIONS[:4])  # a database from before iteration 37
    monkeypatch.setattr(fts, "fts5_supported", lambda: False)
    store = ReportStore(conn)
    a = _add(store, "k1", title="Medlar outlook", tags=("fruit",))
    b = _add(store, "k2", owner="analyst_b", title="Medlar other")
    assert not fts.has_index(conn)
    monkeypatch.undo()
    assert db.migrate(conn) == max(v for v, _ in db.MIGRATIONS)
    assert fts.has_index(conn) and _fts_ids(conn) == {a, b}
    store = ReportStore(conn)
    assert [e.report_id for e in _ranked(store, "medlar").entries] == [a]
    assert [e.report_id for e in _ranked(store, "fruit").entries] == [a]  # tags backfilled
    assert db.migrate(conn) == max(v for v, _ in db.MIGRATIONS) and len(_fts_ids(conn)) == 2
    conn.close()


def test_ensure_schema_creates_index_later(tmp_path, monkeypatch) -> None:
    conn = _no_fts_store(tmp_path, monkeypatch)
    store = ReportStore(conn)
    a = _add(store, "k1", title="Sloe notes")
    assert not fts.has_index(conn)
    monkeypatch.undo()
    store = ReportStore(conn)  # a newer SQLite: the index is created and backfilled
    assert fts.has_index(conn) and _fts_ids(conn) == {a}
    conn.close()


def test_search_falls_back_to_substring(tmp_path, monkeypatch) -> None:
    conn = _no_fts_store(tmp_path, monkeypatch)
    store = ReportStore(conn)
    rid = _add(store, "k1", title="Greengage notes")
    assert store.ranked_search("analyst_a", '"greengage"') is None
    res = _ranked(store, "greengage")
    assert res.path == "substring_fallback" and [e.report_id for e in res.entries] == [rid]
    tracer = Recorder()
    ctx = _ctx(store)
    ctx.tracer = tracer
    out = commands.dispatch("/search greengage", ctx).text
    assert "newest first" in out and rid in out
    span = [f for k, n, f in tracer.spans if n == "search_reports"]
    assert span and span[-1]["search_path"] == "substring_fallback"
    assert "greengage" not in repr(tracer.spans).lower()  # never the query text
    # the delete kind has no FTS dependent without an index
    audit = A.AuditLog(conn)
    assert flow.setup_delete(conn, audit, store) is not None
    try:
        assert A._DELETABLE[flow.KIND][0].fts_dependents == ()
    finally:
        A.unregister_deletable(flow.KIND)
        conn.close()


def test_ranked_path_traced(store) -> None:
    _add(store, "k1", title="Loquat notes")
    tracer = Recorder()
    ctx = _ctx(store)
    ctx.tracer = tracer
    commands.dispatch("/search loquat", ctx)
    assert [f["search_path"] for _, n, f in tracer.spans if n == "search_reports"] == ["ranked"]


def test_library_tool_search_mode(store) -> None:
    rid = _add(store, "k1", title="Kumquat notes")
    tracer = Recorder()
    tools = make_library_executors(
        store=store, audit=None, owner="analyst_a", scope=ACME, session_id="s",
        turn_id=TURN, user_message="find", tools_used=lambda: (), pending=None,
        request_delete=None, tracer=tracer,
    )  # fmt: skip
    bad = tools["search_reports"]({"text": "kumquat", "mode": "fuzzy"})
    assert "mode" in str(bad).lower() and rid not in str(bad)
    ok = tools["search_reports"]({"text": "kumquat", "mode": "ranked"})
    assert rid in str(ok)
    assert tracer.spans[-1][2]["search_path"] == "ranked"
    tools["search_reports"]({"text": "kumquat", "mode": "substring"})
    assert tracer.spans[-1][2]["search_path"] == "substring"
    # iteration 38: the default is the hybrid mode; without an embedder it degrades to ranked
    tools["search_reports"]({"text": "kumquat"})
    assert tracer.spans[-1][2]["search_path"] == "ranked"
    assert tracer.spans[-1][2]["semantic_unavailable"] is True
