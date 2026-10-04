"""Spike (iteration 2): LangGraph interrupt/resume through an encrypted SqliteSaver.

Proves, offline: (1) ``interrupt()`` pauses a graph and ``Command(resume=...)`` continues it, also
from a NEW saver and graph object on the same sqlite file (a process restart); (2) with
``EncryptedSerializer`` the checkpoint blobs on disk contain no plaintext state; (3) a role subgraph
compiled with ``checkpointer=False`` works as a node of the parent and is not checkpointed
on its own.

The AES key below is a synthetic test value. It is set through monkeypatch and is not a real key.
"""

from __future__ import annotations

import sqlite3
from operator import add
from typing import Annotated, TypedDict

from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

SENTINEL = "PLAINTEXT-SENTINEL-7f3a9c"
TEST_KEY = "0123456789abcdef0123456789abcdef"  # synthetic, 32 bytes


class State(TypedDict):
    secret: str
    log: Annotated[list[str], add]
    answer: str


class SubState(TypedDict):
    secret: str
    log: Annotated[list[str], add]


def _sub_graph():
    def sub_node(state: SubState) -> dict:
        return {"log": [f"sub saw {len(state['secret'])} chars"]}

    sg = StateGraph(SubState)
    sg.add_node("sub_node", sub_node)
    sg.add_edge(START, "sub_node")
    sg.add_edge("sub_node", END)
    return sg.compile(checkpointer=False)


def _build(saver: SqliteSaver):
    runs = {"ask": 0}

    def ask(state: State) -> dict:
        runs["ask"] += 1  # a node that calls interrupt() is re-run from its start on resume
        reply = interrupt({"question": "confirm delete?"})
        return {"answer": reply, "log": [f"asked, got {reply}"]}

    g = StateGraph(State)
    g.add_node("role", _sub_graph())  # compiled subgraph used directly as a node
    g.add_node("ask", ask)
    g.add_edge(START, "role")
    g.add_edge("role", "ask")
    g.add_edge("ask", END)
    return g.compile(checkpointer=saver), runs


def _encrypted_saver(path, monkeypatch) -> SqliteSaver:
    monkeypatch.setenv("LANGGRAPH_AES_KEY", TEST_KEY)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn, serde=EncryptedSerializer.from_pycryptodome_aes())


def test_interrupt_and_resume_through_encrypted_sqlite_saver(tmp_path, monkeypatch):
    db = tmp_path / "ckpt.sqlite"
    cfg = {"configurable": {"thread_id": "t1"}}

    saver = _encrypted_saver(db, monkeypatch)
    graph, _ = _build(saver)
    out = graph.invoke({"secret": SENTINEL, "log": [], "answer": ""}, cfg)
    assert out["__interrupt__"][0].value == {"question": "confirm delete?"}
    assert graph.get_state(cfg).next == ("ask",)
    saver.conn.close()

    # a fresh saver, connection and graph on the same file: the resume survives a restart
    saver2 = _encrypted_saver(db, monkeypatch)
    graph2, runs2 = _build(saver2)
    assert graph2.get_state(cfg).next == ("ask",)
    final = graph2.invoke(Command(resume="yes"), cfg)
    assert final["answer"] == "yes"
    assert final["log"] == [f"sub saw {len(SENTINEL)} chars", "asked, got yes"]
    assert runs2["ask"] == 1  # new graph object: one run (the resume run)
    assert graph2.get_state(cfg).next == ()
    saver2.conn.close()


def test_checkpoint_blobs_on_disk_do_not_contain_plaintext_state(tmp_path, monkeypatch):
    db = tmp_path / "ckpt.sqlite"
    cfg = {"configurable": {"thread_id": "t2"}}
    saver = _encrypted_saver(db, monkeypatch)
    graph, _ = _build(saver)
    graph.invoke({"secret": SENTINEL, "log": [], "answer": ""}, cfg)
    graph.invoke(Command(resume="yes"), cfg)
    saver.conn.commit()
    saver.conn.close()

    conn = sqlite3.connect(str(db))
    blobs: list[bytes] = []
    for table in ("checkpoints", "writes"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            blobs += [
                v.encode() if isinstance(v, str) else bytes(v)
                for v in row
                if isinstance(v, (bytes, str))
            ]
    conn.close()
    assert blobs, "expected checkpoint rows"
    assert all(SENTINEL.encode() not in b for b in blobs)
    assert SENTINEL.encode() not in db.read_bytes()  # and not anywhere in the raw file


def test_control_unencrypted_saver_does_leak_the_sentinel(tmp_path):
    """Control: proves the check above can fail, so the encrypted result is meaningful."""
    db = tmp_path / "plain.sqlite"
    conn = sqlite3.connect(str(db), check_same_thread=False)
    saver = SqliteSaver(conn)
    graph, _ = _build(saver)
    graph.invoke(
        {"secret": SENTINEL, "log": [], "answer": ""}, {"configurable": {"thread_id": "t3"}}
    )
    conn.commit()
    conn.close()
    assert SENTINEL.encode() in db.read_bytes()


def test_subgraph_with_checkpointer_false_leaves_no_own_checkpoint_namespace(tmp_path, monkeypatch):
    db = tmp_path / "ckpt.sqlite"
    cfg = {"configurable": {"thread_id": "t4"}}
    saver = _encrypted_saver(db, monkeypatch)
    graph, _ = _build(saver)
    graph.invoke({"secret": SENTINEL, "log": [], "answer": ""}, cfg)
    namespaces = {
        c.config["configurable"].get("checkpoint_ns", "") for c in graph.get_state_history(cfg)
    }
    assert namespaces == {""}  # only the parent's namespace: the subgraph was not checkpointed
    saver.conn.close()
