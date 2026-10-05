"""Iteration 14b: narrow crash resume and TurnBudget persistence (HLD 4.0.6, FR-76, R2-m6).

Offline only: scripted router and analyst models, a fake BigQuery client and a real encrypted
SqliteSaver under ``tmp_path`` with a synthetic key generated in the test. All data is synthetic.
"""

from __future__ import annotations

import os
import secrets
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opsfleet_agent.graph import graph as gr
from opsfleet_agent.graph import resume as rs
from opsfleet_agent.graph.budget import TurnBudget, TurnKind
from opsfleet_agent.roles import light_path as lp
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.session import Profile
from tests.unit import test_graph as _tg
from tests.unit.test_graph import PROFILE, Env, Router, Scripted, sql_call
from tests.unit.test_run_sql import SIMPLE

# A UserWarning (e.g. LangGraph's durability notice) must never fail or leak from a turn.
pytestmark = pytest.mark.filterwarnings("error::UserWarning")

ANSWER = "There were 3 complete orders."
SID = "sess-1"  # Env's session id

# re-export test_graph's module fixtures (the same objects, so pytest registers them here)
detector = _tg.detector
settings = _tg.settings


class _Crash(BaseException):
    """A simulated process kill: not an Exception, so no node or turn handler catches it."""


def new_key() -> str:
    """A synthetic 32-character AES key, generated per test (never a real secret)."""
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))


def saver(tmp_path, key: str):
    return gr.build_checkpointer(tmp_path / "data", {gr.AES_KEY_ENV: key})


def escalating_analyst() -> Scripted:
    """Quick runs SQL then escalates; Deep re-issues the same SQL (a duplicate) and answers."""
    return Scripted(
        sql_call(SIMPLE, "c1"),
        ModelTurn("[[ESCALATE]]"),
        sql_call(SIMPLE, "c2"),
        ModelTurn(ANSWER),
    )


def crash_on_entry(monkeypatch, target: str) -> None:
    """Kill the run once, on entry of ``target`` (so after the node before it was saved)."""
    real = gr._make_nodes
    fired = [False]

    def make(ctx):
        nodes = real(ctx)
        fn = nodes[target]

        def crashing(state):
            if not fired[0]:
                fired[0] = True
                raise _Crash
            return fn(state)

        return {**nodes, target: crashing}

    monkeypatch.setattr(gr, "_make_nodes", make)


def parent_next(env: Env) -> tuple[str, ...]:
    snap = env.graph.open_resume(env.session)
    return snap.next


def stored(env: Env) -> dict[str, Any]:
    cfg = env.graph._config(SID)
    compiled = env.graph.open_resume(env.session)._built[1]
    return dict(compiled.get_state(cfg, subgraphs=True).values)


# --- budget persistence ---


def test_resume_budget_persisted(tmp_path, settings, detector, monkeypatch) -> None:
    """The TurnBudget counters are in the checkpoint and the resumed turn continues them."""
    key = new_key()
    analyst = escalating_analyst()
    router = Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    crash_on_entry(monkeypatch, "deep")
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")

    snap = stored(env)["turn_ctx"]["budget"]
    assert snap["kind"] == TurnKind.QA.value
    assert snap["calls"] >= 2 and snap["sql_queries"] == 1 and snap["escalated"] is True
    assert snap["role_calls"]  # per-role sub-cap usage is kept too

    resumed = Env(
        tmp_path, settings, detector, router, analyst,
        client=env.client, saver=saver(tmp_path, key),
    )  # fmt: skip
    out = rs.resume_turn(resumed.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.RESUMED and out.result is not None
    assert out.result.llm_calls > snap["calls"]  # continued, not reset to zero
    assert out.result.sql_queries >= snap["sql_queries"]


def test_budget_restore_never_lowers_and_clamps() -> None:
    now = [100.0]
    b = TurnBudget(TurnKind.QA, clock=lambda: now[0])
    b.calls = 4
    snap = {
        "kind": "qa", "calls": 2, "sql_queries": 99, "retries": 1, "escalated": True,
        "role_calls": {"quick": 3}, "elapsed_s": 30.0,
    }  # fmt: skip
    assert b.restore(snap) is True
    assert b.calls == 4  # never lowered
    assert b.sql_queries == b.caps.sql_queries  # clamped to the cap
    assert b.escalated and b.role_calls["quick"] == 3
    assert b.elapsed() == pytest.approx(30.0)


@pytest.mark.parametrize(
    "snap",
    [
        None,
        {},
        {"kind": "report"},
        {"kind": "qa", "calls": True, "sql_queries": 0, "retries": 0, "escalated": False,
         "role_calls": {}, "elapsed_s": 0.0},
        {"kind": "qa", "calls": 1, "sql_queries": 0, "retries": 0, "escalated": False,
         "role_calls": {}, "elapsed_s": float("nan")},
        {"kind": "qa", "calls": 1, "sql_queries": 0, "retries": 0, "escalated": False,
         "role_calls": {str(i): 1 for i in range(40)}, "elapsed_s": 0.0},
    ],
)  # fmt: skip
def test_budget_restore_malformed_fails_closed(snap) -> None:
    b = TurnBudget(TurnKind.QA, clock=lambda: 0.0)
    assert b.restore(snap) is False
    assert b.calls == b.caps.llm_calls and b.sql_queries == b.caps.sql_queries
    assert b.deadline_hit() and b.escalated


# --- crash after each parent node ---


@pytest.mark.parametrize(
    "target", ["load_context", "quick", "deep", "force_answer", "grounding", "finalize"]
)
def test_resume_after_crash_each_node(tmp_path, settings, detector, monkeypatch, target) -> None:
    """Kill the run before ``target`` (after its predecessor was checkpointed), then resume:
    no SQL runs twice, the budget is not reset, and only the interrupted turn finishes."""
    base = Env(tmp_path / "base", settings, detector, Router("simple"), escalating_analyst())
    baseline = base.ask("How many complete orders are there?")
    assert baseline.outcome == "answered" and len(base.client.executed) == 1

    key = new_key()
    analyst, router = escalating_analyst(), Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    crash_on_entry(monkeypatch, target)
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    assert parent_next(env) == (target,)
    before = stored(env)["turn_ctx"]["budget"]
    executed_before = len(env.client.executed)

    resumed = Env(
        tmp_path, settings, detector, router, analyst,
        client=env.client, saver=saver(tmp_path, key),
    )  # fmt: skip
    out = rs.resume_turn(resumed.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.RESUMED and out.result is not None
    assert len(env.client.executed) == 1 >= executed_before  # no duplicate SQL execution
    assert out.result.llm_calls >= before["calls"]  # the budget was not reset
    assert out.result.llm_calls == baseline.llm_calls  # nothing was re-run or lost
    assert out.result.sql_queries == baseline.sql_queries
    assert out.result.outcome == baseline.outcome and ANSWER in out.result.text
    assert parent_next(resumed) == ()  # finished; a new turn was never started
    again = rs.resume_turn(resumed.graph, SID, PROFILE)
    assert again.kind is rs.ResumeKind.NOTHING_PENDING


def test_resume_before_input_guard_asks_again(tmp_path, settings, detector, monkeypatch) -> None:
    """The raw text is never stored, so a turn killed before input_guard is not replayed."""
    key = new_key()
    analyst, router = escalating_analyst(), Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    crash_on_entry(monkeypatch, "input_guard")
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    out = rs.resume_turn(env.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.ASK_AGAIN and out.result is None
    assert analyst.calls == [] and router.calls == [] and env.client.executed == []


def test_resume_malformed_turn_ctx_fails_closed(tmp_path, settings, detector, monkeypatch) -> None:
    """A tampered or missing turn context exhausts the budget: no SQL, no tool-using call."""
    key = new_key()
    analyst, router = escalating_analyst(), Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    crash_on_entry(monkeypatch, "quick")
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    cfg = env.graph._config(SID)
    compiled = env.graph.open_resume(env.session)._built[1]
    compiled.update_state(cfg, {"turn_ctx": {"budget": {"kind": "qa", "calls": "x"}}})
    tool_calls_before = len([c for c in analyst.calls if c[2]])

    out = rs.resume_turn(env.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.RESUMED and out.result is not None
    assert env.client.executed == []
    assert len([c for c in analyst.calls if c[2]]) == tool_calls_before


# --- refusals ---


def test_resume_wrong_key_refused(tmp_path, settings, detector, monkeypatch) -> None:
    key = new_key()
    analyst, router = escalating_analyst(), Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    crash_on_entry(monkeypatch, "deep")
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    calls_before = (len(analyst.calls), len(router.calls), len(env.client.executed))

    other = new_key()
    assert other != key
    wrong = Env(
        tmp_path, settings, detector, router, analyst,
        client=env.client, saver=saver(tmp_path, other),
    )  # fmt: skip
    out = rs.resume_turn(wrong.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.KEY_REFUSED and out.result is None
    assert out.text == rs.KEY_REFUSED_TEXT and "\n" not in out.text
    assert gr.AES_KEY_ENV in out.text and other not in out.text and key not in out.text
    assert (len(analyst.calls), len(router.calls), len(env.client.executed)) == calls_before

    # the right key still resumes: the refusal changed nothing
    right = Env(
        tmp_path, settings, detector, router, analyst,
        client=env.client, saver=saver(tmp_path, key),
    )  # fmt: skip
    assert rs.resume_turn(right.graph, SID, PROFILE).kind is rs.ResumeKind.RESUMED


@pytest.mark.parametrize("env_key", [None, "", "short", "x" * 17])
def test_resume_missing_key_refused(tmp_path, settings, detector, capsys, env_key) -> None:
    analyst, router = escalating_analyst(), Router("simple")
    environ = {} if env_key is None else {gr.AES_KEY_ENV: env_key}

    def factory() -> gr.AgentGraph:
        built = gr.build_checkpointer(tmp_path / "data", environ)  # raises ConfigError
        return Env(tmp_path, settings, detector, router, analyst, saver=built).graph

    out = rs.resume_turn(factory, SID, PROFILE)
    assert out.kind is rs.ResumeKind.KEY_REFUSED and out.result is None
    assert out.text == rs.KEY_REFUSED_TEXT and "\n" not in out.text
    assert "Traceback" not in out.text + capsys.readouterr().err
    if env_key:
        assert env_key not in out.text
    assert analyst.calls == [] and router.calls == []


def test_resume_store_error_is_one_line(tmp_path, settings, detector) -> None:
    def factory() -> gr.AgentGraph:
        raise gr.ConfigError("Cannot open the checkpoint store (OSError).\nsecond line")

    out = rs.resume_turn(factory, SID, PROFILE)
    assert out.kind is rs.ResumeKind.STORE_REFUSED and out.text == rs.STORE_REFUSED_TEXT
    assert "OSError" not in out.text and "second line" not in out.text  # never echoed


def test_resume_non_mac_value_error_is_store_refused(tmp_path, settings, detector) -> None:
    """Only a MAC check failure (a wrong key) is KEY_REFUSED; another ValueError is not."""
    env = Env(
        tmp_path, settings, detector, Router("simple"), escalating_analyst(),
        saver=saver(tmp_path, new_key()),
    )  # fmt: skip

    def broken(session):
        raise ValueError("unsupported pickle protocol: 9 (synthetic)")

    env.graph.open_resume = broken
    out = rs.resume_turn(env.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.STORE_REFUSED and out.text == rs.STORE_REFUSED_TEXT
    assert "pickle" not in out.text and out.result is None


# --- scope drift and nothing pending ---


def test_resume_scope_drift_new_session(tmp_path, settings, detector, monkeypatch) -> None:
    """FR-76 (unit half): a changed product scope never replays; a new session starts."""
    key = new_key()
    analyst, router = escalating_analyst(), Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    crash_on_entry(monkeypatch, "deep")
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    assert stored(env)["scope_snapshot"] == {"all": False, "brands": ["Acme"]}
    calls_before = (len(analyst.calls), len(router.calls), len(env.client.executed))

    for drifted in (
        Profile("analyst_a", "Analyst A", brands=("Other",)),
        Profile("analyst_a", "Analyst A", brands=("Acme", "Other")),
    ):
        fresh = Env(
            tmp_path, settings, detector, router, analyst,
            client=env.client, saver=saver(tmp_path, key),
        )  # fmt: skip
        out = rs.resume_turn(fresh.graph, SID, drifted)
        assert out.kind is rs.ResumeKind.NEW_SESSION and out.start_new_session
        assert out.text == rs.SCOPE_DRIFT_TEXT and out.result is None
    assert (len(analyst.calls), len(router.calls), len(env.client.executed)) == calls_before


def test_resume_missing_scope_snapshot_is_drift(tmp_path, settings, detector, monkeypatch) -> None:
    key = new_key()
    env = Env(
        tmp_path, settings, detector, Router("simple"), escalating_analyst(),
        saver=saver(tmp_path, key),
    )  # fmt: skip
    crash_on_entry(monkeypatch, "deep")
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    cfg = env.graph._config(SID)
    compiled = env.graph.open_resume(env.session)._built[1]
    compiled.update_state(cfg, {"scope_snapshot": {}})
    out = rs.resume_turn(env.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.NEW_SESSION and out.start_new_session


def test_resume_nothing_pending(tmp_path, settings, detector) -> None:
    key = new_key()
    analyst, router = escalating_analyst(), Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))

    out = rs.resume_turn(env.graph, "never-seen-session", PROFILE)  # no checkpoint at all
    assert out.kind is rs.ResumeKind.NOTHING_PENDING and out.text == rs.NOTHING_PENDING_TEXT

    assert env.ask("How many complete orders are there?").outcome == "answered"
    counts = (len(analyst.calls), len(router.calls), len(env.client.executed))
    out = rs.resume_turn(env.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.NOTHING_PENDING and "\n" not in out.text
    assert out.result is None and not out.start_new_session
    assert (len(analyst.calls), len(router.calls), len(env.client.executed)) == counts


# --- owner (M1) ---


def test_resume_rejects_other_users_session(tmp_path, settings, detector, monkeypatch) -> None:
    """Same scope, different user: the session is never resumed under another profile."""
    key = new_key()
    analyst, router = escalating_analyst(), Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    crash_on_entry(monkeypatch, "deep")
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    assert stored(env)["owner"] == PROFILE.user_id
    calls_before = (len(analyst.calls), len(router.calls), len(env.client.executed))

    other = Profile("analyst_b", "Analyst B", brands=PROFILE.brands)  # identical scope
    fresh = Env(
        tmp_path, settings, detector, router, analyst,
        client=env.client, saver=saver(tmp_path, key),
    )  # fmt: skip
    out = rs.resume_turn(fresh.graph, SID, other)
    assert out.kind is rs.ResumeKind.NEW_SESSION and out.start_new_session
    assert out.text == rs.OWNER_TEXT and out.result is None
    assert (len(analyst.calls), len(router.calls), len(env.client.executed)) == calls_before
    assert parent_next(fresh) == ("deep",)  # still pending for its owner


@pytest.mark.parametrize("owner", [None, "", 7])
def test_resume_missing_owner_is_drift(tmp_path, settings, detector, monkeypatch, owner) -> None:
    key = new_key()
    env = Env(
        tmp_path, settings, detector, Router("simple"), escalating_analyst(),
        saver=saver(tmp_path, key),
    )  # fmt: skip
    crash_on_entry(monkeypatch, "deep")
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    compiled = env.graph.open_resume(env.session)._built[1]
    compiled.update_state(env.graph._config(SID), {"owner": owner})
    executed = len(env.client.executed)
    out = rs.resume_turn(env.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.NEW_SESSION and out.start_new_session
    assert len(env.client.executed) == executed


# --- restore validation (L2, L4) ---


def _pending_after_crash(tmp_path, settings, detector, monkeypatch, target="deep"):
    key = new_key()
    env = Env(
        tmp_path, settings, detector, Router("simple"), escalating_analyst(),
        saver=saver(tmp_path, key),
    )  # fmt: skip
    crash_on_entry(monkeypatch, target)
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    return env, env.graph.open_resume(env.session)


def _entry(**over: Any) -> dict[str, Any]:
    e = {"sql": "SELECT 1", "purpose": "synthetic", "query_id": "q1", "rows": 3,
         "sql_hash": "h1", "executed_sql_hash": "h2"}  # fmt: skip
    e.update(over)
    return e


@pytest.mark.parametrize(
    "ledger",
    [
        "not-a-list",
        [{"sql": 1}],
        [_entry(rows=-1)],
        [_entry(rows=True)],
        [_entry(rows="3")],
        [_entry(sql=None)],
        [_entry(purpose=5)],
        [_entry(extra="x")],
        [{k: v for k, v in _entry().items() if k != "query_id"}],
        [_entry()] * 65,
    ],
)
def test_restore_malformed_ledger_with_valid_budget_fails_closed(
    tmp_path, settings, detector, monkeypatch, ledger
) -> None:
    """A valid budget does not save a malformed ledger: the budget is exhausted and run_sql
    gives up, because a bad ledger may hide queries that already ran."""
    import copy

    _, pending = _pending_after_crash(tmp_path, settings, detector, monkeypatch)
    ctx = pending._built[0]
    snap = copy.deepcopy(pending.values["turn_ctx"])
    assert ctx.budget.restore(copy.deepcopy(snap["budget"])) is True  # the budget is valid
    ctx.budget.calls = 0
    snap["sql"]["ledger"] = ledger
    assert gr._restore_ctx(ctx, snap) is False
    assert ctx.budget.calls == ctx.budget.caps.llm_calls
    assert ctx.budget.sql_queries == ctx.budget.caps.sql_queries
    assert ctx.sql_turn.gave_up is True and ctx.sql_turn.ledger == []


def test_restore_valid_ledger_is_kept(tmp_path, settings, detector, monkeypatch) -> None:
    import copy

    _, pending = _pending_after_crash(tmp_path, settings, detector, monkeypatch)
    ctx = pending._built[0]
    snap = copy.deepcopy(pending.values["turn_ctx"])
    assert len(snap["sql"]["ledger"]) == 1
    snap["sql"]["ledger"].append(_entry(purpose=None, rows=0))
    assert gr._restore_ctx(ctx, snap) is True
    assert len(ctx.sql_turn.ledger) == 2 and ctx.sql_turn.gave_up is False
    assert ctx.budget.calls < ctx.budget.caps.llm_calls


def test_budget_restore_clamps_calls() -> None:
    b = TurnBudget(TurnKind.QA, clock=lambda: 0.0)
    snap = {
        "kind": "qa", "calls": 10_000, "sql_queries": 0, "retries": 10_000, "escalated": False,
        "role_calls": {"quick": 10_000}, "elapsed_s": 0.0,
    }  # fmt: skip
    assert b.restore(snap) is True
    assert b.calls == b.caps.llm_calls  # clamped, so the turn cannot run past its cap
    assert b.retries == b._max_turn_retries and b.role_calls["quick"] == b.subcap("quick")


@pytest.mark.parametrize("field", ["calls", "sql_queries", "retries"])
@pytest.mark.parametrize("bad", [-1, "1", 1.5, None])
def test_budget_restore_rejects_bad_counters(field, bad) -> None:
    b = TurnBudget(TurnKind.QA, clock=lambda: 0.0)
    snap = {
        "kind": "qa", "calls": 1, "sql_queries": 0, "retries": 0, "escalated": False,
        "role_calls": {}, "elapsed_s": 0.0,
    }  # fmt: skip
    snap[field] = bad
    assert b.restore(snap) is False
    assert b.calls == b.caps.llm_calls and b.sql_queries == b.caps.sql_queries


def test_finish_restores_turn_id(tmp_path, settings, detector, monkeypatch) -> None:
    _, pending = _pending_after_crash(tmp_path, settings, detector, monkeypatch)
    tid = pending.values["turn_id"]
    ctx = pending._built[0]
    assert tid and ctx.turn_id == ""  # open_resume builds a context without a turn id
    out = pending.finish()
    assert out.outcome == "answered"
    assert ctx.turn_id == tid and ctx.sql_turn.turn_id == tid


def test_light_route_without_text_fails_closed(tmp_path, settings, detector, monkeypatch) -> None:
    """A resumed light route with no stored text never shows an empty or 'answered' turn."""
    key = new_key()
    router = Router("smalltalk", "Hi! Ask me about the store data.")
    env = Env(
        tmp_path, settings, detector, router, escalating_analyst(), saver=saver(tmp_path, key)
    )
    crash_on_entry(monkeypatch, "finalize")
    with pytest.raises(_Crash):
        env.ask("hello")
    compiled = env.graph.open_resume(env.session)._built[1]
    compiled.update_state(env.graph._config(SID), {"final_text": ""})
    out = rs.resume_turn(env.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.RESUMED and out.result is not None
    assert out.result.outcome == "error" and out.result.text == gr.ERROR_TEXT


@pytest.mark.parametrize("reply", ["<b></b>", "\u200b"])
def test_light_reply_empty_after_sanitising_is_answered(tmp_path, settings, detector, reply):
    """A light reply the output guard strips to nothing ends as the template, not an error."""
    env = Env(tmp_path, settings, detector, Router("smalltalk", reply), escalating_analyst())
    result = env.ask("hello")
    assert result.outcome == "answered" and result.text == lp.GREETING_TEMPLATE


# --- routes and a crash inside a node (L3) ---


@pytest.mark.parametrize("target", ["light", "finalize"])
def test_resume_light_route(tmp_path, settings, detector, monkeypatch, target) -> None:
    key = new_key()
    router = Router("smalltalk", "Hi! Ask me about the store data.")
    analyst = escalating_analyst()
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    crash_on_entry(monkeypatch, target)
    with pytest.raises(_Crash):
        env.ask("hello")
    assert parent_next(env) == (target,)
    router_calls = len(router.calls)
    out = rs.resume_turn(env.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.RESUMED and out.result is not None
    assert out.result.route == "light" and out.result.outcome == "answered"
    assert "store data" in out.result.text
    assert analyst.calls == [] and env.client.executed == []
    # the light node re-runs only when the kill hit it (at most one repeated call)
    assert len(router.calls) - router_calls == (1 if target == "light" else 0)
    assert parent_next(env) == ()


def test_resume_complex_route(tmp_path, settings, detector, monkeypatch) -> None:
    """Complex: load_context -> deep. A kill before deep resumes deep with the ledger kept."""
    key = new_key()
    analyst = Scripted(sql_call(SIMPLE, "c1"), sql_call(SIMPLE, "c2"), ModelTurn(ANSWER))
    router = Router("complex")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))

    crash_on_entry(monkeypatch, "deep")
    with pytest.raises(_Crash):
        env.ask("Compare complete orders by month")
    assert parent_next(env) == ("deep",) and env.client.executed == []
    assert stored(env)["label"] == "complex"
    resumed = Env(
        tmp_path, settings, detector, router, analyst,
        client=env.client, saver=saver(tmp_path, key),
    )  # fmt: skip
    out = rs.resume_turn(resumed.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.RESUMED and out.result is not None
    assert out.result.label == "complex" and ANSWER in out.result.text
    assert len(env.client.executed) == 1  # c2 repeats c1: the duplicate check stops it
    assert parent_next(resumed) == ()


def test_resume_crash_inside_node_repeats_at_most_once(
    tmp_path, settings, detector, monkeypatch
) -> None:
    """OD-2: a kill INSIDE quick (after its query ran) re-runs quick from its start, so the
    query repeats at most once; nothing else repeats and the turn finishes."""
    key = new_key()
    crashed = [False]

    def crash_once(messages, specs):
        if not crashed[0]:
            crashed[0] = True
            raise _Crash
        return ModelTurn("[[ESCALATE]]")

    analyst = Scripted(
        sql_call(SIMPLE, "c1"), crash_once,  # first quick run: query, then the kill
        sql_call(SIMPLE, "c1"), ModelTurn("[[ESCALATE]]"),  # quick again after resume
        sql_call(SIMPLE, "c2"), ModelTurn(ANSWER),  # deep
    )  # fmt: skip
    router = Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    with pytest.raises(_Crash):
        env.ask("How many complete orders are there?")
    assert parent_next(env) == ("quick",) and len(env.client.executed) == 1
    resumed = Env(  # a new process: no in-memory session state survives the kill
        tmp_path, settings, detector, router, analyst,
        client=env.client, saver=saver(tmp_path, key),
    )  # fmt: skip
    out = rs.resume_turn(resumed.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.RESUMED and out.result is not None
    assert ANSWER in out.result.text and out.result.outcome == "answered"
    assert len(env.client.executed) == 2  # exactly one repeat, never more
    assert parent_next(resumed) == ()


# --- a real process kill (H1 / OD-1) ---

_REPO = Path(__file__).resolve().parents[2]
_KILL_CHILD = """
import os, sys
from pathlib import Path
from opsfleet_agent.config import load_settings
from opsfleet_agent.graph import graph as gr
from opsfleet_agent.guards.pii import PiiDetector, build_allowlist
from tests.unit.test_graph import Env, Router
from tests.unit.test_resume import escalating_analyst, saver

data, target = Path(sys.argv[1]), sys.argv[2]
real = gr._make_nodes

def make(ctx):
    nodes = real(ctx)

    def die(state):  # inside the node: no unwinding, no finally, no pending-write flush
        os._exit(9)

    return {**nodes, target: die}

gr._make_nodes = make
detector = PiiDetector(build_allowlist(("Acme",), ("Jeans",), ("Men", "Women")))
env = Env(data, load_settings(dotenv=False), detector, Router("simple"), escalating_analyst(),
          saver=saver(data, os.environ[gr.AES_KEY_ENV]))
env.ask("How many complete orders are there?")
sys.exit(0)  # not reached when the kill fires
"""


@pytest.mark.parametrize("target", ["deep", "force_answer", "grounding"])
def test_resume_after_real_process_kill(tmp_path, settings, detector, target) -> None:
    """os._exit inside ``target`` in a child process (no Python cleanup, unlike a raised
    exception): with durability="sync" every earlier node's checkpoint is already on disk,
    so this process resumes AT ``target`` and the completed query never runs again."""
    key = new_key()
    script = tmp_path / "kill_child.py"
    script.write_text(_KILL_CHILD, encoding="utf-8")
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", gr.AES_KEY_ENV)
    }  # fmt: skip
    env.update({
        # the child imports exactly what this process imports (src first, then our sys.path)
        "PYTHONPATH": os.pathsep.join([str(_REPO / "src"), *sys.path]),
        "GOOGLE_CLOUD_PROJECT": "synthetic-project", "GEMINI_API_KEY": "synthetic",
        gr.AES_KEY_ENV: key,
    })  # fmt: skip
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path), target],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert proc.returncode == 9, proc.stderr[-2000:]  # killed inside the node, not finished
    assert "durability" not in proc.stderr  # the subgraph opt-out stays silent for users

    # quick escalated after its query; deep re-issues it; deep then answers
    analyst = Scripted(sql_call(SIMPLE, "c2"), ModelTurn(ANSWER))
    router = Router("simple")
    resumed = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    assert parent_next(resumed) == (target,)  # resumes at the killed node, nothing earlier
    assert stored(resumed)["turn_ctx"]["budget"]["sql_queries"] == 1
    out = rs.resume_turn(resumed.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.RESUMED and out.result is not None
    assert resumed.client.executed == []  # the query completed before the kill never re-runs
    assert router.calls == []  # input_guard (the router) is not re-run either
    assert ANSWER in out.result.text and out.result.outcome == "answered"
    assert parent_next(resumed) == ()


# --- iteration 15: cross-turn context survives a resume ---


@pytest.mark.parametrize("target", ["quick", "deep"])
def test_resume_rebuilds_turn_context(tmp_path, settings, detector, monkeypatch, target) -> None:
    """A resumed turn starts at quick/deep with a fresh TurnContext: the analyst still gets
    the earlier turns and the code-built context section, not a bare message."""
    key = new_key()
    first = "How many complete orders are there?"
    analyst = Scripted(
        ModelTurn(ANSWER),
        sql_call(SIMPLE, "c1"), ModelTurn("[[ESCALATE]]"), sql_call(SIMPLE, "c2"),
        ModelTurn(ANSWER),
    )  # fmt: skip
    router = Router("simple")
    env = Env(tmp_path, settings, detector, router, analyst, saver=saver(tmp_path, key))
    assert env.ask(first).outcome == "answered"
    router.calls.clear()  # the fake router labels only its first call
    crash_on_entry(monkeypatch, target)
    with pytest.raises(_Crash):
        env.ask("And how many were cancelled?")

    resumed = Env(
        tmp_path, settings, detector, router, analyst,
        client=env.client, saver=saver(tmp_path, key),
    )  # fmt: skip
    n = len(analyst.calls)
    out = rs.resume_turn(resumed.graph, SID, PROFILE)
    assert out.kind is rs.ResumeKind.RESUMED and out.result is not None
    assert out.result.outcome == "answered" and ANSWER in out.result.text
    messages = analyst.calls[n][1]  # the first analyst call after the resume
    assert "## Context for this turn" in messages[0]["content"]
    assert any(first in m["content"] for m in messages[1:-1])  # the earlier turn is there
    assert "And how many were cancelled?" in messages[-1]["content"]
