"""D-154: a refused table tells the analyst what it may use, and the analyst rewrites (offline)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opsfleet_agent.guards.sql_policy import (
    ALLOWED_TABLES,
    PII_COLUMNS,
    SOURCE_NOT_ALLOWED_HINT,
    allowed_sources_text,
    check_sql,
)
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.tools.run_sql import SOURCE_NOT_ALLOWED_MESSAGE, RunSqlTurn
from tests.unit.test_graph import SIMPLE, Router, Scripted, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)
from tests.unit.test_run_sql import call, make_tool, new_session

EVENTS = "SELECT COUNT(*) AS n FROM `bigquery-public-data.thelook_ecommerce.events`"
INVENTORY = "SELECT COUNT(*) AS n FROM inventory_items"


def test_allowed_sources_lists_every_table_and_no_pii() -> None:
    text = allowed_sources_text()
    for table, cols in ALLOWED_TABLES.items():
        assert f"{table} (" in text
        for col in cols - PII_COLUMNS.get(table, frozenset()):
            assert col in text
    for col in PII_COLUMNS["users"]:
        assert col not in text.replace("created_at", "")  # no PII column names
    assert "SELECT" not in text and "`" not in text


@pytest.mark.parametrize("sql", [EVENTS, INVENTORY])
def test_policy_hint_tells_the_model_to_rewrite(sql: str) -> None:
    decision = check_sql(sql)
    assert not decision.allowed and decision.reason_code.value == "source_not_allowed"
    assert decision.hint == SOURCE_NOT_ALLOWED_HINT
    assert "Rewrite" in decision.hint and "order_items (" in decision.hint
    assert "sale_price" in decision.hint and "email" not in decision.hint


def test_run_sql_refusal_envelope(tmp_path: Path) -> None:
    tool, client = make_tool(tmp_path)
    out = call(tool, EVENTS, new_session(), RunSqlTurn("turn-1"))
    err = out["error"]
    assert out["ok"] is False and err["rule"] == "source_not_allowed"
    assert err["message"] == SOURCE_NOT_ALLOWED_MESSAGE and "Rewrite" in err["message"]
    assert err["retryable"] is True and err["hint"] == SOURCE_NOT_ALLOWED_HINT
    assert "events" not in err["hint"] and client.calls == []  # no dry run, no bytes


@pytest.mark.parametrize("label", ["complex", "simple"])
def test_analyst_rewrites_after_a_refused_table(make_env, label: str) -> None:  # noqa: F811
    analyst = Scripted(
        sql_call(EVENTS, "c1"), sql_call(SIMPLE, "c2"), ModelTurn("There were 3 complete orders.")
    )
    env = make_env(Router(label), analyst)
    out = env.ask("How many complete orders are there, using the events table?")
    assert out.outcome == "answered" and "3 complete orders" in out.text
    tool_msgs = [m for m in analyst.calls[1][1] if isinstance(m, dict) and m.get("role") == "tool"]
    refused = json.loads(tool_msgs[-1]["content"])
    assert refused["error"]["rule"] == "source_not_allowed"
    assert "order_items (" in refused["error"]["hint"]
    assert len([c for c in analyst.calls if c[2]]) == 3  # refused, rewrite, answer


def test_repeated_refusals_stay_bounded(make_env) -> None:  # noqa: F811
    analyst = Scripted(sql_call(EVENTS))  # the model never rewrites
    env = make_env(Router("complex"), analyst)
    out = env.ask("compare event counts across years and explain")
    assert out.outcome in ("partial", "answered", "error")
    assert len(analyst.calls) < 50
