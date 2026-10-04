"""Iteration 13: ``run_sql`` step order (HLD §5.1) and the differencing wiring rules (a)-(d).

A spy records every guard step, every BigQuery call and the scrub in one event list, so each
test can assert both the order and that nothing executes before the guards pass.

HLD §5.1 step 7 is split in code (see the ``run_sql`` module docstring): 7a
``DifferencingGuard.prepare`` runs before the dry run and execute, 7b ``release`` runs on the
executed rows (or the memoised ones) before any row reaches the model.
"""

from __future__ import annotations

import inspect
import itertools
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from opsfleet_agent.guards.differencing import CELL_COUNT_COLUMN, DifferencingGuard
from opsfleet_agent.guards.scope import ProductScope, ScopeRefusal
from opsfleet_agent.store.db import migrate
from opsfleet_agent.store.fingerprints import FingerprintStore
from opsfleet_agent.tools import run_sql as run_sql_mod
from opsfleet_agent.tools.run_sql import INVALID_ARGS, RunSqlTurn
from tests.unit.test_run_sql import (
    NESTED_UNPLACEABLE,
    SIMPLE,
    Clock,
    RoutingClient,
    by_state,
    call,
    default_responder,
    make_store,
    make_tool,
    new_session,
    total,
)


class SpyGuard:
    """Wraps the real guard; records prepare/release and the exact plan objects."""

    def __init__(self, inner: DifferencingGuard, events: list[str]) -> None:
        self.inner = inner
        self.events = events
        self.prepared: list[tuple[Any, dict[str, Any]]] = []
        self.released: list[Any] = []

    def prepare(self, result: Any, scope: Any, **kw: Any) -> Any:
        self.events.append("diff.prepare")
        plan = self.inner.prepare(result, scope, **kw)
        self.prepared.append((plan, kw))
        return plan

    def release(self, plan: Any, rows: Any, **k: Any) -> Any:
        self.events.append("diff.release")
        self.released.append(plan)
        return self.inner.release(plan, rows, **k)

    def abandon(self, plan: Any) -> None:
        self.inner.abandon(plan)  # not an ordered step: a no-op after release

    def open_plan_count(self) -> int:
        return self.inner.open_plan_count()


def instrument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: RoutingClient | None = None,
    **kw: Any,
) -> tuple[Any, RoutingClient, SpyGuard, list[str]]:
    events: list[str] = []
    for name in ("apply_scope", "apply_small_cell", "verify_scoped"):
        real = getattr(run_sql_mod, name)

        def wrapped(*a: Any, _real: Any = real, _name: str = name, **k: Any) -> Any:
            events.append(_name)
            return _real(*a, **k)

        monkeypatch.setattr(run_sql_mod, name, wrapped)
    real_scrub = run_sql_mod._scrub_rows

    def scrub(*a: Any, **k: Any) -> Any:
        events.append("scrub")
        return real_scrub(*a, **k)

    monkeypatch.setattr(run_sql_mod, "_scrub_rows", scrub)
    client = client if client is not None else RoutingClient(default_responder)
    client.events = events
    guard = SpyGuard(DifferencingGuard(make_store(tmp_path)), events)
    tool, _ = make_tool(tmp_path, client, guard=guard, **kw)
    return tool, client, guard, events


# --- HLD §5.1 order -----------------------------------------------------------------------------


def test_run_sql_order_matches_hld_5_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, _, _, events = instrument(tmp_path, monkeypatch)
    out = call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True, out
    assert events == [
        "apply_scope",  # 1-3 policy, scope rewrite, verify
        "apply_small_cell",  # 4
        "diff.prepare",  # 7a (pure, before any BigQuery call)
        "verify_scoped",  # 5 on the statement that will run
        "dry_run",  # 6
        "execute",  # 8
        "diff.release",  # 7b, before the model sees a row
        "scrub",  # 9 (10, the cap, follows)
    ]

    events.clear()
    out = call(tool, total("u.age >= 30"), new_session(), RunSqlTurn("turn-2"))
    assert out["ok"] is True, out
    assert events == [
        "apply_scope",
        "apply_small_cell",  # -> PopulationCheck
        "verify_scoped",  # 4b: the population query is verified, dry-run and capped too
        "dry_run",
        "execute",
        "diff.prepare",  # with the executed population
        "verify_scoped",
        "dry_run",
        "execute",
        "diff.release",
        "scrub",
    ]


def test_run_sql_small_cell_and_differencing_before_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, client, guard, events = instrument(tmp_path, monkeypatch)
    out = call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True
    plan, _ = guard.prepared[0]
    executed = [c["sql"] for c in client.executed]
    # What ran is exactly the plan's statement: small-cell HAVING + differencing instrumentation.
    assert executed == [plan.query.sql]
    assert "HAVING" in plan.query.sql.upper() and CELL_COUNT_COLUMN in plan.query.sql
    assert events.index("apply_small_cell") < events.index("diff.prepare")
    assert events.index("diff.prepare") < events.index("dry_run")
    assert events.index("diff.release") < events.index("scrub")


@pytest.mark.parametrize(
    "case",
    ["scope_refused", "small_cell_refused", "prepare_refused", "verify_raises", "dry_run_fails"],
)
def test_nothing_executes_before_the_guards_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from google.api_core import exceptions as gexc

    session = new_session()
    sql = by_state("u.age >= 30")
    client = RoutingClient(default_responder)
    if case == "scope_refused":
        session = new_session(scope=None)
    elif case == "small_cell_refused":
        sql = NESTED_UNPLACEABLE
    elif case == "dry_run_fails":
        client = RoutingClient(default_responder, dry_exc=gexc.BadRequest("Syntax error: x"))
    tool, client, guard, events = instrument(tmp_path, monkeypatch, client)
    if case == "prepare_refused":
        monkeypatch.setattr(guard.inner, "store", None)
    if case == "verify_raises":

        def refuse(*a: Any, **k: Any) -> None:
            events.append("verify_scoped")
            raise run_sql_mod.ScopeInvariantError("synthetic")

        monkeypatch.setattr(run_sql_mod, "verify_scoped", refuse)
    out = call(tool, sql, session, RunSqlTurn("turn-1"))
    assert out["ok"] is False
    assert "execute" not in events and client.executed == []
    assert "diff.release" not in events and "scrub" not in events


# --- (a) sequential: one prepare -> execute -> release at a time --------------------------------


def test_calls_never_interleave(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent calls run their prepare -> execute -> release blocks one after the other.

    ``open_store`` connections are thread-bound (sqlite3 ``check_same_thread``); a call from
    another thread fails closed in prepare. This test opens its own cross-thread connection.
    """
    gate = threading.Event()
    entered = threading.Event()

    def slow_execute(sql: str) -> None:
        entered.set()
        gate.wait(timeout=5)

    tool, client, guard, events = instrument(
        tmp_path, monkeypatch, RoutingClient(default_responder, on_execute=slow_execute)
    )
    # Worker threads need a connection that may cross threads (see test docstring).
    conn = sqlite3.connect(str(tmp_path / "mt.db"), isolation_level=None, check_same_thread=False)
    migrate(conn)
    guard.inner = DifferencingGuard(FingerprintStore(conn, clock=Clock()))
    session = new_session()
    results: list[dict[str, Any]] = []
    threads = [
        threading.Thread(
            target=lambda p=p: results.append(
                call(tool, by_state(p), session, RunSqlTurn(f"turn-{p}"))
            )
        )
        for p in ("u.age >= 30", "u.age >= 40")
    ]
    threads[0].start()
    assert entered.wait(timeout=5)
    threads[1].start()
    threads[1].join(timeout=0.2)  # blocked on the lock while the first call is mid-execute
    assert events.count("diff.prepare") == 1
    gate.set()
    for th in threads:
        th.join(timeout=10)
    assert all(r["ok"] for r in results), results
    # Each call's prepare -> execute -> release block is contiguous.
    steps = [e for e in events if e in {"diff.prepare", "execute", "diff.release"}]
    assert steps == ["diff.prepare", "execute", "diff.release"] * 2


def test_busy_lock_times_out_without_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tool, client, _, events = instrument(tmp_path, monkeypatch, lock_timeout_s=0.01)
    tool._lock.acquire()
    try:
        out = call(tool, SIMPLE, new_session(), RunSqlTurn("turn-1"))
    finally:
        tool._lock.release()
    assert out["ok"] is False and out["error"]["code"] == "TOOL_BUSY"
    assert events == [] and client.calls == []


# --- (b) only the guard's own plan is executed and released -------------------------------------


def test_released_plan_is_the_prepared_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tool, client, guard, _ = instrument(tmp_path, monkeypatch)
    assert call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))["ok"]
    assert len(guard.prepared) == 1 and len(guard.released) == 1
    assert guard.released[0] is guard.prepared[0][0]
    assert client.executed[0]["sql"] == guard.prepared[0][0].query.sql


def test_lint_run_sql_source_never_builds_a_plan() -> None:
    """Lint, not a behaviour test: greps the ``run_sql`` source so a hand-built plan (one that
    would bypass ``DifferencingGuard.prepare``) is caught in review. The behaviour is covered by
    ``test_forged_plan_is_refused_before_execute``."""
    source = inspect.getsource(run_sql_mod)
    assert "DifferencingPlan(" not in source
    assert "applies=False" not in source and "applies = False" not in source


@pytest.mark.parametrize("forgery", ["not_a_plan", "other_scope", "not_scoped", "other_brands"])
def test_forged_plan_is_refused_before_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forgery: str
) -> None:
    tool, client, guard, events = instrument(tmp_path, monkeypatch)
    real_prepare = guard.inner.prepare

    def forge(result: Any, scope: Any, **kw: Any) -> Any:
        if forgery == "not_a_plan":  # garbage from the guard: nothing was issued
            return {"query": result.query, "applies": False}
        plan = real_prepare(result, scope, **kw)
        # The other forgeries corrupt the *issued* plan in place (a guard bug), so the adapter
        # holds the guard's own object and must abandon it before refusing (L-a).
        if forgery == "other_scope":
            other = ProductScope.for_brands(["Other"])
            query: Any = replace(plan.query, scope_key=other.scope_key)
        elif forgery == "other_brands":  # right scope_key, different brands (L-2)
            params = tuple(replace(p, values=("Other",)) for p in plan.query.parameters)
            assert params != plan.query.parameters
            query = replace(plan.query, parameters=params)
        else:
            query = "SELECT 1"
        object.__setattr__(plan, "query", query)
        assert guard.inner.open_plan_count() == 1
        return plan

    monkeypatch.setattr(guard.inner, "prepare", forge)
    out = call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False and out["error"]["code"] == "SQL_POLICY"
    assert client.calls == [] and "diff.release" not in events
    assert guard.open_plan_count() == 0  # rule (e): the refused plan is closed (L-a)


# --- (c) the population comes from the executed population query --------------------------------


def test_population_comes_only_from_the_executed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def responder(sql: str) -> list[dict[str, Any]]:
        return [{"population": 237}] if "AS population" in sql else [{"n": 237}]

    tool, client, guard, _ = instrument(tmp_path, monkeypatch, RoutingClient(responder))
    # The model cannot supply it: an extra argument is refused before anything runs.
    out = tool.run(
        {"sql": total("u.age >= 30"), "purpose": "x", "population": 10_000},
        new_session(),
        RunSqlTurn("turn-1"),
    )
    assert out["error"]["code"] == INVALID_ARGS and guard.prepared == []
    assert client.calls == []

    out = call(tool, total("u.age >= 30"), new_session(), RunSqlTurn("turn-2"))
    assert out["ok"] is True
    ((_, kw),) = guard.prepared
    assert kw["population"] == 237  # from the fake BigQuery rows, nowhere else
    assert "AS population" in client.executed[0]["sql"]


def test_population_below_k_never_reaches_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, client, guard, _ = instrument(
        tmp_path,
        monkeypatch,
        RoutingClient(lambda sql: [{"population": 3}] if "AS population" in sql else [{"n": 3}]),
    )
    out = call(tool, total("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False and out["error"]["rule"] == "small_cell_unplaceable"
    assert guard.prepared == [] and len(client.executed) == 1


def test_non_population_paths_pass_no_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, _, guard, _ = instrument(tmp_path, monkeypatch)
    assert call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))["ok"]
    ((_, kw),) = guard.prepared
    assert "population" not in kw


# --- (d) a memo hit still calls release ---------------------------------------------------------


def test_memo_hit_still_calls_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, client, guard, events = instrument(tmp_path, monkeypatch)
    session = new_session()
    assert call(tool, by_state("u.age >= 30"), session, RunSqlTurn("turn-1"))["ok"]
    events.clear()
    out = call(tool, by_state("u.age >= 30"), session, RunSqlTurn("turn-2"))
    assert out["ok"] is True and out["meta"]["cache_hit"] is True
    assert events == [
        "apply_scope",
        "apply_small_cell",
        "diff.prepare",
        "verify_scoped",
        "diff.release",  # no dry run, no execute, but release still runs
        "scrub",
    ]
    assert len(client.executed) == 1 and guard.released[-1] is guard.prepared[-1][0]


def test_memo_hit_refused_by_release_returns_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, _, guard, _ = instrument(tmp_path, monkeypatch)
    session = new_session()
    assert call(tool, by_state("u.age >= 30"), session, RunSqlTurn("turn-1"))["ok"]
    calls = itertools.count()

    def refuse(plan: Any, rows: Any, **k: Any) -> ScopeRefusal:
        next(calls)
        from opsfleet_agent.guards import differencing

        return differencing._unavailable()

    monkeypatch.setattr(guard.inner, "release", refuse)
    out = call(tool, by_state("u.age >= 30"), session, RunSqlTurn("turn-2"))
    assert out["ok"] is False and out["error"]["rule"] == "differencing"
    assert "SYNTH" not in str(out) and next(calls) == 1
    assert guard.open_plan_count() == 0  # the unredeemed plan was abandoned
