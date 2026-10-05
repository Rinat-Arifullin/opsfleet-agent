"""Iteration 22a: the two-phase delete of saved reports (preview, confirm, execute).

Offline only: the wired AgentGraph from ``test_graph`` (scripted models, fake BigQuery), a real
SQLite store under ``tmp_path`` and a fixed clock. All reports are synthetic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from opsfleet_agent import commands
from opsfleet_agent.commands.delete import DELETE_COMMAND, OFF_TEXT
from opsfleet_agent.delete import flow
from opsfleet_agent.graph.resume import close_interrupted_turn, resume_turn
from opsfleet_agent.roles.analyst import ModelTurn, ToolCall
from opsfleet_agent.session import Profile, Session
from opsfleet_agent.store import audit as A
from opsfleet_agent.store.db import open_store
from opsfleet_agent.store.reports import ReportStore
from opsfleet_agent.tools import registry
from tests.unit.test_graph import (  # noqa: F401 - pytest fixtures used by name
    PROFILE,
    Router,
    Scripted,
    detector,
    make_env,
    settings,
)
from tests.unit.test_reports import _save_args

OTHER = Profile("analyst_b", "Analyst B", brands=("Acme",))
NOW = 1_900_000_000.0


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def denv(make_env, tmp_path):  # noqa: F811 - the imported fixture
    """A graph with the delete feature on, over its own store and audit log."""
    env = make_env(Router("simple"), Scripted(ModelTurn("There were 3 complete orders.")))
    env.session = Session(uuid4().hex, PROFILE)
    conn = open_store(tmp_path / "delete.db")
    audit, store, clock = A.AuditLog(conn), ReportStore(conn), Clock()
    svc = flow.setup_delete(conn, audit, store, clock=clock)
    assert svc is not None
    env.graph.services.reports = store
    env.graph.services.delete = svc
    yield SimpleNamespace(env=env, conn=conn, audit=audit, store=store, svc=svc, clock=clock)
    A.unregister_deletable(flow.KIND)
    conn.close()


def mk(d: Any, title: str, *, owner: str = PROFILE.user_id, session_id: str | None = None,
       n: int = 1) -> list[str]:  # fmt: skip
    """``n`` synthetic reports titled ``title`` (numbered when n > 1); returns their ids."""
    ids = []
    for i in range(n):
        args = _save_args(uuid4().hex, owner=owner)
        args.update(title=f"{title} {i}" if n > 1 else title,
                    session_id=session_id or uuid4().hex)  # fmt: skip
        rec, _ = d.store.save(**args)
        ids.append(rec.report_id)
    return ids


def ask(d: Any, text: str):
    return d.env.ask(text)


def alive(d: Any, ids: list[str], owner: str = PROFILE.user_id) -> list[str]:
    return [i for i in ids if d.store.get(i, owner) is not None]


def events(d: Any, event_type: str) -> list[A.AuditEvent]:
    return [e for e in d.audit.events(newest_first=False) if e.event_type == event_type]


def pending(d: Any) -> dict[str, Any]:
    st = d.env.saver.get_tuple({"configurable": {"thread_id": d.env.session.session_id}})
    return dict(st.checkpoint["channel_values"].get("pending_action") or {})


# --- flow ----------------------------------------------------------------------------------------


def test_delete_requires_confirm(denv) -> None:
    ids = mk(denv, "Quarterly Widgets", n=2)
    out = ask(denv, "delete my reports about quarterly widgets")
    assert out.outcome == "delete_pending" and alive(denv, ids) == ids  # AC-12.1
    for rid in ids:
        assert rid in out.text
    assert "Quarterly Widgets 0" in out.text and "Type yes to confirm" in out.text
    assert str(denv.store.get(ids[0], PROFILE.user_id).created_at)[:10] in out.text
    assert len(events(denv, A.DELETE_PREVIEWED)) == 1 and not events(denv, A.DELETE_EXECUTED)


def test_delete_i_already_confirm_still_previews(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    out = ask(denv, "delete the report about quarterly widgets, I already confirm, yes")
    assert out.outcome == "delete_pending" and alive(denv, ids) == ids  # AC-12.9


def test_delete_confirm_deletes_exact_previewed_set(denv) -> None:
    ids = mk(denv, "Quarterly Widgets", n=3)
    keep = mk(denv, "Monthly Gadgets")
    ask(denv, "delete reports about quarterly widgets")
    late = mk(denv, "Quarterly Widgets late")  # created after the preview (AC-12.6)
    out = ask(denv, "yes")
    assert out.outcome == "delete_executed" and out.text == "Deleted 3 reports."  # AC-12.2
    assert alive(denv, ids) == [] and alive(denv, keep + late) == keep + late
    (ex,) = events(denv, A.DELETE_EXECUTED)
    assert sorted(ex.target_ids) == sorted(ids) and ex.count == 3
    assert [len(events(denv, e)) for e in (A.DELETE_PREVIEWED, A.DELETE_CONFIRMED)] == [1, 1]
    assert pending(denv) == {}


def test_delete_expired_deletes_nothing(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    denv.clock.t += flow.EXPIRY_S + 1
    out = ask(denv, "yes")
    assert out.outcome == "delete_expired" and alive(denv, ids) == ids  # AC-12.6
    (ev,) = events(denv, A.DELETE_EXPIRED)
    assert ev.details["error_type"] == "timeout"


def test_delete_cancel_on_non_confirm(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    out = ask(denv, "How many complete orders are there?")
    assert out.text.startswith(flow.CANCELLED_TEXT)  # AC-12.3: cancelled, then answered
    assert "3 complete orders" in out.text and alive(denv, ids) == ids
    (ev,) = events(denv, A.DELETE_CANCELLED)
    assert ev.details["error_type"] == "declined" and not events(denv, A.DELETE_CONFIRMED)


def test_delete_owner_only(denv) -> None:
    mine = mk(denv, "Quarterly Widgets")
    theirs = mk(denv, "Quarterly Widgets", owner=OTHER.user_id)
    out = ask(denv, "delete reports about quarterly widgets")
    assert theirs[0] not in out.text and "1 saved report" in out.text
    ask(denv, "yes")
    assert alive(denv, mine) == [] and alive(denv, theirs, OTHER.user_id) == theirs  # AC-12.4
    by_id = ask(denv, f"delete report {theirs[0]}")
    assert by_id.text == flow.NONE_TEXT and alive(denv, theirs, OTHER.user_id) == theirs


def test_delete_by_session_id(denv) -> None:
    here = mk(denv, "Alpha", session_id=denv.env.session.session_id, n=2)
    elsewhere = mk(denv, "Alpha")
    out = ask(denv, "delete the reports from this session")
    assert "2 saved reports" in out.text and elsewhere[0] not in out.text  # AC-12.5
    ask(denv, "yes")
    assert alive(denv, here) == [] and alive(denv, elsewhere) == elsewhere
    by_ids = mk(denv, "Beta", n=2)
    ask(denv, f"delete reports {by_ids[0]} and {by_ids[1]}")
    assert ask(denv, "y").outcome == "delete_executed" and alive(denv, by_ids) == []


def stage(d: Any, text: str, turn: int = 1) -> tuple[dict[str, Any], dict[str, Any]]:
    """Service-level preview; returns (pending, owner/session kwargs)."""
    sid = d.env.session.session_id
    st = d.svc.preview(flow.parse_delete_request(text), owner=PROFILE.user_id,
                       scope=d.env.graph._sql_session(d.env.session).scope, session_id=sid,
                       turn_id=uuid4().hex, preview_turn=turn)  # fmt: skip
    assert st.step == "pending", st
    return st.pending, {"owner": PROFILE.user_id, "session_id": sid}


def test_delete_confirmation_bound_to_preview_set(denv) -> None:
    ids = mk(denv, "Quarterly Widgets", n=2)
    extra = mk(denv, "Unrelated")
    pa, kw = stage(denv, "delete reports about quarterly widgets")
    # a tampered checkpoint (an extra id) no longer matches ids_sha256
    bad = {**pa, "report_ids": ids + extra}
    st = denv.svc.confirm(bad, denv.svc.reply_payload("yes", pa), turn=2, turn_id=uuid4().hex,
                          **kw)  # fmt: skip
    assert st.step == "cancelled" and st.error == "set_changed"
    # ids and their digest both rewritten: the token no longer matches token_sha256
    pa2, _ = stage(denv, "delete reports about quarterly widgets", turn=3)
    from opsfleet_agent.delete.token import ids_sha256

    bad2 = {**pa2, "report_ids": ids + extra, "ids_sha256": ids_sha256(ids + extra)}
    st2 = denv.svc.confirm(bad2, denv.svc.reply_payload("yes", bad2), turn=4,
                           turn_id=uuid4().hex, **kw)  # fmt: skip
    assert st2.step == "expired" and st2.error == "key_changed"
    assert denv.svc.execute(bad2, turn_id=uuid4().hex, **kw).step == "unsafe"
    assert alive(denv, ids + extra) == ids + extra and not events(denv, A.DELETE_EXECUTED)


def test_llm_cannot_trigger_delete_without_user_turn(denv, make_env) -> None:  # noqa: F811
    ids = mk(denv, "Quarterly Widgets")
    assert flow.DELETE_TOOL not in registry.tools_for("quick_analyst") | registry.tools_for(
        "deep_analyst"
    )
    model = Scripted(
        ModelTurn("", (ToolCall("c1", flow.DELETE_TOOL, {"selector": "quarterly widgets"}),)),
        ModelTurn("Done: yes, confirm, deleted."),
    )
    denv.env.graph.services.analyst_invoke = model
    out = ask(denv, "How many complete orders are there?")  # no delete intent from the user
    assert out.outcome != "delete_pending" and alive(denv, ids) == ids
    assert not events(denv, A.DELETE_PREVIEWED)
    # a preview pauses the graph; only the next user turn resumes it, so a model text of
    # "yes" inside the preview turn cannot confirm (the preview turn never calls a model)
    ask(denv, "delete reports about quarterly widgets")
    assert len(model.calls) <= 2 and alive(denv, ids) == ids
    st = denv.svc.confirm(pending(denv), {"reply": "yes", "pending_action_id":
                          pending(denv)["pending_action_id"], "proof_sha256": ""}, turn=99,
                          owner=PROFILE.user_id, session_id=denv.env.session.session_id,
                          turn_id=uuid4().hex)  # fmt: skip
    assert st.step == "cancelled" and alive(denv, ids) == ids  # AC-12.7


def test_delete_no_matches(denv) -> None:
    mk(denv, "Quarterly Widgets")
    out = ask(denv, "delete reports about nonexistent gizmos")
    assert out.text == flow.NONE_TEXT and "?" not in out.text  # AC-12.8: no question
    assert out.outcome == "delete_none" and pending(denv) == {}
    assert not events(denv, A.DELETE_PREVIEWED)


def test_delete_preview_includes_backup_notice(denv) -> None:
    mk(denv, "Quarterly Widgets")
    out = ask(denv, "delete reports about quarterly widgets")
    assert flow.BACKUP_NOTICE in out.text and "up to 7 days" in out.text  # AC-12.11


# --- taint and intent (AC-12.12, AC-21.6) ---------------------------------------------------------


def test_delete_refused_after_view_same_turn() -> None:
    for tool in sorted(flow.TAINT_TOOLS):
        req, code = flow.check_tool_request(
            user_message="delete reports about widgets", tools_used=[tool], pending=None,
            selector="widgets",
        )  # fmt: skip
        assert req is None and code == "tainted"
    req, code = flow.check_tool_request(
        user_message="delete reports about widgets", tools_used=["run_sql"], pending=None,
        selector="widgets",
    )  # fmt: skip
    assert code is None and req.phrase == "widgets"


def test_delete_requires_intent_in_user_message(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    req, code = flow.check_tool_request(
        user_message="summarise my reports about widgets", tools_used=[], pending=None,
        selector="widgets",
    )  # fmt: skip
    assert req is None and code == "no_intent"
    assert flow.parse_delete_request("show reports about quarterly widgets") is None
    out = ask(denv, "show reports about quarterly widgets")
    assert out.outcome != "delete_pending" and alive(denv, ids) == ids
    assert not events(denv, A.DELETE_PREVIEWED)
    _, code = flow.check_tool_request(user_message="delete reports about widgets",
                                      tools_used=[], pending={"step": "preview"},
                                      selector="widgets")  # fmt: skip
    assert code == "delete_pending"


def test_session_delete_excludes_viewed_reports(denv) -> None:
    created_here = mk(denv, "Alpha", session_id=denv.env.session.session_id)
    viewed = mk(denv, "Viewed Earlier")  # created in another session
    ctx = commands.CommandContext(user_id=PROFILE.user_id, session_id=denv.env.session.session_id,
                                  report_store=denv.store)  # fmt: skip
    commands.dispatch(f"/open {viewed[0]}", ctx)  # viewed in THIS session
    out = ask(denv, "delete reports from this session")
    assert viewed[0] not in out.text and created_here[0] in out.text  # AC-21.6
    ask(denv, "yes")
    assert alive(denv, viewed) == viewed and alive(denv, created_here) == []


# --- structure -----------------------------------------------------------------------------------


def test_confirm_prompt_rendered_by_code(denv) -> None:
    mk(denv, "Quarterly Widgets", n=2)
    out = ask(denv, "delete reports about quarterly widgets")
    rows = [denv.store.get(i, PROFILE.user_id) for i in pending(denv)["report_ids"]]
    scope = denv.env.graph._sql_session(denv.env.session).scope
    assert out.text == flow.render_prompt(rows, scope)
    assert denv.env.router.calls == [] and denv.env.analyst.calls == []  # no model text


def test_delete_not_alone_in_step() -> None:
    calls = [SimpleNamespace(id="a", name=flow.DELETE_TOOL), SimpleNamespace(id="b", name="x")]
    errs = flow.gate_step(calls)
    assert set(errs) == {"a", "b"}
    assert all(e["error"]["code"] == "delete_not_alone" for e in errs.values())
    assert flow.gate_step(calls[:1]) is None and flow.gate_step(calls[1:]) is None


def test_execute_delete_requires_confirmed_record(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    pa = pending(denv)
    st = denv.svc.execute(pa, owner=PROFILE.user_id, session_id=denv.env.session.session_id,
                          turn_id=uuid4().hex)  # fmt: skip
    assert st.step == "unsafe" and st.error == "not_confirmed" and alive(denv, ids) == ids
    assert not events(denv, A.DELETE_EXECUTED)


def test_previewed_audit_idempotent_on_replay(denv) -> None:
    mk(denv, "Quarterly Widgets")
    req = flow.parse_delete_request("delete reports about quarterly widgets")
    kw = dict(owner=PROFILE.user_id, scope=denv.env.graph._sql_session(denv.env.session).scope,
              session_id=denv.env.session.session_id, turn_id=uuid4().hex, preview_turn=1)
    a, b = denv.svc.preview(req, **kw), denv.svc.preview(req, **kw)
    assert a.pending["pending_action_id"] == b.pending["pending_action_id"]
    assert a.audit_event_id == b.audit_event_id and len(events(denv, A.DELETE_PREVIEWED)) == 1


def test_second_delete_while_pending_rejected(denv) -> None:
    first = mk(denv, "Quarterly Widgets")
    second = mk(denv, "Monthly Gadgets")
    ask(denv, "delete reports about quarterly widgets")
    out = ask(denv, "delete reports about monthly gadgets")
    assert out.text == flow.DELETE_PENDING_TEXT and pending(denv) == {}
    assert alive(denv, first + second) == first + second
    assert len(events(denv, A.DELETE_PREVIEWED)) == 1 and len(events(denv, A.DELETE_CANCELLED)) == 1
    assert ask(denv, "yes").outcome != "delete_executed"  # nothing left to confirm
    assert alive(denv, first + second) == first + second


def test_cancelled_delete_reply_gets_fresh_context(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    out = ask(denv, "How many complete orders are there?")
    assert "3 complete orders" in out.text
    prompts = " ".join(str(m) for _, msgs, _ in denv.env.analyst.calls for m in msgs)
    assert ids[0] not in prompts and "This will delete" not in prompts


def test_slash_delete_command_previews_and_feature_off(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    ctx = commands.CommandContext(
        user_id=PROFILE.user_id, session_id=denv.env.session.session_id,
        delete_start=lambda a: denv.env.graph.start_delete(a, session=denv.env.session).text,
    )  # fmt: skip
    commands.register_command(DELETE_COMMAND)
    try:
        res = commands.dispatch("/delete quarterly widgets", ctx)
        assert ids[0] in res.text and alive(denv, ids) == ids
        assert ask(denv, "yes").outcome == "delete_executed" and alive(denv, ids) == []
        off = commands.dispatch("/delete x", commands.CommandContext("u", "s"))
        assert off.text == OFF_TEXT
    finally:
        commands.unregister_command(DELETE_COMMAND.name)
    assert "/delete" not in commands.COMMANDS


def test_wire_delete_fails_closed(denv) -> None:
    from opsfleet_agent.cli import wire_delete

    try:
        assert wire_delete(None, denv.audit, denv.store) is None
        assert "/delete" not in commands.COMMANDS  # feature-off rollback path
        assert wire_delete(denv.conn, denv.audit, denv.store) is not None
        assert "/delete" in commands.COMMANDS
    finally:
        commands.unregister_command("/delete")


def test_delete_without_service_is_unavailable(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    denv.env.graph.services.delete = None
    out = ask(denv, "delete reports about quarterly widgets")
    assert out.text == flow.UNAVAILABLE_TEXT and alive(denv, ids) == ids


# --- size (AC-21.7) --------------------------------------------------------------------------


def test_large_delete_requires_typed_count(denv) -> None:
    ids = mk(denv, "Bulk Widgets", n=21)
    out = ask(denv, "delete reports about bulk widgets")
    assert "Type 21 to confirm" in out.text
    assert ask(denv, "yes").text.startswith(flow.CANCELLED_TEXT) and len(alive(denv, ids)) == 21
    ask(denv, "delete reports about bulk widgets")
    assert ask(denv, "21").text == "Deleted 21 reports." and alive(denv, ids) == []


def test_large_delete_preview_truncated_but_bound(denv) -> None:
    ids = mk(denv, "Bulk Widgets", n=45)
    out = ask(denv, "delete reports about bulk widgets")
    assert out.text.startswith("This will delete 45 saved reports:")
    assert sum(1 for i in ids if i in out.text) == flow.PREVIEW_SIZE
    assert "... and 25 more" in out.text and len(pending(denv)["report_ids"]) == 45
    assert ask(denv, "45").text == "Deleted 45 reports." and alive(denv, ids) == []


# --- other ---------------------------------------------------------------------------------------


def test_flow_audit_failure_aborts_delete(denv, monkeypatch) -> None:
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    real = denv.audit._insert

    def failing(event):
        if event.event_type == A.DELETE_EXECUTED:
            raise RuntimeError("synthetic audit failure")
        return real(event)

    monkeypatch.setattr(denv.audit, "_insert", failing)
    out = ask(denv, "yes")
    assert out.text == flow.UNSAFE_TEXT and "could not be completed safely" in out.text
    assert alive(denv, ids) == ids and not events(denv, A.DELETE_EXECUTED)  # AC-28.2
    errs = [s for s in denv.env.spans if s[0] == "error" and s[1] == "delete"]
    assert errs and errs[-1][2]["code"]


def test_matcher_rejects_empty_and_wildcards(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    for text in ("delete all reports", "delete reports %", "delete reports about *",
                 "delete my reports about ab"):  # fmt: skip
        req = flow.parse_delete_request(text)
        assert req is not None and req.error == flow.SELECTOR_EMPTY, text
        assert ask(denv, text).text == flow.SELECTOR_EMPTY_TEXT  # AC-12.14
    assert alive(denv, ids) == ids and not events(denv, A.DELETE_PREVIEWED)


# --- review round: M1 restart, M2 grammar, m2/m3 binding ----------------------------------------


def _crash_in_execute(d: Any, monkeypatch, *, after_commit: bool = False) -> None:
    """"yes" confirms, then the process dies (Ctrl-C) before or after the delete commits."""
    if after_commit:
        real_delete = flow.A.audited_delete

        def boom(*a, **k):
            real_delete(*a, **k)
            raise KeyboardInterrupt

        monkeypatch.setattr(flow.A, "audited_delete", boom)
    else:

        def boom(self, *a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(flow.DeleteService, "execute", boom)
    with pytest.raises(KeyboardInterrupt):
        ask(d, "yes")
    monkeypatch.undo()


def _restart(d: Any, hours: float = 10) -> None:
    """A new process: a fresh K_delete and no in-memory confirm, hours later."""
    d.clock.t += hours * 3600
    d.svc = d.env.graph.services.delete = flow.DeleteService(d.audit, d.store, clock=d.clock)


def test_restart_between_confirm_and_execute_deletes_nothing(denv, monkeypatch) -> None:
    """M1 repro: confirmed, crashed before execute_delete, resumed after a restart."""
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    _crash_in_execute(denv, monkeypatch)
    assert len(events(denv, A.DELETE_CONFIRMED)) == 1 and alive(denv, ids) == ids
    _restart(denv)
    out = denv.env.graph.open_resume(denv.env.session).finish()
    assert out.outcome == "delete_expired" and out.text == flow.EXPIRED_TEXT
    assert alive(denv, ids) == ids and not events(denv, A.DELETE_EXECUTED)
    (ev,) = events(denv, A.DELETE_EXPIRED)
    assert ev.details["error_type"] == "key_changed"
    assert pending(denv) == {}  # closed: a second resume has nothing to run


def test_restart_after_delete_committed_reports_recorded_count(denv, monkeypatch) -> None:
    ids = mk(denv, "Quarterly Widgets", n=2)
    ask(denv, "delete reports about quarterly widgets")
    _crash_in_execute(denv, monkeypatch, after_commit=True)
    assert alive(denv, ids) == []
    _restart(denv)
    out = denv.env.graph.open_resume(denv.env.session).finish()
    assert out.outcome == "delete_executed" and out.text == "Deleted 2 reports."
    assert len(events(denv, A.DELETE_EXECUTED)) == 1 and not events(denv, A.DELETE_EXPIRED)


def test_stranded_execute_not_run_by_next_turn(denv, monkeypatch) -> None:
    """MJ-1: a stranded execute is never run as a side effect of an unrelated next turn,
    even inside EXECUTE_GRACE_S in the same process: it expires (``stranded``), the user is
    told nothing was deleted, then the turn is answered."""
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    _crash_in_execute(denv, monkeypatch)
    denv.clock.t += flow.EXECUTE_GRACE_S - 1
    out = ask(denv, "How many complete orders are there?")
    assert out.text == f"{flow.STRANDED_TEXT}\n\nThere were 3 complete orders."
    assert alive(denv, ids) == ids and not events(denv, A.DELETE_EXECUTED)
    (ev,) = events(denv, A.DELETE_EXPIRED)
    assert ev.details["error_type"] == flow.STRANDED and pending(denv) == {}
    assert denv.env.graph.open_resume(denv.env.session).next == ()  # nothing left to run


def test_stranded_execute_explicit_resume_in_grace_executes(denv, monkeypatch) -> None:
    """OD-10: only an explicit resume (``--resume`` / finish) may still run it, in grace."""
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    _crash_in_execute(denv, monkeypatch)
    denv.clock.t += flow.EXECUTE_GRACE_S - 1
    out = denv.env.graph.open_resume(denv.env.session).finish()
    assert out.outcome == "delete_executed" and out.text == "Deleted 1 report."
    assert alive(denv, ids) == [] and len(events(denv, A.DELETE_EXECUTED)) == 1


def test_stranded_execute_after_restart_closed_then_turn_answered(denv, monkeypatch) -> None:
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    _crash_in_execute(denv, monkeypatch)
    _restart(denv)
    out = ask(denv, "How many complete orders are there?")
    assert out.text.startswith(flow.STRANDED_TEXT) and "3 complete orders" in out.text
    assert alive(denv, ids) == ids and not events(denv, A.DELETE_EXECUTED)
    (ev,) = events(denv, A.DELETE_EXPIRED)
    assert ev.details["error_type"] == flow.STRANDED


def _ctrl_c(d: Any, monkeypatch, method: str, tid: str) -> None:
    """The CLI path: Ctrl-C in the reply turn ``tid``, then ``_cancelled``'s close."""

    def boom(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(flow.DeleteService, method, boom)
    with pytest.raises(KeyboardInterrupt):
        d.env.graph.run_turn("yes", session=d.env.session, turn_id=tid)
    monkeypatch.undo()


@pytest.mark.parametrize("method", ["execute", "confirm"])
def test_ctrl_c_after_yes_cancels_and_deletes_nothing(denv, monkeypatch, method) -> None:
    """MJ-1 (reviewer probe): a Ctrl-C after "yes", at the execute or the confirm stage, is a
    cancel: ``delete.cancelled`` (``interrupted``), and the next turn deletes nothing."""
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    tid = uuid4().hex
    _ctrl_c(denv, monkeypatch, method, tid)
    assert close_interrupted_turn(denv.env.graph, denv.env.session, tid) is True
    (ev,) = events(denv, A.DELETE_CANCELLED)
    assert ev.details["error_type"] == flow.INTERRUPTED and ev.turn_id == tid
    assert pending(denv) == {} and denv.env.graph.open_resume(denv.env.session).next == ()
    out = ask(denv, "no wait, stop. how many complete orders?")
    assert out.text == "There were 3 complete orders."
    assert alive(denv, ids) == ids and not events(denv, A.DELETE_EXECUTED)
    assert not events(denv, A.DELETE_EXPIRED)
    # the proof is gone: even an explicit finish cannot run it now
    assert denv.env.graph.open_resume(denv.env.session).finish().outcome == "error"


def test_ctrl_c_after_commit_reports_recorded_count(denv, monkeypatch) -> None:
    """Residual (documented): a Ctrl-C after the commit but before the checkpoint closes the
    turn with the recorded count; no cancel row contradicts the executed one."""
    ids = mk(denv, "Quarterly Widgets", n=2)
    ask(denv, "delete reports about quarterly widgets")
    _crash_in_execute(denv, monkeypatch, after_commit=True)
    assert alive(denv, ids) == []
    assert close_interrupted_turn(denv.env.graph, denv.env.session, None) is True
    assert len(events(denv, A.DELETE_EXECUTED)) == 1 and not events(denv, A.DELETE_CANCELLED)
    assert pending(denv) == {}


def test_ctrl_c_close_ignores_another_turn_id(denv, monkeypatch) -> None:
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    _ctrl_c(denv, monkeypatch, "execute", uuid4().hex)
    assert close_interrupted_turn(denv.env.graph, denv.env.session, uuid4().hex) is False
    assert not events(denv, A.DELETE_CANCELLED) and alive(denv, ids) == ids


def terminal(d: Any) -> list[str]:
    return [e.event_type for e in d.audit.events(newest_first=False)
            if e.event_type in (A.DELETE_EXECUTED, A.DELETE_CANCELLED, A.DELETE_EXPIRED)]


@pytest.mark.parametrize("lapse", ["key_changed", "timeout"])
def test_resume_confirm_stage_lapsed_expires_then_fresh_preview(denv, lapse) -> None:
    """rr22a3-1: preview, quit, then --resume after a restart (new K_delete) or after the
    preview expired: EXPIRED is written, the turn is closed, and the next delete request
    gets a fresh preview (not DELETE_PENDING_TEXT and a CANCELLED declined)."""
    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    first = pending(denv)["pending_action_id"]
    if lapse == "key_changed":
        _restart(denv)
    else:
        denv.clock.t += flow.EXPIRY_S + 1
    out = resume_turn(denv.env.graph, denv.env.session.session_id, PROFILE)
    assert flow.EXPIRED_TEXT in out.text and terminal(denv) == [A.DELETE_EXPIRED]
    assert events(denv, A.DELETE_EXPIRED)[0].details["error_type"] == lapse
    assert pending(denv) == {} and not denv.env.graph.open_resume(denv.env.session).next
    out = ask(denv, "delete reports about quarterly widgets")
    assert out.outcome == "delete_pending" and out.text != flow.DELETE_PENDING_TEXT
    assert pending(denv)["pending_action_id"] != first
    assert len(events(denv, A.DELETE_PREVIEWED)) == 2 and terminal(denv) == [A.DELETE_EXPIRED]
    assert ask(denv, "yes").text == "Deleted 1 report." and alive(denv, ids) == []


@pytest.mark.parametrize("then", ["next_turn", "finish"])
def test_close_state_write_fails_keeps_one_terminal_row(denv, monkeypatch, then) -> None:
    """rr22a3-2: a Ctrl-C close wrote CANCELLED but the checkpoint write failed. Neither the
    next turn (_close_stranded) nor an explicit resume (finish -> execute) writes a second
    terminal row; nothing is deleted."""
    import langgraph.pregel as P

    ids = mk(denv, "Quarterly Widgets")
    ask(denv, "delete reports about quarterly widgets")
    tid = uuid4().hex
    _ctrl_c(denv, monkeypatch, "execute", tid)

    def fail(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("checkpoint write failed")

    with monkeypatch.context() as m:
        m.setattr(P.Pregel, "update_state", fail)
        assert close_interrupted_turn(denv.env.graph, denv.env.session, tid) is False
    assert terminal(denv) == [A.DELETE_CANCELLED]
    if then == "next_turn":
        out = ask(denv, "How many complete orders are there?")
        assert "There were 3 complete orders." in out.text
    else:
        assert denv.env.graph.open_resume(denv.env.session).finish().outcome == "delete_cancelled"
    assert terminal(denv) == [A.DELETE_CANCELLED] and alive(denv, ids) == ids
    assert pending(denv) == {}


def _confirmed(d: Any, text: str = "delete reports about quarterly widgets"):
    pa, kw = stage(d, text)
    st = d.svc.confirm(pa, d.svc.reply_payload("yes", pa), turn=2, turn_id=uuid4().hex, **kw)
    assert st.step == "confirmed"
    return pa, kw


def test_execute_after_grace_window_expires(denv) -> None:
    ids = mk(denv, "Quarterly Widgets")
    pa, kw = _confirmed(denv)
    denv.clock.t += flow.EXECUTE_GRACE_S + 1
    st = denv.svc.execute(pa, turn_id=uuid4().hex, **kw)
    assert st.step == "expired" and alive(denv, ids) == ids
    (ev,) = events(denv, A.DELETE_EXPIRED)
    assert ev.details["error_type"] == "timeout"


@pytest.mark.parametrize("tamper", ["subset", "superset"])
def test_execute_rejects_report_ids_changed_after_confirm(denv, tamper) -> None:
    """m3(a): the checkpointed ids change between confirm and execute."""
    ids = mk(denv, "Quarterly Widgets", n=2)
    extra = mk(denv, "Unrelated")
    pa, kw = _confirmed(denv)
    bad = {**pa, "report_ids": ids[:1] if tamper == "subset" else ids + extra}
    st = denv.svc.execute(bad, turn_id=uuid4().hex, **kw)
    assert st.step == "unsafe" and st.error == "binding_mismatch"
    assert alive(denv, ids + extra) == ids + extra and not events(denv, A.DELETE_EXECUTED)


@pytest.mark.parametrize("change", ["reowned", "deleted"])
def test_execute_set_changed_deletes_nothing_and_reasks(denv, change) -> None:
    """OD-14 (owner 2026-10-05: re-ask). A previewed row re-owned or deleted between confirm
    and execute: nothing is deleted (not even the rest), EXPIRED set_changed is written, the
    reply names the vanished id and asks for a fresh preview (AC-12.6)."""
    ids = mk(denv, "Quarterly Widgets", n=2)
    pa, kw = _confirmed(denv)
    if change == "reowned":
        denv.conn.execute("UPDATE saved_report SET owner_user_id = ? WHERE report_id = ?",
                          (OTHER.user_id, ids[0]))  # fmt: skip
    else:
        denv.conn.execute("DELETE FROM saved_report WHERE report_id = ?", (ids[0],))
    denv.conn.commit()
    st = denv.svc.execute(pa, turn_id=uuid4().hex, **kw)
    assert st.step == "expired" and st.error == flow.SET_CHANGED and st.count == 0
    assert st.text == (f"1 previewed report no longer exists ({ids[0]}), so nothing was deleted."
                       " 1 other report still matches; ask again to see a fresh preview and"
                       " confirm.")  # fmt: skip
    assert alive(denv, ids[1:]) == ids[1:] and not events(denv, A.DELETE_EXECUTED)
    (ev,) = events(denv, A.DELETE_EXPIRED)
    assert ev.details["error_type"] == flow.SET_CHANGED and sorted(ev.target_ids) == sorted(ids)
    # single use: the same confirmation can never run again, a new preview is needed
    again = denv.svc.execute(pa, turn_id=uuid4().hex, **kw)
    assert again.step != "executed" and alive(denv, ids[1:]) == ids[1:]


def test_set_changed_through_graph_then_fresh_preview(denv) -> None:
    """Graph level: a previewed row was re-owned before "yes": EXPIRED set_changed, nothing
    deleted or pending; asking again previews only what exists, under a new pending action."""
    ids = mk(denv, "Quarterly Widgets", n=2)
    ask(denv, "delete reports about quarterly widgets")
    first = pending(denv)["pending_action_id"]
    denv.conn.execute("UPDATE saved_report SET owner_user_id = ? WHERE report_id = ?",
                      (OTHER.user_id, ids[0]))  # fmt: skip
    denv.conn.commit()
    out = ask(denv, "yes")
    assert out.outcome == "delete_expired" and ids[0] in out.text
    assert "nothing was deleted" in out.text and "ask again" in out.text
    assert alive(denv, ids[1:]) == ids[1:] and pending(denv) == {}
    (ev,) = events(denv, A.DELETE_EXPIRED)
    assert ev.details["error_type"] == flow.SET_CHANGED
    out = ask(denv, "delete reports about quarterly widgets")
    assert out.outcome == "delete_pending" and ids[1] in out.text and ids[0] not in out.text
    assert pending(denv)["pending_action_id"] != first
    assert ask(denv, "yes").text == "Deleted 1 report." and alive(denv, ids) == []


def test_set_changed_text_when_nothing_left() -> None:
    gone = [uuid4().hex for _ in range(2)]
    assert flow._set_changed_text(gone, 0) == (
        f"2 previewed reports no longer exist ({gone[0]}, {gone[1]}), so nothing was deleted.")


@pytest.mark.parametrize("text", [
    "remove the cancelled orders from the revenue report",
    "can you erase test accounts from the report?",
    "delete returned items in the report",
    "exclude and remove outliers in my report",
    "Please delete returned items from this report and recompute",
    "delete the outliers in report 2 and redo the chart",
    "delete reportage",
    "Remove the report header and show revenue by month",  # mn-2: report + noun
    "delete the report's second chart",
    "remove report-level duplicates",
    "remove report-level duplicates and recompute revenue",
    "delete report sections with no data",
])  # fmt: skip
def test_nl_analysis_requests_are_not_deletes(denv, text) -> None:
    """M2: the verb's direct object must be reports, a report id or this session's reports."""
    ids = mk(denv, "Quarterly Widgets")
    assert flow.parse_delete_request(text) is None
    assert ask(denv, text).outcome != "delete_pending"
    assert alive(denv, ids) == ids and not events(denv, A.DELETE_PREVIEWED)


@pytest.mark.parametrize("text", [
    "delete reports about quarterly widgets",
    "Please remove my saved reports about quarterly widgets",
    "erase the report titled quarterly widgets",
    "delete the reports from this session",
    "delete report " + "a" * 32,
    "delete " + "b" * 32,
    "delete my reports in this session",
    "remove the report named quarterly widgets.",
])  # fmt: skip
def test_nl_report_deletes_still_parse(text) -> None:
    req = flow.parse_delete_request(text)
    assert req is not None and not req.error, text


@pytest.mark.parametrize("text", [
    "delete reports not from this session",
    "delete all my reports except those from this session",
    "delete my reports other than the ones from this session",
    "delete reports excluding this session",
    "delete reports about widgets but not gadgets",
    "delete reports that aren't from this session",
])  # fmt: skip
def test_nl_negated_selector_refused(denv, text) -> None:
    """mn-1: a negated or exclusive selector is refused, never read as kind=session."""
    here = mk(denv, "Here", session_id=denv.env.session.session_id)
    other = mk(denv, "Widgets elsewhere")
    req = flow.parse_delete_request(text)
    assert req is not None and req.error == flow.SELECTOR_EMPTY and req.kind != "session"
    out = ask(denv, text)
    assert out.outcome == "refused" and out.text == flow.SELECTOR_EMPTY_TEXT
    assert not events(denv, A.DELETE_PREVIEWED) and alive(denv, here + other) == here + other


def test_uppercase_report_id_recognised_and_stored_lowercase(denv) -> None:
    """mn-3: an uppercase id is the same id; the request and the preview keep it lowercase."""
    (rid,) = mk(denv, "Quarterly Widgets")
    req = flow.parse_delete_request(f"DELETE REPORT {rid.upper()}")
    assert req == flow.DeleteRequest("ids", ids=(rid,))
    assert flow.parse_delete_request(rid.upper(), command=True) == req
    assert ask(denv, f"Delete report {rid.upper()}").outcome == "delete_pending"
    assert pending(denv)["report_ids"] == [rid]


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\u00a0", "\u3000", "\u205f"])
def test_fold_maps_unicode_separators_to_space(sep) -> None:
    """mn-4: Z* separators split words; they are never dropped (no "widgetsand")."""
    assert flow._fold(f"widgets{sep}and") == "widgets and"
    req = flow.parse_delete_request(f"delete reports about widgets{sep}gadgets")
    assert req is not None and req.phrase == "widgets gadgets"
