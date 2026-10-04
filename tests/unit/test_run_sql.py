"""Iteration 13: ``run_sql`` behaviour and red-team cases (HLD §4.4, §5.1-§5.5).

Fakes only, no network: BigQuery is :class:`RoutingClient` (rows chosen per statement) and the
differencing guard runs on a real SQLite fingerprint store under ``tmp_path``. All data is
synthetic. ``SENTINEL`` stands in for a provider message or a value that must never reach the
model, the trace or the audit record.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from google.api_core import exceptions as gexc

from opsfleet_agent.bq.client import BigQueryRunner
from opsfleet_agent.bq.errors import ErrorCode
from opsfleet_agent.bq.memo import sql_hash
from opsfleet_agent.graph.budget import TurnBudget, TurnKind
from opsfleet_agent.guards.differencing import (
    CAPACITY_HINT,
    CELL_COUNT_COLUMN,
    ROW_COUNT_COLUMN,
    DifferencingGuard,
)
from opsfleet_agent.guards.scope import ProductScope, ScopeInvariantError
from opsfleet_agent.obs.tracer import Tracer
from opsfleet_agent.store import fingerprints as fingerprints_mod
from opsfleet_agent.store.db import open_store
from opsfleet_agent.store.fingerprints import FingerprintStore, FingerprintStoreError
from opsfleet_agent.tools import registry
from opsfleet_agent.tools import run_sql as run_sql_mod
from opsfleet_agent.tools.registry import RUN_SQL, RUN_SQL_ENABLED, TOOLS_BY_ROLE, tools_for
from opsfleet_agent.tools.run_sql import (
    DUPLICATE_QUERY,
    EMPTY_HINT_FINAL,
    EMPTY_HINT_FIRST,
    GIVE_UP,
    INVALID_ARGS,
    SQL_POLICY,
    SQL_TOO_LONG,
    TRUNCATED_HINT,
    RunSqlSession,
    RunSqlTool,
    RunSqlTurn,
    default_lock_timeout_s,
    scoped_job_config_factory,
    worst_case_call_s,
)
from tests.unit.test_bq_client import PROJECT, SENTINEL, FakeClient

ACME = ProductScope.for_brands(["Acme"])
REFRESH = date(2026, 9, 30)
T0 = 1_900_000_000


def t(name: str) -> str:
    return f"`bigquery-public-data.thelook_ecommerce.{name}`"


U, OI, P, ORD = t("users"), t("order_items"), t("products"), t("orders")
# QI group-by: small-cell rewrite (HAVING) + differencing instrumentation (_cell_customers).
BY_STATE = (
    f"SELECT u.state, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
    f"JOIN {OI} AS oi ON oi.user_id = u.id WHERE {{where}} GROUP BY u.state"
)
# QI group-by with a COUNT(*) measure: the guard injects _cell_customers *and* _cell_rows.
BY_STATE_ROWS = (
    f"SELECT u.state, COUNT(*) AS n FROM {U} AS u "
    f"JOIN {OI} AS oi ON oi.user_id = u.id WHERE {{where}} GROUP BY u.state"
)
# QI-filtered total: population check path.
TOTAL = f"SELECT COUNT(DISTINCT u.id) AS n FROM {U} AS u WHERE {{where}}"
# No QI: plain scoped statement, differencing does not apply.
SIMPLE = f"SELECT oi.status, COUNT(*) AS n FROM {OI} AS oi GROUP BY oi.status"
SIMPLE2 = f"SELECT oi.status, SUM(oi.sale_price) AS revenue FROM {OI} AS oi GROUP BY oi.status"
NESTED_UNPLACEABLE = (
    f"SELECT MAX(s.n) AS m FROM (SELECT u.state, COUNT(*) AS n FROM {U} u GROUP BY u.state) s"
)


# --- fakes ------------------------------------------------------------------------------------


class Clock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class RoutingClient(FakeClient):
    """FakeClient whose executed rows come from ``responder(sql)``; optional queued errors.

    ``events`` (when set) records ``dry_run`` / ``execute`` in call order.
    """

    def __init__(
        self,
        responder: Callable[[str], list[dict[str, Any]]] | None = None,
        *,
        exec_errors: list[BaseException] | None = None,
        on_execute: Callable[[str], None] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.responder = responder
        self.exec_errors = list(exec_errors or [])
        self.on_execute = on_execute
        self.events: list[str] | None = None

    def query(self, query, job_config=None, **kwargs):
        dry = bool(job_config.dry_run)
        if self.events is not None:
            self.events.append("dry_run" if dry else "execute")
        if not dry:
            if self.on_execute is not None:
                self.on_execute(query)
            if self.exec_errors:
                self.calls.append({"sql": query, "job_config": job_config, **kwargs})
                raise self.exec_errors.pop(0)
            if self.responder is not None:
                self.rows = self.responder(query)
        return super().query(query, job_config, **kwargs)


def default_responder(sql: str) -> list[dict[str, Any]]:
    """Synthetic rows shaped like the statement: population, instrumented cells or plain."""
    if "AS population" in sql:
        return [{"population": 200}]
    if CELL_COUNT_COLUMN in sql:
        rows: list[dict[str, Any]] = [
            {"state": "SYNTH-A", "n": 120, CELL_COUNT_COLUMN: 120},
            {"state": "SYNTH-B", "n": 80, CELL_COUNT_COLUMN: 80},
        ]
        if ROW_COUNT_COLUMN in sql:
            for row, items in zip(rows, (300, 210), strict=True):
                row[ROW_COUNT_COLUMN] = items
        return rows
    return [{"status": "Complete", "n": 3}]


def make_store(tmp_path: Path, name: str = "app.db") -> FingerprintStore:
    return FingerprintStore(open_store(tmp_path / name), clock=Clock())


def make_tool(
    tmp_path: Path,
    client: FakeClient | None = None,
    *,
    guard: Any = None,
    factory: Callable[..., Any] | None = None,
    row_cap: int = 200,
    **kw: Any,
) -> tuple[RunSqlTool, FakeClient]:
    client = client if client is not None else RoutingClient(default_responder)
    runner = BigQueryRunner(
        client,
        PROJECT,
        row_cap=row_cap,
        job_config_factory=factory if factory is not None else scoped_job_config_factory(),
    )
    if guard is None:
        guard = DifferencingGuard(make_store(tmp_path))
    kw.setdefault("refresh_date", lambda: REFRESH)
    kw.setdefault("sleep", lambda s: None)
    refresh = kw.pop("refresh_date")
    return RunSqlTool(runner, guard, refresh, **kw), client


def new_session(scope: ProductScope | None = ACME) -> RunSqlSession:
    return RunSqlSession(user_id="user-1", session_id="session-1", scope=scope)


def call(tool: RunSqlTool, sql: str, session: RunSqlSession, turn: RunSqlTurn) -> dict[str, Any]:
    return tool.run({"sql": sql, "purpose": "synthetic test"}, session, turn)


def by_state(*preds: str) -> str:
    return BY_STATE.format(where=" AND ".join(preds))


def total(*preds: str) -> str:
    return TOTAL.format(where=" AND ".join(preds))


# --- named tests --------------------------------------------------------------------------------


def test_large_result_truncation_flagged(tmp_path: Path) -> None:
    rows = [{"status": f"SYNTH-{i}", "n": i} for i in range(250)]
    tool, client = make_tool(tmp_path, RoutingClient(lambda sql: rows))
    out = call(tool, SIMPLE, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True
    data = out["data"]
    assert data["row_count"] == 200 and len(data["rows"]) == 200
    assert data["truncated"] is True
    assert data["hint"] == TRUNCATED_HINT
    # Fetch is capped at the source: at most row_cap + 1 rows were requested.
    assert client.jobs[0].result_kwargs["max_results"] == 201

    # The tool's own 200-row cap holds even when the runner allows more.
    tool, _ = make_tool(tmp_path, RoutingClient(lambda sql: rows), row_cap=400)
    out = call(tool, SIMPLE, new_session(), RunSqlTurn("turn-2"))
    assert out["data"]["row_count"] == 200 and out["data"]["truncated"] is True

    # A small result is not flagged.
    tool, _ = make_tool(tmp_path, RoutingClient(lambda sql: rows[:5]))
    out = call(tool, SIMPLE, new_session(), RunSqlTurn("turn-3"))
    assert out["data"]["truncated"] is False and "hint" not in out["data"]


def test_empty_result_handling(tmp_path: Path) -> None:
    tool, _ = make_tool(tmp_path, RoutingClient(lambda sql: []))
    session, turn = new_session(), RunSqlTurn("turn-1")
    first = call(tool, SIMPLE, session, turn)
    assert first["ok"] is True
    assert first["data"]["rows"] == [] and first["data"]["row_count"] == 0
    assert first["data"]["truncated"] is False
    assert first["data"]["hint"] == EMPTY_HINT_FIRST  # diagnostic: filters, window, spelling
    second = call(tool, SIMPLE2, session, turn)
    assert second["data"]["hint"] == EMPTY_HINT_FINAL  # then: answer "no rows matched"
    assert turn.consecutive_failures == 0  # an empty result is a success, not a failure


# --- envelope, ledger, parameters ---------------------------------------------------------------


def test_success_envelope_ledger_and_scope_parameters(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path)
    session, turn = new_session(), RunSqlTurn("turn-1")
    out = call(tool, by_state("u.age >= 30"), session, turn)
    assert out["ok"] is True, out
    data = out["data"]
    assert data["columns"] == ["state", "n"]  # _cell_customers never reaches the model
    assert all(CELL_COUNT_COLUMN not in r for r in data["rows"])
    assert data["suppressed_groups"] == "groups with fewer than 5 customers are hidden"
    assert data["scope_label"] == "brands: Acme"
    assert data["bytes_estimated"] == 1000 and data["bytes_billed"] == 1000
    assert data["query_id"] and out["meta"] == {
        "tool": "run_sql",
        "duration_ms": out["meta"]["duration_ms"],
        "cache_hit": False,
    }
    # @scope_brands is set on the dry run AND the real run.
    assert len(client.calls) == 2
    for c in client.calls:
        params = c["job_config"].query_parameters
        assert [(p.name, list(p.values)) for p in params] == [("scope_brands", ["Acme"])]
    # Execute config keeps the caps from the runner.
    exec_cfg = client.executed[0]["job_config"]
    assert exec_cfg.maximum_bytes_billed and int(exec_cfg.job_timeout_ms) == 60_000
    assert exec_cfg.labels["tool"] == "run_sql"
    # The ledger holds the scoped SQL (FR: SQL ledger), not the model's text, and before the
    # differencing injection (L-c); the hash of the statement that ran is kept apart.
    assert len(turn.ledger) == 1
    entry = turn.ledger[0]
    executed = client.executed[0]["sql"]
    assert "@scope_brands" in entry["sql"] and "__u" in entry["sql"]
    assert CELL_COUNT_COLUMN in executed and "_cell_" not in entry["sql"]
    assert entry["sql_hash"] == sql_hash(entry["sql"])
    assert entry["executed_sql_hash"] == sql_hash(executed) != entry["sql_hash"]
    assert entry["query_id"] == data["query_id"] and entry["rows"] == 2


def test_cells_and_column_names_are_scrubbed_and_json_safe(tmp_path: Path) -> None:
    email = "synthetic.person@example.test"
    rows = [
        {
            "status": f"contact {email}",
            "n": Decimal("1.50"),
            "day": date(2026, 1, 2),
            "blob": b"\x00\x01",
            "nested": [{"k": email}],
            "inf": float("inf"),
        }
    ]
    tool, _ = make_tool(tmp_path, RoutingClient(lambda sql: rows))
    out = call(tool, SIMPLE, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True
    text = json.dumps(out)  # serialisable
    assert email not in text
    row = out["data"]["rows"][0]
    assert row["n"] == 1.5 and row["day"] == "2026-01-02" and row["blob"] == "[binary]"
    assert row["inf"] is None


def test_memo_hit_next_turn_bills_nothing(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path)
    session = new_session()
    first = call(tool, SIMPLE, session, RunSqlTurn("turn-1"))
    second = call(tool, SIMPLE, session, RunSqlTurn("turn-2"))
    assert first["ok"] and second["ok"]
    assert second["meta"]["cache_hit"] is True
    assert second["data"]["bytes_billed"] == 0 and second["data"]["bytes_estimated"] == 0
    assert second["data"]["rows"] == first["data"]["rows"]
    assert len(client.calls) == 2  # one dry run + one execute, both from the first turn


def test_refresh_date_failure_bypasses_memo_but_still_guards(tmp_path: Path) -> None:
    def broken() -> date:
        raise RuntimeError("no refresh date")

    tool, client = make_tool(tmp_path, refresh_date=broken)
    session = new_session()
    assert call(tool, by_state("u.age >= 30"), session, RunSqlTurn("turn-1"))["ok"]
    assert call(tool, by_state("u.age >= 30"), session, RunSqlTurn("turn-2"))["ok"]
    assert len(client.executed) == 2  # no memo: executed again, released again


# --- red team: nothing reaches execute ----------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT u.email FROM {U} AS u",  # PII column
        f"SELECT * FROM {U}",  # star over PII
        "SELECT * FROM `other-project.dataset.secrets`",  # outside the dataset
        "SELECT table_name FROM `bigquery-public-data.thelook_ecommerce.INFORMATION_SCHEMA.TABLES`",
        f"DELETE FROM {ORD} WHERE TRUE",  # DML
        f"SELECT 1; SELECT * FROM {U}",  # two statements
        f"WITH __p AS (SELECT * FROM {P}) SELECT p.brand FROM __p AS p",  # forged scope CTE
        f"SELECT o.status FROM {ORD} AS o WHERE @scope_brands IS NOT NULL",  # forged parameter
        f"SELECT p.name FROM {P} AS p WHERE p.brand = 'Other' OR TRUE",  # scope bypass by filter
    ],
)
def test_scope_and_policy_bypass_never_executes(tmp_path: Path, sql: str) -> None:
    tool, client = make_tool(tmp_path)
    out = call(tool, sql, new_session(), RunSqlTurn("turn-1"))
    if out["ok"]:
        # The only acceptable success is a fully scoped statement.
        executed = client.executed[0]["sql"]
        assert "@scope_brands" in executed and "__p" in executed
        assert client.executed[0]["job_config"].query_parameters
    else:
        assert out["error"]["code"] == SQL_POLICY
        assert client.calls == []


def test_empty_scope_is_refused_without_any_call(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path)
    out = call(tool, SIMPLE, new_session(scope=None), RunSqlTurn("turn-1"))
    assert out["ok"] is False
    assert out["error"]["code"] == SQL_POLICY and out["error"]["rule"] == "empty_scope"
    assert out["error"]["retryable"] is False
    assert client.calls == []


def test_refused_small_cell_never_executes(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path)
    out = call(tool, NESTED_UNPLACEABLE, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False
    assert out["error"]["code"] == SQL_POLICY
    assert out["error"]["rule"] == "small_cell_unplaceable"
    assert out["error"]["retryable"] is True
    assert client.calls == []


def test_population_check_path(tmp_path: Path) -> None:
    seen: list[str] = []

    def responder(sql: str) -> list[dict[str, Any]]:
        seen.append(sql)
        return [{"population": 237}] if "AS population" in sql else [{"n": 237}]

    tool, client = make_tool(tmp_path, RoutingClient(responder))
    budget = TurnBudget(TurnKind.QA)
    turn = RunSqlTurn("turn-1", sql_counter=budget)
    out = call(tool, total("u.age >= 30"), new_session(), turn)
    assert out["ok"] is True, out
    assert out["data"]["rows"] == [{"n": 237}]
    # Two statements, population first, both dry-run, capped and parameterised.
    assert len(seen) == 2 and "AS population" in seen[0] and "AS population" not in seen[1]
    assert [c["job_config"].dry_run for c in client.calls] == [True, None, True, None]
    assert all(c["job_config"].query_parameters for c in client.calls)
    assert all(c["job_config"].maximum_bytes_billed for c in client.executed)
    assert budget.sql_queries == 2  # the population query counts in the SQL budget
    assert len(turn.ledger) == 1  # the ledger records the answer query only


@pytest.mark.parametrize(
    "population_rows",
    [
        [{"population": 3}],  # below k
        [{"population": True}],  # bool is not a count
        [{"population": "500"}],  # not an int
        [{"other": 500}],  # missing column
        [{"population": 500, "other": 1}],  # extra column (L-1)
        [{"other": 1, "population": 500}],  # extra column first
        [{"population": 500}, {"population": 500}],  # not exactly one row
        [],
    ],
)
def test_population_check_refusal_never_runs_the_answer(
    tmp_path: Path, population_rows: list[dict[str, Any]]
) -> None:
    tool, client = make_tool(
        tmp_path,
        RoutingClient(lambda sql: population_rows if "AS population" in sql else [{"n": 1}]),
    )
    out = call(tool, total("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False
    assert out["error"]["code"] == SQL_POLICY
    assert out["error"]["rule"] in {"small_cell_unplaceable", "rewrite_invariant"}
    assert len(client.executed) == 1 and "AS population" in client.executed[0]["sql"]


def test_population_near_pair_refused_before_answer_executes(tmp_path: Path) -> None:
    pops = iter([200, 197])
    answers: list[str] = []

    def responder(sql: str) -> list[dict[str, Any]]:
        if "AS population" in sql:
            return [{"population": next(pops)}]
        answers.append(sql)
        return [{"n": 1}]

    tool, _ = make_tool(tmp_path, RoutingClient(responder))
    session, turn = new_session(), RunSqlTurn("turn-1")
    assert call(tool, total("u.age >= 30"), session, turn)["ok"]
    out = call(tool, total("u.age >= 30", "u.gender = 'M'"), session, turn)
    assert out["ok"] is False and out["error"]["rule"] == "differencing"
    assert len(answers) == 1  # the second answer statement never ran


def test_differencing_refusal_after_execute_returns_no_rows(tmp_path: Path) -> None:
    cells = iter([{"SYNTH-A": 120}, {"SYNTH-A": 117}])

    def responder(sql: str) -> list[dict[str, Any]]:
        return [{"state": s, "n": n, CELL_COUNT_COLUMN: n} for s, n in next(cells).items()]

    tool, client = make_tool(tmp_path, RoutingClient(responder))
    session, turn = new_session(), RunSqlTurn("turn-1")
    assert call(tool, by_state("u.age >= 30"), session, turn)["ok"]
    out = call(tool, by_state("u.age >= 31"), session, turn)
    assert out["ok"] is False and out["error"]["rule"] == "differencing"
    assert "rows" not in json.dumps(out) and "117" not in json.dumps(out)
    assert len(turn.ledger) == 1


def test_store_missing_fails_closed(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path, guard=DifferencingGuard(None))
    out = call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False
    assert out["error"]["code"] == SQL_POLICY and out["error"]["rule"] == "differencing"
    assert out["error"]["retryable"] is False  # "unavailable": do not loop on it
    assert client.calls == []


def test_store_errors_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)

    def boom(*a: Any, **k: Any) -> Any:
        raise FingerprintStoreError("synthetic store failure")

    # Pre-execution read fails: nothing runs.
    monkeypatch.setattr(store, "candidates", boom)
    tool, client = make_tool(tmp_path, guard=DifferencingGuard(store))
    out = call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False and out["error"]["rule"] == "differencing"
    assert client.calls == []

    # Post-execution record fails: the query ran but no row reaches the model.
    store2 = make_store(tmp_path, "app2.db")
    monkeypatch.setattr(store2, "check_and_record", boom)
    tool, client = make_tool(tmp_path, guard=DifferencingGuard(store2))
    turn = RunSqlTurn("turn-1")
    out = call(tool, by_state("u.age >= 30"), new_session(), turn)
    assert out["ok"] is False and out["error"]["rule"] == "differencing"
    assert len(client.executed) == 1
    assert "SYNTH-A" not in json.dumps(out) and turn.ledger == []


def test_guard_exception_fails_closed(tmp_path: Path) -> None:
    class Broken:
        def prepare(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError(SENTINEL)

        def release(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
            raise AssertionError("must not be reached")

    tool, client = make_tool(tmp_path, guard=Broken())
    out = call(tool, SIMPLE, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False and out["error"]["rule"] == "rewrite_invariant"
    assert SENTINEL not in json.dumps(out)
    assert client.calls == []


def test_missing_parameter_injector_fails_closed(tmp_path: Path) -> None:
    from opsfleet_agent.bq.client import _job_config

    tool, client = make_tool(tmp_path, factory=_job_config)
    out = call(tool, SIMPLE, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False and out["error"]["rule"] == "rewrite_invariant"
    assert client.calls == []
    # The context variable never leaks out of a call.
    assert run_sql_mod._PARAMS.get() is None


# --- BigQuery errors: codes only ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("where", "exc", "code"),
    [
        ("dry", gexc.BadRequest(f"Syntax error: Unexpected {SENTINEL} at [1:8]"), "SQL_SYNTAX"),
        ("dry", gexc.BadRequest(f"Unrecognized name: {SENTINEL} at [1:8]"), "UNKNOWN_COLUMN"),
        ("exec", gexc.BadRequest(f"Division by zero: {SENTINEL}"), "BQ_RUNTIME"),
        ("exec", gexc.Forbidden(f"Quota exceeded {SENTINEL}"), None),
    ],
)
def test_bq_errors_mapped_without_raw_text(
    tmp_path: Path, where: str, exc: BaseException, code: str | None
) -> None:
    audit: list[dict[str, Any]] = []
    tracer = Tracer(tmp_path / "traces", session_id="session-1")
    client = RoutingClient(
        default_responder,
        dry_exc=exc if where == "dry" else None,
        exec_errors=[exc, exc] if where == "exec" else None,
    )
    tool, _ = make_tool(tmp_path, client, tracer=tracer, audit=audit.append)
    out = call(tool, SIMPLE, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False
    if code is not None:
        assert out["error"]["code"] == code
    assert out["error"]["code"] in {c.value for c in ErrorCode}
    assert "identifier" not in out["error"]  # SENTINEL is not in the SQL: never echoed
    trace_text = (tmp_path / "traces" / "session-1.jsonl").read_text()
    for text in (json.dumps(out), trace_text, json.dumps(audit)):
        assert SENTINEL not in text
    assert audit and audit[-1]["code"] == out["error"]["code"]
    assert set(audit[-1]) == {
        "event", "outcome", "code", "rule", "class", "stage", "sql_hash",
        "executed_sql_hash", "turn_id", "session_id", "rows", "bytes_billed",
    }  # fmt: skip
    assert audit[-1]["executed_sql_hash"] is None  # nothing ran to completion


def test_trace_and_audit_hold_no_values(tmp_path: Path) -> None:
    audit: list[dict[str, Any]] = []
    tracer = Tracer(tmp_path / "traces", session_id="session-1")
    tool, _ = make_tool(tmp_path, tracer=tracer, audit=audit.append)
    sql = by_state("u.age >= 30", f"u.traffic_source = '{SENTINEL}'")
    out = call(tool, sql, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True
    spans = [json.loads(line) for line in (tmp_path / "traces" / "session-1.jsonl").open()]
    assert {s["type"] for s in spans} >= {"sql", "tool"}
    text = json.dumps(spans) + json.dumps(audit)
    assert SENTINEL not in text and "SYNTH-A" not in text
    sql_span = next(s for s in spans if s["type"] == "sql")
    assert "@scope_brands" in sql_span["sql_text"]  # the scoped statement, literals stripped
    assert audit[0]["outcome"] == "ok" and audit[0]["rows"] == 2


def test_tracer_and_audit_failures_do_not_break_the_call(tmp_path: Path) -> None:
    class BadTracer:
        def record(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("trace down")

    def bad_audit(record: dict[str, Any]) -> None:
        raise RuntimeError("audit down")

    tool, _ = make_tool(tmp_path, tracer=BadTracer(), audit=bad_audit)
    assert call(tool, SIMPLE, new_session(), RunSqlTurn("turn-1"))["ok"] is True


# --- per-turn rules -----------------------------------------------------------------------------


def test_duplicate_query_in_turn_points_to_earlier_result(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path)
    session, turn = new_session(), RunSqlTurn("turn-1")
    first = call(tool, SIMPLE, session, turn)
    same = "  " + SIMPLE.replace(" ", "\n  ") + " ;"  # equal after whitespace normalising
    again = call(tool, same, session, turn)
    assert again["ok"] is False and again["error"]["code"] == DUPLICATE_QUERY
    assert again["error"]["query_id"] == first["data"]["query_id"]
    assert len(client.calls) == 2 and turn.consecutive_failures == 0


def fingerprint_count(store: FingerprintStore) -> int:
    return int(store.conn.execute("SELECT COUNT(*) FROM aggregate_fingerprint").fetchone()[0])


@pytest.mark.parametrize("sql", [by_state("u.age >= 30"), total("u.age >= 30")])
def test_duplicate_query_records_no_fingerprint(tmp_path: Path, sql: str) -> None:
    """M-2: the duplicate check runs before population, prepare and release."""
    store = make_store(tmp_path)
    calls: list[str] = []

    class Counting(DifferencingGuard):
        def prepare(self, *a: Any, **k: Any) -> Any:
            calls.append("prepare")
            return super().prepare(*a, **k)

        def release(self, *a: Any, **k: Any) -> Any:
            calls.append("release")
            return super().release(*a, **k)

    tool, client = make_tool(tmp_path, guard=Counting(store))
    session, turn = new_session(), RunSqlTurn("turn-1")
    first = call(tool, sql, session, turn)
    assert first["ok"] is True, first
    before = (fingerprint_count(store), len(client.calls), list(calls))
    assert before[0] >= 1
    # Different model text, same scoped statement: still a duplicate.
    again = call(tool, "  " + sql.replace(" ", "\n ") + " ;", session, turn)
    assert again["ok"] is False and again["error"]["code"] == DUPLICATE_QUERY
    assert again["error"]["query_id"] == first["data"]["query_id"]
    assert (fingerprint_count(store), len(client.calls), calls) == before


def test_colliding_scrubbed_column_names_keep_every_value(tmp_path: Path) -> None:
    """L-3: two names that scrub to ``<EMAIL>`` become ``<EMAIL>`` and ``<EMAIL>_2``."""
    a, b = "synthetic.a@example.test", "synthetic.b@example.test"
    rows = [{a: 1, b: 2, "status": "Complete"}]
    tool, _ = make_tool(tmp_path, RoutingClient(lambda sql: rows))
    out = call(tool, SIMPLE, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True, out
    assert out["data"]["columns"] == ["<EMAIL>", "<EMAIL>_2", "status"]
    assert out["data"]["rows"] == [{"<EMAIL>": 1, "<EMAIL>_2": 2, "status": "Complete"}]
    assert a not in json.dumps(out) and b not in json.dumps(out)


def test_model_alias_of_count_column_is_kept_when_nothing_injected(tmp_path: Path) -> None:
    """L-4: only a column the plan injected is stripped; a model's own alias stays."""
    sql = f"SELECT oi.status, COUNT(*) AS {CELL_COUNT_COLUMN} FROM {OI} AS oi GROUP BY oi.status"
    rows = [{"status": "Complete", CELL_COUNT_COLUMN: 7}]
    tool, _ = make_tool(tmp_path, RoutingClient(lambda s: rows))
    out = call(tool, sql, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True, out
    assert out["data"]["columns"] == ["status", CELL_COUNT_COLUMN]
    assert out["data"]["rows"] == [{"status": "Complete", CELL_COUNT_COLUMN: 7}]


def test_injected_count_column_is_stripped_from_columns_and_rows(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path)
    out = call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True, out
    assert CELL_COUNT_COLUMN in client.executed[0]["sql"]  # it was injected
    assert out["data"]["columns"] == ["state", "n"]
    assert all(CELL_COUNT_COLUMN not in row for row in out["data"]["rows"])


def test_every_answered_call_releases_its_plan_once(tmp_path: Path) -> None:
    """Miss, memo hit (fresh prepare + release) and population path leave no open plan."""
    guard = DifferencingGuard(make_store(tmp_path))
    tool, client = make_tool(tmp_path, guard=guard)
    session = new_session()
    for turn_id, sql in (("turn-1", by_state("u.age >= 30")), ("turn-2", by_state("u.age >= 30"))):
        out = call(tool, sql, session, RunSqlTurn(turn_id))
        assert out["ok"] is True, out
        assert guard.open_plan_count() == 0
    assert out["meta"]["cache_hit"] is True
    out = call(tool, total("u.age >= 30"), session, RunSqlTurn("turn-3"))
    assert out["ok"] is True, out
    assert guard.open_plan_count() == 0
    assert all(CELL_COUNT_COLUMN not in r for r in out["data"]["rows"])


def _raise_invariant(*a: Any, **k: Any) -> None:
    raise ScopeInvariantError("synthetic invariant failure")


def _raise_runtime(*a: Any, **k: Any) -> Any:
    raise RuntimeError(SENTINEL)


@pytest.mark.parametrize(
    ("client_kw", "patch"),
    [
        ({"dry_exc": gexc.BadRequest("synthetic dry-run failure")}, None),
        ({"estimate": 10**15}, None),  # dry run over the per-query cap
        ({"exec_errors": [gexc.BadRequest("synthetic runtime failure")]}, None),
        ({"exec_errors": [TimeoutError()]}, None),
        ({"exec_errors": [gexc.ServiceUnavailable("x"), gexc.ServiceUnavailable("y")]}, None),
        ({}, ("verify_scoped", _raise_invariant)),
        ({}, ("_execute_and_release", _raise_runtime)),
        ({}, ("factory", None)),  # runner without the parameter injector
    ],
    ids=[
        "dry_run",
        "over_cap",
        "execute",
        "timeout",
        "unavailable",
        "invariant",
        "exception",
        "no_injector",
    ],  # fmt: skip
)
def test_failure_after_prepare_closes_its_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client_kw: dict[str, Any],
    patch: tuple[str, Any] | None,
) -> None:
    """Rule (e): every path after prepare that never reaches release abandons the plan. It
    records nothing and does not block the next call."""
    from opsfleet_agent.bq.client import _job_config

    store = make_store(tmp_path)
    guard = DifferencingGuard(store)
    kw: dict[str, Any] = {}
    if patch is not None and patch[0] == "factory":
        kw["factory"] = _job_config
    tool, _ = make_tool(tmp_path, RoutingClient(default_responder, **client_kw), guard=guard, **kw)
    if patch is not None and patch[0] == "verify_scoped":
        monkeypatch.setattr(run_sql_mod, "verify_scoped", patch[1])
    elif patch is not None and patch[0] == "_execute_and_release":
        monkeypatch.setattr(tool, "_execute_and_release", patch[1])
    session = new_session()
    out = call(tool, by_state("u.age >= 30"), session, RunSqlTurn("turn-1"))
    assert out["ok"] is False
    assert SENTINEL not in json.dumps(out)
    assert guard.open_plan_count() == 0 and fingerprint_count(store) == 0
    monkeypatch.undo()
    tool2, _ = make_tool(tmp_path, guard=guard)
    out = call(tool2, by_state("u.age >= 30"), session, RunSqlTurn("turn-2"))
    assert out["ok"] is True, out
    assert guard.open_plan_count() == 0 and fingerprint_count(store) == 1


def test_refusals_after_prepare_leave_no_open_plan(tmp_path: Path) -> None:
    """A release refusal and a later in-turn duplicate leave the open-plan count at zero."""
    cells = iter([{"SYNTH-A": 120}, {"SYNTH-A": 117}])

    def responder(sql: str) -> list[dict[str, Any]]:
        return [{"state": s, "n": n, CELL_COUNT_COLUMN: n} for s, n in next(cells).items()]

    guard = DifferencingGuard(make_store(tmp_path))
    tool, _ = make_tool(tmp_path, RoutingClient(responder), guard=guard)
    session, turn = new_session(), RunSqlTurn("turn-1")
    assert call(tool, by_state("u.age >= 30"), session, turn)["ok"]
    out = call(tool, by_state("u.age >= 31"), session, turn)
    assert out["ok"] is False and out["error"]["rule"] == "differencing"
    assert guard.open_plan_count() == 0
    out = call(tool, by_state("u.age >= 30"), session, turn)
    assert out["ok"] is False and out["error"]["code"] == DUPLICATE_QUERY
    assert guard.open_plan_count() == 0


def test_memo_duplicate_in_turn_closes_its_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the memo holds the statement from this turn (the statement-key pre-check bypassed),
    the call is refused *before* release (L-b): no fingerprint is recorded, no plan stays
    open and no row reaches the model."""
    store = make_store(tmp_path)
    releases: list[Any] = []

    class Counting(DifferencingGuard):
        def release(self, *a: Any, **k: Any) -> Any:
            releases.append(a[0])
            return super().release(*a, **k)

    guard = Counting(store)
    tool, client = make_tool(tmp_path, guard=guard)
    session, turn = new_session(), RunSqlTurn("turn-1")
    assert call(tool, by_state("u.age >= 30"), session, turn)["ok"]
    # Force the second call past both pre-checks (model text, statement key) into the memo,
    # as after a call that stored its rows and then failed before accounting.
    turn.statements.clear()
    turn.seen.clear()
    before = (fingerprint_count(store), len(releases), len(client.calls))
    assert before[0] == 1
    out = call(tool, by_state("u.age >= 30"), session, turn)
    assert out["ok"] is False and out["error"]["code"] == DUPLICATE_QUERY
    assert "SYNTH" not in json.dumps(out)
    assert (fingerprint_count(store), len(releases), len(client.calls)) == before
    assert guard.open_plan_count() == 0


def test_injected_row_count_column_never_reaches_model_memo_key_or_trace(
    tmp_path: Path,
) -> None:
    """Item 1: ``plan.injected_columns`` (``_cell_customers`` and ``_cell_rows``) are stripped
    from columns and rows; the trace gets the pre-injection statement."""
    tracer = Tracer(tmp_path / "traces", session_id="session-1")
    guard = DifferencingGuard(make_store(tmp_path))
    tool, client = make_tool(tmp_path, guard=guard, tracer=tracer)
    out = call(tool, BY_STATE_ROWS.format(where="u.age >= 30"), new_session(), RunSqlTurn("t1"))
    assert out["ok"] is True, out
    executed = client.executed[0]["sql"]
    assert CELL_COUNT_COLUMN in executed and ROW_COUNT_COLUMN in executed  # both injected
    assert out["data"]["columns"] == ["state", "n"]
    assert out["data"]["rows"] == [{"state": "SYNTH-A", "n": 120}, {"state": "SYNTH-B", "n": 80}]
    text = json.dumps(out)
    assert CELL_COUNT_COLUMN not in text and ROW_COUNT_COLUMN not in text
    raw = (tmp_path / "traces" / "session-1.jsonl").read_text()
    assert "_cell_" not in raw and "300" not in raw
    assert guard.open_plan_count() == 0


def test_ledger_holds_pre_injection_sql_and_a_separate_executed_hash(tmp_path: Path) -> None:
    """L-c: the ledger ``sql`` (shown to the writer and verifier) is the statement before the
    differencing injection, the same text the trace gets; ``sql_hash`` hashes that text, so it
    is stable whatever the guard injects. ``executed_sql_hash`` (ledger and audit) is the hash
    of the statement that ran."""
    audit: list[dict[str, Any]] = []
    tracer = Tracer(tmp_path / "traces", session_id="session-1")
    tool, client = make_tool(tmp_path, tracer=tracer, audit=audit.append)
    sql = BY_STATE_ROWS.format(where="u.age >= 30")
    session = new_session()
    t1, t2 = RunSqlTurn("turn-1"), RunSqlTurn("turn-2")
    assert call(tool, sql, session, t1)["ok"] is True
    assert call(tool, sql, session, t2)["ok"] is True  # a memo hit in a new turn
    executed = client.executed[0]["sql"]
    (e1,), (e2,) = t1.ledger, t2.ledger
    for entry in (e1, e2):
        assert CELL_COUNT_COLUMN not in entry["sql"] and ROW_COUNT_COLUMN not in entry["sql"]
        assert entry["sql_hash"] == sql_hash(entry["sql"])
        assert entry["executed_sql_hash"] == sql_hash(executed)
    assert e1["sql"] == e2["sql"] and e1["sql_hash"] == e2["sql_hash"]
    ok_records = [r for r in audit if r["outcome"] == "ok"]
    assert len(ok_records) == 2
    for record in ok_records:
        assert record["sql_hash"] == e1["sql_hash"]
        assert record["executed_sql_hash"] == sql_hash(executed) != record["sql_hash"]


def test_memo_hit_strips_injected_columns_and_still_goes_through_release(
    tmp_path: Path,
) -> None:
    """Item 4: the memo stores the executed rows *with* the injected columns; a hit returns
    rows only through ``release`` (which records a fingerprint check) and strips them."""
    store = make_store(tmp_path)
    releases: list[Any] = []

    class Counting(DifferencingGuard):
        def release(self, plan: Any, rows: Any, **k: Any) -> Any:
            releases.append([dict(r) for r in rows])
            return super().release(plan, rows, **k)

    guard = Counting(store)
    tool, client = make_tool(tmp_path, guard=guard)
    session = new_session()
    sql = BY_STATE_ROWS.format(where="u.age >= 30")
    first = call(tool, sql, session, RunSqlTurn("turn-1"))
    hit = call(tool, sql, session, RunSqlTurn("turn-2"))
    assert first["ok"] is True and hit["ok"] is True, (first, hit)
    assert hit["meta"]["cache_hit"] is True and len(client.executed) == 1
    # the memo holds the raw rows (with both counts), and release saw them on the hit too
    (cached,) = [e.value for e in session.memo._data.values()]
    assert all(CELL_COUNT_COLUMN in r and ROW_COUNT_COLUMN in r for r in cached.rows)
    assert len(releases) == 2 and all(ROW_COUNT_COLUMN in r for r in releases[1])
    for out in (first, hit):
        assert out["data"]["columns"] == ["state", "n"]
        assert CELL_COUNT_COLUMN not in json.dumps(out) and ROW_COUNT_COLUMN not in json.dumps(out)
    assert guard.open_plan_count() == 0


def test_memo_hit_cannot_bypass_a_differencing_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 4: a hit whose release refuses returns no rows, even though the memo has them."""
    store = make_store(tmp_path)
    guard = DifferencingGuard(store)
    tool, client = make_tool(tmp_path, guard=guard)
    session = new_session()
    sql = BY_STATE_ROWS.format(where="u.age >= 30")
    assert call(tool, sql, session, RunSqlTurn("turn-1"))["ok"]

    def refuse(*a: Any, **k: Any) -> Any:
        return None  # "near pair": the guard refuses the release

    monkeypatch.setattr(store, "check_and_record", refuse)
    out = call(tool, sql, session, RunSqlTurn("turn-2"))
    assert out["ok"] is False and out["error"]["rule"] == "differencing"
    assert "SYNTH" not in json.dumps(out) and len(client.executed) == 1
    assert guard.open_plan_count() == 0


def test_capacity_refusal_keeps_its_cause_and_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule (f): the guard's capacity refusal reaches the model with ``CAPACITY_HINT``,
    not retryable, and nothing executes."""
    store = make_store(tmp_path)
    guard = DifferencingGuard(store)
    tool, client = make_tool(tmp_path, guard=guard)
    session = new_session()
    assert call(tool, by_state("u.age >= 30"), session, RunSqlTurn("turn-1"))["ok"]
    assert call(tool, by_state("u.age >= 40"), session, RunSqlTurn("turn-2"))["ok"]
    monkeypatch.setattr(fingerprints_mod, "MAX_CANDIDATES", 1)
    before = len(client.calls)
    out = call(tool, by_state("u.age >= 31"), session, RunSqlTurn("turn-3"))
    assert out["ok"] is False and out["error"]["rule"] == "differencing"
    assert out["error"]["hint"] == CAPACITY_HINT and out["error"]["retryable"] is False
    assert len(client.calls) == before and guard.open_plan_count() == 0


def test_release_exception_is_an_invariant_failure_not_unavailable(tmp_path: Path) -> None:
    """Rule (f): run_sql never builds an "unavailable" refusal itself."""
    store = make_store(tmp_path)

    class Raising(DifferencingGuard):
        def release(self, plan: Any, rows: Any, **k: Any) -> Any:
            raise RuntimeError(SENTINEL)

    guard = Raising(store)
    tool, _ = make_tool(tmp_path, guard=guard)
    out = call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False and out["error"]["rule"] == "rewrite_invariant"
    assert SENTINEL not in json.dumps(out) and "SYNTH" not in json.dumps(out)
    assert guard.open_plan_count() == 0


def test_copied_plan_the_guard_never_issued_is_refused(tmp_path: Path) -> None:
    """N-1: a guard that hands back a ``dataclasses.replace`` copy of the plan it issued. The
    copy passes the adapter's type, scope_key and parameter checks; ``release`` redeems only
    the issued object, so the copy fails closed as an invariant failure: no rows, no
    fingerprint.

    Pinned gaps (guard API, not run_sql): the guard has no public "did I issue this plan"
    check, so the copy is only caught at ``release``, *after* the dry run and execute; and
    ``abandon`` is identity-based (a copy is a no-op, G-1), so the issued original, which
    only the faulty guard ever held, stays open until evicted."""
    store = make_store(tmp_path)

    class Copying(DifferencingGuard):
        def prepare(self, *a: Any, **k: Any) -> Any:
            plan = super().prepare(*a, **k)
            return dataclasses.replace(plan)  # same fields and token, not the issued object

    guard = Copying(store)
    tool, client = make_tool(tmp_path, guard=guard)
    out = call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False and out["error"]["rule"] == "rewrite_invariant"
    assert "data" not in out and "SYNTH" not in json.dumps(out)
    assert fingerprint_count(store) == 0  # release refused before recording anything
    assert len(client.executed) == 1  # gap: caught at release, not before execute
    assert guard.open_plan_count() == 1  # gap: the issued original, never seen by run_sql


def test_trace_strips_sql_literals(tmp_path: Path) -> None:
    """L-5: run_sql passes the scoped SQL to the tracer, which must strip every literal."""
    tracer = Tracer(tmp_path / "traces", session_id="session-1")
    tool, client = make_tool(tmp_path, tracer=tracer)
    number = "987654321"
    sql = (
        f"SELECT oi.status, COUNT(*) AS n FROM {OI} AS oi "
        f"WHERE oi.status != '{SENTINEL}' AND oi.sale_price < {number} GROUP BY oi.status"
    )
    out = call(tool, sql, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True, out
    assert SENTINEL in client.executed[0]["sql"] and number in client.executed[0]["sql"]
    raw = (tmp_path / "traces" / "session-1.jsonl").read_text()
    assert SENTINEL not in raw and number not in raw
    sql_span = next(json.loads(x) for x in raw.splitlines() if json.loads(x)["type"] == "sql")
    assert sql_span["sql_text"] and "@scope_brands" in sql_span["sql_text"]
    assert sql_span["sql_hash"]


# --- lock timeout (M-1) -------------------------------------------------------------------------


def test_lock_timeout_exceeds_worst_case_call(tmp_path: Path) -> None:
    """Worst legitimate call: population + answer query, each with the retry, at the runner's
    deadlines plus the retry sleep (the review's ~430 s estimate is the floor)."""
    tool, _ = make_tool(tmp_path)
    worst = worst_case_call_s(tool.runner, tool.retry_delay_s)
    assert worst >= 430
    assert tool.lock_timeout_s > worst
    assert tool.lock_timeout_s == default_lock_timeout_s(tool.runner, tool.retry_delay_s)
    # Longer runner deadlines and retry sleep raise the lock timeout with them.
    slow = BigQueryRunner(
        RoutingClient(default_responder), PROJECT, api_timeout_s=120, job_timeout_ms=600_000
    )
    slow_tool = RunSqlTool(slow, DifferencingGuard(make_store(tmp_path, "slow.db")),
                           lambda: REFRESH, retry_delay_s=30.0, sleep=lambda s: None)  # fmt: skip
    slow_worst = worst_case_call_s(slow, 30.0)
    assert slow_worst > worst and slow_tool.lock_timeout_s > slow_worst


def test_give_up_after_three_consecutive_failures(tmp_path: Path) -> None:
    exc = gexc.BadRequest("Syntax error: synthetic")
    tool, client = make_tool(tmp_path, RoutingClient(default_responder, dry_exc=exc))
    session, turn = new_session(), RunSqlTurn("turn-1")
    codes = [call(tool, f"{SIMPLE} LIMIT {i}", session, turn)["error"]["code"] for i in range(1, 4)]
    assert codes == ["SQL_SYNTAX", "SQL_SYNTAX", GIVE_UP]
    assert turn.gave_up is True
    before = len(client.calls)
    later = call(tool, f"{SIMPLE} LIMIT 9", session, turn)
    assert later["error"]["code"] == GIVE_UP and len(client.calls) == before


def test_success_resets_and_final_refusals_do_not_count(tmp_path: Path) -> None:
    tool, _ = make_tool(tmp_path)
    session, turn = new_session(), RunSqlTurn("turn-1")
    call(tool, NESTED_UNPLACEABLE, session, turn)  # retryable policy refusal
    assert turn.consecutive_failures == 1
    call(tool, SIMPLE, session, turn)
    assert turn.consecutive_failures == 0
    none_scope = new_session(scope=None)
    for i in range(4):
        call(tool, f"{SIMPLE} LIMIT {i + 1}", none_scope, turn)  # not retryable
    assert turn.consecutive_failures == 0 and turn.gave_up is False


def test_bq_unavailable_retries_once_then_stops_the_turn(tmp_path: Path) -> None:
    sleeps: list[float] = []
    unavailable = gexc.ServiceUnavailable("synthetic outage")
    client = RoutingClient(default_responder, exec_errors=[unavailable])
    tool, _ = make_tool(tmp_path, client, sleep=sleeps.append)
    turn = RunSqlTurn("turn-1")
    assert call(tool, SIMPLE, new_session(), turn)["ok"] is True  # second attempt succeeds
    assert sleeps == [2.0]

    sleeps.clear()
    client = RoutingClient(default_responder, exec_errors=[unavailable, unavailable, unavailable])
    tool, _ = make_tool(tmp_path, client, sleep=sleeps.append)
    turn = RunSqlTurn("turn-1")
    out = call(tool, SIMPLE, new_session(), turn)
    assert out["error"]["code"] == "BQ_UNAVAILABLE" and out["error"]["retryable"] is False
    assert sleeps == [2.0] and len(client.executed) == 2  # bounded: one retry
    assert turn.bq_unavailable is True
    calls = len(client.calls)
    assert call(tool, SIMPLE2, new_session(), turn)["error"]["code"] == "BQ_UNAVAILABLE"
    assert len(client.calls) == calls


def test_session_byte_budget_is_shared(tmp_path: Path) -> None:
    from opsfleet_agent.bq.client import SessionByteBudget

    tool, client = make_tool(tmp_path, RoutingClient(default_responder, estimate=900, billed=900))
    session = RunSqlSession("user-1", "session-1", ACME, bytes=SessionByteBudget(cap=1500))
    assert call(tool, SIMPLE, session, RunSqlTurn("turn-1"))["ok"] is True
    out = call(tool, SIMPLE2, session, RunSqlTurn("turn-1"))
    assert out["error"]["code"] == "SESSION_BUDGET"
    assert len(client.executed) == 1


# --- arguments ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        None,
        "SELECT 1",
        {"sql": SIMPLE},
        {"purpose": "x"},
        {"sql": SIMPLE, "purpose": "x", "population": 10_000},
        {"sql": SIMPLE, "purpose": "x", "scope": "all"},
        {"sql": 1, "purpose": "x"},
        {"sql": SIMPLE, "purpose": None},
        {"sql": "   ", "purpose": "x"},
        {"sql": SIMPLE, "purpose": "x" * 201},
        {"sql": SIMPLE, "purpose": SENTINEL, "extra": SENTINEL},
    ],
)
def test_invalid_args_never_execute(tmp_path: Path, args: Any) -> None:
    tool, client = make_tool(tmp_path)
    out = tool.run(args, new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is False and out["error"]["code"] == INVALID_ARGS
    assert SENTINEL not in json.dumps(out)
    assert client.calls == []


def test_sql_too_long(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path)
    sql = SIMPLE + " " * (8001 - len(SIMPLE))
    out = tool.run({"sql": sql, "purpose": "x"}, new_session(), RunSqlTurn("turn-1"))
    assert out["error"]["code"] == SQL_TOO_LONG and out["error"]["retryable"] is True
    assert client.calls == []


# --- registry -----------------------------------------------------------------------------------


def test_role_tool_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    from opsfleet_agent.config import ROLES

    assert RUN_SQL_ENABLED is False  # until the owner passes the iteration 13 gate
    assert list(inspect.signature(tools_for).parameters) == ["role"]  # no override argument
    disabled = {role: tools_for(role) for role in ROLES}
    monkeypatch.setattr(registry, "RUN_SQL_ENABLED", True)  # the only switch
    enabled = {role: tools_for(role) for role in ROLES}
    for role in ROLES:
        tools = disabled[role]
        assert RUN_SQL not in tools
        if role in {"quick_analyst", "deep_analyst"}:
            assert tools == {"list_tables", "get_schema"}
            assert enabled[role] == {"list_tables", "get_schema", RUN_SQL}
        elif role == "library_agent":
            assert "delete_reports" in tools and not tools & {"get_schema", RUN_SQL}
            assert enabled[role] == tools
        else:
            assert tools == frozenset() and enabled[role] == frozenset()
    assert tools_for("unknown-role") == frozenset()
    assert set(TOOLS_BY_ROLE) <= set(ROLES)


def test_serial_calls_are_fast_enough(tmp_path: Path) -> None:
    tool, _ = make_tool(tmp_path)
    start = time.monotonic()
    session = new_session()
    outs = [
        call(tool, f"{SIMPLE} LIMIT {i + 1}", session, RunSqlTurn(f"turn-{i}")) for i in range(5)
    ]
    assert time.monotonic() - start < 10
    assert all(out["ok"] is True for out in outs), outs


def test_client_row_cap_reaches_release_as_truncated(tmp_path: Path) -> None:
    """R5-H1: the client's 200-row cap is a top-N cut. run_sql forwards the client's flag to
    ``release`` on a fresh run and on a memo hit, so a capped probe that lacks a cell another
    query saw is refused."""
    store = make_store(tmp_path)
    seen: list[bool] = []

    class Recording(DifferencingGuard):
        def release(self, plan: Any, rows: Any, **k: Any) -> Any:
            seen.append(k["truncated"])
            return super().release(plan, rows, **k)

    def responder(sql: str) -> list[dict[str, Any]]:
        if CELL_COUNT_COLUMN not in sql:
            return default_responder(sql)
        if "37" in sql:  # 201 synthetic states: the client keeps 200 and flags truncation
            states = [f"SYNTH-S{i:03d}" for i in range(201)]
        else:  # the full breakdown: 199 of those states and CA, 200 rows, not truncated
            states = [f"SYNTH-S{i:03d}" for i in range(199)] + ["SYNTH-CA"]
        return [{"state": s, "n": 300, CELL_COUNT_COLUMN: 300} for s in states]

    guard = Recording(store)
    tool, _ = make_tool(tmp_path, RoutingClient(responder), guard=guard)
    session = new_session()
    probe = by_state("u.age >= 30", "u.age != 37")
    capped = call(tool, probe, session, RunSqlTurn("turn-1"))
    hit = call(tool, probe, session, RunSqlTurn("turn-2"))
    assert capped["ok"] is True and hit["ok"] is True, (capped, hit)
    assert hit["meta"]["cache_hit"] is True and capped["data"]["truncated"] is True
    assert seen == [True, True]
    # the stored capped side lacks CA: the full breakdown is a difference over a cut, refused
    out = call(tool, by_state("u.age >= 30"), session, RunSqlTurn("turn-3"))
    assert out["ok"] is False and out["error"]["rule"] == "differencing", out
    assert seen == [True, True, False] and "SYNTH" not in json.dumps(out)
    assert guard.open_plan_count() == 0


def test_release_truncated_fails_closed_on_a_memo_entry(tmp_path: Path) -> None:
    """A result with more rows than the tool's cap counts as truncated even when its flag
    says otherwise (a runner with a larger cap, or a memo entry): the model sees a top-N."""
    store = make_store(tmp_path)
    seen: list[bool] = []

    class Recording(DifferencingGuard):
        def release(self, plan: Any, rows: Any, **k: Any) -> Any:
            seen.append(k["truncated"])
            return super().release(plan, rows, **k)

    rows = [{"state": f"SYNTH-S{i:03d}", "n": 300, CELL_COUNT_COLUMN: 300} for i in range(250)]
    tool, _ = make_tool(
        tmp_path, RoutingClient(lambda sql: rows), guard=Recording(store), row_cap=400
    )
    out = call(tool, by_state("u.age >= 30"), new_session(), RunSqlTurn("turn-1"))
    assert out["ok"] is True and out["data"]["truncated"] is True, out
    assert seen == [True]
