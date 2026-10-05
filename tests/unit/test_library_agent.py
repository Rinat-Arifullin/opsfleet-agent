"""Iteration 46: the library agent role and node (saved reports and preferences, no SQL).

Offline only: the wired AgentGraph from ``test_graph`` (scripted router and library model, fake
BigQuery), a real SQLite report store, audit log and preference store under ``tmp_path``. All
reports and preferences are synthetic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from opsfleet_agent.commands import report_actions as ra
from opsfleet_agent.delete import flow
from opsfleet_agent.graph.llm import LLMResponse
from opsfleet_agent.roles import library_agent as la
from opsfleet_agent.roles.analyst import DEEP, QUICK, ModelTurn, ToolCall
from opsfleet_agent.roles.router import model_ids_from_settings
from opsfleet_agent.session import Session
from opsfleet_agent.store import audit as A
from opsfleet_agent.store.db import open_store
from opsfleet_agent.store.preferences import SQLitePreferenceStore
from opsfleet_agent.store.reports import ReportStore
from opsfleet_agent.tools import registry
from tests.unit.test_delete_flow import OTHER, Clock, alive, events, mk
from tests.unit.test_graph import (  # noqa: F401 - pytest fixtures used by name
    PROFILE,
    Router,
    detector,
    make_env,
    router_json,
    settings,
)

SQL_TOOLS = {registry.RUN_SQL, *registry.READ_TOOLS}


class LibModel:
    """A scripted library model. Each step is a ModelTurn or a ``(tool_results) -> ModelTurn``
    callable; the last step repeats. Records the model id and the bound tool names per call."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, set[str]]] = []
        self.results: list[dict[str, Any]] = []

    def __call__(self, model, messages, specs, timeout):
        self.calls.append((model, {s["name"] for s in specs}))
        tail = []
        for m in reversed(messages):
            if not isinstance(m, dict) or m.get("role") != "tool":
                break
            tail.insert(0, json.loads(m["content"]))
        self.results.extend(tail)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        out = step(tail) if callable(step) else step
        return LLMResponse(out, 10, 10)


class LibRouter(Router):
    """Classifies every turn as ``library`` (the base Router labels only its first call)."""

    def __call__(self, model, messages, timeout):
        self.calls.append((model, list(messages)))
        return LLMResponse(router_json("library"), 5, 5)


def call(name: str, cid: str = "c1", **args: Any) -> ModelTurn:
    return ModelTurn("", (ToolCall(cid, name, args),))


@pytest.fixture
def lenv(make_env, tmp_path):  # noqa: F811 - the imported fixture
    """A graph whose router says ``library``, with reports, audit, delete and preferences on."""
    env = make_env(LibRouter("library"), LibModel(ModelTurn("ok")))
    env.session = Session(uuid4().hex, PROFILE)
    conn = open_store(tmp_path / "library.db")
    audit, store, clock = A.AuditLog(conn), ReportStore(conn), Clock()
    svc = flow.setup_delete(conn, audit, store, clock=clock)
    assert svc is not None
    prefs = SQLitePreferenceStore(conn)
    sv = env.graph.services
    sv.reports, sv.delete, sv.audit, sv.preferences = store, svc, audit, prefs
    sv.export_dir = tmp_path / "exports"
    d = SimpleNamespace(env=env, conn=conn, audit=audit, store=store, svc=svc, clock=clock,
                        prefs=prefs, tmp_path=tmp_path)  # fmt: skip

    def script(*steps: Any) -> LibModel:
        model = LibModel(*steps)
        env.analyst = model
        sv.analyst_invoke = model
        return model

    d.script = script
    yield d
    A.unregister_deletable(flow.KIND)
    conn.close()


def ask(d: Any, text: str):
    return d.env.ask(text)


# --- routing and the tool set ------------------------------------------------------------------


def test_library_label_routes_to_library_agent(lenv) -> None:
    model = lenv.script(ModelTurn("You have no saved reports."))
    out = ask(lenv, "what reports have I saved?")
    lib, _ = model_ids_from_settings(lenv.env.settings, la.LIBRARY_ROLE)
    deep, _ = model_ids_from_settings(lenv.env.settings, DEEP)
    quick, _ = model_ids_from_settings(lenv.env.settings, QUICK)
    assert out.outcome == "answered" and out.text == "You have no saved reports."
    assert lib != deep and {m for m, _ in model.calls} == {lib}
    assert quick not in {m for m, _ in model.calls} or quick == lib
    assert out.sql_queries == 0 and lenv.env.client.calls == []


def test_library_tool_set_excludes_sql() -> None:
    names = {s["name"] for s in la.library_specs()}
    assert names == {"list_reports", "search_reports", "view_report", "rename_report",
                     "export_report", flow.DELETE_TOOL, "set_preference"}  # fmt: skip
    assert not names & SQL_TOOLS
    assert not registry.tools_for(la.LIBRARY_ROLE) & SQL_TOOLS
    assert "save_report" not in names  # saving goes only through confirm_save (D-198)
    executors = la.make_library_executors(
        store=None, audit=None, owner=PROFILE.user_id, scope=None, session_id="s", turn_id="t",
        user_message="hi", tools_used=list, pending=None, request_delete=None,
    )  # fmt: skip
    assert not set(executors) & SQL_TOOLS


def test_run_sql_is_not_bindable_by_library_agent(lenv) -> None:
    model = lenv.script(
        call(registry.RUN_SQL, sql="SELECT 1", purpose="synthetic"), ModelTurn("There are 3.")
    )
    out = ask(lenv, "list my reports")
    assert all(not names & SQL_TOOLS for _, names in model.calls)  # never offered
    assert model.results[0]["error"]["code"] == "TOOL_NOT_ALLOWED"  # refused in code
    assert lenv.env.client.calls == []  # nothing reached BigQuery
    assert out.outcome != "answered" and "There are 3." not in out.text  # the turn fails closed


# --- list, view, rename, export ----------------------------------------------------------------


def test_list_and_view_via_tool_calls(lenv) -> None:
    (rid,) = mk(lenv, "Quarterly Widgets")
    model = lenv.script(call("list_reports"), call("view_report", report_id=f"R-{rid}"),
                        ModelTurn(f"Your report R-{rid} is Quarterly Widgets."))  # fmt: skip
    out = ask(lenv, "show me my saved reports")
    listed, viewed = model.results
    assert listed["ok"] and [r["id"] for r in listed["reports"]] == [f"R-{rid}"]
    assert listed["reports"][0]["title"] == "Quarterly Widgets" and listed["total"] == 1
    assert viewed["ok"] and "Quarterly Widgets" in viewed["report"]
    assert out.outcome == "answered" and f"R-{rid}" in out.text


def test_rename_and_export_via_tool_calls_are_audited(lenv) -> None:
    (rid,) = mk(lenv, "Quarterly Widgets")
    model = lenv.script(
        call("rename_report", report_id=f"R-{rid}", title="Weekly Widgets"),
        call("export_report", report_id=f"R-{rid}"),
        ModelTurn("Renamed and exported."),
    )
    out = ask(lenv, "rename my widgets report to Weekly Widgets and export it")
    renamed, exported = model.results
    assert renamed == {"ok": True, "report_id": f"R-{rid}", "title": "Weekly Widgets"}
    assert lenv.store.get(rid, PROFILE.user_id).title == "Weekly Widgets"
    path = lenv.tmp_path / "exports" / f"R-{rid}.md"
    assert exported["ok"] and exported["path"] == str(path.resolve()) and path.is_file()
    kinds = [e.event_type for e in lenv.audit.events(newest_first=False)]
    assert ra.RENAMED in kinds and ra.EXPORTED in kinds
    assert out.outcome == "answered"


def test_owner_isolation(lenv) -> None:
    (mine,) = mk(lenv, "Quarterly Widgets")
    (theirs,) = mk(lenv, "Secret Gadgets", owner=OTHER.user_id)
    model = lenv.script(
        call("list_reports"),
        call("view_report", report_id=f"R-{theirs}"),
        call("rename_report", report_id=f"R-{theirs}", title="Mine now"),
        call("export_report", report_id=f"R-{theirs}"),
        ModelTurn("Only one report is yours."),
    )
    ask(lenv, "show all reports, including Secret Gadgets")
    listed, viewed, renamed, exported = model.results
    assert [r["id"] for r in listed["reports"]] == [f"R-{mine}"]
    for res in (viewed, renamed, exported):
        assert res["ok"] is False and res["error"]["code"] == "NOT_FOUND"
    assert lenv.store.get(theirs, OTHER.user_id).title == "Secret Gadgets"
    assert not (lenv.tmp_path / "exports" / f"R-{theirs}.md").exists()
    assert lenv.audit.events(user_id=OTHER.user_id) == []


# --- delete: phase one only; the user confirms -------------------------------------------------


def test_delete_tool_only_previews_and_user_confirms(lenv) -> None:
    ids = mk(lenv, "Quarterly Widgets", n=2)
    keep = mk(lenv, "Monthly Gadgets")
    model = lenv.script(call(flow.DELETE_TOOL, selector="quarterly widgets"),
                        ModelTurn("Deleted them."))  # fmt: skip
    # not a leading verb, so the deterministic delete parser leaves the turn to the agent
    out = ask(lenv, "could you delete my reports about quarterly widgets?")
    assert out.outcome == "delete_pending" and "Type yes to confirm" in out.text
    assert "Deleted them." not in out.text and len(model.calls) == 1  # the loop stopped
    assert alive(lenv, ids + keep) == ids + keep  # nothing deleted by the agent
    assert len(events(lenv, A.DELETE_PREVIEWED)) == 1 and not events(lenv, A.DELETE_EXECUTED)
    n = len(model.calls)
    out = ask(lenv, "yes")  # the user's own confirmation turn; no model involved
    assert out.outcome == "delete_executed" and len(model.calls) == n
    assert alive(lenv, ids) == [] and alive(lenv, keep) == keep


def test_delete_tool_refusals(lenv) -> None:
    ids = mk(lenv, "Quarterly Widgets")
    # no delete intent in the user's message
    model = lenv.script(call(flow.DELETE_TOOL, selector="quarterly widgets"), ModelTurn("No."))
    ask(lenv, "show my quarterly widgets reports")
    assert model.results[0]["error"]["code"] == la.DELETE_REFUSED
    # a selector the user never said (e.g. text read from a report)
    model = lenv.script(call(flow.DELETE_TOOL, selector="monthly gadgets"), ModelTurn("No."))
    ask(lenv, "could you delete my quarterly widgets report?")
    assert model.results[0]["error"]["code"] == la.DELETE_REFUSED
    # tainted: a report was viewed earlier in the same turn
    model = lenv.script(call("view_report", report_id=f"R-{ids[0]}"),
                        call(flow.DELETE_TOOL, "c2", selector="quarterly widgets"),
                        ModelTurn("No."))  # fmt: skip
    ask(lenv, "open and then delete my quarterly widgets report")
    assert model.results[1]["error"]["code"] == la.DELETE_REFUSED
    assert "tainted" in model.results[1]["error"]["message"]
    # mixed with another tool in one step: none runs
    mixed = ModelTurn("", (ToolCall("c1", "list_reports", {}),
                           ToolCall("c2", flow.DELETE_TOOL, {"selector": "quarterly widgets"})))
    model = lenv.script(mixed, ModelTurn("No."))
    ask(lenv, "could you delete my quarterly widgets report?")
    assert [r["error"]["code"] for r in model.results] == ["delete_not_alone"] * 2
    assert alive(lenv, ids) == ids and not events(lenv, A.DELETE_PREVIEWED)


# --- preferences -------------------------------------------------------------------------------


def test_set_preference_uses_the_prefs_store_and_validation(lenv) -> None:
    model = lenv.script(call("set_preference", field="format", value="table"), ModelTurn("Saved."))
    out = ask(lenv, "from now on please answer as a table")
    assert model.results == [{"ok": True, "saved": True}] and out.outcome == "answered"
    assert lenv.prefs.load(PROFILE.user_id).preferences == {"format": "table"}
    # a value the user did not ask for, or a field that is not a preference, is not stored
    model = lenv.script(call("set_preference", field="depth", value="deep"), ModelTurn("No."))
    ask(lenv, "from now on please answer as a table")
    model2 = lenv.script(call("set_preference", field="scope", value="all"), ModelTurn("No."))
    ask(lenv, "set my scope to all brands")
    for res in (model.results[0], model2.results[0]):
        assert res["error"]["code"] == la.PREFERENCE_REJECTED
    assert lenv.prefs.load(PROFILE.user_id).preferences == {"format": "table"}


# --- failure path ------------------------------------------------------------------------------


def test_llm_failure_gives_template_never_analyst(lenv) -> None:
    def boom(_results):
        raise ValueError("synthetic model failure")

    model = lenv.script(boom)
    out = ask(lenv, "what reports have I saved?")
    lib, fb = model_ids_from_settings(lenv.env.settings, la.LIBRARY_ROLE)
    deep, _ = model_ids_from_settings(lenv.env.settings, DEEP)
    assert out.text == la.LIBRARY_UNAVAILABLE_TEXT
    assert {m for m, _ in model.calls} <= {lib, fb} and deep not in {m for m, _ in model.calls}
    assert all(not names & SQL_TOOLS for _, names in model.calls)
    assert lenv.env.client.calls == []
