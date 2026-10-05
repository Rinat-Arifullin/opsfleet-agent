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
* ``library`` runs on the deep analyst for now (seam: 15/22a add the library and delete nodes);
* iteration 17: a ``report`` turn that ran SQL continues after grounding with
  ``report_writer`` (writer -> verifier -> output guard) and ``confirm_save``, which pauses on
  ``interrupt()``. The user's next message answers it (``AgentGraph.run_turn``): save stores the
  draft once (idempotent key), cancel or an unrelated message saves nothing, revise starts a
  new report turn, and a delete request is refused while the draft stays pending (TR-14).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Final, TypedDict

from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from opsfleet_agent.bq.client import BigQueryRunner
from opsfleet_agent.bq.schema import TableMetadataCache
from opsfleet_agent.config import ConfigError, Settings
from opsfleet_agent.delete import flow as delete_flow
from opsfleet_agent.golden.seed import Hit, to_store_items
from opsfleet_agent.graph.assumptions import ASSUMPTIONS_ADDED, assumptions_footer
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
    StoreItem,
    assemble_context,
    covers,
    ledger_entry_for_state,
    previous_user_text,
    scoped_figures,
    snapshot_of,
    tag_scope,
)
from opsfleet_agent.graph.fixed_replies import (
    FIXED_KEY,
    contains_marker,
    fixed_kind,
    is_marker,
    static_texts,
)
from opsfleet_agent.graph.grounding import check_grounding, extract_figures, merge_figures
from opsfleet_agent.graph.intents import (
    COMMENT_FALLBACK_TEXT,
    CUSTOMER_BANDS_NOTICE,
    CUSTOMER_BANDS_RULE,
    CUSTOMER_BANDS_SECTION,
    asks_for_customer_pii,
    is_customer_ranking_request,
    is_sql_request,
    mentions_customer_id,
    mentions_customers,
)
from opsfleet_agent.graph.llm import Limiters, LLMSuccess, LLMWrapper
from opsfleet_agent.graph.memory import SessionMemory
from opsfleet_agent.graph.providers import is_local
from opsfleet_agent.guards.echo import ECHO_REJECTED, ECHO_RETRY_RULE, is_echo
from opsfleet_agent.guards.echo import normalise as normalise_echo
from opsfleet_agent.guards.input import PII_REQUEST, REFUSALS, check_input
from opsfleet_agent.guards.output import REFUSAL_TEXT, ROLE_TOOLS, OutputVerdict, check_output
from opsfleet_agent.guards.plain_language import (
    PLAIN_LANGUAGE_RULE,
    PLAIN_LANGUAGE_SECTION,
    SCHEMA_TERMS_REWRITTEN,
    SQL_STRIPPED,
    humanize_identifiers,
    sql_request_reply,
    strip_sql,
)
from opsfleet_agent.guards.scope import ProductScope
from opsfleet_agent.obs import progress
from opsfleet_agent.persona import Persona, assemble_prompt
from opsfleet_agent.reports.library import display_id
from opsfleet_agent.reports.schema import REQUIRED_SECTIONS, missing_sections
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
from opsfleet_agent.roles.report_writer import REPORT_ROLE_SUBCAPS, WRITER_ROLE, produce_report
from opsfleet_agent.roles.router import (
    LIGHT_ROLE_SUBCAPS,
    Invoke,
    RouterInput,
    UserTurn,
    model_ids_from_settings,
    route,
)
from opsfleet_agent.roles.verifier import VERIFIER_ROLE
from opsfleet_agent.session import Profile, Session, default_data_dir
from opsfleet_agent.store.db import StoreError
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
MAX_DESCRIBED_QUERIES: Final = 6  # D-151a: prior queries the "show me the SQL" reply describes
MAX_PRIOR_LEDGER: Final = 20  # prior-turn ledger entries kept in state (context.MAX_PRIOR_LEDGER)
DEFAULT_WINDOW_START: Final = date(2019, 1, 1)
ERROR_TEXT: Final = "Something went wrong while handling that. Please try again."
UNAVAILABLE_TEXT: Final = (
    "I could not complete the analysis within the limits for one question. "
    "Please try a narrower question."
)
# D-152: the budget-hit template when the force answer could not run but an earlier answer in
# this session is in context: point back to it instead of a bare "could not complete".
PARTIAL_WITH_CONTEXT_TEXT: Final = (
    "I ran out of time for this question before I could check it against the data, so I "
    "have nothing new to add yet. My previous answer above still stands. You can ask a "
    "narrower follow-up, for example about one product or one period."
)
# D-157: router labels a customer-ranking request may get by mistake (the PII wording in the
# router prompt); such a request is relabelled in code.
CUSTOMER_OVERRIDE_LABELS: Final = frozenset({"injection", "off_topic"})
ECHO_ERROR_CLASS: Final = "echo"  # D-156: the analyst repeated an earlier reply twice
CUSTOMER_ID_ERROR_CLASS: Final = "customer_id"  # D-159: a ranking answer named customer IDs twice
CUSTOMER_ID_REJECTED: Final = "customer_id_in_answer"
MAX_PREVIOUS_ANSWER_CHARS: Final = 1500  # the force answer sees one earlier answer, trimmed
_FORCE_RULES: Final = (
    "The analysis was cut short. Write a brief answer for the user that says what was "
    "found, if anything, and what is missing. Use only the queries listed below and the "
    "previous answer, if one is given. "
    "Do not state any number you were not given. Do not call tools."
)
_COMMENT_RULES: Final = (
    "The user made a comment or stated an opinion about your previous answer; it is not a "
    "new data request. Reply in two to four short sentences: agree, or add a caveat, using "
    "only the previous answer and the user's message, then suggest one concrete check you "
    "could run on the data. Do not state any number that is not in the previous answer or "
    "the user's message. Do not call tools."
)
_QA_ROLE_SUBCAPS: Final = {**LIGHT_ROLE_SUBCAPS, QUICK: 6, DEEP: 6, FORCE_ANSWER_ROLE: 1}
# iteration 17: a report turn runs under the REPORT caps (14 calls, 180 s) + writer/verifier subcaps
_REPORT_ROLE_SUBCAPS: Final = {**_QA_ROLE_SUBCAPS, **REPORT_ROLE_SUBCAPS}
CONFIRM_NODE: Final = "confirm_save"
DELETE_CONFIRM_NODE: Final = "confirm_delete"  # iteration 22a
DELETE_EXECUTE_NODE: Final = "execute_delete"  # iteration 22a
# live1: the options are capitalised as the user sees them (the reply match is case-blind)
REPORT_PROMPT: Final = "Reply Save to store this report, Revise <what to change>, or Cancel."
PARTIAL_REPORT_NOTE: Final = (
    "Partial analysis: some queries for this report could not be completed, so it covers "
    "only the results that were retrieved."
)
SAVE_DISABLED_TEXT: Final = "Saving reports is turned off right now, so this report was not saved."
SAVE_FAILED_TEXT: Final = "The report could not be saved; nothing was stored. Please ask again."
CANCELLED_TEXT: Final = "Cancelled: the report draft was not saved."
NOT_SAVED_TEXT: Final = "The report draft was not saved."
REVISING_TEXT: Final = "Revising the report draft (the previous draft was not saved)."
DELETE_WHILE_PENDING_TEXT: Final = (
    "A report draft is waiting for your answer, so nothing can be deleted now. "
    "Reply Save, Revise <what to change>, or Cancel first."
)
NOTHING_TO_SAVE_TEXT: Final = "There is no earlier answer in this session to save as a report."
CANCELLED_NOTHING_TO_SAVE_TEXT: Final = (
    "The last report draft was cancelled, so there is nothing to save. Ask again for a new report."
)
# M1 (round 2): a turn on another user's session is refused with the same text as an unknown
# session (no existence oracle) and the session's state is never read, changed or reset
OTHER_OWNER_TEXT: Final = (
    f"No saved session with that id for this user under the current {AES_KEY_ENV}."
)
SCOPE_CHANGED_TEXT: Final = (
    "Your product access changed since this draft was written, so it was not saved."
)
_MAX_REPLY_CHARS: Final = 2000  # the draft reply is classified on a bounded prefix
# m2: commands match plain-ASCII replies only (re.A: no Unicode case folding), so a
# look-alike (long s, the Kelvin sign) is never consent; a bare "y" is not a save.
_SAVE_RE: Final = re.compile(
    r"^\s*(?:save(?: it| this| the report| the draft)?|yes|confirm|ok,? save)\s*[.!]?\s*$",
    re.I | re.A,
)
_CANCEL_RE: Final = re.compile(
    r"^\s*(?:cancel|no|n|discard|don'?t save|do not save)\s*[.!]?\s*$", re.I | re.A
)
_REVISE_RE: Final = re.compile(r"^\s*revise\b[\s:,.-]*(?P<changes>.*)$", re.I | re.S | re.A)
_DELETE_RE: Final = re.compile(r"\b(?:delete|remove|erase|forget|drop|wipe|purge)\b", re.I | re.A)
_SAVE_LAST_RE: Final = re.compile(
    r"^\s*save (?:this|that|the last answer|the answer)(?: as a report)?\s*[.!]?\s*$",
    re.I | re.A,
)
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
    fixed_reply: str  # D-156: kind of code-owned static reply this turn shows ("" = none)
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
    # iteration 17: the pending report draft (title, markdown, sections, sql_used, hash, ...)
    report: dict[str, Any]
    # D-117: refs (trio_id@version) of the Golden examples load_context put in the prompt, so a
    # resumed turn rebuilds the same examples without another embedding call
    golden_refs: list[str]
    # iteration 22a: the pending delete (ids, sha256(token), binding fields, step). Not in
    # _TURN_RESET: it lives from the preview turn to the confirmation turn. Never the token.
    pending_action: dict[str, Any]
    # D-162: sticky aggregate-only mode. Set by the first customer-ranking turn of the session
    # and never cleared: every later turn runs run_sql in bands-only mode. Not in _TURN_RESET.
    aggregate_only: bool


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
    "fixed_reply": "",
    "context_message": "",
    "report": {},  # iteration 17: a draft lives one turn (until confirm_save resolves it)
    "golden_refs": [],  # D-117: retrieved per turn
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
    reports: Any = None  # iteration 17: store.reports.ReportStore; None = saving disabled
    # D-96: brands for the context name check (assemble_context drops an item naming a known
    # brand outside the scope). Offline source until the catalogue query lands (OD-12).
    known_brands: Collection[str] = ()
    # D-117: golden.GoldenIndex; None = Golden examples disabled
    golden_index: Any = None
    # iteration 22a: delete.flow.DeleteService; None = delete disabled (fail closed)
    delete: Any = None

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
    can_confirm: bool = False  # iteration 17: a checkpointer exists, so interrupt() can pause
    forced_label: str | None = None  # iteration 17: "revise" re-runs the turn as a report
    delete_request: Any = None  # iteration 22a: a parsed delete selector routes START -> preview
    delete_turn: int = 0  # iteration 22a: the session's user-turn number (confirm = preview + 1)

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
    budget = TurnBudget(
        TurnKind.QA, clock=services.clock, role_subcaps=dict(_QA_ROLE_SUBCAPS),
        time_bounded=not is_local(s),  # D-149: no wall-clock limits for the local provider
    )  # fmt: skip
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
# The model's own statement (shown back instead of the scoped one); optional, so a snapshot
# written before it was recorded is still valid.
_LEDGER_OPTIONAL: Final = "model_sql"


def _ledger(obj: Any) -> list[dict[str, Any]] | None:
    """The run_sql ledger, with every entry in the exact shape ``RunSqlTool._account`` writes."""
    entries = _dict_list(obj)
    if entries is None:
        return None
    for e in entries:
        rows = e.get("rows")
        if (
            set(e) - {_LEDGER_OPTIONAL} != _LEDGER_KEYS
            or not all(isinstance(e[k], str) for k in _LEDGER_STR)
            or not isinstance(e.get(_LEDGER_OPTIONAL, ""), str)
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
    b = snap.get("budget")
    if isinstance(b, dict) and b.get("kind") == TurnKind.REPORT.value:
        _promote_report(ctx)  # iteration 17: a report turn resumes under the REPORT caps
    budget_ok = ctx.budget.restore(b)
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
    if ctx.sql_session.aggregate_only:  # D-162: a resumed turn of a sticky session
        t.aggregate_only = True
    ctx.tool_names[:] = names
    ctx.new_figures[:] = figures
    ctx.guard_codes |= set(codes)
    ctx.notice = notice
    return True


def _promote_report(ctx: TurnContext) -> None:
    """Iteration 17: switch the turn to the REPORT budget (AC-06.4), keeping every count.

    Idempotent. The new budget continues the QA counters (``restore`` never lowers them), the
    start time and the usage records, and replaces the budget everywhere it is referenced."""
    old = ctx.budget
    if old.kind is TurnKind.REPORT:
        return
    new = TurnBudget(
        TurnKind.REPORT, clock=old._clock, role_subcaps=dict(_REPORT_ROLE_SUBCAPS),
        time_bounded=old.time_bounded,
    )  # fmt: skip
    new.restore({**old.snapshot(), "kind": TurnKind.REPORT.value})
    new._start = old._start
    new.usage = old.usage
    ctx.budget = ctx.llm.budget = ctx.sql_turn.sql_counter = new


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


_DELETE_ROUTE: Final[dict[str, str]] = {"route": "delete", "label": "delete"}


def _delete_spans(ctx: TurnContext, st: Any, pid: object) -> None:
    """The delete span for an audited step; an error span (code only) for an unsafe one."""
    if st.event:
        _record(ctx, "delete", st.event, event=st.event, count=st.count,
                pending_action_id=pid if isinstance(pid, str) else None,
                audit_event_id=st.audit_event_id)  # fmt: skip
    if st.step == "unsafe":
        _record(ctx, "error", "delete", code=st.error or "delete_unsafe",
                message=delete_flow.UNSAFE_TEXT)  # fmt: skip


def _turn_detector(ctx: TurnContext) -> Any:
    """D-153: the PII detector for this turn, with the session's scope brands allowlisted
    (an all-products scope uses the known brands). A brand in scope is never masked as a
    person; a duck-typed detector without ``with_brands`` (tests) is used as is."""
    base = ctx.services.detector
    extend = getattr(base, "with_brands", None)
    if base is None or extend is None:
        return base
    scope = ctx.sql_session.scope
    brands = ctx.services.known_brands if scope.all_products else scope.brands
    return extend(tuple(brands or ()))


def _make_nodes(ctx: TurnContext) -> dict[str, Callable[[TurnState], dict[str, Any]]]:
    sv = ctx.services
    settings = sv.settings

    def input_guard(state: TurnState) -> dict[str, Any]:
        decision = check_input(ctx.raw_text, detector=_turn_detector(ctx))
        if not decision.allowed or decision.scrubbed is None:
            _record(ctx, "guard", "input", verdict="refuse", rule=decision.rule)
            text = decision.refusal or REFUSAL_TEXT
            return {"route": "refuse", "final_text": text, "outcome": "refused", "label": "refused"}
        ctx.notice = decision.pii_notice
        update: dict[str, Any] = {
            "message": decision.scrubbed,
            "pii_notice": decision.pii_notice or "",
        }
        # D-162: once a turn of this session was a customer ranking, every later turn stays
        # aggregate-only (no reset within the session; a new session starts clear).
        sticky = bool(state.get("aggregate_only")) or ctx.sql_session.aggregate_only
        if sticky:
            ctx.sql_session.aggregate_only = ctx.sql_turn.aggregate_only = True
        model, fb = model_ids_from_settings(settings, "router")
        prev = _previous_user(state, ctx.sql_session.scope)
        rd = route(
            RouterInput(UserTurn.from_decision(decision), prev),
            llm=ctx.llm, model=model, invoke=sv.router_invoke, fallback_model=fb, tracer=ctx.tracer,
        )  # fmt: skip
        update.update(label=rd.label, route=rd.route, is_english=rd.is_english)
        refusal: str | None = None
        if ctx.forced_label and rd.route != "refuse":  # iteration 17: "revise" stays a report
            update.update(label=ctx.forced_label, route="full")
        elif rd.route != "refuse" and is_sql_request(decision.scrubbed):
            # D-151a: "show me the SQL" never shows SQL; the light path gives the code-owned
            # reply that describes the data used in business words (no analyst, no query).
            update.update(label="meta", route="light")
            _record(ctx, "router", "intent", label="meta", route="light", sql_request=True)
        elif rd.label in CUSTOMER_OVERRIDE_LABELS and is_customer_ranking_request(
            decision.scrubbed
        ):
            # D-157: "who are our top 10 customers by spend?" is a data question, answered with
            # spend bands (D-159, below); the SQL policy and the output guard block direct
            # identifiers whatever the label.
            if asks_for_customer_pii(decision.scrubbed):
                update.update(label=rd.label, route="refuse")
                refusal = REFUSALS[PII_REQUEST]
                _record(ctx, "router", "intent", label=rd.label, override="customer_pii")
            else:
                update.update(label="simple", route="full")
                _record(ctx, "router", "intent", label="simple", route="full",
                        override="customer_ranking")  # fmt: skip
        if update["route"] == "full" and is_customer_ranking_request(decision.scrubbed):
            # D-159: a customer ranking, whatever the label, is answered with spend bands and
            # customer counts only. run_sql refuses id-grain queries for the rest of the turn.
            ctx.sql_turn.aggregate_only = True
            ctx.sql_session.aggregate_only = True  # D-162: sticky for the rest of the session
            update["aggregate_only"] = True
            ctx.notice = ctx.notice or CUSTOMER_BANDS_NOTICE
        elif sticky and update["route"] == "full" and mentions_customers(decision.scrubbed):
            # D-162: a follow-up about customers ("show their IDs") is still answered in bands
            ctx.notice = ctx.notice or CUSTOMER_BANDS_NOTICE
        if update["route"] == "refuse":
            update.update(final_text=refusal or rd.refusal_text or REFUSAL_TEXT, outcome="refused")
        return update

    def light(state: TurnState) -> dict[str, Any]:
        model, fb = model_ids_from_settings(settings, "light_path")
        static_reply = None
        if is_sql_request(state["message"]):  # D-151a: describe the in-scope prior queries
            scope = ctx.sql_session.scope
            sqls = [
                e["sql"] for e in state.get("prior_ledger") or []
                if isinstance(e.get("sql"), str) and covers(scope, e.get("scope"))
            ][-MAX_DESCRIBED_QUERIES:]  # fmt: skip
            static_reply = sql_request_reply(sqls)
        res = run_light_path(
            UserTurn(state["message"]), state["label"], profile=ctx.profile, persona=ctx.persona,
            llm=ctx.llm, model=model, invoke=sv.router_invoke, fallback_model=fb,
            tool_calls=tuple(ctx.tool_names), detector=_turn_detector(ctx), tracer=ctx.tracer,
            static_reply=static_reply,
        )  # fmt: skip
        ctx.guard_codes |= set(res.guard_codes)
        outcome = "blocked" if res.source == "blocked" else "answered"
        update = {"final_text": res.text, "outcome": outcome, "role": "light_path"}
        if res.source in ("static", "template"):  # D-156: flag the code-owned reply in history
            update["fixed_reply"] = fixed_kind(res.text) or res.source
        return update

    def load_context(state: TurnState) -> dict[str, Any]:
        if state.get("label") == "report":
            _promote_report(ctx)  # iteration 17: REPORT caps (14 calls, 180 s) for the turn
        a = _assemble(state, state["message"])
        golden: dict[str, Any] = {}
        refs: list[str] = []
        # D-117: examples only for a turn that goes on to an analyst (no clarification, not
        # sent back to light), retrieved on the scrubbed message to answer; outside the LLM cap
        analyst_bound = state.get("route") != "light" or a.resolved_clarification
        if a.clarification is None and analyst_bound and sv.golden_index is not None:
            items, refs, golden = _retrieve_golden(ctx, a.message)
            if items:  # assemble_context is pure: the second pass only adds the examples
                a = _assemble(state, state["message"], items)
        ctx.assembled = a
        _record(
            ctx, "context", "load_context", **a.trace_fields(), **ctx.persona.trace_fields,
            **({"golden": golden} if golden else {}),
        )  # fmt: skip
        update: dict[str, Any] = {
            "scope_label": ctx.profile.scope_label,
            "memory": a.memory.to_state(),
            "context_message": a.message,
            "golden_refs": refs,
        }
        if a.clarification is not None:  # AC-23.2: one question, no SQL
            update.update(route="clarify", final_text=a.clarification.text, outcome="answered")
        elif a.resolved_clarification and state.get("route") == "light":
            # R2-M1: "1" after a clarification is routed light by the router, but it completes
            # an analytic question. The original label is unknown: fail-open label (Deep).
            update.update(route="full", label="complex")
        elif _is_comment(state, a):
            # D-152/D-155: a statement about the previous answer ("so it is worth promoting",
            # router label `comment`) gets one brief reply from that answer, not an analyst
            # loop. With no previous answer the route stays light (static fallback text).
            update.update(status="comment", route="full")
            _record(ctx, "router", "intent", label=state.get("label"), route="comment")
        return update

    def _assemble(
        state: TurnState, message: str, golden: tuple[StoreItem, ...] | list[StoreItem] = ()
    ) -> AssembledContext:
        return assemble_context(
            message,
            scope=ctx.sql_session.scope,
            scope_label=ctx.profile.scope_label,
            history=state.get("history") or [],
            summary=state.get("history_summary") or None,
            prior_ledger=state.get("prior_ledger") or [],
            store_items=golden,  # D-117 Golden examples; seam: saved report bodies (19)
            memory=SessionMemory.from_state(state.get("memory")),
            known_brands=sv.known_brands,  # D-96
        )

    def _assembled(state: TurnState) -> AssembledContext:
        # A resumed turn (14b) restarts at quick/deep with a fresh TurnContext: rebuild the
        # context from the checkpointed state. load_context already cleared any pending
        # clarification and saved the merged message, so this neither asks nor merges again.
        if ctx.assembled is None:
            golden = _golden_from_refs(ctx, state.get("golden_refs"))  # D-117: no embed call
            msg = state.get("context_message") or state["message"]
            ctx.assembled = _assemble(state, msg, golden)
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
            on_tool_name=lambda name: _tool_requested(ctx, name),
            on_envelope=lambda name, env: _collect_figures(ctx, name, env),
        )

    def _analyst(role: str, state: TurnState) -> dict[str, Any]:
        lo, hi = sv.window()
        a = _assembled(state)  # iteration 15: scope-filtered, fenced context (FR-76)

        def messages(extra: tuple[tuple[str, str], ...] = ()) -> list[dict[str, Any]]:
            system = build_system_prompt(
                role, scope_label=ctx.profile.scope_label, persona=ctx.persona,
                window=(lo.isoformat(), hi.isoformat()),
                prior_queries=ctx.sql_turn.ledger if role == DEEP else (),
                context_section=a.prompt_section(), extra_rules=extra,
            )  # fmt: skip
            return [
                {"role": "system", "content": system},
                *a.history,
                {"role": "user", "content": a.message},
            ]

        ranking = ctx.sql_turn.aggregate_only  # D-159: a customer-ranking turn
        bands = ((CUSTOMER_BANDS_SECTION, CUSTOMER_BANDS_RULE),) if ranking else ()
        res = run_analyst(role, _deps(), messages(bands))
        if bands and res.status == "ok" and mentions_customer_id(res.output):
            # D-159: a ranking answer that names customers by ID is never shown. One bounded
            # retry with the bands rule; a second one goes to the force-answer fallback.
            _record(ctx, "guard", "customer_id", verdict="retry", role=role,
                    rule_hits=[CUSTOMER_ID_REJECTED])  # fmt: skip
            res = run_analyst(role, _deps(), messages((*bands, ("Retry", CUSTOMER_BANDS_RULE))))
            if res.status == "ok" and mentions_customer_id(res.output):
                _record(ctx, "guard", "customer_id", verdict="block", role=role,
                        rule_hits=[CUSTOMER_ID_REJECTED])  # fmt: skip
                return {"status": "partial", "draft": "", "error_class": CUSTOMER_ID_ERROR_CLASS,
                        "role": role}  # fmt: skip
        if res.status == "ok" and _is_echo(state, res.output):
            # D-156: the answer repeats an earlier reply. One bounded retry with a corrective
            # rule; a second echo goes to the force-answer fallback and is never shown.
            _record(ctx, "guard", "echo", verdict="retry", role=role, rule_hits=[ECHO_REJECTED])
            res = run_analyst(role, _deps(), messages((*bands, ("Retry", ECHO_RETRY_RULE))))
            if res.status == "ok" and _is_echo(state, res.output):
                _record(ctx, "guard", "echo", verdict="block", role=role,
                        rule_hits=[ECHO_REJECTED])  # fmt: skip
                return {"status": "partial", "draft": "", "error_class": ECHO_ERROR_CLASS,
                        "role": role}  # fmt: skip
        return {
            "status": res.status,
            "draft": res.output,
            "error_class": res.error_class or "",
            "role": role,
        }

    def force_answer(state: TurnState) -> dict[str, Any]:
        if state.get("status") == "ok":
            return {}
        comment = state.get("status") == "comment"
        # D-156: after an echo the earlier answer is not offered again (it was what got copied)
        echoed = state.get("error_class") == ECHO_ERROR_CLASS
        previous = "" if echoed else _previous_answer(_assembled(state))
        text = _force_text(ctx, state, previous, comment=comment)
        status = "comment" if comment else "partial"
        return {"draft": text, "role": FORCE_ANSWER_ROLE, "status": status}

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

    # --- iteration 17: report writer -> verifier -> output guard -> confirm -> save ----------

    def report_writer(state: TurnState) -> dict[str, Any]:
        # A report needs a grounded analysis that ran SQL; otherwise finalize shows the
        # analysis answer as before (nothing to confirm, nothing saved).
        # live1: a partial analysis (the analyst gave up after some queries succeeded) still
        # gets a report draft over what it found, with a code-owned note saying so; a blocked,
        # comment, customer-ID or echo outcome never does.
        ledger = [dict(e) for e in ctx.sql_turn.ledger]
        status = state.get("status")
        if (
            not ledger
            or status not in ("ok", "partial")
            or state.get("grounding_blocked")
            or not str(state.get("draft") or "").strip()
            or state.get("error_class") in (CUSTOMER_ID_ERROR_CLASS, ECHO_ERROR_CLASS)
        ):
            return {}
        scope = ctx.sql_session.scope
        figures = merge_figures(scoped_figures(state.get("figures"), scope), ctx.new_figures)
        rep = _build_report(
            ctx, question=state.get("context_message") or state.get("message", ""),
            analysis=state.get("draft", ""), ledger=ledger, figures=figures,
            extra_notes=(PARTIAL_REPORT_NOTE,) if status == "partial" else (),
        )  # fmt: skip
        if rep is None:
            return {}
        text = f"{rep['markdown']}\n\n{REPORT_PROMPT}"
        return {"report": rep, "route": "report", "final_text": text, "outcome": "report_pending"}

    def confirm_save(state: TurnState) -> dict[str, Any]:
        # Re-runs from the top on resume (LangGraph), so everything before interrupt() is
        # read-only. Saving happens only on an explicit "save" answer (AC-06.1, AC-06.5).
        rep = state.get("report") or {}
        if not rep:
            return {}
        if sv.reports is None or not ctx.can_confirm:  # rollback: shown, never saved
            text = f"{rep.get('markdown', '')}\n\n{SAVE_DISABLED_TEXT}"
            return {"final_text": text, "outcome": "report_unsaved"}
        decision = interrupt(
            {"kind": CONFIRM_NODE, "title": rep.get("title", ""), "draft_hash": rep.get("hash", "")}
        )
        if decision == "save":
            tid = str(state.get("turn_id") or "")
            return _store_report(ctx, rep, turn_id=tid, key=_confirm_key(tid, rep))
        if decision == "revise":
            return {"final_text": REVISING_TEXT, "outcome": "report_cancelled"}
        return {"final_text": CANCELLED_TEXT, "outcome": "report_cancelled"}

    # --- iteration 22a: two-phase delete (preview -> interrupt -> confirm -> execute) -------

    def delete_preview(state: TurnState) -> dict[str, Any]:
        svc, req = sv.delete, ctx.delete_request
        if svc is None or req is None or not ctx.can_confirm:
            text = delete_flow.UNAVAILABLE_TEXT
            return {**_DELETE_ROUTE, "final_text": text, "outcome": "refused", "pending_action": {}}
        st = svc.preview(
            req, owner=ctx.profile.user_id, scope=ctx.sql_session.scope,
            session_id=ctx.session_id, turn_id=ctx.turn_id, preview_turn=ctx.delete_turn,
        )  # fmt: skip
        _delete_spans(ctx, st, (st.pending or {}).get("pending_action_id"))
        return {**_DELETE_ROUTE, "final_text": st.text, "outcome": st.outcome,
                "pending_action": st.pending or {}}  # fmt: skip

    def confirm_delete(state: TurnState) -> dict[str, Any]:
        # Re-runs from the top on resume (LangGraph): everything before interrupt() is
        # read-only. Confirm never deletes; execute_delete is a separate node (HLD 6.3.3).
        pa = state.get("pending_action") or {}
        if pa.get("step") != "preview":
            return {}
        reply = interrupt({"kind": DELETE_CONFIRM_NODE, "count": pa.get("count", 0),
                           "pending_action_id": pa.get("pending_action_id", "")})  # fmt: skip
        svc = sv.delete
        if svc is None:  # the feature went off while pending: nothing is deleted
            return {**_DELETE_ROUTE, "pending_action": {}, "outcome": "delete_expired",
                    "final_text": delete_flow.EXPIRED_TEXT}  # fmt: skip
        st = svc.confirm(pa, reply, turn=ctx.delete_turn, owner=ctx.profile.user_id,
                         session_id=ctx.session_id, turn_id=ctx.turn_id)  # fmt: skip
        _delete_spans(ctx, st, pa.get("pending_action_id"))
        if st.step == "confirmed":  # MJ-1: the confirmation turn owns the pending execute
            return {**_DELETE_ROUTE, "pending_action": {**pa, "step": "confirmed"},
                    "turn_id": ctx.turn_id}  # fmt: skip
        return {**_DELETE_ROUTE, "pending_action": {}, "final_text": st.text,
                "outcome": st.outcome}  # fmt: skip

    def execute_delete(state: TurnState) -> dict[str, Any]:
        pa = state.get("pending_action") or {}
        svc = sv.delete
        if svc is None or pa.get("step") != "confirmed":
            st = delete_flow.Step("unsafe", delete_flow.UNSAFE_TEXT, error="not_confirmed")
        else:
            st = svc.execute(pa, owner=ctx.profile.user_id, session_id=ctx.session_id,
                             turn_id=ctx.turn_id)  # fmt: skip
        _delete_spans(ctx, st, pa.get("pending_action_id"))
        return {**_DELETE_ROUTE, "pending_action": {}, "final_text": st.text,
                "outcome": st.outcome}  # fmt: skip

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
        "report_writer": report_writer,  # iteration 17
        CONFIRM_NODE: confirm_save,  # iteration 17
        "delete_preview": delete_preview,  # iteration 22a
        DELETE_CONFIRM_NODE: confirm_delete,
        DELETE_EXECUTE_NODE: execute_delete,
        "finalize": finalize,
    }


def _retrieve_golden(
    ctx: TurnContext, question: str
) -> tuple[list[StoreItem], list[str], dict[str, Any]]:
    """D-117: top-k Golden examples for ``question`` as store items, their refs, trace fields.

    ``GoldenIndex.retrieve`` never raises and makes at most one embedding call; it is not an LLM
    call, so it does not count against the turn cap (HLD 6.1). The items are untrusted data:
    ``assemble_context`` fences them as EXAMPLE blocks, scrubs them, caps them per kind and by
    characters, and drops one naming a known brand outside the scope. Any failure here means
    no examples and ``unavailable`` in the trace, never a failed turn."""
    scope = ctx.sql_session.scope
    try:
        r = ctx.services.golden_index.retrieve(question, scope)
        items = to_store_items(r.hits, scope)
        refs = [h.trio.ref for h in r.hits]
        trace = {"trio_refs": r.trio_refs, "unavailable": r.unavailable, "mode": r.mode}
    except Exception as exc:  # noqa: BLE001 - a broken index never breaks a turn
        logger.error("golden retrieval failed: %s", type(exc).__name__)
        return [], [], {"trio_refs": [], "unavailable": True, "mode": "none"}
    return items, refs, trace


def _golden_from_refs(ctx: TurnContext, refs: Any) -> list[StoreItem]:
    """D-117 resume: rebuild the examples load_context chose, by ref, with no embedding call.

    Only trios still in the index and still eligible for the current scope come back, each once
    (a tampered checkpoint cannot repeat one), in the saved order, at most ``k`` of them."""
    index = ctx.services.golden_index
    if index is None or not isinstance(refs, list) or not refs:
        return []
    scope = ctx.sql_session.scope
    try:
        by_ref = {t.ref: t for t in index.eligible(scope)}
        wanted = [r for r in dict.fromkeys(r for r in refs if isinstance(r, str)) if r in by_ref]
        hits = [Hit(by_ref[r], 0.0) for r in wanted[: index.k]]  # deduped, order kept
        return to_store_items(hits, scope)
    except Exception as exc:  # noqa: BLE001 - resume falls back to no examples
        logger.error("golden rebuild failed: %s", type(exc).__name__)
        return []


# --- iteration 17: report helpers ----------------------------------------------------------------


_KNOWN_HEADINGS: Final = frozenset((*REQUIRED_SECTIONS, "Verification notes"))
_LABEL_PREFIXES: Final = ("Scope: ", "Data window: ")
_MAX_GUARD_LINES: Final = 400  # a rendered body is far shorter (MAX_ITEMS per section)


def _guard_text(ctx: TurnContext, text: str) -> OutputVerdict:
    return check_output(
        text, role=WRITER_ROLE, label="report", tool_calls=(),
        protected_snippets=analyst_protected_snippets(ctx.persona),
        detector=_turn_detector(ctx),
    )  # fmt: skip


def _split_owned(line: str) -> tuple[str, str]:
    """(code-owned prefix, guarded content) of one rendered body line."""
    if line.startswith("## ") and line[3:].strip() in _KNOWN_HEADINGS:
        return line, ""
    if line.startswith("# "):
        return "# ", line[2:]
    for prefix in _LABEL_PREFIXES:
        if line.startswith(prefix):
            return prefix, line[len(prefix) :]
    return "", line


def _report_guard(ctx: TurnContext, text: str) -> OutputVerdict:
    """The output guard for a report body (the writer calls no tools: ``tool_calls=()``).

    live1: the structure is code-owned, so only the content is guarded. The heading lines and
    the "# ", "Scope: " and "Data window: " labels are kept as rendered and never shown to the
    NER: a live model masked "Data" of "Data window" as a person, the body then lacked a
    required section and every report was refused. Every content line (title and label
    values included) still goes through the full guard; a block anywhere blocks the body."""
    if not isinstance(text, str) or not text.strip():
        return _guard_text(ctx, text)
    if text.strip() in _KNOWN_HEADINGS:  # the store re-guards each section name
        return OutputVerdict(True, text, ())
    lines = text.split("\n")
    if len(lines) > _MAX_GUARD_LINES:
        return _guard_text(ctx, text)
    parts = [_split_owned(line) for line in lines]
    if not any(prefix for prefix, _ in parts):
        return _guard_text(ctx, text)
    verdict = _guard_text(ctx, "\n".join(content for _, content in parts))
    if not verdict.allowed:
        return verdict
    events = list(verdict.events)
    guarded = verdict.text.split("\n")
    if len(guarded) != len(parts):  # the guard changed the line count: guard line by line
        guarded, events = [], []
        for _, content in parts:
            if not content.strip():
                guarded.append(content)
                continue
            v = _guard_text(ctx, content)
            if not v.allowed:
                return v
            guarded.append(" ".join(v.text.split()))
            events.extend(v.events)
    out: list[str] = []
    for (prefix, _), content in zip(parts, guarded, strict=True):
        if prefix in _LABEL_PREFIXES and not content.strip():
            content = "(not set)"
        elif prefix == "# " and not content.strip():
            content = "Report"
        out.append(prefix if prefix.startswith("## ") else prefix + content)
    return OutputVerdict(True, "\n".join(out), tuple(events))


def _build_report(
    ctx: TurnContext,
    *,
    question: str,
    analysis: str,
    ledger: list[dict[str, Any]],
    figures: list[dict[str, Any]],
    extra_notes: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Writer + verifier (bounded, budgeted), then the output guard on the rendered body.
    None when no draft passes: the caller falls back to the analysis answer; nothing is saved."""
    sv = ctx.services
    res = produce_report(
        question=question, analysis=analysis, sql_ledger=ledger, figures=figures,
        scope_label=ctx.profile.scope_label, window=sv.window(), llm=ctx.llm,
        invoke=sv.analyst_invoke,
        models={r: model_ids_from_settings(sv.settings, r) for r in (WRITER_ROLE, VERIFIER_ROLE)},
        persona=ctx.persona, deadline_hit=ctx.budget.deadline_hit, extra_notes=extra_notes,
    )  # fmt: skip
    if not res.ok or res.draft is None:
        _record(ctx, "guard", "report", verdict="no_draft")
        return None
    verdict = _report_guard(ctx, res.markdown)
    codes = set(verdict.codes())
    ctx.guard_codes |= codes
    if not verdict.allowed or not verdict.text or missing_sections(verdict.text):
        _record(ctx, "guard", "report", verdict="block", rule_hits=sorted(codes))
        return None
    _record(ctx, "guard", "report", verdict="allow", rule_hits=sorted(codes))
    d = res.draft
    # B1: the title and sections come from the GUARDED body, never from the raw draft
    return {
        "title": _guarded_title(verdict.text),
        "markdown": verdict.text,
        "sections": _guarded_sections(verdict.text),
        "sql_used": list(res.sql_used),
        "hash": res.draft_hash,
        "model": res.model,
        "status": res.status,
        "data_window": d.data_window,
        "tags": [],
    }


def _guarded_title(body: str) -> str:
    """The H1 of the guarded body (the writer renders the title there); "Report" if none."""
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:200] or "Report"
    return "Report"


def _guarded_sections(body: str) -> dict[str, str]:
    """``{heading: text}`` of the guarded body's ``## `` sections (stored as sections_json)."""
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = out.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _confirm_key(turn_id: str, rep: dict[str, Any]) -> str:
    """The idempotency key of a confirmed draft: one save per (turn, draft hash)."""
    return hashlib.sha256(f"{turn_id}:{rep.get('hash', '')}".encode()).hexdigest()


def _reply_forms(raw: str) -> tuple[str, str]:
    """(command text, folded text) of a bounded reply (m2). Commands match only a plain-ASCII
    reply (anything else is no command: fail closed, nothing saved); delete intent is
    searched on the NFKC-normalised, ASCII-folded text, so a look-alike cannot hide it."""
    reply = raw[:_MAX_REPLY_CHARS]
    nfkc = unicodedata.normalize("NFKC", reply)
    folded = unicodedata.normalize("NFKD", nfkc).encode("ascii", "ignore").decode("ascii")
    return (reply if reply.isascii() else ""), folded


def _store_report(
    ctx: TurnContext, rep: dict[str, Any], *, turn_id: str, key: str
) -> dict[str, Any]:
    """One idempotent save with owner and session; the store re-runs the output guard.
    A store failure saves nothing and says so (the draft is not kept pending)."""
    sv = ctx.services

    def guard(body: str) -> tuple[bool, str]:
        v = _report_guard(ctx, body)
        return v.allowed, v.text

    try:
        rec, created = sv.reports.save(
            owner_user_id=ctx.profile.user_id, session_id=ctx.session_id, turn_id=turn_id,
            title=rep["title"], body_markdown=rep["markdown"],
            sections=rep.get("sections") or {}, sql_used=rep.get("sql_used") or [],
            scope_snapshot=scope_snapshot(ctx.sql_session.scope),
            data_window=str(rep.get("data_window") or ""), draft_hash=rep["hash"],
            idempotency_key=key, guard=guard, tags=rep.get("tags") or [],
            model_used=str(rep.get("model") or ""), persona_version=str(ctx.persona.version),
        )  # fmt: skip
    except (StoreError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        logger.error("report save failed: %s", type(exc).__name__)
        return {"final_text": SAVE_FAILED_TEXT, "outcome": "report_unsaved"}
    verb = "Saved" if created else "Already saved"
    text = f'{verb} report "{rec.title}" (id {display_id(rec.report_id)}).'
    return {"final_text": text, "outcome": "report_saved"}


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


def _is_comment(state: TurnState, a: AssembledContext) -> bool:
    """D-152/D-155: a turn the router labelled ``comment`` that follows an earlier answer in
    the (scope-covered) history. A first message never qualifies: there is nothing to comment
    on, so the light path gives the static :data:`COMMENT_FALLBACK_TEXT`."""
    return (
        state.get("label") == "comment"
        and state.get("route") == "light"
        and not a.resolved_clarification
        and _previous_answer(a) != ""
    )


def _has_answer_history(state: TurnState) -> bool:
    """True when the session history holds an assistant message (cheap pre-check; the
    scope filter and the D-156 marker check run in load_context)."""
    return any(
        isinstance(m, Mapping) and m.get("role") == "assistant" for m in state.get("history") or []
    )


def _previous_answer(a: AssembledContext) -> str:
    """The latest assistant answer in the assembled (scope-filtered) history, trimmed.
    A code-owned static reply (a D-156 marker) is not an answer and is skipped."""
    for m in reversed(a.history):
        content = str(m.get("content") or "").strip()
        if m.get("role") == "assistant" and content and not is_marker(content):
            return content[:MAX_PREVIOUS_ANSWER_CHARS]
    return ""


def _earlier_answers(state: TurnState, message: str) -> list[str]:
    """D-156: earlier assistant answers the current answer must not repeat. An answer to the
    same question (asked again) is left out: repeating it is correct, not an echo."""
    history = list(state.get("history") or [])[-MAX_HISTORY_MESSAGES:]
    current = normalise_echo(message)
    out: list[str] = []
    asked = ""
    for m in history:
        text = str(m.get("text") or "")
        if m.get("role") == "user":
            asked = normalise_echo(text)
        elif m.get("role") == "assistant" and text.strip() and asked != current:
            out.append(text)
    return out


def _is_echo(state: TurnState, answer: str) -> bool:
    if contains_marker(answer):  # it quotes the stand-in for a code-owned reply
        return True
    message = state.get("context_message") or state.get("message") or ""
    return is_echo(answer, _earlier_answers(state, message), static_texts())


def _force_text(
    ctx: TurnContext, state: TurnState, previous: str = "", *, comment: bool = False
) -> str:
    """One bounded force_answer LLM call; the deterministic template when it is not possible.

    ``previous`` is the latest earlier answer (D-152): the force answer and the comment reply
    see it, so a follow-up cut short can still build on what was already said."""
    ledger = ctx.sql_turn.ledger
    summary = "\n".join(f"- {q.get('purpose', '')} ({q.get('rows', 0)} rows)" for q in ledger)
    if comment:
        template = COMMENT_FALLBACK_TEXT
    elif summary:
        template = f"{UNAVAILABLE_TEXT}\n\nQueries run:\n{summary}"
    else:
        template = PARTIAL_WITH_CONTEXT_TEXT if previous else UNAVAILABLE_TEXT
    if not comment and not ledger:
        # No query ran this turn: the model has nothing to report and writes a promise
        # ("I'll get those figures") that grounding cannot catch, so the template is shown.
        return template
    reason ="comment" if comment else state.get("error_class") or "role_failed"
    fa = ctx.budget.force_answer(None, reason)
    if fa.template_only:
        return template
    sv = ctx.services
    model, fb = model_ids_from_settings(sv.settings, "fallback")
    sections = [("Comment reply", _COMMENT_RULES)] if comment else [("Force answer", _FORCE_RULES)]
    sections.append((PLAIN_LANGUAGE_SECTION, PLAIN_LANGUAGE_RULE))  # D-151
    if not comment:
        sections.append(("Queries", summary or "(none)"))
    system = assemble_prompt(sections, ctx.persona)
    # The previous answer is the agent's own earlier (guarded) output; it goes as an assistant
    # message, like history, so the user's text stays the last, untrusted message.
    messages = [
        {"role": "system", "content": system},
        *([{"role": "assistant", "content": previous}] if previous else []),
        {"role": "user", "content": state.get("context_message") or state["message"]},
    ]
    res = ctx.llm.call(
        FORCE_ANSWER_ROLE, model, lambda t: sv.analyst_invoke(model, messages, [], t),
        fb, (lambda t: sv.analyst_invoke(fb, messages, [], t)) if fb else None,
    )  # fmt: skip
    if isinstance(res, LLMSuccess):
        value = res.response.value
        text = value.text if isinstance(value, ModelTurn) else value
        if isinstance(text, str) and text.strip() and not getattr(value, "tool_calls", ()):
            if ctx.sql_turn.aggregate_only and mentions_customer_id(text):
                # D-159: a ranking turn never shows customer IDs: the template is shown instead
                _record(ctx, "guard", "customer_id", verdict="block", role=FORCE_ANSWER_ROLE,
                        rule_hits=[CUSTOMER_ID_REJECTED])  # fmt: skip
                return template
            if not _is_echo(state, text):
                return text.strip()
            # D-156: a reply that repeats an earlier one is never shown: the template is
            _record(ctx, "guard", "echo", verdict="block", role=FORCE_ANSWER_ROLE,
                    rule_hits=[ECHO_REJECTED])  # fmt: skip
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
        detector=_turn_detector(ctx),
    )  # fmt: skip


def _assumptions(ctx: TurnContext, state: TurnState, text: str) -> str:
    """The scope and definitions footer an allowed data answer is missing (live eval 1).

    Only for answers built on data (a query ran this turn, or follow-up context from an
    earlier one); never for a comment reply or a code-owned template."""
    if state.get("status") == "comment" or state.get("label") == "comment":
        return ""
    if not (ctx.sql_turn.ledger or state.get("prior_ledger")) or fixed_kind(text):
        return ""
    memory = SessionMemory.from_state(state.get("memory"))
    return assumptions_footer(
        text, state.get("message") or "",
        brands=ctx.profile.brands, all_products=ctx.profile.all_products,
        churn_restated=memory.churn_definition is not None,
    )  # fmt: skip


def _finalize(ctx: TurnContext, state: TurnState) -> dict[str, Any]:
    label = state.get("label", "")
    update: dict[str, Any] = {}
    if state.get("route") in ("refuse", "light", "clarify", "report", "delete"):
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
        text = verdict.text if verdict.text else REFUSAL_TEXT
        if verdict.allowed and verdict.text:
            # D-151: plain-language rewrite of the allowed answer (analyst or force answer).
            # It runs after the guard, so the guard judged the model's own words, and it only
            # swaps identifiers for business words: no digit, URL or PII can be introduced.
            # D-151a: SQL is removed first, so the rewrite never turns it into prose.
            text = strip_sql(verdict.text)
            if text != verdict.text:
                codes.add(SQL_STRIPPED)
            humanized = humanize_identifiers(text)
            if humanized != text:
                codes.add(SCHEMA_TERMS_REWRITTEN)
            text = humanized
            footer = _assumptions(ctx, state, text)
            if footer:
                text = f"{text.rstrip()}\n\n{footer}"
                codes.add(ASSUMPTIONS_ADDED)
        ctx.guard_codes |= codes
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
    # D-151a: no route shows SQL (light, clarify, report and resumed text included).
    stripped = strip_sql(text)
    if stripped != text:
        ctx.guard_codes.add(SQL_STRIPPED)
        text = stripped
    update["final_text"] = text
    update["outcome"] = outcome
    if state.get("route") != "clarify" and state.get("memory"):
        # R2-M1(c): a pending clarification lives exactly one turn (load_context clears it
        # too; this covers paths that skip load_context, e.g. errors before it).
        mem = SessionMemory.from_state(state.get("memory"))
        update["memory"] = mem.without_pending().to_state()
    if state.get("message") and state.get("route") != "refuse":
        snap = snapshot_of(ctx.sql_session.scope)
        # iteration 17: a report turn keeps the draft (not "Saved ...") as the assistant answer
        answer = strip_sql((state.get("report") or {}).get("markdown") or text)
        reply: dict[str, Any] = {
            "role": "assistant", "text": answer[:MAX_HISTORY_CHARS], "scope": snap,
        }  # fmt: skip
        # D-156: a code-owned static reply is flagged at write time; prompts show a marker
        kind = state.get("fixed_reply") or fixed_kind(answer)
        if kind:
            reply[FIXED_KEY] = kind
        update["history"] = [{"role": "user", "text": state["message"], "scope": snap}, reply]
        if ctx.sql_turn.ledger:  # prior-turn grounding set for follow-ups (AC-07.1/07.2)
            scope = ctx.sql_session.scope
            update["prior_ledger"] = [ledger_entry_for_state(e, scope) for e in ctx.sql_turn.ledger]
    return update


# --- supervisor ---------------------------------------------------------------------------------


def _tool_requested(ctx: TurnContext, name: str) -> None:
    ctx.tool_names.append(name)
    progress.report(progress.TOOL_PREFIX + name)  # D-147: display only; never raises


def _safe(name: str, fn: Callable[[TurnState], dict[str, Any]]):
    def run(state: TurnState) -> dict[str, Any]:
        progress.report(name)  # D-147: the CLI spinner's stage; a no-op without a hook
        try:
            return fn(state)
        except GraphBubbleUp:  # iteration 17: interrupt() must reach LangGraph, never swallowed
            raise
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
        if pending is not None:
            return "load_context"
        # D-155: a `comment` after an answer needs the history for its brief reply (D-152).
        if state.get("label") == "comment" and _has_answer_history(state):
            return "load_context"
        return "light"
    return "load_context"


def _after_context(state: TurnState) -> str:
    # TODO(14b/15/17): report -> writer/verifier/confirm_save, library -> library agent.
    if _errored(state) or state.get("route") == "clarify":
        return "finalize"
    if state.get("route") == "light":  # pending clarification not resolved (R2-M1), or a
        return "light"  # `comment` with no previous answer in the scope-covered history
    if state.get("status") == "comment":  # D-152: one brief reply, no analyst loop
        return "force_answer"
    return "quick" if state.get("label") == "simple" else "deep"


def _after_quick(state: TurnState) -> str:
    if _errored(state):
        return "finalize"
    return "deep" if state.get("status") == "escalate" else "force_answer"


def _after_role(state: TurnState) -> str:
    return "finalize" if _errored(state) else "force_answer"


def _after_force(state: TurnState) -> str:
    return "finalize" if _errored(state) else "grounding"


def _after_grounding(state: TurnState) -> str:
    # iteration 17: a report turn continues to the writer; every other label is unchanged
    if not _errored(state) and state.get("label") == "report":
        return "report_writer"
    return "finalize"


def _after_writer(state: TurnState) -> str:
    return CONFIRM_NODE if not _errored(state) and state.get("route") == "report" else "finalize"


def _pending_step(state: TurnState) -> str:
    return str((state.get("pending_action") or {}).get("step") or "")


def _build(ctx: TurnContext, checkpointer: Any) -> Any:
    nodes = _make_nodes(ctx)
    g = StateGraph(TurnState)
    for name, fn in nodes.items():
        g.add_node(name, _snapshotting(ctx, _safe(name, fn)))
    # iteration 22a: only code (a parsed user request) routes a turn into the delete flow
    g.add_conditional_edges(
        START,
        lambda _s: "delete_preview" if ctx.delete_request is not None else "input_guard",
        ["delete_preview", "input_guard"],
    )
    g.add_conditional_edges(
        "delete_preview",
        lambda s: (
            DELETE_CONFIRM_NODE
            if not _errored(s) and ctx.can_confirm and _pending_step(s) == "preview"
            else "finalize"
        ),
        [DELETE_CONFIRM_NODE, "finalize"],
    )
    g.add_conditional_edges(
        DELETE_CONFIRM_NODE,
        lambda s: (
            "execute_delete" if not _errored(s) and _pending_step(s) == "confirmed" else "finalize"
        ),
        ["execute_delete", "finalize"],
    )
    g.add_edge("execute_delete", "finalize")
    g.add_conditional_edges("input_guard", _after_guard, ["finalize", "light", "load_context"])
    g.add_conditional_edges(
        "load_context", _after_context, ["quick", "deep", "light", "force_answer", "finalize"]
    )
    g.add_conditional_edges("quick", _after_quick, ["deep", "force_answer", "finalize"])
    g.add_conditional_edges("deep", _after_role, ["force_answer", "finalize"])
    g.add_edge("light", "finalize")
    g.add_conditional_edges("force_answer", _after_force, ["grounding", "finalize"])
    g.add_conditional_edges("grounding", _after_grounding, ["report_writer", "finalize"])
    g.add_conditional_edges("report_writer", _after_writer, [CONFIRM_NODE, "finalize"])
    g.add_edge(CONFIRM_NODE, "finalize")  # iteration 17
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


class AgentGraph:
    """Runs one turn at a time per process. Per-session SQL state lives here across turns."""

    def __init__(self, services: GraphServices, checkpointer: Any) -> None:
        self.services = services
        self.checkpointer = checkpointer
        self._sql_sessions: dict[str, RunSqlSession] = {}
        # iteration 22a: user turns per session (in memory; a delete confirms only at
        # preview_turn + 1, and a restart expires any pending delete with the key)
        self._turn_seq: dict[str, int] = {}
        # MJ-1: the turn id of the reply that resumed a pending delete, per session (in
        # memory), so a Ctrl-C in that reply turn closes the delete it paused
        self._delete_reply_turn: dict[str, str] = {}

    def _next_turn(self, session: Session) -> None:
        sid = session.session_id
        self._turn_seq[sid] = self._turn_seq.get(sid, 0) + 1

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
        self._next_turn(session)
        return self._run(raw_text, session, turn_id or uuid.uuid4().hex[:12])

    def start_delete(
        self, args: str, *, session: Session, turn_id: str | None = None
    ) -> TurnResult:
        """``/delete <selector>`` (iteration 22a): the same preview as a natural-language
        request, without the intent words. Never raises."""
        self._next_turn(session)
        req = delete_flow.parse_delete_request(args, command=True)
        if req is None:
            req = delete_flow.DeleteRequest("phrase", error=delete_flow.SELECTOR_EMPTY)
        return self._run(args, session, turn_id or uuid.uuid4().hex[:12], delete_req=req)

    def _run(
        self,
        raw_text: str,
        session: Session,
        tid: str,
        forced_label: str | None = None,
        check_pending: bool = True,
        delete_req: delete_flow.DeleteRequest | None = None,
    ) -> TurnResult:
        try:
            sql_session = self._sql_session(session)
            ctx = _new_context(self.services, raw_text, session, sql_session, tid)
            ctx.can_confirm = self.checkpointer is not None  # iteration 17: interrupt() can pause
            ctx.forced_label = forced_label
            graph = _build(ctx, self.checkpointer)
            if self.checkpointer is not None:
                st = graph.get_state(self._config(session.session_id))
                values = dict(st.values or {})
                if values.get("owner") and values.get("owner") != session.profile.user_id:
                    # M1: another user's session is refused before any graph write, so its
                    # history, ledger and pending draft stay exactly as they were
                    return TurnResult(OTHER_OWNER_TEXT, outcome="refused")
                if values.get("aggregate_only"):  # D-162: survives a process restart
                    sql_session.aggregate_only = True
                # iteration 22a: a confirmed delete stranded before execute_delete (a crash
                # or Ctrl-C) is closed first, re-verified, never silently dropped (OD-10)
                if check_pending and DELETE_EXECUTE_NODE in tuple(st.next):
                    return self._close_stranded(raw_text, session, tid, forced_label,
                                                delete_req)  # fmt: skip
                # iteration 22a: the reply to a pending delete (only a user turn reaches here)
                if check_pending and DELETE_CONFIRM_NODE in tuple(st.next):
                    return self._answer_delete(ctx, graph, values, raw_text, session, tid,
                                               delete_req)  # fmt: skip
                # iteration 17: a reply to a pending report draft, or "save this as a report"
                if check_pending and CONFIRM_NODE in tuple(st.next):
                    if delete_req is not None:  # TR-14: no delete while a draft waits
                        return TurnResult(DELETE_WHILE_PENDING_TEXT, label="report",
                                          route="report", outcome="refused")  # fmt: skip
                    return self._answer_draft(ctx, graph, values, raw_text, session, tid)
                if check_pending and _SAVE_LAST_RE.match(_reply_forms(raw_text)[0]):
                    return self._save_last(ctx, values, session, tid)
            if delete_req is None and check_pending:  # deterministic: never the model
                delete_req = delete_flow.parse_delete_request(raw_text)
            if delete_req is not None:
                return self._start_delete(ctx, graph, delete_req, session, tid)
            start = {
                **_TURN_RESET,
                "turn_id": tid,
                "scope_snapshot": scope_snapshot(sql_session.scope),
                "owner": session.profile.user_id,
                "turn_ctx": {},
                "pending_action": {},
            }
            config, durable = self._config(session.session_id), self._durability()
            out = run_with_recursion_guard(lambda: graph.invoke(start, config, **durable))
        except Exception as exc:
            logger.error("turn failed: %s", type(exc).__name__)
            return TurnResult(ERROR_TEXT, outcome="error")
        return _result(ctx, out)

    # --- iteration 22a: the two-phase delete ---------------------------------------------------

    def _start_delete(
        self, ctx: TurnContext, graph: Any, req: delete_flow.DeleteRequest, session: Session,
        tid: str,
    ) -> TurnResult:  # fmt: skip
        """Preview a stated selector (AC-12.1). The raw text never enters state; the graph
        runs delete_preview, then pauses in confirm_delete (interrupt)."""
        if req.error:
            return TurnResult(delete_flow.SELECTOR_EMPTY_TEXT, label="delete", route="delete",
                              outcome="refused")  # fmt: skip
        if self.services.delete is None or self.checkpointer is None:
            return TurnResult(delete_flow.UNAVAILABLE_TEXT, label="delete", route="delete",
                              outcome="refused")  # fmt: skip
        ctx.delete_request = req
        ctx.delete_turn = self._turn_seq.get(session.session_id, 0)
        start = {
            **_TURN_RESET,
            "turn_id": tid,
            "scope_snapshot": scope_snapshot(ctx.sql_session.scope),
            "owner": session.profile.user_id,
            "turn_ctx": {},
            "pending_action": {},
        }
        config, durable = self._config(session.session_id), self._durability()
        out = run_with_recursion_guard(lambda: graph.invoke(start, config, **durable))
        return _result(ctx, out)

    def delete_reply_turn(self, session_id: str) -> str | None:
        """The turn id of the last reply that resumed a pending delete in ``session_id``."""
        return self._delete_reply_turn.get(session_id)

    def _close_stranded(
        self, raw: str, session: Session, tid: str, forced_label: str | None,
        delete_req: delete_flow.DeleteRequest | None,
    ) -> TurnResult:  # fmt: skip
        """MJ-1: a confirmed delete left before execute_delete (a crash, a kill) is never run
        by an unrelated next turn. It is expired (``stranded``, audited), the user is told
        nothing was deleted, and then the turn is answered. Only an explicit resume
        (``--resume``, :meth:`PendingTurn.finish`) may still execute it (OD-10)."""
        closed = self.open_resume(session).close_delete(delete_flow.STRANDED, tid)
        st = _build(_new_context(self.services, "", session, self._sql_session(session), ""),
                    self.checkpointer).get_state(self._config(session.session_id))  # fmt: skip
        if DELETE_EXECUTE_NODE in tuple(st.next):  # still stuck: never loop, never delete
            return closed
        res = self._run(raw, session, tid, forced_label=forced_label, delete_req=delete_req)
        return dataclasses.replace(res, text=f"{closed.text}\n\n{res.text}")

    def _answer_delete(
        self, ctx: TurnContext, graph: Any, values: dict[str, Any], raw: str, session: Session,
        tid: str, delete_req: delete_flow.DeleteRequest | None,
    ) -> TurnResult:  # fmt: skip
        """The next user turn after a preview. Only an exact confirmation carries a proof;
        anything else resumes with an empty proof (cancelled and audited) and is then
        answered as a normal turn with a fresh context (AC-12.3). A second delete request
        cancels the pending one (AC-12.13: one pending delete per session)."""
        svc = self.services.delete
        pa = values.get("pending_action") or {}
        ctx.turn_id = tid  # the confirmation turn is audited under its own id
        ctx.delete_turn = self._turn_seq.get(session.session_id, 0)
        config, durable = self._config(session.session_id), self._durability()
        self._delete_reply_turn[session.session_id] = tid  # MJ-1: a Ctrl-C here closes it

        def resume(payload: dict[str, str]) -> TurnResult:
            out = run_with_recursion_guard(
                lambda: graph.invoke(Command(resume=payload), config, **durable)
            )
            return _result(ctx, out)

        cancel = {"reply": "", "pending_action_id": str(pa.get("pending_action_id") or ""),
                  "proof_sha256": ""}  # fmt: skip
        if delete_req is not None or delete_flow.parse_delete_request(raw) is not None:
            resume(cancel)
            return TurnResult(delete_flow.DELETE_PENDING_TEXT, label="delete", route="delete",
                              outcome="delete_cancelled")  # fmt: skip
        if delete_flow.is_confirm(raw, pa.get("count")):
            return resume(svc.reply_payload(raw, pa) if svc is not None else cancel)
        closed = resume(cancel)
        res = self._run(raw, session, tid, check_pending=False)
        return dataclasses.replace(res, text=f"{closed.text}\n\n{res.text}")

    # --- iteration 17: the confirm-before-save answer and "save this as a report" -------------

    def _answer_draft(
        self, ctx: TurnContext, graph: Any, values: dict[str, Any], raw: str, session: Session,
        tid: str,
    ) -> TurnResult:  # fmt: skip
        """Classify the reply to a pending draft. Only an explicit save stores it (AC-06.1);
        revise is a new turn with a full REPORT budget, cancel or anything else saves nothing
        (AC-06.4); a delete request is refused and the draft stays pending (TR-14)."""
        reply, folded = _reply_forms(raw)
        config, durable = self._config(session.session_id), self._durability()

        def resume(decision: str) -> TurnResult:
            ctx.turn_id = ctx.sql_turn.turn_id = str(values.get("turn_id") or "")
            if not _restore_ctx(ctx, values.get("turn_ctx")):
                logger.error("report confirm: turn context malformed; budget exhausted")
            out = run_with_recursion_guard(
                lambda: graph.invoke(Command(resume=decision), config, **durable)
            )
            return _result(ctx, out)

        # TR-14 (m1): delete intent is checked FIRST ("revise and delete ...", "save, then
        # wipe ..." are refused); no delete while a draft waits; the state is untouched
        if _DELETE_RE.search(folded):
            return TurnResult(DELETE_WHILE_PENDING_TEXT, label="report", route="report",
                              outcome="refused")  # fmt: skip
        if _SAVE_RE.match(reply) or _SAVE_LAST_RE.match(reply):
            if values.get("scope_snapshot") != scope_snapshot(ctx.sql_session.scope):
                resume("cancel")  # m4: access changed since the draft: dropped, never saved
                return TurnResult(SCOPE_CHANGED_TEXT, label="report", route="report",
                                  outcome="report_cancelled")  # fmt: skip
            return resume("save")
        if _CANCEL_RE.match(reply):
            return resume("cancel")
        revise = _REVISE_RE.match(reply)
        if revise:
            resume("revise")  # the old draft is closed unsaved before the new turn starts
            base = values.get("context_message") or values.get("message") or ""
            changes = " ".join(revise.group("changes").split())
            text = f"{base}\n\nRevision request: {changes}" if changes else base
            # M3: under the caller's turn id, so a Ctrl-C close (close_interrupted_turn) matches
            res = self._run(text, session, tid, "report", check_pending=False)
            return dataclasses.replace(res, text=f"{REVISING_TEXT}\n\n{res.text}")
        resume("cancel")  # an unrelated message: the draft is dropped and the agent says so
        res = self._run(raw, session, tid, check_pending=False)
        return dataclasses.replace(res, text=f"{NOT_SAVED_TEXT}\n\n{res.text}")

    def _save_last(
        self, ctx: TurnContext, values: dict[str, Any], session: Session, tid: str
    ) -> TurnResult:
        """AC-21.2: "save this as a report" turns the last in-scope answer into a report and
        saves it at once (the request is the confirmation). Idempotent: a repeat finds the
        saved report by its key before any LLM call. The graph history is not changed."""
        sv = self.services
        if sv.reports is None:
            return TurnResult(SAVE_DISABLED_TEXT, label="report", route="report",
                              outcome="report_unsaved")  # fmt: skip
        if values.get("owner") != session.profile.user_id:  # M1: defence in depth
            return TurnResult(NOTHING_TO_SAVE_TEXT, label="report", route="report",
                              outcome="report_unsaved")  # fmt: skip
        if values.get("outcome") == "report_cancelled":  # m5: a cancelled draft stays unsaved
            return TurnResult(CANCELLED_NOTHING_TO_SAVE_TEXT, label="report", route="report",
                              outcome="report_unsaved")  # fmt: skip
        scope = ctx.sql_session.scope
        history = [h for h in values.get("history") or [] if covers(scope, h.get("scope"))]
        answer_i = next(
            (i for i in range(len(history) - 1, -1, -1) if history[i].get("role") == "assistant"),
            None,
        )
        before = history[:answer_i] if answer_i is not None else []
        question = next(
            (h.get("text", "") for h in reversed(before) if h.get("role") == "user"), ""
        )
        ledger = [
            {k: v for k, v in e.items() if k != "scope"}
            for e in values.get("prior_ledger") or []
            if covers(scope, e.get("scope"))
        ][-6:]
        if answer_i is None or not question or not ledger:
            return TurnResult(NOTHING_TO_SAVE_TEXT, label="report", route="report",
                              outcome="report_unsaved")  # fmt: skip
        answer = str(history[answer_i].get("text") or "")
        owner = session.profile.user_id
        key = hashlib.sha256(f"last:{session.session_id}:{owner}:{answer}".encode()).hexdigest()
        found = sv.reports.get_by_key(key, owner)
        if found is not None:
            text = f'Already saved report "{found.title}" (id {display_id(found.report_id)}).'
            return TurnResult(text, label="report", route="report", outcome="report_saved")
        _promote_report(ctx)
        rep = _build_report(
            ctx, question=question, analysis=answer, ledger=ledger,
            figures=scoped_figures(values.get("figures"), scope),
        )  # fmt: skip
        if rep is None:
            out = {"final_text": SAVE_FAILED_TEXT, "outcome": "report_unsaved"}
        else:
            out = _store_report(ctx, rep, turn_id=tid, key=key)
        return _result(ctx, {**out, "label": "report", "route": "report"})

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
        values = dict(snap.values)
        if values.get("aggregate_only") and values.get("owner") in (
            None, "", session.profile.user_id
        ):  # D-162: a resumed turn keeps the session's aggregate-only mode
            ctx.sql_session.aggregate_only = True
        return PendingTurn(self, session, (ctx, graph), tuple(snap.next), values)


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
        if DELETE_CONFIRM_NODE in self.next:  # iteration 22a: re-shown, never confirmed here
            svc = self.agent.services.delete
            pa = self.values.get("pending_action") or {}
            why = "key_changed" if svc is None else svc.lapse_reason(pa)
            if why is not None:  # a restart made a new K_delete, or the preview lapsed:
                # close it (EXPIRED, turn closed) so the next delete request previews afresh
                tid = str(self.values.get("turn_id") or "") or uuid.uuid4().hex
                return self.close_delete(why, tid)
            text = self.values.get("final_text") or ERROR_TEXT
            return TurnResult(text, label="delete", route="delete", outcome="delete_pending")
        if DELETE_EXECUTE_NODE in self.next:  # 22a M1: stopped between confirm and execute
            return self._finish_delete()
        if CONFIRM_NODE in self.next:  # iteration 17: a pending draft is re-shown, never saved
            saved = self._already_saved()
            if saved is not None:
                return saved
            text = self.values.get("final_text") or ERROR_TEXT
            return TurnResult(text, label="report", route="report", outcome="report_pending")
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

    def close_delete(self, reason: str, turn_id: str) -> TurnResult:
        """MJ-1: close a delete paused in confirm_delete or stranded before execute_delete
        WITHOUT running it: ``delete.cancelled`` (``interrupted``, a Ctrl-C) or
        ``delete.expired`` (``stranded``), then the turn is closed in the checkpoint as if
        finalize ran. The service drops the proof first, so even a failed state write can
        never lead to a delete. Never raises."""
        if self._built is None or not {DELETE_CONFIRM_NODE, DELETE_EXECUTE_NODE} & set(self.next):
            return TurnResult(ERROR_TEXT, outcome="error")
        ctx, graph = self._built
        svc = self.agent.services.delete
        pa = self.values.get("pending_action") or {}
        if svc is None or not pa:  # the feature went off while pending: nothing is deleted
            st = delete_flow.Step("expired", delete_flow.EXPIRED_TEXT, error=reason)
        else:
            st = svc.abandon(pa, reason, owner=self.session.profile.user_id,
                             session_id=self.session.session_id, turn_id=turn_id)  # fmt: skip
        try:
            _delete_spans(ctx, st, pa.get("pending_action_id"))
            graph.update_state(
                self.agent._config(self.session.session_id),
                {**_DELETE_ROUTE, "pending_action": {}, "final_text": st.text,
                 "outcome": st.outcome},
                as_node="finalize",
            )  # fmt: skip
        except Exception as exc:
            logger.error("delete close failed: %s", type(exc).__name__)
            return TurnResult(delete_flow.UNSAFE_TEXT, label="delete", route="delete",
                              outcome="delete_unsafe")  # fmt: skip
        return TurnResult(st.text, label="delete", route="delete", outcome=st.outcome)

    def _finish_delete(self) -> TurnResult:
        """Run ONLY execute_delete. It re-verifies before deleting: an executed delete
        reports its recorded count; a new K_delete (restart) or a confirm older than
        ``EXECUTE_GRACE_S`` writes ``delete.expired`` and deletes nothing (OD-10)."""
        ctx, graph = self._built
        try:
            ctx.turn_id = ctx.sql_turn.turn_id = str(self.values.get("turn_id") or "")
            config = self.agent._config(self.session.session_id)
            durable = self.agent._durability()
            out = run_with_recursion_guard(lambda: graph.invoke(None, config, **durable))
        except Exception as exc:
            logger.error("resume: delete finish failed: %s", type(exc).__name__)
            return TurnResult(delete_flow.UNSAFE_TEXT, label="delete", route="delete",
                              outcome="delete_unsafe")  # fmt: skip
        return _result(ctx, out)

    def _already_saved(self) -> TurnResult | None:
        """m3: a crash after the store write but before the checkpoint leaves a saved draft
        pending. Then the turn is closed (as if finalize ran) and the user is told; it is
        never saved twice. None (re-show the draft) when not saved or on any error."""
        reports = self.agent.services.reports
        rep = self.values.get("report") or {}
        if reports is None or not rep or self._built is None:
            return None
        try:
            key = _confirm_key(str(self.values.get("turn_id") or ""), rep)
            found = reports.get_by_key(key, self.session.profile.user_id)
            if found is None:
                return None
            text = f'Already saved report "{found.title}" (id {display_id(found.report_id)}).'
            self._built[1].update_state(
                self.agent._config(self.session.session_id),
                {"outcome": "report_saved", "final_text": text},
                as_node="finalize",
            )
        except Exception as exc:
            logger.error("resume: saved-draft check failed: %s", type(exc).__name__)
            return None
        return TurnResult(text, label="report", route="report", outcome="report_saved")


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
