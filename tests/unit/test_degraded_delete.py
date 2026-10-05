"""Iteration 24 x 22a: the two-phase delete through the quota / degraded wrapper.

A typed delete request is parsed by regex in the graph and goes straight to the preview (no
router call, so no gate and no count); confirm, cancel and execute are deterministic code too,
so neither an exhausted LLM quota nor an outage may block or alter them (iter24-ods.md OD-8).
Offline, synthetic data only.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from opsfleet_agent import commands
from opsfleet_agent.commands.delete import DELETE_COMMAND
from opsfleet_agent.delete import flow
from opsfleet_agent.graph.degraded import (
    AI_UNAVAILABLE_TEXT,
    QUOTA_CHECK_FAILED_TEXT,
    DegradedGraph,
    LLMHealth,
)
from opsfleet_agent.graph.resume import ResumeKind, close_interrupted_turn, resume_turn
from opsfleet_agent.store import audit as A
from opsfleet_agent.store.db import StoreError
from opsfleet_agent.store.quota import QuotaLimits, QuotaStore
from tests.unit.test_degraded import DownModel
from tests.unit.test_delete_flow import (  # noqa: F401 - pytest fixtures used by name
    PROFILE,
    alive,
    denv,
    detector,
    events,
    make_env,
    mk,
    pending,
    settings,
    terminal,
)

REQ = "delete reports about quarterly widgets"
ORDERS = "How many complete orders are there?"
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


@pytest.fixture
def wenv(denv):  # noqa: F811
    """The delete env with the gated invokes and the DegradedGraph wrapper (as build_runtime)."""
    quota = QuotaStore(denv.conn, QuotaLimits(llm_per_hour=50, llm_per_day=500), lambda: NOW)
    health = LLMHealth(quota, PROFILE.user_id)
    g = denv.env.graph
    g.services = dataclasses.replace(
        g.services,
        router_invoke=health.wrap(g.services.router_invoke),
        analyst_invoke=health.wrap(g.services.analyst_invoke),
    )
    denv.quota, denv.health = quota, health
    denv.wrapper = DegradedGraph(g, quota, health)
    return denv


def wask(d, text: str, **kw):
    return d.wrapper.run_turn(text, session=d.env.session, **kw)


def _exhaust(d) -> None:
    d.quota.record_calls(PROFILE.user_id, 10_000)
    assert d.quota.check(PROFILE.user_id).allowed is False


def test_delete_preview_confirm_execute_through_wrapper(wenv) -> None:
    ids = mk(wenv, "Quarterly Widgets", n=2)
    out = wask(wenv, REQ)
    assert out.outcome == "delete_pending" and alive(wenv, ids) == ids
    out = wask(wenv, "yes")
    assert out.outcome == "delete_executed" and out.text == "Deleted 2 reports."
    assert alive(wenv, ids) == [] and pending(wenv) == {}
    assert terminal(wenv) == [A.DELETE_EXECUTED]


def test_slash_delete_through_wrapper_has_no_llm_call(wenv) -> None:
    ids = mk(wenv, "Quarterly Widgets")
    _exhaust(wenv)  # /delete is code only: not blocked, not counted
    before = wenv.quota.usage(PROFILE.user_id)
    ctx = commands.CommandContext(
        user_id=PROFILE.user_id, session_id=wenv.env.session.session_id,
        delete_start=lambda a: wenv.wrapper.start_delete(a, session=wenv.env.session).text,
    )  # fmt: skip
    commands.register_command(DELETE_COMMAND)
    try:
        res = commands.dispatch("/delete quarterly widgets", ctx)
    finally:
        commands.unregister_command(DELETE_COMMAND.name)
    assert ids[0] in res.text and alive(wenv, ids) == ids
    assert wenv.quota.usage(PROFILE.user_id) == before
    assert wask(wenv, "yes").outcome == "delete_executed" and alive(wenv, ids) == []


def test_confirm_not_blocked_by_exhausted_llm_quota(wenv) -> None:
    ids = mk(wenv, "Quarterly Widgets")
    wask(wenv, REQ)
    _exhaust(wenv)
    seen = (wenv.env.router.calls, wenv.quota.usage(PROFILE.user_id)["llm_hour"])
    out = wask(wenv, "yes")
    assert out.outcome == "delete_executed" and out.text == "Deleted 1 report."
    assert alive(wenv, ids) == [] and terminal(wenv) == [A.DELETE_EXECUTED]
    assert (wenv.env.router.calls, wenv.quota.usage(PROFILE.user_id)["llm_hour"]) == seen


def test_cancel_at_quota_writes_audit_and_keeps_text(wenv) -> None:
    """A non-confirm reply cancels (audit row written), then is a normal gated turn: at
    quota it is refused, and the cancel text stays in front (nothing was deleted)."""
    ids = mk(wenv, "Quarterly Widgets")
    wask(wenv, REQ)
    _exhaust(wenv)
    out = wask(wenv, ORDERS)
    assert out.text.startswith(flow.CANCELLED_TEXT)
    assert alive(wenv, ids) == ids and terminal(wenv) == [A.DELETE_CANCELLED]
    assert pending(wenv) == {}


def test_cancel_during_outage_writes_audit_and_keeps_text(wenv) -> None:
    ids = mk(wenv, "Quarterly Widgets")
    wask(wenv, REQ)
    g = wenv.env.graph
    g.services = dataclasses.replace(
        g.services,
        router_invoke=wenv.health.wrap(DownModel()),
        analyst_invoke=wenv.health.wrap(DownModel()),
    )
    out = wask(wenv, ORDERS)
    assert out.text.startswith(flow.CANCELLED_TEXT) and AI_UNAVAILABLE_TEXT in out.text
    assert alive(wenv, ids) == ids and terminal(wenv) == [A.DELETE_CANCELLED]


def test_typed_preview_at_quota_is_not_gated(wenv) -> None:
    """A typed request is regex-parsed to the preview: no LLM call, no gate, no count."""
    ids = mk(wenv, "Quarterly Widgets")
    _exhaust(wenv)
    before = wenv.quota.usage(PROFILE.user_id)
    calls = wenv.env.router.calls
    out = wask(wenv, REQ)
    assert out.outcome == "delete_pending"
    assert wenv.quota.usage(PROFILE.user_id) == before and wenv.env.router.calls == calls
    assert alive(wenv, ids) == ids


def test_cancel_text_kept_when_outage_reply_is_clarification(wenv) -> None:
    ids = mk(wenv, "Quarterly Widgets")
    wask(wenv, REQ)
    g = wenv.env.graph
    g.services = dataclasses.replace(
        g.services,
        router_invoke=wenv.health.wrap(DownModel()),
        analyst_invoke=wenv.health.wrap(DownModel()),
    )
    out = wask(wenv, "How did they do?")
    assert terminal(wenv) == [A.DELETE_CANCELLED] and alive(wenv, ids) == ids
    assert AI_UNAVAILABLE_TEXT in out.text
    assert out.text.startswith(flow.CANCELLED_TEXT), out.text


@pytest.mark.parametrize("head", [
    flow.CANCELLED_TEXT, flow.EXPIRED_TEXT, flow.UNSAFE_TEXT, flow.DELETE_PENDING_TEXT,
    flow.STRANDED_TEXT,
])  # fmt: skip
def test_closed_delete_head_matches_any_delete_text_by_prefix(head) -> None:
    from opsfleet_agent.graph.degraded import _closed_delete_head

    assert _closed_delete_head(f"{head}\n\nWhat do you mean?") == head
    assert _closed_delete_head(head) == ""  # nothing follows: not a head
    assert _closed_delete_head("Something else\n\nWhat do you mean?") == ""


def test_quota_check_failure_fails_closed_but_not_delete_steps(wenv, monkeypatch) -> None:
    """A broken quota store refuses LLM turns visibly; confirm/cancel/execute and /delete are
    deterministic code and never go through the gate (OD-9)."""
    ids = mk(wenv, "Quarterly Widgets", n=2)

    def broken(user_id):
        raise StoreError("db locked")

    monkeypatch.setattr(wenv.quota, "check", broken)
    out = wask(wenv, ORDERS)
    assert out.outcome == "refused" and out.text == QUOTA_CHECK_FAILED_TEXT
    assert wenv.health.failures == 0  # a refusal, not a provider failure
    out = wask(wenv, REQ)
    assert out.outcome == "delete_pending"
    out = wask(wenv, "yes")
    assert out.outcome == "delete_executed" and alive(wenv, ids) == []


@pytest.mark.parametrize("method", ["execute", "confirm"])
def test_ctrl_c_after_yes_with_wrapper(wenv, monkeypatch, method) -> None:
    ids = mk(wenv, "Quarterly Widgets")
    wask(wenv, REQ)
    _exhaust(wenv)  # the close never depends on quota
    tid = uuid4().hex

    def boom(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(flow.DeleteService, method, boom)
    with pytest.raises(KeyboardInterrupt):
        wenv.wrapper.run_turn("yes", session=wenv.env.session, turn_id=tid)
    monkeypatch.undo()
    assert close_interrupted_turn(wenv.wrapper, wenv.env.session, tid) is True
    (ev,) = events(wenv, A.DELETE_CANCELLED)
    assert ev.details["error_type"] == flow.INTERRUPTED and ev.turn_id == tid
    assert alive(wenv, ids) == ids and not events(wenv, A.DELETE_EXECUTED)
    assert pending(wenv) == {} and not wenv.env.graph.open_resume(wenv.env.session).next


@pytest.mark.parametrize("lapse", ["key_changed", "timeout"])
def test_resume_pending_delete_through_wrapper(wenv, lapse) -> None:
    ids = mk(wenv, "Quarterly Widgets")
    wask(wenv, REQ)
    if lapse == "key_changed":
        wenv.clock.t += 10 * 3600
        wenv.svc = wenv.env.graph.services.delete = flow.DeleteService(
            wenv.audit, wenv.store, clock=wenv.clock
        )
    else:
        wenv.clock.t += flow.EXPIRY_S + 1
    _exhaust(wenv)
    out = resume_turn(wenv.wrapper, wenv.env.session.session_id, PROFILE)
    assert out.kind == ResumeKind.RESUMED and flow.EXPIRED_TEXT in out.text
    assert terminal(wenv) == [A.DELETE_EXPIRED] and alive(wenv, ids) == ids
    assert pending(wenv) == {}
    # a fresh request previews again (after the quota recovers: here a new user window)
    assert events(wenv, A.DELETE_EXPIRED)[0].details["error_type"] == lapse


def test_resume_stranded_execute_through_wrapper(wenv, monkeypatch) -> None:
    from tests.unit.test_delete_flow import _crash_in_execute

    ids = mk(wenv, "Quarterly Widgets")
    wask(wenv, REQ)
    _crash_in_execute(wenv, monkeypatch)
    out = resume_turn(wenv.wrapper, wenv.env.session.session_id, PROFILE)
    assert out.result is not None and out.result.outcome == "delete_executed"
    assert alive(wenv, ids) == [] and len(events(wenv, A.DELETE_EXECUTED)) == 1
