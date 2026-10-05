"""Iteration 22a: K_delete, the derived token and the confirmation proof (ADR-007).

Offline only; synthetic reports. The flow fixture comes from ``test_delete_flow``.
"""

from __future__ import annotations

import json
import pickle
from uuid import uuid4

import pytest

from opsfleet_agent.delete import flow
from opsfleet_agent.delete.token import (
    DeleteKey,
    derive_token,
    make_proof,
    new_key,
    token_sha256,
)
from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.store import audit as A
from tests.unit import test_delete_flow as _flow
from tests.unit.test_delete_flow import (  # noqa: F401 - pytest fixtures used by name
    PROFILE,
    alive,
    ask,
    events,
    mk,
    pending,
    stage,
)
from tests.unit.test_graph import detector, make_env, settings  # noqa: F401

denv = _flow.denv  # the shared fixture

TEXT = "delete reports about quarterly widgets"


def token_of(d, pa) -> str:
    return derive_token(d.svc._key, flow._fields(pa))


def checkpoint_dump(d) -> str:
    """Every checkpoint and pending write of the session, as repr and as pickled bytes."""
    config = {"configurable": {"thread_id": d.env.session.session_id}}
    saver = d.env.saver
    blob = " ".join(repr(t) for t in saver.list(config))
    blob += repr(saver.storage) + repr(saver.writes)
    return blob + repr(pickle.dumps((dict(saver.storage), dict(saver.writes))))


def proof_of(d, pa) -> str:
    return d.svc._held[pa["pending_action_id"]]["proof"]


def test_confirm_delete_never_deletes(denv) -> None:
    ids = mk(denv, "Quarterly Widgets", n=2)
    pa, kw = stage(denv, TEXT)
    st = denv.svc.confirm(pa, denv.svc.reply_payload("yes", pa), turn=2, turn_id=uuid4().hex,
                          **kw)  # fmt: skip
    assert st.step == "confirmed" and st.text == "" and st.count == 2
    assert alive(denv, ids) == ids and not events(denv, A.DELETE_EXECUTED)
    assert len(events(denv, A.DELETE_CONFIRMED)) == 1


def test_confirm_delete_rerun_keeps_token(denv) -> None:
    mk(denv, "Quarterly Widgets")
    pa, kw = stage(denv, TEXT)
    before = (pa["token_sha256"], token_of(denv, pa))
    pay = denv.svc.reply_payload("yes", pa)
    assert denv.svc.reply_payload("yes", pa) == pay  # a retry derives the same proof
    a = denv.svc.confirm(pa, pay, turn=2, turn_id=uuid4().hex, **kw)
    b = denv.svc.confirm(pa, pay, turn=2, turn_id=uuid4().hex, **kw)  # node rerun (AC-12.13)
    assert a.step == b.step == "confirmed" and a.audit_event_id == b.audit_event_id
    assert (pa["token_sha256"], token_of(denv, pa)) == before
    assert len(events(denv, A.DELETE_CONFIRMED)) == 1


def test_delete_token_derived_not_stored(denv) -> None:
    mk(denv, "Quarterly Widgets")
    pa, _ = stage(denv, TEXT)
    token = token_of(denv, pa)
    assert "token" not in pa and "proof" not in pa and token not in json.dumps(pa)
    assert pa["token_sha256"] == token_sha256(token) != token
    other = derive_token(new_key(), flow._fields(pa))
    assert other != token  # bound to this process's key
    key = denv.svc._key
    assert repr(key) == str(key) == "[secret]" and key._k.hex() not in repr(denv.svc.__dict__)
    with pytest.raises(TypeError):
        pickle.dumps(key)
    with pytest.raises(ValueError):
        DeleteKey(b"short")


def test_delete_token_never_in_traces(denv, tmp_path, caplog) -> None:
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, TEXT)
    pa = pending(denv)
    denv.svc.reply_payload("yes", pa)
    token, proof = token_of(denv, pa), proof_of(denv, pa)
    key_hex = denv.svc._key._k.hex()
    assert ask(denv, "yes").outcome == "delete_executed" and alive(denv, ids) == []
    secrets = (token, proof, key_hex)
    raw = repr(denv.env.spans)  # before any scrubbing
    assert "delete" in raw and not any(s in raw for s in secrets)
    t = tr.Tracer(tmp_path, uuid4().hex)
    for span_type, name, fields in denv.env.spans:
        t.record(span_type, name, **fields)
    written = t.path.read_text()
    audit_rows = repr(denv.conn.execute("SELECT * FROM audit_event").fetchall())
    for blob in (written, audit_rows, caplog.text):
        assert not any(s in blob for s in secrets)
    # defence in depth: even a leaked value is scrubbed (tests/unit/test_tracer.py:108)
    t.record("delete", event="x", pending_action_id=token, audit_event_id=proof)
    assert not any(s in t.path.read_text() for s in secrets)


def test_checkpoint_holds_token_hash_only(denv) -> None:
    mk(denv, "Quarterly Widgets")
    ask(denv, TEXT)
    pa = pending(denv)
    token, key_hex = token_of(denv, pa), denv.svc._key._k.hex()
    dump = checkpoint_dump(denv)
    assert pa["token_sha256"] in dump
    assert token not in dump and key_hex not in dump
    assert ask(denv, "yes").outcome == "delete_executed"
    proof = proof_of(denv, pa)  # m1: the proof stays in memory; the resume carries its hash
    dump = checkpoint_dump(denv)
    assert token not in dump and key_hex not in dump
    assert proof not in dump and proof.encode() not in dump.encode()
    assert token_sha256(proof) in dump


def _replayed(d, pa, kw):
    pay = d.svc.reply_payload("yes", pa)
    assert d.svc.confirm(pa, pay, turn=2, turn_id=uuid4().hex, **kw).step == "confirmed"
    assert d.svc.execute(pa, turn_id=uuid4().hex, **kw).step == "executed"
    return pay


def _pay(d, pa, **over):
    return {**d.svc.reply_payload("yes", pa), **over}


def _no_held_proof(d, pa, kw):
    pay = d.svc.reply_payload("yes", pa)
    d.svc._held[pa["pending_action_id"]]["proof"] = None  # e.g. evicted, or another process
    return pay


CASES = {
    "garbage": lambda d, pa, kw: _pay(d, pa, proof_sha256="0" * 64),
    "empty": lambda d, pa, kw: _pay(d, pa, proof_sha256=""),
    "not_str": lambda d, pa, kw: _pay(d, pa, proof_sha256=None),
    "raw_proof": lambda d, pa, kw: _pay(d, pa, proof_sha256=proof_of(d, pa)),
    "other_reply": lambda d, pa, kw: _pay(d, pa, reply="y"),  # proof was made for "yes"
    "other_token": lambda d, pa, kw: _pay(
        d, pa, proof_sha256=token_sha256(make_proof("f" * 64, "yes", flow._fields(pa)))
    ),
    "no_held_proof": _no_held_proof,
    "replayed": _replayed,
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_proof_mismatch_cancels(denv, case) -> None:
    ids = mk(denv, "Quarterly Widgets")
    pa, kw = stage(denv, TEXT)
    pay = CASES[case](denv, pa, kw)
    executed = len(events(denv, A.DELETE_EXECUTED))
    st = denv.svc.confirm(pa, pay, turn=2, turn_id=uuid4().hex, **kw)
    reason = "replayed" if case == "replayed" else "proof_mismatch"
    assert st.step == "cancelled" and st.error == reason and st.text == flow.CANCELLED_TEXT
    (ev,) = events(denv, A.DELETE_CANCELLED)  # audited
    assert ev.details["error_type"] == reason and ev.pending_action_id == pa["pending_action_id"]
    assert len(events(denv, A.DELETE_EXECUTED)) == executed  # no second delete
    if case != "replayed":
        assert alive(denv, ids) == ids and not events(denv, A.DELETE_CONFIRMED)


@pytest.mark.parametrize("offset", [0, 2, -1])
def test_confirm_only_on_next_turn(denv, offset) -> None:
    ids = mk(denv, "Quarterly Widgets")
    pa, kw = stage(denv, TEXT, turn=5)
    st = denv.svc.confirm(pa, denv.svc.reply_payload("yes", pa), turn=5 + offset,
                          turn_id=uuid4().hex, **kw)  # fmt: skip
    assert st.step == "cancelled" and st.error == "wrong_turn" and alive(denv, ids) == ids
    pa2, _ = stage(denv, "delete reports about quarterly", turn=7)
    ok = denv.svc.confirm(pa2, denv.svc.reply_payload("yes", pa2), turn=8,
                          turn_id=uuid4().hex, **kw)  # fmt: skip
    assert ok.step == "confirmed"


def test_secrets_registered_once_per_action_and_bounded(denv, monkeypatch) -> None:
    """m4: a token or proof is registered with the scrubber once per pending action (not on
    every derive), and only the newest HELD_MAX actions keep theirs."""
    mk(denv, "Quarterly Widgets")
    calls: list[str] = []
    monkeypatch.setattr(flow, "register_secret", calls.append)
    pa, kw = stage(denv, TEXT)
    for _ in range(3):
        assert denv.svc.is_live(pa)
        denv.svc.reply_payload("yes", pa)
    assert denv.svc.confirm(pa, denv.svc.reply_payload("yes", pa), turn=2,
                            turn_id=uuid4().hex, **kw).step == "confirmed"  # fmt: skip
    assert sorted(calls) == sorted([token_of(denv, pa), proof_of(denv, pa)])
    monkeypatch.setattr(flow, "HELD_MAX", 3)
    forgotten: list[str] = []
    monkeypatch.setattr(flow, "forget_secret", forgotten.append)
    for turn in range(4):
        stage(denv, "delete reports about quarterly", turn=10 + turn)
    assert len(denv.svc._held) == 3 and pa["pending_action_id"] not in denv.svc._held
    assert set(calls[:2]) <= set(forgotten)  # the evicted action's token and proof
