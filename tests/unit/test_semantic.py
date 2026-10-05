"""Iteration 38 (AC-21.13/14): semantic report search fused with FTS by RRF.

Offline, synthetic data only: a local concept embedder stands in for the provider (no network).
Covers the hybrid fusion, the degradation to FTS and to the word match, owner and scope
filtering before scoring, the lazy backfill (bounded, one embed call), re-embedding on rename,
embedding outside the transaction, an embedding failure never failing a save, the audited
delete of vector rows (zero rows left, no residue in the raw DB/WAL bytes) and the trace
(the path that ran, never the query text)."""

from __future__ import annotations

import hashlib
import struct
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from opsfleet_agent import commands
from opsfleet_agent.delete import flow
from opsfleet_agent.reports import fts, library, semantic
from opsfleet_agent.roles.library_agent import make_library_executors
from opsfleet_agent.store import audit as A
from opsfleet_agent.store import db
from opsfleet_agent.store.reports import ReportStore
from opsfleet_agent.store.vector_schema import VECTOR_TABLE
from tests.unit.test_library import ACME, _add, _ctx

TURN = "a1b2c3d4e5f6"
PA = "0a" * 16
MODEL = "fake-concept"
# synonyms share a dimension; words outside the vocabulary are ignored (zero contribution)
CONCEPTS = {"medlar": 0, "loquat": 0, "quince": 1, "japonica": 1, "sorrel": 2, "oxalis": 2}
DIM = 8


class ConceptEmbedder:
    """Deterministic: one dimension per concept, plus a small text fingerprint in the last
    dimension so every stored vector has distinctive bytes (for the residue test)."""

    def __init__(self, fail: bool = False, bad: bool = False, conn: Any = None) -> None:
        self.calls: list[list[str]] = []
        self.fail, self.bad, self.conn = fail, bad, conn
        self.in_tx: list[bool] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.conn is not None:
            self.in_tx.append(self.conn.in_transaction)
        if self.fail:
            raise TimeoutError("synthetic provider timeout")
        if self.bad:
            return [[float("nan")] * DIM for _ in texts]
        out = []
        for t in texts:
            v = [0.0] * DIM
            for tok in t.casefold().replace(".", " ").split():
                if tok in CONCEPTS:
                    v[CONCEPTS[tok]] += 1.0
            fp = int(hashlib.sha256(t.encode()).hexdigest()[:6], 16)
            v[DIM - 1] = 1e-3 * (1 + fp / 0xFFFFFF)
            out.append(v)
        return out


class Recorder:
    def __init__(self) -> None:
        self.spans: list[tuple[str, str, dict[str, Any]]] = []

    def record(self, kind: str, name: str, **fields: Any) -> None:
        self.spans.append((kind, name, fields))


def _index(emb: Any) -> semantic.SemanticIndex:
    return semantic.SemanticIndex(emb, MODEL, DIM)


@pytest.fixture
def emb() -> ConceptEmbedder:
    return ConceptEmbedder()


@pytest.fixture
def store(tmp_path, emb) -> ReportStore:
    conn = db.open_store(tmp_path / "sem.db")
    emb.conn = conn
    return ReportStore(conn, semantic=_index(emb))


def _hybrid(store, text, owner="analyst_a", scope=ACME, **kw) -> library.ListResult:
    return library.search_reports(store, owner, scope, text=text, mode="semantic", **kw)


def _vectors(conn) -> dict[str, tuple[str, bytes, str]]:
    rows = conn.execute(
        f"SELECT report_id, owner_user_id, vector, content_hash FROM {VECTOR_TABLE}"
    )
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


# --- write side ---------------------------------------------------------------------------------


def test_save_embeds_outside_the_transaction(store, emb) -> None:
    rid = _add(store, "k1", title="Medlar outlook")
    vecs = _vectors(store.conn)
    assert set(vecs) == {rid} and vecs[rid][0] == "analyst_a"
    assert len(vecs[rid][1]) == 4 * DIM  # packed float32, model dims
    assert emb.in_tx == [False]  # embedded after the commit, never inside the save transaction
    assert len(emb.calls) == 1 and "Medlar outlook" in emb.calls[0][0]
    # a repeat save (same idempotency key) writes nothing and embeds nothing
    _add(store, "k1", title="Medlar outlook")
    assert len(emb.calls) == 1


@pytest.mark.parametrize("mode", ["fail", "bad"])
def test_embedding_failure_does_not_fail_save(tmp_path, mode) -> None:
    emb = ConceptEmbedder(fail=mode == "fail", bad=mode == "bad")
    store = ReportStore(db.open_store(tmp_path / "f.db"), semantic=_index(emb))
    rid = _add(store, "k1", title="Medlar outlook")
    assert store.get(rid, "analyst_a") is not None and store.count("analyst_a") == 1
    assert _vectors(store.conn) == {} and store.semantic.failures == 1
    assert not store.conn.in_transaction
    # search still works: semantic unavailable, degraded to the full-text ranking
    res = _hybrid(store, "medlar")
    assert [e.report_id for e in res.entries] == [rid] and res.semantic_unavailable


def test_rename_reembeds(store, emb) -> None:
    rid = _add(store, "k1", title="Quince notes")
    before = _vectors(store.conn)[rid]
    assert _hybrid(store, "loquat").entries == ()  # no medlar concept yet
    renamed = store.rename(rid, "analyst_a", "Medlar notes", guard=lambda t: (True, t))
    assert renamed is not None and renamed.title == "Medlar notes"
    after = _vectors(store.conn)[rid]
    assert after[2] != before[2] and after[1] != before[1]
    assert "Medlar notes" in emb.calls[-1][0] and emb.in_tx[-1] is False
    res = _hybrid(store, "loquat")  # a synonym: FTS finds nothing, semantic finds the rename
    assert [e.report_id for e in res.entries] == [rid] and res.path == "hybrid"
    # another user's rename is "not found" and embeds nothing
    n = len(emb.calls)
    assert store.rename(rid, "analyst_b", "Sorrel", guard=lambda t: (True, t)) is None
    assert len(emb.calls) == n


def test_document_text_is_scrubbed_and_capped() -> None:
    rec = SimpleNamespace(
        title="Medlar for someone@example.invalid", sections={"Summary": "x " * 5000},
        body_markdown="", tags=["fruit"],
    )  # fmt: skip
    text = semantic.document_text(rec)
    assert "someone@example.invalid" not in text and len(text) <= semantic.MAX_DOC_CHARS


# --- query side ---------------------------------------------------------------------------------


def test_search_semantic_hybrid_finds_synonyms(store) -> None:
    medlar = _add(store, "k1", title="Medlar outlook")
    _add(store, "k2", title="Quince outlook")
    res = _hybrid(store, "loquat")  # no word overlap: only the semantic list has it
    assert res.path == "hybrid" and not res.semantic_unavailable
    assert [e.report_id for e in res.entries] == [medlar] and res.total == 1
    # the /search command uses the hybrid mode and says best match first
    out = commands.dispatch("/search loquat", _ctx(store)).text
    assert medlar in out and "best match first" in out


def test_rrf_ordering() -> None:
    # c: 1/63 + 1/61 > b: 1/62 + 1/62 > a: 1/61 > d: 1/64
    assert semantic.rrf_fuse(["a", "b", "c", "d"], ["c", "b"]) == ["c", "b", "a", "d"]
    # equal scores: the better best rank, then the id (deterministic)
    assert semantic.rrf_fuse(["b", "a"], ["a", "b"]) == ["a", "b"]
    assert semantic.rrf_fuse(["x", "x", "y"]) == ["x", "y"]  # duplicates count once
    assert semantic.rrf_fuse([], []) == []


def test_search_semantic_rrf_puts_both_lists_first(store) -> None:
    both = _add(store, "k1", title="Medlar medlar harvest")  # word match and meaning
    word = _add(store, "k2", title="Harvest calendar")  # word match only
    meaning = _add(store, "k3", title="Loquat outlook")  # meaning only
    res = _hybrid(store, "medlar harvest")
    ids = [e.report_id for e in res.entries]
    assert ids[0] == both and set(ids) == {both, meaning} | (set(ids) & {word})
    assert res.path == "hybrid" and len(ids) <= library.MAX_RESULTS


def test_search_semantic_degrades_to_fts(tmp_path, monkeypatch) -> None:
    emb = ConceptEmbedder(fail=True)
    store = ReportStore(db.open_store(tmp_path / "d.db"), semantic=_index(emb))
    rid = _add(store, "k1", title="Medlar outlook")
    emb.fail = True
    res = _hybrid(store, "medlar")
    assert res.semantic_unavailable and [e.report_id for e in res.entries] == [rid]
    assert res.path == ("ranked" if fts.has_index(store.conn) else "substring_fallback")
    # no semantic index at all: the same degradation
    plain = ReportStore(store.conn)
    res = _hybrid(plain, "medlar")
    assert res.semantic_unavailable and [e.report_id for e in res.entries] == [rid]
    # and without the FTS index: down to the word match
    monkeypatch.setattr(store, "ranked_search", lambda owner, match: None)
    res = _hybrid(store, "medlar")
    assert res.path == "substring_fallback" and res.semantic_unavailable
    assert [e.report_id for e in res.entries] == [rid]
    # without the FTS index but with embeddings: word match fused with semantic
    emb.fail = False
    res = _hybrid(store, "loquat")
    assert res.path == "hybrid_substring" and [e.report_id for e in res.entries] == [rid]


def test_search_semantic_owner_and_scope(tmp_path) -> None:
    conn = db.open_store(tmp_path / "o.db")
    plain = ReportStore(conn)  # saved before semantic search existed: no vectors yet
    mine = _add(plain, "k1", title="Medlar mine")
    theirs = _add(plain, "k2", owner="analyst_b", title="Medlar theirs")
    drift = _add(plain, "k3", title="Medlar drift", brands=("Other",))
    old = _add(plain, "k4", title="Medlar old", created="2020-01-01 00:00:00")
    assert _vectors(conn) == {}
    emb = ConceptEmbedder()
    store = ReportStore(conn, semantic=_index(emb))
    res = _hybrid(store, "loquat", date_from="2021-01-01")
    assert [e.report_id for e in res.entries] == [mine]
    # scope, owner and the filters apply before scoring: only the candidate was embedded
    assert len(emb.calls) == 1
    sent = " ".join(emb.calls[0])
    assert "Medlar mine" in sent
    for other in ("Medlar theirs", "Medlar drift", "Medlar old"):
        assert other not in sent
    assert set(_vectors(conn)) == {mine}  # never another owner's or an out-of-scope report
    # another owner never sees analyst_a's report, and gets their own
    res_b = _hybrid(store, "loquat", owner="analyst_b")
    assert [e.report_id for e in res_b.entries] == [theirs]
    # a stored vector of another owner is never scored for this owner
    direct = store.semantic.search(conn, "analyst_a", [store.get(theirs, "analyst_b")], "loquat")
    assert direct == []
    assert {drift, old}.isdisjoint(e.report_id for e in res.entries)


def test_search_semantic_backfill_bounded_one_call(tmp_path) -> None:
    conn = db.open_store(tmp_path / "b.db")
    plain = ReportStore(conn)
    for i in range(semantic.MAX_BACKFILL + 4):
        _add(plain, f"k{i}", title=f"Medlar {i}")
    emb = ConceptEmbedder()
    store = ReportStore(conn, semantic=_index(emb))
    res = _hybrid(store, "loquat")
    assert len(emb.calls) == 1 and len(emb.calls[0]) == 1 + semantic.MAX_BACKFILL
    assert len(_vectors(conn)) == semantic.MAX_BACKFILL
    assert 0 < len(res.entries) <= library.MAX_RESULTS
    _hybrid(store, "loquat")  # the query vector is cached; the remaining 4 are backfilled
    assert len(emb.calls) == 2 and len(emb.calls[1]) == 4
    _hybrid(store, "loquat")  # nothing left to do: no embed call at all
    assert len(emb.calls) == 2


def test_search_semantic_query_scrubbed_and_not_traced(store, emb) -> None:
    rid = _add(store, "k1", title="Medlar outlook")
    tracer = Recorder()
    ctx = _ctx(store)
    ctx.tracer = tracer
    out = commands.dispatch("/search loquat someone@example.invalid", ctx).text
    assert rid in out
    assert "someone@example.invalid" not in " ".join(emb.calls[-1])
    span = [f for _, n, f in tracer.spans if n == "search_reports"][-1]
    assert span["search_path"] == "hybrid" and span["semantic_unavailable"] is False
    assert "loquat" not in str(tracer.spans)


def test_library_agent_defaults_to_hybrid(store) -> None:
    rid = _add(store, "k1", title="Medlar outlook")
    tracer = Recorder()
    tools = make_library_executors(
        store=store, audit=None, owner="analyst_a", scope=ACME, session_id="s",
        turn_id=TURN, user_message="find", tools_used=lambda: (), pending=None,
        request_delete=None, tracer=tracer,
    )  # fmt: skip
    out = tools["search_reports"]({"text": "loquat"})
    assert rid in str(out)
    assert tracer.spans[-1][2]["search_path"] == "hybrid"


def test_migration_is_ddl_only(tmp_path) -> None:
    conn = db.connect(tmp_path / "m.db")
    db.migrate(conn, db.MIGRATIONS[:5])  # a database from before iteration 38
    assert (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (VECTOR_TABLE,)).fetchone() is None
    )
    assert db.migrate(conn) == 6
    assert conn.execute(f"SELECT COUNT(*) FROM {VECTOR_TABLE}").fetchone()[0] == 0


# --- delete (red gate) --------------------------------------------------------------------------


@pytest.fixture
def denv(tmp_path):
    path = tmp_path / "del.db"
    conn = db.open_store(path)
    emb = ConceptEmbedder(conn=conn)
    audit, store = A.AuditLog(conn), ReportStore(conn, semantic=_index(emb))
    assert flow.setup_delete(conn, audit, store) is not None
    yield SimpleNamespace(conn=conn, audit=audit, store=store, path=path)
    A.unregister_deletable(flow.KIND)
    conn.close()


def _delete(d, rid: str, owner: str = "analyst_a") -> int:
    out = A.audited_delete(
        d.audit, kind=flow.KIND, actor_user_id=owner, session_id=uuid4().hex, turn_id=TURN,
        pending_action_id=PA, target_ids=[rid],
    )  # fmt: skip
    return out.deleted


def test_delete_leaves_no_vector_residue(denv) -> None:
    keep = _add(denv.store, "k1", title="Quince keep")
    gone = _add(denv.store, "k2", title="Medlar gone")
    vecs = _vectors(denv.conn)
    blob = vecs[gone][1]
    assert len(blob) == 4 * DIM and set(vecs) == {keep, gone}
    db.checkpoint_truncate(denv.conn)
    assert blob in denv.path.read_bytes()  # the probe works: the vector is on disk
    assert _delete(denv, gone) == 1
    assert (
        denv.conn.execute(
            f"SELECT COUNT(*) FROM {VECTOR_TABLE} WHERE report_id=?", (gone,)
        ).fetchone()[0]
        == 0
    )
    assert set(_vectors(denv.conn)) == {keep}
    db.checkpoint_truncate(denv.conn)
    for p in (denv.path, denv.path.with_name(denv.path.name + "-wal")):
        if p.exists():
            assert blob not in p.read_bytes()
    # the audit record exists (written first, same transaction)
    n = denv.conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]
    assert n >= 1


def test_delete_of_report_without_vector(denv) -> None:
    denv.store.semantic.embedder.fail = True
    rid = _add(denv.store, "k1", title="Medlar unembedded")
    assert _vectors(denv.conn) == {}
    assert _delete(denv, rid) == 1  # zero dependent rows is an exact count too


def test_backfill_after_delete_leaves_no_orphan(denv) -> None:
    rid = _add(denv.store, "k1", title="Medlar orphan")
    rec = denv.store.get(rid, "analyst_a")
    assert _delete(denv, rid) == 1
    # a late write for a deleted report (a racing embed) is a no-op: no orphan vector
    denv.store.semantic._write(denv.conn, [(rid, "analyst_a", [1.0] * DIM, "h")])
    assert _vectors(denv.conn) == {} and rec is not None


def test_pack_roundtrip() -> None:
    vec = [0.5, -1.25, 3.0]
    assert semantic.unpack(semantic.pack(vec), 3) == vec
    assert semantic.unpack(b"\x00" * 5, 3) is None
    assert semantic.unpack(struct.pack("<2f", 1.0, 2.0), 3) is None
