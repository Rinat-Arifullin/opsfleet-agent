"""Library agent role (iteration 46; HLD §4.0, §4.2 role table): a bounded tool loop over the
user's saved reports and preferences, for router label ``library``.

The same pattern as :mod:`roles.analyst`: a small ``call_model <-> run_tools`` LangGraph loop
compiled with ``checkpointer=False``, every model call through the budgeted
:class:`LLMWrapper` (retries, the fallback model, the per-role sub-cap), the whole loop under
:func:`run_with_recursion_guard`, and tool dispatch through the shared ``_dispatch`` (only
``tools_for(LIBRARY_ROLE)`` with a bound executor runs; any other name is refused and recorded,
so the output guard fails the turn closed).

Enforced in code, not in the prompt:

* no SQL or BigQuery tool: ``library_specs`` declares only library tools and an import-time
  check refuses a registry that gives this role ``run_sql``, ``list_tables`` or ``get_schema``;
* the user's identity, product scope, session and turn are bound into the executors by code
  (:func:`make_library_executors`); the model never passes an owner;
* ``delete_reports`` only REQUESTS the two-phase delete (D-197): it runs the iteration 22a
  checks (``delete_flow.check_tool_request``: intent in the user's message, no taint, nothing
  pending, a narrow selector), requires the selector to be the user's own words, and hands a
  parsed request to the graph, which runs ``delete_preview`` and pauses in ``confirm_delete``.
  Only the user's NEXT message can confirm; nothing here can. A step that mixes the delete
  with any other tool is refused as a whole (``delete_flow.gate_step``);
* ``set_preference`` goes through ``graph.memory.set_preference`` and the same per-user
  store as ``/prefs`` (D-195);
* on any failure the caller shows :data:`LIBRARY_UNAVAILABLE_TEXT`; there is no fallback to
  an analyst (D-198).
"""

from __future__ import annotations

import logging
import operator
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Final, TypedDict

from langgraph.graph import END, START, StateGraph

from opsfleet_agent.commands.report_actions import export_report, rename_report
from opsfleet_agent.delete import flow as delete_flow
from opsfleet_agent.graph.budget import (
    BudgetExhausted,
    ForceAnswer,
    PartialAnswer,
    run_with_recursion_guard,
)
from opsfleet_agent.graph.llm import LLMFailure, LLMSuccess
from opsfleet_agent.graph.memory import render_preferences, set_preference
from opsfleet_agent.guards.plain_language import PLAIN_LANGUAGE_RULE, PLAIN_LANGUAGE_SECTION
from opsfleet_agent.persona import PERSONA_LABEL, SAFETY_PREAMBLE, Persona, assemble_prompt
from opsfleet_agent.reports.library import (
    SEARCH_MODES,
    LibraryError,
    display_id,
    list_reports,
    search_reports,
    view_report,
)
from opsfleet_agent.reports.matcher import MatchError
from opsfleet_agent.roles.analyst import (
    MAX_TOOL_CALLS_PER_STEP,
    MAX_TOOL_NAME_CHARS,
    TOO_MANY_CALLS,
    AnalystDeps,
    AnalystResult,
    ModelTurn,
    ToolCall,
    ToolSpec,
    _assistant_message,
    _dispatch,
    _error_envelope,
    _invoke_subgraph,
    serialise_envelope,
)
from opsfleet_agent.tools.registry import READ_TOOLS, RUN_SQL, tools_for

__all__ = [
    "LIBRARY_PROMPT_VERSION",
    "LIBRARY_ROLE",
    "LIBRARY_TOOL_SPECS",
    "LIBRARY_UNAVAILABLE_TEXT",
    "build_library_prompt",
    "library_protected_snippets",
    "library_specs",
    "load_library_prompt",
    "make_library_executors",
    "run_library_agent",
]

logger = logging.getLogger(__name__)

LIBRARY_ROLE: Final = "library_agent"
LIBRARY_PROMPT_VERSION: Final = "library-v1"
LIBRARY_UNAVAILABLE_TEXT: Final = (
    "I could not finish working with your saved reports right now. Please try again, or use "
    "/reports, /open, /search, /rename, /export or /delete."
)
MAX_LIST_RESULTS: Final = 20
DELETE_REQUESTED: Final = "delete_requested"  # the loop's terminal status after a delete call

# error codes returned to the model (envelopes, never shown to the user as such)
NOT_FOUND: Final = "NOT_FOUND"
INVALID_ARGS: Final = "INVALID_ARGS"
STORE_UNAVAILABLE: Final = "STORE_UNAVAILABLE"
DELETE_UNAVAILABLE: Final = "DELETE_UNAVAILABLE"
DELETE_REFUSED: Final = "DELETE_REFUSED"
PREFERENCE_REJECTED: Final = "PREFERENCE_REJECTED"

_SQL_TOOL_NAMES: Final = READ_TOOLS | {RUN_SQL}
_ROLE_TEXT: Final = (
    "You are the library assistant: you list, search, open, rename, export and prepare the "
    "deletion of the user's own saved reports, and save their answer preferences."
)
_RESET_RE: Final = re.compile(r"\b(?:reset|clear|forget)\b", re.I)
_WORD_RE: Final = re.compile(r"[a-z0-9]+")


def _str(desc: str) -> dict[str, str]:
    return {"type": "string", "description": desc}


LIBRARY_TOOL_SPECS: Final[dict[str, ToolSpec]] = {
    "list_reports": {
        "name": "list_reports",
        "description": "List the user's saved reports, newest first (at most 20).",
        "parameters": {
            "type": "object",
            "properties": {"query": _str("Optional words to match in the title or body")},
        },
    },
    "search_reports": {
        "name": "search_reports",
        "description": "Find saved reports by text, tags and creation dates (all must match).",
        "parameters": {
            "type": "object",
            "properties": {
                "text": _str("Words to find in the title or body"),
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
                "date_from": _str("Created on or after, YYYY-MM-DD"),
                "date_to": _str("Created on or before, YYYY-MM-DD"),
                "mode": {
                    "type": "string",
                    "enum": list(SEARCH_MODES),
                    "description": "substring (default) or ranked: best full-text match first",
                },
            },
        },
    },
    "view_report": {
        "name": "view_report",
        "description": "Open one saved report by its id (R-...).",
        "parameters": {
            "type": "object",
            "properties": {"report_id": _str("The report id, e.g. R-...")},
            "required": ["report_id"],
        },
    },
    "rename_report": {
        "name": "rename_report",
        "description": "Give one saved report a new title.",
        "parameters": {
            "type": "object",
            "properties": {
                "report_id": _str("The report id, e.g. R-..."),
                "title": _str("The new title, exactly as the user asked"),
            },
            "required": ["report_id", "title"],
        },
    },
    "export_report": {
        "name": "export_report",
        "description": "Write one saved report to a Markdown file and return its path.",
        "parameters": {
            "type": "object",
            "properties": {"report_id": _str("The report id, e.g. R-...")},
            "required": ["report_id"],
        },
    },
    delete_flow.DELETE_TOOL: {
        "name": delete_flow.DELETE_TOOL,
        "description": (
            "Prepare deleting saved reports: the user sees a preview and must confirm in their "
            "next message. Nothing is deleted by this call. Call it alone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "selector": _str("The user's own words naming the reports (topic or ids)"),
                "report_ids": {"type": "array", "items": {"type": "string"},
                               "description": "Report ids the user named"},
            },
        },
    },  # fmt: skip
    "set_preference": {
        "name": "set_preference",
        "description": "Save an answer preference or a short note the user asked for.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": _str("set, view or reset"),
                "field": _str("format, depth or charts"),
                "value": _str("format: table|bullets|prose; depth: brief|standard|deep; "
                              "charts: true|false"),
                "note": _str("A short background note, in the user's words"),
            },  # fmt: skip
        },
    },
}
# save_report is in the role's allowlist (HLD §4.2) but is not declared: a report is saved
# only through the report flow's confirm_save interrupt, never by a library tool call.

# Enforced at import, not by convention: this role must never be able to bind a SQL tool.
if _SQL_TOOL_NAMES & (set(LIBRARY_TOOL_SPECS) | tools_for(LIBRARY_ROLE)):  # pragma: no cover
    raise RuntimeError("the library agent must not have SQL or schema tools")


def library_specs() -> list[ToolSpec]:
    """Declarations bound to both models: ``tools_for(LIBRARY_ROLE)`` with a spec, minus any
    SQL or schema tool (defence in depth if the registry ever changed)."""
    names = sorted(tools_for(LIBRARY_ROLE) - _SQL_TOOL_NAMES)
    return [LIBRARY_TOOL_SPECS[n] for n in names if n in LIBRARY_TOOL_SPECS]


# --- prompt -------------------------------------------------------------------------------------


def _prompt_path() -> Path:
    return Path(__file__).resolve().parents[3] / "prompts" / "library_agent.md"


def load_library_prompt(path: Path | None = None) -> str:
    text = (path or _prompt_path()).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("library prompt is empty")
    return text


def build_library_prompt(
    *,
    scope_label: str,
    persona: Persona,
    preferences: str = "",
    context_section: str = "",
    prompt: str | None = None,
) -> str:
    """Code-built safety preamble, the rules, the fenced persona, then the fenced preferences
    (the same assembly as the analyst prompt)."""
    sections = [
        ("Scope", f"The user's product scope: {scope_label}."),
        ("Role", _ROLE_TEXT),
        ("Library rules", prompt if prompt is not None else load_library_prompt()),
        (PLAIN_LANGUAGE_SECTION, PLAIN_LANGUAGE_RULE),
    ]
    if context_section:
        sections.append(("Context for this turn", context_section))
    return assemble_prompt(sections, persona, preferences)


def library_protected_snippets(persona: Persona, prompt: str | None = None) -> tuple[str, ...]:
    """Prompt text the final answer must not echo."""
    from opsfleet_agent.guards.output import MIN_PROTECTED_SNIPPET_CHARS

    out = [SAFETY_PREAMBLE, PERSONA_LABEL, _ROLE_TEXT]
    out.append(prompt if prompt is not None else load_library_prompt())
    body = persona.text.strip()
    if sum(c.isalnum() for c in body) >= MIN_PROTECTED_SNIPPET_CHARS:
        out.append(body)
    return tuple(out)


# --- executors (identity and scope bound by code) ------------------------------------------------


def _err(code: str, message: str, hint: str = "Tell the user plainly.") -> dict[str, Any]:
    return _error_envelope(code, message, hint)


def _opt_str(args: Mapping[str, Any], key: str) -> str | None:
    v = args.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _entries(res: Any) -> dict[str, Any]:
    rows = [
        {
            "id": display_id(e.report_id),
            "title": e.title if not e.masked else "(outside your current product scope)",
            "created": e.created_at[:10],
            "tags": list(e.tags),
        }
        for e in res.entries
    ]
    return {"ok": True, "reports": rows, "total": res.total, "more": res.total > len(rows)}


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(delete_flow._fold(text).lower()))


def make_library_executors(
    *,
    store: Any,
    audit: Any,
    owner: str,
    scope: Any,
    session_id: str,
    turn_id: str,
    user_message: str,
    tools_used: Callable[[], Sequence[str]],
    pending: Mapping[str, Any] | None,
    request_delete: Callable[[delete_flow.DeleteRequest], None] | None,
    preferences: Any = None,
    scope_snapshot: Mapping[str, Any] | None = None,
    detector: Any = None,
    export_dir: Path | None = None,
    tracer: Any = None,
) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """The library tools for one turn. ``owner``, ``scope``, the session and the turn come from
    code; model arguments only select reports and values. ``request_delete`` is None when the
    two-phase delete is unavailable (no service or no checkpoint to pause on)."""

    def no_store() -> dict[str, Any]:
        return _err(STORE_UNAVAILABLE, "Saved reports are not available right now.")

    def list_tool(args: dict[str, Any]) -> dict[str, Any]:
        if store is None:
            return no_store()
        try:
            res = list_reports(store, owner, scope, _opt_str(args, "query"), MAX_LIST_RESULTS)
        except MatchError as exc:
            return _err(INVALID_ARGS, str(exc), "Use fewer or plainer words, or no query.")
        return _entries(res)

    def search_tool(args: dict[str, Any]) -> dict[str, Any]:
        if store is None:
            return no_store()
        tags = args.get("tags") or ()
        if isinstance(tags, str):
            tags = (tags,)
        if not isinstance(tags, list | tuple):
            return _err(INVALID_ARGS, "tags must be a list of words.", "Send a list.")
        mode = _opt_str(args, "mode") or "substring"
        if mode not in SEARCH_MODES:
            return _err(INVALID_ARGS, "mode must be substring or ranked.", "Use one of them.")
        try:
            res = search_reports(
                store, owner, scope, text=_opt_str(args, "text"),
                tags=[str(t) for t in tags], date_from=_opt_str(args, "date_from"),
                date_to=_opt_str(args, "date_to"), limit=MAX_LIST_RESULTS, mode=mode,
            )  # fmt: skip
        except (LibraryError, MatchError) as exc:
            return _err(INVALID_ARGS, str(exc), "Fix the filters once, then try again.")
        if tracer is not None:  # iteration 37: which search path ran; never the query text
            try:
                tracer.record(
                    "tool", "search_reports", tool="search_reports", outcome="ok",
                    search_path=res.path, rows=len(res.entries),
                )  # fmt: skip
            except Exception:  # noqa: BLE001, S110 - tracing never breaks the tool
                pass
        return _entries(res)

    def view_tool(args: dict[str, Any]) -> dict[str, Any]:
        if store is None:
            return no_store()
        rid = args.get("report_id")
        res = view_report(store, owner, scope, rid if isinstance(rid, str) else "")
        if res.status == "not_found":
            return _err(NOT_FOUND, res.text, "Check the id with list_reports.")
        return {"ok": True, "status": res.status, "report": res.text}

    def rename_tool(args: dict[str, Any]) -> dict[str, Any]:
        if store is None:
            return no_store()
        return rename_report(
            store=store, audit=audit, owner=owner, scope=scope, session_id=session_id,
            turn_id=turn_id, report_id=args.get("report_id"), title=args.get("title"),
            detector=detector,
        )  # fmt: skip

    def export_tool(args: dict[str, Any]) -> dict[str, Any]:
        if store is None:
            return no_store()
        return export_report(
            store=store, audit=audit, owner=owner, scope=scope, session_id=session_id,
            turn_id=turn_id, report_id=args.get("report_id"), export_dir=export_dir,
        )  # fmt: skip

    def delete_tool(args: dict[str, Any]) -> dict[str, Any]:
        if request_delete is None:
            return _err(DELETE_UNAVAILABLE, delete_flow.UNAVAILABLE_TEXT)
        ids = args.get("report_ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return _err(INVALID_ARGS, "report_ids must be a list of ids.", "Send a list.")
        selector = " ".join(ids) if ids else (args.get("selector") or "")
        if not isinstance(selector, str) or not selector.strip():
            return _err(DELETE_REFUSED, delete_flow.SELECTOR_EMPTY_TEXT)
        used = [n for n in tools_used() if n != delete_flow.DELETE_TOOL]
        req, code = delete_flow.check_tool_request(
            user_message=user_message, tools_used=used, pending=pending, selector=selector
        )
        if req is None:
            return _err(DELETE_REFUSED, f"The delete cannot be prepared ({code}).",
                        "Tell the user which reports to name, or to use /delete.")  # fmt: skip
        # D-197: the selector must be the user's own words, never text the model made up
        # (or read in a report): every word of it appears in the user's message
        if not _words(selector) <= _words(user_message):
            return _err(DELETE_REFUSED, "The selector is not in the user's words.",
                        "Use the user's own words for which reports.")  # fmt: skip
        request_delete(req)
        return {
            "ok": True,
            "pending_confirmation": True,
            "message": "A preview is shown; the user must confirm in their next message.",
        }

    def preference_tool(args: dict[str, Any]) -> dict[str, Any]:
        if preferences is None:
            return _err(STORE_UNAVAILABLE, "Preferences cannot be saved right now.")
        action = _opt_str(args, "action") or "set"
        try:
            current = preferences.load(owner)
        except Exception:  # store read failure: class name only
            return _err(STORE_UNAVAILABLE, "Preferences cannot be read right now.")
        if action == "view":
            return {"ok": True, "preferences": render_preferences(current) or "(none set)"}
        if action == "reset":
            if not _RESET_RE.search(user_message):  # only an explicit request clears them
                return _err(PREFERENCE_REJECTED, "The user did not ask to reset.")
            preferences.reset(owner)
            return {"ok": True, "reset": True}
        value: Any = args.get("value")
        if args.get("field") == "charts" and isinstance(value, str):
            value = {"true": True, "false": False}.get(value.strip().lower(), value)
        res = set_preference(
            current, message=user_message, scope_snapshot=dict(scope_snapshot or {}),
            action=action, field=_opt_str(args, "field"), value=value,
            note=_opt_str(args, "note"),
        )  # fmt: skip
        if not res.ok:
            return _err(PREFERENCE_REJECTED, f"Not saved ({res.code}).",
                        "Only save what the user asked for in this message.")  # fmt: skip
        preferences.save(owner, res.memory.persistable())
        return {"ok": True, "saved": True}

    return {
        "list_reports": list_tool,
        "search_reports": search_tool,
        "view_report": view_tool,
        "rename_report": rename_tool,
        "export_report": export_tool,
        delete_flow.DELETE_TOOL: delete_tool,
        "set_preference": preference_tool,
    }


# --- subgraph -----------------------------------------------------------------------------------


class _State(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], operator.add]
    status: str  # running | ok | partial | failed | delete_requested
    output: str
    error_class: str | None


def build_library_graph(deps: AnalystDeps) -> Any:
    """Compile the library loop. ``checkpointer=False``: only the parent graph persists."""
    model, fallback = deps.models[LIBRARY_ROLE]

    def call_model(state: _State) -> dict[str, Any]:
        specs = library_specs()  # the same declarations for the primary and the fallback
        messages = state["messages"]
        primary = lambda timeout: deps.invoke(model, messages, specs, timeout)  # noqa: E731
        fb = (lambda timeout: deps.invoke(fallback, messages, specs, timeout)) if fallback else None
        res = deps.llm.call(LIBRARY_ROLE, model, primary, fallback, fb)
        if isinstance(res, LLMFailure):
            return {"status": "failed", "error_class": str(res.error_class)}
        if isinstance(res, BudgetExhausted | ForceAnswer):
            return {"status": "partial", "error_class": "budget"}
        assert isinstance(res, LLMSuccess)
        turn = res.response.value
        if isinstance(turn, str):
            turn = ModelTurn(text=turn)
        if not isinstance(turn, ModelTurn):
            return {"status": "failed", "error_class": "internal"}
        if turn.tool_calls:
            return {"messages": [_assistant_message(turn)], "status": "running"}
        text = turn.text.strip()
        if not text:
            return {"status": "partial", "error_class": "empty"}
        return {"status": "ok", "output": text}

    def run_tools(state: _State) -> dict[str, Any]:
        raw_calls = state["messages"][-1]["tool_calls"]
        calls = [ToolCall(str(r["id"]), r["name"], r["args"]) for r in raw_calls]
        gated = delete_flow.gate_step(calls)  # a delete mixed with any other tool: none runs
        out: list[dict[str, Any]] = []
        requested = False
        for i, call in enumerate(calls):
            if isinstance(call.name, str) and 0 < len(call.name) <= MAX_TOOL_NAME_CHARS:
                deps.on_tool_name(call.name)  # recorded, refused or not: the guard fails closed
            if i >= MAX_TOOL_CALLS_PER_STEP:
                env = _error_envelope(
                    TOO_MANY_CALLS, "Too many tool calls at once.", "Call fewer tools per step."
                )
            elif gated is not None:
                env = gated.get(call.id) or {"ok": False, "error": {"code": "delete_not_alone"}}
            else:
                env = _dispatch(LIBRARY_ROLE, call, deps)
            if call.name == delete_flow.DELETE_TOOL and env.get("pending_confirmation"):
                requested = True
            if i < MAX_TOOL_CALLS_PER_STEP:
                try:
                    deps.on_envelope(str(call.name), env)
                except Exception as exc:
                    logger.error("on_envelope raised %s", type(exc).__name__)
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name if isinstance(call.name, str) else "",
                    "content": serialise_envelope(env),
                }
            )
        update: dict[str, Any] = {"messages": out}
        if requested:  # the graph shows the preview and pauses; the model says nothing more
            update["status"] = DELETE_REQUESTED
        return update

    def after_model(state: _State) -> str:
        return "run_tools" if state.get("status") == "running" else END

    def after_tools(state: _State) -> str:
        return "call_model" if state.get("status") == "running" else END

    g = StateGraph(_State)
    g.add_node("call_model", call_model)
    g.add_node("run_tools", run_tools)
    g.add_edge(START, "call_model")
    g.add_conditional_edges("call_model", after_model, ["run_tools", END])
    g.add_conditional_edges("run_tools", after_tools, ["call_model", END])
    return g.compile(checkpointer=False)


def run_library_agent(deps: AnalystDeps, messages: list[dict[str, Any]]) -> AnalystResult:
    """Run the library loop to completion. Never raises; returns the HLD role envelope with
    status ok, partial, failed or ``delete_requested``."""
    calls0 = deps.llm.budget.role_calls[LIBRARY_ROLE]
    try:
        graph = build_library_graph(deps)
        out = run_with_recursion_guard(
            lambda: _invoke_subgraph(graph, {"messages": messages, "status": "running"})
        )
    except Exception as exc:  # failed/internal, class name only (HLD §4.0.5)
        logger.error("library agent raised %s", type(exc).__name__)
        result = AnalystResult("failed", error_class="internal")
    else:
        if isinstance(out, PartialAnswer):
            result = AnalystResult("partial", error_class="recursion_limit")
        else:
            status = out.get("status", "failed")
            if status == "running":  # defensive: the loop must end with a terminal status
                status = "failed"
            result = AnalystResult(
                status,
                out.get("output", "") or "",
                out.get("error_class"),
                ("answer",) if status in ("partial", "failed") else (),
            )
    used = AnalystResult(
        result.status, result.output, result.error_class, result.missing,
        deps.llm.budget.role_calls[LIBRARY_ROLE] - calls0, 0,
    )  # fmt: skip
    if deps.tracer is not None:
        try:
            deps.tracer.record(
                "role", LIBRARY_ROLE, agent=LIBRARY_ROLE, model=deps.models[LIBRARY_ROLE][0],
                prompt_version=LIBRARY_PROMPT_VERSION, llm_calls=used.llm_calls, sql=0,
                status=used.status, error_class=used.error_class,
            )  # fmt: skip
        except Exception:
            pass
    return used
