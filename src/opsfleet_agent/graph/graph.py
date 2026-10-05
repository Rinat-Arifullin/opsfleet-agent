"""Parent graph: a code supervisor over typed state (HLD §2.3, §4.0.3, §4.1, §7.2).

Flow::

    input_guard -> refuse -> finalize
                -> light (run_light_path) -> finalize
                -> load_context -> quick -> (escalate) deep -> force_answer -> grounding -> finalize
                                -> deep ---------------------^

Routing is plain conditional edges over the typed ``TurnState``; no model chooses a route.
Only this parent graph is checkpointed (encrypted SQLite, a file separate from ``agent.db``);
the role subgraphs are compiled with ``checkpointer=False``.

Contract:

* the raw user text never enters graph state: the ``input_guard`` node reads it from the
  non-serialised :class:`TurnContext` and puts only the scrubbed text into state, so only the
  scrubbed text can reach the checkpoint;
* every node is wrapped by ``_safe``: an exception becomes a templated message (no traceback,
  no exception text); ``AgentGraph.run_turn`` wraps ``invoke`` the same way;
* the supervisor never dispatches a role twice per turn (quick -> deep once, via
  ``TurnBudget.try_escalate``); every LLM call goes through the budgeted ``LLMWrapper``;
* ``report`` and ``library`` labels run on the deep analyst for now (seam: 14b/15/17 add the
  writer, verifier, library and delete nodes and their interrupts at ``_after_context``).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Final, TypedDict

from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from opsfleet_agent.bq.client import BigQueryRunner
from opsfleet_agent.bq.schema import TableMetadataCache
from opsfleet_agent.config import ConfigError, Settings
from opsfleet_agent.graph.budget import (
    FORCE_ANSWER_ROLE,
    RECURSION_LIMIT,
    PartialAnswer,
    TurnBudget,
    TurnKind,
    run_with_recursion_guard,
)
from opsfleet_agent.graph.context import (
    AssembledContext,
    assemble_context,
    ledger_entry_for_state,
    previous_user_text,
    scoped_figures,
    snapshot_of,
    tag_scope,
)
from opsfleet_agent.graph.grounding import check_grounding, extract_figures, merge_figures
from opsfleet_agent.graph.llm import Limiters, LLMSuccess, LLMWrapper
from opsfleet_agent.graph.memory import SessionMemory
from opsfleet_agent.guards.input import check_input
from opsfleet_agent.guards.output import REFUSAL_TEXT, ROLE_TOOLS, check_output
from opsfleet_agent.guards.scope import ProductScope
from opsfleet_agent.persona import Persona, assemble_prompt
from opsfleet_agent.roles.analyst import (
    DEEP,
    QUICK,
    AnalystDeps,
    AnalystInvoke,
    ModelTurn,
    analyst_protected_snippets,
    build_system_prompt,
    run_analyst,
    serialise_envelope,
)
from opsfleet_agent.roles.light_path import run_light_path
from opsfleet_agent.roles.router import (
    LIGHT_ROLE_SUBCAPS,
    Invoke,
    RouterInput,
    UserTurn,
    model_ids_from_settings,
    route,
)
from opsfleet_agent.session import Profile, Session, default_data_dir
from opsfleet_agent.tools.run_sql import (
    RunSqlSession,
    RunSqlTool,
    RunSqlTurn,
    _injects_parameters,
)
from opsfleet_agent.tools.schema_tool import get_schema, list_tables

__all__ = [
    "CHECKPOINT_FILE",
    "HISTORY_TURNS",
    "AgentGraph",
    "GraphServices",
    "PendingTurn",
    "TurnResult",
    "build_checkpointer",
    "build_run_sql_tool",
    "scope_snapshot",
]

logger = logging.getLogger(__name__)

CHECKPOINT_FILE: Final = "checkpoints.db"
AES_KEY_ENV: Final = "LANGGRAPH_AES_KEY"
HISTORY_TURNS: Final = 12  # last 12 turns verbatim (HLD §4.1 step 1)
MAX_HISTORY_MESSAGES: Final = 2 * HISTORY_TURNS
MAX_HISTORY_CHARS: Final = 4000
MAX_PRIOR_LEDGER: Final = 20  # prior-turn ledger entries kept in state (context.MAX_PRIOR_LEDGER)
DEFAULT_WINDOW_START: Final = date(2019, 1, 1)
ERROR_TEXT: Final = "Something went wrong while handling that. Please try again."
UNAVAILABLE_TEXT: Final = (
    "I could not complete the analysis within the limits for one question. "
    "Please try a narrower question."
)
_FORCE_RULES: Final = (
    "The analysis was cut short. Write a brief answer for the user that says what was "
    "found, if anything, and what is missing. Use only the queries listed below. "
    "Do not state any number you were not given. Do not call tools."
)
_QA_ROLE_SUBCAPS: Final = {**LIGHT_ROLE_SUBCAPS, QUICK: 6, DEEP: 6, FORCE_ANSWER_ROLE: 1}
_AES_LENGTHS: Final = (16, 24, 32)


# --- checkpointer -------------------------------------------------------------------------------


def build_checkpointer(
    data_dir: Path | None = None, env: Mapping[str, str] | None = None
) -> SqliteSaver:
    """Encrypted SqliteSaver in ``<data_dir>/checkpoints.db`` (separate from ``agent.db``).

    The AES key comes from ``LANGGRAPH_AES_KEY`` (16, 24 or 32 bytes). A missing or invalid
    key is a one-line :class:`ConfigError`; the key is never echoed.
    """
    environ = os.environ if env is None else env
    key = (environ.get(AES_KEY_ENV) or "").encode("utf-8")
    if len(key) not in _AES_LENGTHS:
        raise ConfigError(
            f"{AES_KEY_ENV} must be set to a key of 16, 24 or 32 characters "
            "(it encrypts the saved conversation state)."
        )
    base = Path(data_dir) if data_dir is not None else default_data_dir()
    path = base / CHECKPOINT_FILE
    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            # created owner-only BEFORE sqlite opens it, so the -wal and -shm files inherit 0600
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        os.chmod(path, 0o600)
        saver = SqliteSaver(conn, serde=EncryptedSerializer.from_pycryptodome_aes(key=key))
        saver.setup()
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise ConfigError(f"Cannot open the checkpoint store ({type(exc).__name__}).") from None
    return saver


def build_run_sql_tool(
    settings: Settings,
    runner: BigQueryRunner,
    guard: Any,
    refresh_date: Callable[[], date],
    *,
    tracer: Any = None,
    audit: Callable[[dict[str, Any]], object] | None = None,
    **kw: Any,
) -> RunSqlTool:
    """Wire the tunables from Settings: ``small_cell_k`` and the BigQuery retry delay.

    Fails closed at build time when the runner would not inject query parameters (the scoped
    job-config factory): without them the brand scope would not reach the warehouse.
    """
    if not _injects_parameters(runner):
        raise ConfigError("run_sql needs a runner built with the scoped job-config factory.")
    return RunSqlTool(
        runner,
        guard,
        refresh_date,
        k=settings.small_cell_k,
        retry_delay_s=settings.bq_unavailable_retry_delay_s,
        tracer=tracer,
        audit=audit,
        **kw,
    )


# --- state --------------------------------------------------------------------------------------


def _append_history(old: list[dict[str, Any]] | None, new: list[dict[str, Any]] | None):
    return [*(old or []), *(new or [])][-MAX_HISTORY_MESSAGES:]


def _append_ledger(old: list[dict[str, Any]] | None, new: list[dict[str, Any]] | None):
    return [*(old or []), *(new or [])][-MAX_PRIOR_LEDGER:]


class TurnState(TypedDict, total=False):
    turn_id: str
    message: str  # scrubbed user text; the raw text never enters state
    label: str
    route: str
    is_english: bool
    pii_notice: str
    scope_label: str
    draft: str
    status: str
    error_class: str
    role: str  # role that wrote the draft
    final_text: str
    outcome: str
    error: bool
    grounding_blocked: bool  # the guard blocked the draft before grounding: finalize refuses
    context_message: str  # load_context's message to answer (a resolved clarification merged)
    history: Annotated[list[dict[str, Any]], _append_history]  # {"role", "text", "scope"}
    figures: Annotated[list[dict[str, Any]], merge_figures]
    # Iteration 15. Not in _TURN_RESET: they live for the session (one checkpoint thread).
    memory: dict[str, Any]  # SessionMemory.to_state(): restatement, preferences, pending clarify
    history_summary: dict[str, Any]  # {"text", "scope"}; seam until the summary call lands
    prior_ledger: Annotated[list[dict[str, Any]], _append_ledger]  # ledger entries + "scope"
    # crash resume (HLD 4.0.6): written at turn start / after every node, read by resume
    scope_snapshot: dict[str, Any]  # the scope the turn started under (FR-76)
    owner: str  # the profile user_id the turn started under; resume refuses any other user
    turn_ctx: dict[str, Any]  # TurnBudget counters, SQL ledger, tool names, figures


_TURN_RESET: Final[dict[str, Any]] = {
    "message": "",
    "label": "",
    "route": "",
    "is_english": True,
    "pii_notice": "",
    "scope_label": "",
    "draft": "",
    "status": "",
    "error_class": "",
    "role": "",
    "final_text": "",
    "outcome": "",
    "error": False,
    "grounding_blocked": False,
    "context_message": "",
}


@dataclass
class GraphServices:
    """Process-wide collaborators (not serialised)."""

    settings: Settings
    persona: Callable[[], Persona]
    detector: Any
    router_invoke: Invoke
    analyst_invoke: AnalystInvoke
    run_sql: RunSqlTool
    cache: TableMetadataCache
    tracer: Any = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], object] = time.sleep
    jitter: Callable[[float], float] | None = None
    data_window: Callable[[], tuple[date, date]] | None = None

    def window(self) -> tuple[date, date]:
        if self.data_window is not None:
            return self.data_window()
        try:
            end = self.cache.refresh_date()
        except Exception:  # metadata unavailable: the window ends today
            end = date.today()
        return DEFAULT_WINDOW_START, end


@dataclass
class TurnContext:
    """Per-turn, non-serialised objects: raw text, budget, wrapper, ledger, tool-name record."""

    services: GraphServices
    raw_text: str
    profile: Profile
    session_id: str
    turn_id: str
    sql_session: RunSqlSession
    budget: TurnBudget
    llm: LLMWrapper
    persona: Persona
    sql_turn: RunSqlTurn = field(init=False)
    tool_names: list[str] = field(default_factory=list)
    new_figures: list[dict[str, Any]] = field(default_factory=list)
    guard_codes: set[str] = field(default_factory=set)
    notice: str | None = None
    prompt_cache: str | None = None
    assembled: AssembledContext | None = None  # set by load_context (iteration 15)

    def __post_init__(self) -> None:
        self.sql_turn = RunSqlTurn(self.turn_id, sql_counter=self.budget)

    @property
    def tracer(self) -> Any:
        return self.services.tracer


@dataclass(frozen=True)
class TurnResult:
    text: str
    label: str = ""
    route: str = ""
    outcome: str = "answered"  # answered | refused | blocked | error
    notice: str | None = None
    llm_calls: int = 0
    sql_queries: int = 0
    guard_codes: frozenset[str] = frozenset()


def _new_context(
    services: GraphServices, raw_text: str, session: Session, sql_session: RunSqlSession, tid: str
) -> TurnContext:
    s = services.settings
    budget = TurnBudget(TurnKind.QA, clock=services.clock, role_subcaps=dict(_QA_ROLE_SUBCAPS))
    rpm = {m: lim.rpm for m, lim in s.limits.items()}
    limiters = Limiters(rpm, s.limiter_fraction, clock=services.clock, sleep=services.sleep)
    extra = {"jitter": services.jitter} if services.jitter is not None else {}
    llm = LLMWrapper(budget, limiters, clock=services.clock, sleep=services.sleep, **extra)
    return TurnContext(
        services, raw_text, session.profile, session.session_id, tid, sql_session, budget, llm,
        services.persona(),
    )  # fmt: skip


# --- crash resume: per-turn context in state (HLD 4.0.6, R2-m6) ---------------------------------

_SQL_FLAGS: Final = ("gave_up", "bq_unavailable")
_SQL_COUNTS: Final = ("consecutive_failures", "empty_results")
_MAX_SNAPSHOT_ITEMS: Final = 64  # bound on every restored list or map (a turn runs <= 6 queries)


def scope_snapshot(scope: ProductScope) -> dict[str, Any]:
    """JSON-safe snapshot of a scope (same shape as the iteration-15 memory snapshots)."""
    if scope.all_products:
        return {"all": True, "brands": []}
    return {"all": False, "brands": sorted(scope.brands)}


def _ctx_snapshot(ctx: TurnContext) -> dict[str, Any]:
    """What a resumed turn needs from the non-serialised context. No raw text, no secrets."""
    t = ctx.sql_turn
    sql: dict[str, Any] = {
        "seen": dict(t.seen),
        "statements": dict(t.statements),
        "ledger": [dict(e) for e in t.ledger],
        "last_error": t.last_error,
        **{f: getattr(t, f) for f in (*_SQL_FLAGS, *_SQL_COUNTS)},
    }
    return {
        "turn_id": ctx.turn_id,
        "budget": ctx.budget.snapshot(),
        "sql": sql,
        "tool_names": list(ctx.tool_names),
        "new_figures": list(ctx.new_figures),
        "guard_codes": sorted(ctx.guard_codes),
        "notice": ctx.notice,
    }


def _str_map(obj: Any) -> dict[str, str | None] | None:
    if not isinstance(obj, dict) or len(obj) > _MAX_SNAPSHOT_ITEMS:
        return None
    if not all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in obj.items()):
        return None
    return dict(obj)


_LEDGER_STR: Final = ("sql", "query_id", "sql_hash", "executed_sql_hash")
_LEDGER_KEYS: Final = frozenset((*_LEDGER_STR, "purpose", "rows"))


def _ledger(obj: Any) -> list[dict[str, Any]] | None:
    """The run_sql ledger, with every entry in the exact shape ``RunSqlTool._account`` writes."""
    entries = _dict_list(obj)
    if entries is None:
        return None
    for e in entries:
        rows = e.get("rows")
        if (
            set(e) != _LEDGER_KEYS
            or not all(isinstance(e[k], str) for k in _LEDGER_STR)
            or not (e["purpose"] is None or isinstance(e["purpose"], str))
            or not isinstance(rows, int)
            or isinstance(rows, bool)
            or rows < 0
        ):
            return None
    return entries


def _dict_list(obj: Any) -> list[dict[str, Any]] | None:
    if not isinstance(obj, list) or len(obj) > _MAX_SNAPSHOT_ITEMS:
        return None
    if not all(isinstance(e, dict) for e in obj):
        return None
    return [dict(e) for e in obj]


def _restore_ctx(ctx: TurnContext, snap: Any) -> bool:
    """Continue the interrupted turn's budget and SQL ledger in a fresh context.

    Completed queries are restored into ``seen``/``statements``/``ledger``, so the run_sql
    duplicate check stops them from running again. A malformed snapshot fails closed: the
    budget is exhausted and run_sql gives up, so the turn ends on force_answer's template.
    """
    snap = snap if isinstance(snap, dict) else {}
    budget_ok = ctx.budget.restore(snap.get("budget"))
    sql = snap.get("sql") if isinstance(snap.get("sql"), dict) else {}
    seen, statements = _str_map(sql.get("seen")), _str_map(sql.get("statements"))
    ledger = _ledger(sql.get("ledger"))
    figures = _dict_list(snap.get("new_figures"))
    names, codes = snap.get("tool_names"), snap.get("guard_codes")
    flags_ok = all(isinstance(sql.get(f), bool) for f in _SQL_FLAGS)
    counts_ok = all(
        isinstance(sql.get(f), int) and not isinstance(sql.get(f), bool) and sql[f] >= 0
        for f in _SQL_COUNTS
    )
    lists_ok = all(
        isinstance(x, list) and len(x) <= _MAX_SNAPSHOT_ITEMS and all(isinstance(i, str) for i in x)
        for x in (names, codes)
    )
    notice = snap.get("notice")
    last_error = sql.get("last_error")
    valid = (
        budget_ok
        and None not in (seen, statements, ledger, figures)
        and flags_ok
        and counts_ok
        and lists_ok
        and (notice is None or isinstance(notice, str))
        and (last_error is None or isinstance(last_error, str))
    )
    t = ctx.sql_turn
    if not valid:
        if budget_ok:
            ctx.budget.restore(None)  # exhaust: a malformed ledger may hide completed queries
        t.gave_up = True
        return False
    t.seen.update(seen)
    t.statements.update(statements)
    t.ledger[:] = ledger
    t.last_error = last_error
    for f in _SQL_FLAGS:
        setattr(t, f, sql[f])
    for f in _SQL_COUNTS:
        setattr(t, f, sql[f])
    ctx.tool_names[:] = names
    ctx.new_figures[:] = figures
    ctx.guard_codes |= set(codes)
    ctx.notice = notice
    return True


def _snapshotting(ctx: TurnContext, fn: Callable[[TurnState], dict[str, Any]]):
    """Add the context snapshot to every node update, so each checkpoint carries it."""

    def run(state: TurnState) -> dict[str, Any]:
        update = fn(state)
        try:
            snap = _ctx_snapshot(ctx)
        except Exception as exc:  # never breaks a turn; resume then fails closed (empty)
            logger.error("turn context snapshot failed: %s", type(exc).__name__)
            return {**update, "turn_ctx": {}}  # a stale snapshot would under-count
        return {**update, "turn_ctx": snap}

    return run


# --- nodes --------------------------------------------------------------------------------------


def _previous_user(state: TurnState, scope: ProductScope) -> UserTurn | None:
    """The router's prior turn, only when the current scope covers it (FR-76, R2-M4)."""
    text = previous_user_text(state.get("history"), scope)
    if text is None:
        return None
    try:
        return UserTurn.from_message("user", text)
    except ValueError:
        return None


def _make_nodes(ctx: TurnContext) -> dict[str, Callable[[TurnState], dict[str, Any]]]:
    sv = ctx.services
    settings = sv.settings

    def input_guard(state: TurnState) -> dict[str, Any]:
        decision = check_input(ctx.raw_text, detector=sv.detector)
        if not decision.allowed or decision.scrubbed is None:
            _record(ctx, "guard", "input", verdict="refuse", rule=decision.rule)
            text = decision.refusal or REFUSAL_TEXT
            return {"route": "refuse", "final_text": text, "outcome": "refused", "label": "refused"}
        ctx.notice = decision.pii_notice
        update: dict[str, Any] = {
            "message": decision.scrubbed,
            "pii_notice": decision.pii_notice or "",
        }
        model, fb = model_ids_from_settings(settings, "router")
        prev = _previous_user(state, ctx.sql_session.scope)
        rd = route(
            RouterInput(UserTurn.from_decision(decision), prev),
            llm=ctx.llm, model=model, invoke=sv.router_invoke, fallback_model=fb, tracer=ctx.tracer,
        )  # fmt: skip
        update.update(label=rd.label, route=rd.route, is_english=rd.is_english)
        if rd.route == "refuse":
            update.update(final_text=rd.refusal_text or REFUSAL_TEXT, outcome="refused")
        return update

    def light(state: TurnState) -> dict[str, Any]:
        model, fb = model_ids_from_settings(settings, "light_path")
        res = run_light_path(
            UserTurn(state["message"]), state["label"], profile=ctx.profile, persona=ctx.persona,
            llm=ctx.llm, model=model, invoke=sv.router_invoke, fallback_model=fb,
            tool_calls=tuple(ctx.tool_names), detector=sv.detector, tracer=ctx.tracer,
        )  # fmt: skip
        ctx.guard_codes |= set(res.guard_codes)
        outcome = "blocked" if res.source == "blocked" else "answered"
        return {"final_text": res.text, "outcome": outcome, "role": "light_path"}

    def load_context(state: TurnState) -> dict[str, Any]:
        _retrieve_golden(state)  # seam: Golden retrieval arrives with iteration 16
        a = _assemble(state, state["message"])
        ctx.assembled = a
        _record(ctx, "context", "load_context", **a.trace_fields(), **ctx.persona.trace_fields)
        update: dict[str, Any] = {
            "scope_label": ctx.profile.scope_label,
            "memory": a.memory.to_state(),
            "context_message": a.message,
        }
        if a.clarification is not None:  # AC-23.2: one question, no SQL
            update.update(route="clarify", final_text=a.clarification.text, outcome="answered")
        elif a.resolved_clarification and state.get("route") == "light":
            # R2-M1: "1" after a clarification is routed light by the router, but it completes
            # an analytic question. The original label is unknown: fail-open label (Deep).
            update.update(route="full", label="complex")
        return update

    def _assemble(state: TurnState, message: str) -> AssembledContext:
        return assemble_context(
            message,
            scope=ctx.sql_session.scope,
            scope_label=ctx.profile.scope_label,
            history=state.get("history") or [],
            summary=state.get("history_summary") or None,
            prior_ledger=state.get("prior_ledger") or [],
            store_items=(),  # seams: report bodies (17/19), Golden trios (16)
            memory=SessionMemory.from_state(state.get("memory")),
            known_brands=(),  # seam: brand catalogue for the name check (TODO owner, OD-4)
        )

    def _assembled(state: TurnState) -> AssembledContext:
        # A resumed turn (14b) restarts at quick/deep with a fresh TurnContext: rebuild the
        # context from the checkpointed state. load_context already cleared any pending
        # clarification and saved the merged message, so this neither asks nor merges again.
        if ctx.assembled is None:
            ctx.assembled = _assemble(state, state.get("context_message") or state["message"])
        return ctx.assembled

    def quick(state: TurnState) -> dict[str, Any]:
        return _analyst(QUICK, state)

    def deep(state: TurnState) -> dict[str, Any]:
        return _analyst(DEEP, state)

    def _deps() -> AnalystDeps:
        models = {r: model_ids_from_settings(settings, r) for r in (QUICK, DEEP)}
        return AnalystDeps(
            llm=ctx.llm,
            invoke=sv.analyst_invoke,
            executors={
                "list_tables": lambda args: list_tables(sv.cache, args),
                "get_schema": lambda args: get_schema(args, sv.cache),
                "run_sql": lambda args: sv.run_sql.run(args, ctx.sql_session, ctx.sql_turn),
            },
            models=models,
            tracer=ctx.tracer,
            on_tool_name=ctx.tool_names.append,
            on_envelope=lambda name, env: _collect_figures(ctx, name, env),
        )

    def _analyst(role: str, state: TurnState) -> dict[str, Any]:
        lo, hi = sv.window()
        a = _assembled(state)  # iteration 15: scope-filtered, fenced context (FR-76)
        system = build_system_prompt(
            role, scope_label=ctx.profile.scope_label, persona=ctx.persona,
            window=(lo.isoformat(), hi.isoformat()),
            prior_queries=ctx.sql_turn.ledger if role == DEEP else (),
            context_section=a.prompt_section(),
        )  # fmt: skip
        messages = [
            {"role": "system", "content": system},
            *a.history,
            {"role": "user", "content": a.message},
        ]
        res = run_analyst(role, _deps(), messages)
        return {
            "status": res.status,
            "draft": res.output,
            "error_class": res.error_class or "",
            "role": role,
        }

    def force_answer(state: TurnState) -> dict[str, Any]:
        if state.get("status") == "ok":
            return {}
        text = _force_text(ctx, state)
        return {"draft": text, "role": FORCE_ANSWER_ROLE, "status": "partial"}

    def grounding(state: TurnState) -> dict[str, Any]:
        scope = ctx.sql_session.scope
        # R2-M4: figures from earlier turns count only while the current scope covers them;
        # out-of-scope ones stay in state (reducer) but are never used for grounding.
        figures = merge_figures(scoped_figures(state.get("figures"), scope), ctx.new_figures)
        # ground what the user will see: the guard strips tags and empty links (joining digits),
        # so its text is checked, and a draft it blocks is left for finalize to refuse
        verdict = _guard(ctx, state, state.get("draft", ""))
        if not verdict.allowed:
            # fail closed: the draft was never grounding-checked, so finalize must not show it
            # even if a second guard pass allows it (a transient detector failure, say)
            ctx.guard_codes |= set(verdict.codes())
            _record(ctx, "guard", "grounding", grounding_flags=0, skipped="blocked")
            return {"figures": tag_scope(ctx.new_figures, scope), "grounding_blocked": True}
        result = check_grounding(
            verdict.text,
            figures,
            window=sv.window(),
            deadline_hit=ctx.budget.deadline_hit,
        )
        _record(ctx, "guard", "grounding", grounding_flags=len(result.unmatched))
        return {"draft": result.text, "figures": tag_scope(ctx.new_figures, scope)}

    def finalize(state: TurnState) -> dict[str, Any]:
        return _finalize(ctx, state)

    return {
        "input_guard": input_guard,
        "light": light,
        "load_context": load_context,
        "quick": quick,
        "deep": deep,
        "force_answer": force_answer,
        "grounding": grounding,
        "finalize": finalize,
    }


def _retrieve_golden(state: TurnState) -> list[Any]:
    return []  # no-op seam (iteration 16)


def _record(ctx: TurnContext, span: str, name: str | None = None, **fields: Any) -> None:
    if ctx.tracer is None:
        return
    try:
        ctx.tracer.record(span, name, **fields)
    except Exception:  # tracing never breaks a turn
        logger.error("trace record failed")


def _collect_figures(ctx: TurnContext, name: str, env: dict[str, Any]) -> None:
    """Numbers of a successful run_sql result become the turn's grounding set (no strings)."""
    if name != "run_sql" or not env.get("ok"):
        return
    # the figures are those of the payload the model actually saw (trimmed rows included)
    try:
        seen = json.loads(serialise_envelope(env))
    except ValueError:
        return
    data = seen.get("data") if isinstance(seen, dict) and seen.get("ok") else None
    if not isinstance(data, dict):
        return
    rows = data.get("rows") or []
    cols = data.get("columns") or []
    names = [c["name"] if isinstance(c, dict) else str(c) for c in cols]
    matrix = [[r.get(n) for n in names] for r in rows if isinstance(r, dict)]
    ctx.new_figures.append(extract_figures(str(data.get("query_id", "")), names, matrix))


def _force_text(ctx: TurnContext, state: TurnState) -> str:
    """One bounded force_answer LLM call; the deterministic template when it is not possible."""
    ledger = ctx.sql_turn.ledger
    summary = "\n".join(f"- {q.get('purpose', '')} ({q.get('rows', 0)} rows)" for q in ledger)
    template = UNAVAILABLE_TEXT + (f"\n\nQueries run:\n{summary}" if summary else "")
    fa = ctx.budget.force_answer(None, state.get("error_class") or "role_failed")
    if fa.template_only:
        return template
    sv = ctx.services
    model, fb = model_ids_from_settings(sv.settings, "fallback")
    system = assemble_prompt(
        [("Force answer", _FORCE_RULES), ("Queries", summary or "(none)")], ctx.persona
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": state["message"]},
    ]
    res = ctx.llm.call(
        FORCE_ANSWER_ROLE, model, lambda t: sv.analyst_invoke(model, messages, [], t),
        fb, (lambda t: sv.analyst_invoke(fb, messages, [], t)) if fb else None,
    )  # fmt: skip
    if isinstance(res, LLMSuccess):
        value = res.response.value
        text = value.text if isinstance(value, ModelTurn) else value
        if isinstance(text, str) and text.strip() and not getattr(value, "tool_calls", ()):
            return text.strip()
    return template


def _checked_tools(ctx: TurnContext, role: str) -> tuple[str, ...]:
    """Tool names the output guard checks. force_answer calls no tools itself, so only names
    that no analyst may call (unknown or foreign tools) count against it."""
    if role != FORCE_ANSWER_ROLE:
        return tuple(ctx.tool_names)
    allowed = ROLE_TOOLS[DEEP]
    return tuple(n for n in ctx.tool_names if n not in allowed)


def _guard(ctx: TurnContext, state: TurnState, draft: str):
    role = state.get("role") or FORCE_ANSWER_ROLE
    label = state.get("label", "")
    eff_label = "complex" if label in ("report", "library") else label  # TODO 14b/15/17
    return check_output(
        draft, role=role, label=eff_label,
        tool_calls=_checked_tools(ctx, role),
        protected_snippets=analyst_protected_snippets(ctx.persona),
        detector=ctx.services.detector,
    )  # fmt: skip


def _finalize(ctx: TurnContext, state: TurnState) -> dict[str, Any]:
    label = state.get("label", "")
    update: dict[str, Any] = {}
    if state.get("route") in ("refuse", "light", "clarify"):
        text = state.get("final_text", "")
        if not text:  # e.g. a resumed state without the light/refusal/clarify text: fail closed
            text, update["outcome"] = ERROR_TEXT, "error"
    elif state.get("error"):
        text, update["outcome"] = ERROR_TEXT, "error"
    elif state.get("grounding_blocked"):
        # the unchecked draft is never shown: no second guard pass can let it through
        text, update["outcome"] = REFUSAL_TEXT, "blocked"
        _record(ctx, "guard", "output", verdict="block", rule_hits=sorted(ctx.guard_codes))
    else:
        verdict = _guard(ctx, state, state.get("draft", ""))
        codes = set(verdict.codes())
        ctx.guard_codes |= codes
        text = verdict.text if verdict.text else REFUSAL_TEXT
        update["outcome"] = "answered" if verdict.allowed else "blocked"
        _record(ctx, "guard", "output", verdict="allow" if verdict.allowed else "block",
                rule_hits=sorted(codes))  # fmt: skip
    outcome = update.get("outcome") or state.get("outcome") or "answered"
    if state.get("route") != "light":  # the light path records its own turn span
        _record(
            ctx, "turn", None, outcome=outcome, path=state.get("route") or "error", label=label,
            llm_calls_total=ctx.budget.calls, sql_queries_total=ctx.budget.sql_queries,
            **ctx.persona.trace_fields,
        )  # fmt: skip
    update["final_text"] = text
    update["outcome"] = outcome
    if state.get("route") != "clarify" and state.get("memory"):
        # R2-M1(c): a pending clarification lives exactly one turn (load_context clears it
        # too; this covers paths that skip load_context, e.g. errors before it).
        mem = SessionMemory.from_state(state.get("memory"))
        update["memory"] = mem.without_pending().to_state()
    if state.get("message") and state.get("route") != "refuse":
        snap = snapshot_of(ctx.sql_session.scope)
        update["history"] = [
            {"role": "user", "text": state["message"], "scope": snap},
            {"role": "assistant", "text": text[:MAX_HISTORY_CHARS], "scope": snap},
        ]
        if ctx.sql_turn.ledger:  # prior-turn grounding set for follow-ups (AC-07.1/07.2)
            scope = ctx.sql_session.scope
            update["prior_ledger"] = [ledger_entry_for_state(e, scope) for e in ctx.sql_turn.ledger]
    return update


# --- supervisor ---------------------------------------------------------------------------------


def _safe(name: str, fn: Callable[[TurnState], dict[str, Any]]):
    def run(state: TurnState) -> dict[str, Any]:
        try:
            return fn(state)
        except Exception as exc:  # templated message only; class name logged, no text
            logger.error("graph node %s raised %s", name, type(exc).__name__)
            return {"error": True, "route": "error"} if name != "finalize" else {
                "final_text": ERROR_TEXT, "outcome": "error",
            }  # fmt: skip

    return run


def _errored(state: TurnState) -> bool:
    return bool(state.get("error")) or state.get("route") == "error"


def _after_guard(state: TurnState) -> str:
    if _errored(state):
        return "finalize"
    if state.get("route") == "refuse":
        return "finalize"
    if state.get("route") == "light":
        # R2-M1(a): a reply to a pending clarification ("1") must reach load_context even when
        # the router calls it light; load_context sends it back to light if it does not resolve.
        pending = SessionMemory.from_state(state.get("memory")).pending_clarification
        return "load_context" if pending is not None else "light"
    return "load_context"


def _after_context(state: TurnState) -> str:
    # TODO(14b/15/17): report -> writer/verifier/confirm_save, library -> library agent.
    if _errored(state) or state.get("route") == "clarify":
        return "finalize"
    if state.get("route") == "light":  # pending clarification not resolved (R2-M1)
        return "light"
    return "quick" if state.get("label") == "simple" else "deep"


def _after_quick(state: TurnState) -> str:
    if _errored(state):
        return "finalize"
    return "deep" if state.get("status") == "escalate" else "force_answer"


def _after_role(state: TurnState) -> str:
    return "finalize" if _errored(state) else "force_answer"


def _after_force(state: TurnState) -> str:
    return "finalize" if _errored(state) else "grounding"


def _build(ctx: TurnContext, checkpointer: Any) -> Any:
    nodes = _make_nodes(ctx)
    g = StateGraph(TurnState)
    for name, fn in nodes.items():
        g.add_node(name, _snapshotting(ctx, _safe(name, fn)))
    g.add_edge(START, "input_guard")
    g.add_conditional_edges("input_guard", _after_guard, ["finalize", "light", "load_context"])
    g.add_conditional_edges("load_context", _after_context, ["quick", "deep", "light", "finalize"])
    g.add_conditional_edges("quick", _after_quick, ["deep", "force_answer", "finalize"])
    g.add_conditional_edges("deep", _after_role, ["force_answer", "finalize"])
    g.add_edge("light", "finalize")
    g.add_conditional_edges("force_answer", _after_force, ["grounding", "finalize"])
    g.add_edge("grounding", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


class AgentGraph:
    """Runs one turn at a time per process. Per-session SQL state lives here across turns."""

    def __init__(self, services: GraphServices, checkpointer: Any) -> None:
        self.services = services
        self.checkpointer = checkpointer
        self._sql_sessions: dict[str, RunSqlSession] = {}

    def _sql_session(self, session: Session) -> RunSqlSession:
        got = self._sql_sessions.get(session.session_id)
        if got is None:
            got = RunSqlSession(
                user_id=session.profile.user_id,
                session_id=session.session_id,
                scope=ProductScope.from_profile(session.profile),
            )
            self._sql_sessions[session.session_id] = got
        return got

    def _config(self, session_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": session_id}, "recursion_limit": RECURSION_LIMIT}

    def run_turn(
        self, raw_text: str, *, session: Session, turn_id: str | None = None
    ) -> TurnResult:
        """Run one turn. Never raises: any failure becomes a templated message."""
        tid = turn_id or uuid.uuid4().hex[:12]
        try:
            sql_session = self._sql_session(session)
            ctx = _new_context(self.services, raw_text, session, sql_session, tid)
            graph = _build(ctx, self.checkpointer)
            start = {
                **_TURN_RESET,
                "turn_id": tid,
                "scope_snapshot": scope_snapshot(sql_session.scope),
                "owner": session.profile.user_id,
                "turn_ctx": {},
            }
            config, durable = self._config(session.session_id), self._durability()
            out = run_with_recursion_guard(lambda: graph.invoke(start, config, **durable))
        except Exception as exc:
            logger.error("turn failed: %s", type(exc).__name__)
            return TurnResult(ERROR_TEXT, outcome="error")
        return _result(ctx, out)

    def _durability(self) -> dict[str, str]:
        """``durability="sync"``: each node's checkpoint is on disk before the next node
        starts, so a hard kill (no Python cleanup) loses at most the running node (OD-1).
        Omitted when there is no checkpointer (LangGraph ignores it and warns)."""
        return {"durability": "sync"} if self.checkpointer is not None else {}

    def open_resume(self, session: Session) -> PendingTurn:
        """Read the session's last checkpoint (decrypting it) for :func:`resume.resume_turn`.

        Raises when the checkpoint cannot be read (a wrong ``LANGGRAPH_AES_KEY`` is a
        ``ValueError`` from the MAC check): the caller refuses; nothing runs.
        """
        stored = self.checkpointer.get_tuple(self._config(session.session_id))
        if stored is None:
            return PendingTurn(self, session, None, (), {})
        ctx = _new_context(self.services, "", session, self._sql_session(session), "")
        graph = _build(ctx, self.checkpointer)
        snap = graph.get_state(self._config(session.session_id))
        return PendingTurn(self, session, (ctx, graph), tuple(snap.next), dict(snap.values))


@dataclass
class PendingTurn:
    """The interrupted turn of a session, read from its checkpoint (not yet replayed)."""

    agent: AgentGraph
    session: Session
    _built: tuple[TurnContext, Any] | None
    next: tuple[str, ...]
    values: dict[str, Any]

    def finish(self) -> TurnResult:
        """Finish ONLY the interrupted turn (``invoke(None)``), with its budget and ledger
        restored. Never starts a new turn. Never raises."""
        if self._built is None or not self.next:
            return TurnResult(ERROR_TEXT, outcome="error")
        ctx, graph = self._built
        try:
            ctx.turn_id = ctx.sql_turn.turn_id = str(self.values.get("turn_id") or "")
            if not _restore_ctx(ctx, self.values.get("turn_ctx")):
                logger.error("resume: turn context malformed; budget exhausted")
            config = self.agent._config(self.session.session_id)
            durable = self.agent._durability()
            out = run_with_recursion_guard(lambda: graph.invoke(None, config, **durable))
        except Exception as exc:
            logger.error("resume failed: %s", type(exc).__name__)
            return TurnResult(ERROR_TEXT, outcome="error")
        return _result(ctx, out)


def _result(ctx: TurnContext, out: Any) -> TurnResult:
    if isinstance(out, PartialAnswer):
        return TurnResult(out.message, outcome="error", llm_calls=ctx.budget.calls)
    return TurnResult(
        text=out.get("final_text") or ERROR_TEXT,
        label=out.get("label", ""),
        route=out.get("route", ""),
        outcome=out.get("outcome") or "answered",
        notice=ctx.notice,
        llm_calls=ctx.budget.calls,
        sql_queries=ctx.budget.sql_queries,
        guard_codes=frozenset(ctx.guard_codes),
    )
