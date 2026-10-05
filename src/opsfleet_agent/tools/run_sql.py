"""``run_sql``: the only model-facing tool that touches BigQuery (iteration 13; HLD §4.4, §5.1).

**Order.** HLD §5.1 lists ten steps. The building blocks fuse some of them, and the
differencing guard (iteration 10) splits step 7 in two. The order used here:

====  ===========================================================  ==========================
HLD   what runs                                                    code
====  ===========================================================  ==========================
1-3   policy check, scope rewrite into ``__p/__oi/__o/__u``,        ``apply_scope``
      re-resolve and verify (``apply_scope`` calls ``check_sql``)
4     small cell / QI: rewrite, population check or refusal         ``apply_small_cell``
4a    duplicate in this turn (same scoped statement): refused   ``_statement_key``
      here, so it records no fingerprint and runs nothing
4b    population check only: verify, dry run + caps, run (SQL       ``_population``
      budget), read ``population`` from the *executed* result,
      ``evaluate``
7a    differencing **prepare** (pure, before any execution):        ``_diff_prepare``
      refuses a near pair it can already decide, else returns the
      plan whose ``query`` (with ``plan.injected_columns``, i.e.
      ``_cell_customers`` and/or ``_cell_rows``) is the only
      statement allowed to run
5     post-rewrite invariant on the statement that will run         ``verify_scoped``
      (``plan.query.sql``), fail closed
6     dry run + per-query and session caps                          ``BigQueryRunner.prepare``
8     execute with ``maximum_bytes_billed``, ``job_timeout_ms``,    ``BigQueryRunner.execute``
      labels; errors mapped to fixed codes
7b    differencing **release** (on a memo hit too), before any      ``_diff_release``
      row reaches the model; the guard drops the injected columns
      from the rows, this module drops them from ``columns``
9     scrub cell values and column names                            ``_scrub_rows``
10    200-row cap with a truncation flag                            ``_cap``
====  ===========================================================  ==========================

Deviation from the HLD text, on purpose: differencing (step 7) is split into a pre-execution
half (7a, *before* the dry run, so a refused near pair never costs bytes) and a
post-execution half (7b, which needs the per-cell customer counts that only the executed
statement returns). The invariant (5) and the dry run (6) run on ``plan.query``, the
statement that actually executes. Nothing reaches ``execute`` before 1-4, 7a, 5 and 6 pass.

**Differencing coupling.** All calls into :mod:`opsfleet_agent.guards.differencing` go through
``_diff_prepare`` / ``_diff_release`` / ``_diff_abandon`` / ``_diff_unavailable`` below.
Wiring rules (security review of iteration 10, each with its own test):

(a) calls are serialised by one lock per tool instance: prepare -> execute -> release of one
    call never interleaves with another;
(b) only a plan returned by ``guard.prepare`` for this exact query is executed and released;
    this module never builds a plan itself;
(c) a population passed to the guard comes only from the executed population query; the
    tool's arguments are a closed schema (``sql``, ``purpose``), so the model cannot pass one;
(d) a memo hit still calls ``prepare`` and then ``release`` with that fresh plan (the memo
    stores the executed rows *with* the injected columns, and its key is ``plan.query.sql``);
    rows reach the model only from ``release``, never straight from the memo. A statement
    already stored in this turn is refused as a duplicate *before* ``release``, so it
    records no fingerprint;
(e) every plan is released at most once, and every plan is closed: whatever happens after
    a successful ``prepare`` (invariant, dry run over a cap, BigQuery error, timeout,
    duplicate, an exception), a ``finally`` calls ``guard.abandon(plan)``, and the adapter's
    own plan checks (wrong scope, wrong parameters) abandon the plan before refusing. ``abandon`` is
    idempotent, records nothing, and is a no-op after ``release``, so the open-plan count is
    back to zero after every call;
(f) "privacy check unavailable" is detected only with
    ``differencing.unavailable_cause(refusal) is not None``; the guard's refusal is passed on
    unchanged (its cause and hint, ``CAPACITY_HINT`` for a full store), never rebuilt here.

**Lock timeout.** Derived from the runner's deadlines and the retry policy
(:func:`default_lock_timeout_s` > :func:`worst_case_call_s`), so a waiting call never times out
while a legitimate one is still inside its bounded worst case.

**Trace.** The ``sql`` span gets the scoped statement *before* differencing injection (the
injected ``_cell_*`` columns are guard internals), and so do the ledger ``sql`` and
``sql_hash`` (shown to the writer and verifier, and stable across fingerprint history). The hash
of the statement that actually ran is kept apart as ``executed_sql_hash`` (ledger and audit).
The tracer replaces every literal (``sanitize_sql``) and a test pins that behaviour
(``test_trace_strips_sql_literals``).

**Scope parameters.** ``@scope_brands`` must be set on the dry run **and** the real run.
:class:`BigQueryRunner` has no ``query_parameters`` argument, so the runner must be built with
:func:`scoped_job_config_factory`, which reads the parameters of the query in flight from a
context variable. A scoped query on a runner without that factory is refused
(``rewrite_invariant``) before any BigQuery call.

Errors never carry provider text, SQL fragments or values: BigQuery failures arrive as
:class:`BqFailure` (fixed messages); the trace and the audit record get reason codes only.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import decimal
import math
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from opsfleet_agent.bq.client import (
    DEFAULT_API_TIMEOUT_S,
    DEFAULT_JOB_TIMEOUT_MS,
    RESULT_GRACE_S,
    BigQueryRunner,
    QueryResult,
    SessionByteBudget,
    SqlCounter,
)
from opsfleet_agent.bq.errors import BqFailure, ErrorCode, Stage
from opsfleet_agent.bq.memo import QueryMemo, make_memo_key, run_memoised, sql_hash
from opsfleet_agent.guards import differencing as _differencing
from opsfleet_agent.guards import pii_regex
from opsfleet_agent.guards.scope import (
    ProductScope,
    ScopedQuery,
    ScopeInvariantError,
    ScopeRefusal,
    ScopeRule,
    apply_scope,
    verify_scoped,
)
from opsfleet_agent.guards.small_cell import (
    DEFAULT_K,
    PopulationCheck,
    SmallCellRewrite,
    apply_small_cell,
)
from opsfleet_agent.guards.sql_policy import MAX_SQL_CHARS, Rule

__all__ = [
    "MAX_PURPOSE_CHARS",
    "POPULATION_COLUMN",
    "TOOL_NAME",
    "RunSqlSession",
    "RunSqlTool",
    "RunSqlTurn",
    "scoped_job_config_factory",
    "worst_case_call_s",
]

TOOL_NAME: Final = "run_sql"
MAX_PURPOSE_CHARS: Final = 200
MAX_ROWS: Final = 200
MAX_CONSECUTIVE_FAILURES: Final = 3  # 1 attempt + 2 corrections (HLD §4.4)
UNAVAILABLE_RETRIES: Final = 1  # BQ_UNAVAILABLE: one retry after the delay, then stop the turn
DEFAULT_RETRY_DELAY_S: Final = 2.0
#: Lock wait = worst-case legitimate call x margin + slack (see :func:`worst_case_call_s`).
LOCK_TIMEOUT_MARGIN: Final = 1.25
LOCK_TIMEOUT_SLACK_S: Final = 60.0
STATEMENTS_PER_CALL: Final = 2  # at most a population query and the answer query
POPULATION_COLUMN: Final = "population"  # the one column of a small-cell population query

# Codes this module adds to the bq package's ErrorCode.
SQL_POLICY: Final = "SQL_POLICY"
SQL_TOO_LONG: Final = "SQL_TOO_LONG"
INVALID_ARGS: Final = "INVALID_ARGS"
GIVE_UP: Final = "GIVE_UP"
DUPLICATE_QUERY: Final = "DUPLICATE_QUERY"
TOOL_BUSY: Final = "TOOL_BUSY"

_RETRYABLE: Final = frozenset(
    {
        ErrorCode.SQL_SYNTAX.value,
        ErrorCode.UNKNOWN_COLUMN.value,
        ErrorCode.BQ_RUNTIME.value,
        ErrorCode.TIMEOUT.value,
        ErrorCode.COST_CAP.value,
        SQL_POLICY,
        SQL_TOO_LONG,
        INVALID_ARGS,
    }
)
# SQL_POLICY rules a corrected query cannot fix.
_FINAL_RULES: Final = frozenset({ScopeRule.EMPTY_SCOPE.value, ScopeRule.SCOPE_INVALID.value})

_MESSAGES: Final[dict[str, tuple[str, str]]] = {
    SQL_POLICY: ("The query was refused by the data policy.", ""),
    SQL_TOO_LONG: (
        "The query is too long.",
        f"Write a shorter query (at most {MAX_SQL_CHARS} characters).",
    ),
    INVALID_ARGS: (
        "The tool call arguments are invalid.",
        "Call run_sql with exactly two string fields: sql and purpose "
        f"(purpose at most {MAX_PURPOSE_CHARS} characters).",
    ),
    GIVE_UP: (
        "Several query attempts failed in a row.",
        "Stop querying for this question; answer with what is known or explain what failed.",
    ),
    DUPLICATE_QUERY: (
        "This exact query already ran in this turn.",
        "Use the earlier result (see query_id) instead of running it again.",
    ),
    TOOL_BUSY: (
        "Another query is still running.",
        "Do not retry this turn; answer from the results already gathered.",
    ),
}
_DIFF_UNAVAILABLE_MESSAGE: Final = "The privacy check is unavailable right now."
#: D-154: a refused table or source is fixable; the hint lists the allowed tables and columns.
SOURCE_NOT_ALLOWED_MESSAGE: Final = (
    "The query used a table or source that is not available. Rewrite it with the allowed "
    "tables and columns in the hint and run it again."
)

EMPTY_HINT_FIRST: Final = (
    "The query returned no rows. Check the filters: the date window against the data "
    "range, the spelling of categories and brands (look them up with SELECT DISTINCT "
    "category), and status values (Complete, Shipped, Processing, Cancelled, Returned)."
)
EMPTY_HINT_FINAL: Final = (
    "The query again returned no rows. Do not query again: tell the user that no rows "
    "matched and which filters were applied."
)
TRUNCATED_HINT: Final = (
    f"Only the first {MAX_ROWS} rows are shown. Aggregate further or add ORDER BY ... LIMIT."
)


# --------------------------------------------------------------------------- lock deadline


def worst_case_call_s(runner: BigQueryRunner, retry_delay_s: float) -> float:
    """Upper bound, in seconds, on one legitimate ``run_sql`` call (security review M-1).

    Per statement and attempt: the dry run (one API call, plus its bounded API retry, both
    capped at ``api_timeout_s``), the execute deadline (``total_timeout_s`` = create job +
    ``job_timeout_ms`` + grace) and one best-effort cancel (``api_timeout_s``). Each statement
    gets ``UNAVAILABLE_RETRIES + 1`` attempts with the retry delay in between, and a call runs
    at most :data:`STATEMENTS_PER_CALL` statements. Local work (store, memo) is covered by the
    slack in :func:`default_lock_timeout_s`.
    """
    api = float(getattr(runner, "api_timeout_s", DEFAULT_API_TIMEOUT_S))
    total = float(
        getattr(
            runner,
            "total_timeout_s",
            api + DEFAULT_JOB_TIMEOUT_MS / 1000 + RESULT_GRACE_S,
        )
    )
    attempt = 2 * api + total + api
    attempts = UNAVAILABLE_RETRIES + 1
    statement = attempts * attempt + UNAVAILABLE_RETRIES * max(0.0, float(retry_delay_s))
    return STATEMENTS_PER_CALL * statement


def default_lock_timeout_s(runner: BigQueryRunner, retry_delay_s: float) -> float:
    """How long a call waits for the previous one: always above :func:`worst_case_call_s`."""
    return worst_case_call_s(runner, retry_delay_s) * LOCK_TIMEOUT_MARGIN + LOCK_TIMEOUT_SLACK_S


# --------------------------------------------------------------------------- scope parameters

_PARAMS: contextvars.ContextVar[ScopedQuery | None] = contextvars.ContextVar(
    "run_sql_scoped_query", default=None
)


def _bigquery_job_config(**kwargs: Any) -> Any:
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(**kwargs)


def scoped_job_config_factory(
    inner: Callable[..., Any] = _bigquery_job_config,
) -> Callable[..., Any]:
    """A ``job_config_factory`` for :class:`BigQueryRunner` that adds the scope parameters of
    the query in flight to **every** job config it builds (dry run and real run alike)."""

    def factory(**kwargs: Any) -> Any:
        scoped = _PARAMS.get()
        if scoped is not None and scoped.parameters:
            kwargs = {**kwargs, "query_parameters": scoped.bigquery_parameters()}
        return inner(**kwargs)

    factory.injects_query_parameters = True  # type: ignore[attr-defined]
    return factory


def _injects_parameters(runner: BigQueryRunner) -> bool:
    # BigQueryRunner keeps its factory private; owner item: add query_parameters to client.py.
    return getattr(getattr(runner, "_job_config", None), "injects_query_parameters", False) is True


@contextlib.contextmanager
def _parameters(scoped: ScopedQuery) -> Iterator[None]:
    token = _PARAMS.set(scoped)
    try:
        yield
    finally:
        _PARAMS.reset(token)


# --------------------------------------------------------------------------- differencing adapter
# The only place that touches the differencing guard's API (iteration 10 rework, keep it thin).

DifferencingGuard = _differencing.DifferencingGuard


def _diff_prepare(
    guard: Any,
    result: SmallCellRewrite | PopulationCheck,
    scope: ProductScope,
    *,
    user_id: str,
    session_id: str,
    population: int | None = None,
) -> Any | ScopeRefusal:
    """Return the guard's plan (an opaque object whose ``.query`` is the statement to run) or
    a refusal. Anything that is not a plan from this guard for this scope fails closed."""
    try:
        if population is None:
            plan = guard.prepare(result, scope, user_id=user_id, session_id=session_id)
        else:
            plan = guard.prepare(
                result, scope, user_id=user_id, session_id=session_id, population=population
            )
    except Exception:  # noqa: BLE001 - a guard bug must not let a query through
        return ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT)
    if isinstance(plan, ScopeRefusal):
        return plan
    if not isinstance(plan, _differencing.DifferencingPlan):
        _diff_abandon(guard, plan)  # a no-op for a foreign object; kept for uniformity
        return ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT)
    query = getattr(plan, "query", None)
    expected = result.then_run if isinstance(result, PopulationCheck) else result.query
    if (
        not isinstance(query, ScopedQuery)
        or query.scope_key != scope.scope_key
        or query.parameters != expected.parameters  # same brands as the scoped query (L-2)
    ):
        _diff_abandon(guard, plan)  # (e): the guard issued this plan; it will never run
        return ScopeRefusal.for_rule(ScopeRule.SCOPE_INVALID)
    return plan


def _diff_query(plan: Any) -> ScopedQuery:
    return plan.query  # type: ignore[no-any-return]


_KNOWN_INJECTED: Final = frozenset(
    {_differencing.CELL_COUNT_COLUMN, _differencing.ROW_COUNT_COLUMN}
)


def _diff_injected_columns(plan: Any) -> frozenset[str]:
    """The columns the guard injected into ``plan.query`` (empty when it injected none).
    Only the guard's own column names count, so a model column is never dropped (L-4)."""
    columns = getattr(plan, "injected_columns", ())
    return frozenset(c for c in columns if c in _KNOWN_INJECTED)


def _diff_abandon(guard: Any, plan: Any) -> None:
    """Close ``plan`` if it is still open (rule (e)). Idempotent and a no-op after release;
    a guard without ``abandon`` or a failing ``abandon`` never masks the call's outcome."""
    abandon = getattr(guard, "abandon", None)
    if callable(abandon):
        with contextlib.suppress(Exception):
            abandon(plan)


def _diff_release(
    guard: Any, plan: Any, rows: list[dict[str, Any]], *, truncated: bool
) -> list[dict[str, Any]] | ScopeRefusal:
    """Release the executed rows of ``plan`` (the same object ``_diff_prepare`` returned).
    The guard redeems the plan once, maps its own store failures to a refusal, and drops every
    injected column from the rows it returns. An exception here is a guard bug: fail closed
    as an invariant failure (never a home-made "unavailable" refusal, rule (f)).
    ``truncated`` is the client's row-cap flag: a capped result is a top-N cut (R5-H1)."""
    try:
        out = guard.release(plan, rows, truncated=truncated)
    except Exception:  # noqa: BLE001 - fail closed
        return ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT)
    if isinstance(out, ScopeRefusal):
        return out
    if not isinstance(out, _differencing.Released):
        return ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT)
    return [dict(r) for r in out.rows]


def _diff_unavailable(refusal: ScopeRefusal) -> bool:
    """True when the refusal means 'privacy check unavailable' (not retryable), rule (f)."""
    return _differencing.unavailable_cause(refusal) is not None


# --------------------------------------------------------------------------- state


_ARG_KEYS: Final = frozenset({"sql", "purpose"})


@dataclass(frozen=True)
class RunSqlArgs:
    """The closed argument schema (HLD §4.4): exactly ``sql`` and ``purpose``, both strings."""

    sql: str
    purpose: str


@dataclass
class RunSqlSession:
    """Per-session state: who, which scope, the byte budget and the memo."""

    user_id: str
    session_id: str
    scope: ProductScope | None
    bytes: SessionByteBudget = field(default_factory=SessionByteBudget)
    memo: QueryMemo[QueryResult] = field(default_factory=QueryMemo)


@dataclass
class RunSqlTurn:
    """Per-turn state. ``ledger`` is the turn's SQL ledger (scoped SQL actually run)."""

    turn_id: str
    sql_counter: SqlCounter | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    gave_up: bool = False
    bq_unavailable: bool = False
    seen: dict[str, str | None] = field(default_factory=dict)  # model sql hash -> query_id
    # scoped statement (after small cell, before differencing) -> query_id; checked before
    # the population query and before differencing, so a duplicate records nothing (M-2)
    statements: dict[str, str | None] = field(default_factory=dict)
    empty_results: int = 0
    ledger: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Failure:
    code: str
    rule: str | None = None
    message: str = ""
    hint: str = ""
    retryable: bool = False
    error_class: str | None = None
    stage: str | None = None
    identifier: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def envelope(self) -> dict[str, Any]:
        err: dict[str, Any] = {
            "code": self.code,
            "rule": self.rule,
            "message": self.message,
            "retryable": self.retryable,
            "hint": self.hint,
        }
        if self.code == ErrorCode.UNKNOWN_COLUMN.value and self.identifier:
            err["identifier"] = self.identifier
        err.update(self.extra)
        return {"ok": False, "error": err}


def _from_refusal(refusal: ScopeRefusal) -> _Failure:
    code = refusal.error_code or SQL_POLICY
    if _diff_unavailable(refusal):
        return _Failure(
            code=code, rule=refusal.rule, message=_DIFF_UNAVAILABLE_MESSAGE, hint=refusal.hint
        )
    message = _MESSAGES.get(code, _MESSAGES[SQL_POLICY])[0]
    if refusal.reason_code == Rule.SOURCE_NOT_ALLOWED.value:
        message = SOURCE_NOT_ALLOWED_MESSAGE  # D-154: rewrite, do not give up
    return _Failure(
        code=code,
        rule=refusal.rule,
        message=message,
        hint=refusal.hint,
        retryable=code in _RETRYABLE and refusal.reason_code not in _FINAL_RULES,
    )


def _from_bq(f: BqFailure) -> _Failure:
    return _Failure(
        code=f.code.value,
        message=f.message,
        hint=f.hint,
        retryable=f.code.value in _RETRYABLE,
        error_class=f.error_class.value if f.error_class is not None else None,
        stage=f.stage.value,
        identifier=f.identifier,
    )


def _fixed(code: str, **extra: Any) -> _Failure:
    message, hint = _MESSAGES[code]
    return _Failure(
        code=code, message=message, hint=hint, retryable=code in _RETRYABLE, extra=extra
    )


# --------------------------------------------------------------------------- scrub and cap


def _default_scrubber(text: str) -> str:
    return pii_regex.scrub(text).text


def _json_safe(value: Any, scrub: Callable[[str], str], depth: int = 0) -> Any:
    if depth > 8:
        return None
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, decimal.Decimal):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        return "[binary]"
    if isinstance(value, Mapping):
        return {scrub(str(k)): _json_safe(v, scrub, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v, scrub, depth + 1) for v in value]
    return scrub(str(value))


def _scrub_rows(
    columns: list[str], rows: list[dict[str, Any]], scrub: Callable[[str], str]
) -> tuple[list[str], list[dict[str, Any]]]:
    # Names that scrub to the same text (two ``<EMAIL>``) get ``_2``, ``_3``... in column
    # order, so no value overwrites another (L-3).
    names: dict[Any, str] = {}
    used: set[str] = set()

    def unique(key: Any) -> str:
        if key not in names:
            base = scrub(str(key))
            name, n = base, 1
            while name in used:
                n += 1
                name = f"{base}_{n}"
            used.add(name)
            names[key] = name
        return names[key]

    out_columns = [unique(c) for c in columns]
    out_rows = []
    for row in rows:
        clean: dict[str, Any] = {}
        for key, value in row.items():
            clean[unique(key)] = _json_safe(value, scrub)
        out_rows.append(clean)
    return out_columns, out_rows


def _cap(rows: list[dict[str, Any]], truncated: bool) -> tuple[list[dict[str, Any]], bool]:
    return rows[:MAX_ROWS], truncated or len(rows) > MAX_ROWS


# --------------------------------------------------------------------------- the tool


class RunSqlTool:
    """Executor of ``run_sql`` tool calls. One instance per process; calls are serialised."""

    name = TOOL_NAME

    def __init__(
        self,
        runner: BigQueryRunner,
        guard: Any,
        refresh_date: Callable[[], date],
        *,
        k: int = DEFAULT_K,
        tracer: Any = None,
        audit: Callable[[dict[str, Any]], object] | None = None,
        cell_scrubber: Callable[[str], str] = _default_scrubber,
        sleep: Callable[[float], object] = time.sleep,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
        lock_timeout_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runner = runner
        self.guard = guard
        self.refresh_date = refresh_date
        self.k = k
        self.tracer = tracer
        self.audit = audit
        self.scrub = cell_scrubber
        self.sleep = sleep
        self.retry_delay_s = retry_delay_s
        # None: derived from the runner's deadlines (M-1). An explicit value is for tests.
        self.lock_timeout_s = (
            default_lock_timeout_s(runner, retry_delay_s)
            if lock_timeout_s is None
            else lock_timeout_s
        )
        self.clock = clock
        self._lock = threading.Lock()  # (a): one prepare -> execute -> release at a time

    # -- entry point --

    def run(self, args: object, session: RunSqlSession, turn: RunSqlTurn) -> dict[str, Any]:
        start = self.clock()
        if not self._lock.acquire(timeout=self.lock_timeout_s):
            failure = _fixed(TOOL_BUSY)
            self._observe(failure, None, args, session, turn, start, model_hash=None)
            return self._envelope_failure(failure, start)
        try:
            return self._run_locked(args, session, turn, start)
        finally:
            self._lock.release()

    def _run_locked(
        self, args: object, session: RunSqlSession, turn: RunSqlTurn, start: float
    ) -> dict[str, Any]:
        parsed = self._parse(args)
        model_hash = sql_hash(parsed.sql) if isinstance(parsed, RunSqlArgs) else None
        if isinstance(parsed, _Failure):
            outcome: _Failure | _Success = parsed
        elif turn.gave_up:
            outcome = self._give_up(turn)
        elif turn.bq_unavailable:
            outcome = _from_bq(BqFailure(code=ErrorCode.BQ_UNAVAILABLE, stage=Stage.PRECHECK))
        elif model_hash in turn.seen:
            outcome = _fixed(DUPLICATE_QUERY, query_id=turn.seen[model_hash])
        else:
            try:
                outcome = self._pipeline(parsed, session, turn)
            except ScopeInvariantError:
                outcome = _from_refusal(ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT))
            except Exception:  # noqa: BLE001 - fail closed, no text
                outcome = _from_refusal(ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT))
        outcome = self._account(outcome, turn, model_hash)
        self._observe(outcome, parsed, args, session, turn, start, model_hash=model_hash)
        if isinstance(outcome, _Failure):
            return self._envelope_failure(outcome, start)
        return outcome.envelope(self.clock() - start)

    # -- argument schema --

    def _parse(self, args: object) -> RunSqlArgs | _Failure:
        # The schema hint only: never echo the arguments. A ``population`` (or any other)
        # field is refused here, so the model cannot feed the differencing guard (c).
        if not isinstance(args, Mapping) or set(args) != _ARG_KEYS:
            return _fixed(INVALID_ARGS)
        sql, purpose = args["sql"], args["purpose"]
        if type(sql) is not str or type(purpose) is not str:
            return _fixed(INVALID_ARGS)
        if len(sql) > MAX_SQL_CHARS:
            return _fixed(SQL_TOO_LONG)
        if not sql.strip() or len(purpose) > MAX_PURPOSE_CHARS:
            return _fixed(INVALID_ARGS)
        return RunSqlArgs(sql=sql, purpose=purpose)

    # -- steps 1..10 --

    def _pipeline(
        self, args: RunSqlArgs, session: RunSqlSession, turn: RunSqlTurn
    ) -> _Failure | _Success:
        scope = session.scope
        # 1-3: policy, scope rewrite, re-resolve + verify
        scoped = apply_scope(args.sql, scope)
        if isinstance(scoped, ScopeRefusal):
            return _from_refusal(scoped)
        assert scope is not None  # apply_scope refuses a None scope
        # 4: small cell
        sc = apply_small_cell(scoped, scope, self.k)
        if isinstance(sc, ScopeRefusal):
            return _from_refusal(sc)
        if isinstance(sc, PopulationCheck):
            statement = sc.then_run
        elif isinstance(sc, SmallCellRewrite):
            statement = sc.query
        else:
            return _from_refusal(ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT))
        # duplicate in this turn: refused before any query, prepare or release (M-2)
        statement_key = _statement_key(statement)
        if statement_key in turn.statements:
            return _fixed(DUPLICATE_QUERY, query_id=turn.statements[statement_key])
        population: int | None = None
        if isinstance(sc, PopulationCheck):
            got = self._population(sc, scope, session, turn)
            if isinstance(got, _Failure):
                return got
            population = got
        # 7a: differencing, pre-execution (pure, no BigQuery)
        plan = _diff_prepare(
            self.guard,
            sc,
            scope,
            user_id=session.user_id,
            session_id=session.session_id,
            population=population,
        )
        if isinstance(plan, ScopeRefusal):
            return _from_refusal(plan)
        try:
            query = _diff_query(plan)
            # 5: invariant on the statement that will run
            verify_scoped(query.sql, scope)
            # 6, 8, 7b: memo / dry run / execute / release
            released = self._execute_and_release(plan, query, scope, session, turn)
        finally:
            _diff_abandon(self.guard, plan)  # (e): no-op after release, closes any other path
        if isinstance(released, _Failure):
            return released
        result, rows, cache_hit = released
        # ``release`` already dropped the injected columns from the rows; drop them from the
        # column list too (and from any row, defensively). A model column of the same name
        # (nothing injected) is the model's own and stays (L-4).
        injected = _diff_injected_columns(plan)
        columns = [c for c in result.columns if c not in injected]
        if injected:
            rows = [{k: v for k, v in r.items() if k not in injected} for r in rows]
        # 9: scrub; 10: cap
        columns, rows = _scrub_rows(columns, rows, self.scrub)
        rows, truncated = _cap(rows, result.truncated)
        suppressed = (
            f"groups with fewer than {self.k} customers are hidden"
            if isinstance(sc, SmallCellRewrite) and sc.query.sql != scoped.sql
            else None
        )
        return _Success(
            columns=columns,
            rows=rows,
            truncated=truncated,
            suppressed_groups=suppressed,
            bytes_estimated=0 if cache_hit else result.bytes_estimated,
            bytes_billed=0 if cache_hit else result.bytes_billed,
            query_id=result.job_id,
            scope_label=_scope_label(scope),
            cache_hit=cache_hit,
            sql=query.sql,
            trace_sql=statement.sql,
            purpose=args.purpose,
            statement_key=statement_key,
        )

    def _population(
        self, check: PopulationCheck, scope: ProductScope, session: RunSqlSession, turn: RunSqlTurn
    ) -> int | _Failure:
        """Run the population query; the count is read only from its executed result (c)."""
        if check.query.scope_key != scope.scope_key or check.then_run.scope_key != scope.scope_key:
            return _from_refusal(ScopeRefusal.for_rule(ScopeRule.SCOPE_INVALID))
        verify_scoped(check.query.sql, scope)
        key = self._memo_key(check.query, scope, session)
        found = session.memo.lookup(key) if key is not None else None
        if found is not None:
            result = found[0]
        else:
            got = self._execute(check.query, session, turn)
            if isinstance(got, _Failure):
                return got
            result = got
            if key is not None:
                session.memo.store(key, result, turn_id=turn.turn_id)
        rows = result.rows
        # exactly one row with exactly the one ``population`` column, else fail closed (L-1)
        if (
            list(result.columns) != [POPULATION_COLUMN]
            or len(rows) != 1
            or result.truncated
            or set(rows[0]) != {POPULATION_COLUMN}
        ):
            return _from_refusal(ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT))
        population = rows[0].get(POPULATION_COLUMN)
        nxt = check.evaluate(population)
        if isinstance(nxt, ScopeRefusal):
            return _from_refusal(nxt)
        return population  # type: ignore[return-value] # evaluate checked: int >= k

    def _memo_key(self, query: ScopedQuery, scope: ProductScope, session: RunSqlSession) -> Any:
        try:
            return make_memo_key(
                query.sql, scope.scope_key, self.refresh_date(), user_id=session.user_id
            )
        except Exception:  # noqa: BLE001 - no refresh date: run without the memo
            return None

    def _execute_and_release(
        self,
        plan: Any,
        query: ScopedQuery,
        scope: ProductScope,
        session: RunSqlSession,
        turn: RunSqlTurn,
    ) -> tuple[QueryResult, list[dict[str, Any]], bool] | _Failure:
        released: list[list[dict[str, Any]]] = []

        def differencing(result: QueryResult) -> _Failure | None:
            # (b): the same plan object. On a memo hit too: the stored QueryResult keeps the
            # client's flag; anything but an explicit False, or more rows than the cap, counts
            # as truncated (fail closed, R5-H1).
            truncated = result.truncated is not False or len(result.rows) > MAX_ROWS
            out = _diff_release(self.guard, plan, result.rows, truncated=truncated)
            if isinstance(out, ScopeRefusal):
                return _from_refusal(out)
            released.append(out)
            return None

        key = self._memo_key(query, scope, session)
        if key is None:
            got = self._execute(query, session, turn)
            if isinstance(got, _Failure):
                return got
            refusal = differencing(got)
            if refusal is not None:
                return refusal
            return got, released[-1], False
        # A statement already stored this turn is a duplicate: refuse it *before* release, so
        # the duplicate records no fingerprint (L-b). Reachable past the statement-key
        # pre-check when an earlier call stored its rows and then failed after release, or
        # when the population query of this turn has the same SQL.
        stored = session.memo.lookup(key)
        if stored is not None and stored[1] == turn.turn_id:
            return _fixed(DUPLICATE_QUERY, query_id=stored[0].job_id)
        outcome = run_memoised(
            session.memo,
            key,
            turn_id=turn.turn_id,
            execute=lambda: self._execute(query, session, turn),
            differencing=differencing,  # (d): runs on a hit as well
            is_failure=lambda x: isinstance(x, _Failure),
        )
        if outcome.failure is not None:
            return outcome.failure  # type: ignore[return-value]
        assert outcome.result is not None
        if outcome.duplicate_in_turn:  # defensive: the lookup above refuses this first
            return _fixed(DUPLICATE_QUERY, query_id=outcome.result.job_id)
        return outcome.result, released[-1], outcome.hit

    def _execute(
        self, query: ScopedQuery, session: RunSqlSession, turn: RunSqlTurn
    ) -> QueryResult | _Failure:
        """Steps 6 and 8 for one statement, with one bounded retry on BQ_UNAVAILABLE."""
        if query.parameters and not _injects_parameters(self.runner):
            return _from_refusal(ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT))
        labels = {"tool": TOOL_NAME, "session_id": session.session_id, "user_id": session.user_id}
        last: BqFailure | None = None
        for attempt in range(UNAVAILABLE_RETRIES + 1):
            if attempt:
                self.sleep(self.retry_delay_s)
            with _parameters(query):
                prepared = self.runner.prepare(query.sql, session.bytes)
                if isinstance(prepared, BqFailure):
                    got: QueryResult | BqFailure = prepared
                else:
                    got = self.runner.execute(prepared, session.bytes, turn.sql_counter, labels)
            if isinstance(got, QueryResult):
                return got
            last = got
            if got.code is not ErrorCode.BQ_UNAVAILABLE:
                break
        assert last is not None
        if last.code is ErrorCode.BQ_UNAVAILABLE:
            turn.bq_unavailable = True
        return _from_bq(last)

    # -- per-turn accounting --

    def _give_up(self, turn: RunSqlTurn) -> _Failure:
        return _fixed(GIVE_UP, last_error=turn.last_error)

    def _account(
        self, outcome: _Failure | _Success, turn: RunSqlTurn, model_hash: str | None
    ) -> _Failure | _Success:
        if isinstance(outcome, _Success):
            turn.consecutive_failures = 0
            turn.last_error = None
            if model_hash is not None:
                turn.seen[model_hash] = outcome.query_id
            turn.statements[outcome.statement_key] = outcome.query_id
            turn.ledger.append(
                {
                    # what the writer and verifier see: the scoped statement before
                    # differencing injection (stable across fingerprint history, L-c)
                    "sql": outcome.trace_sql,
                    "purpose": self.scrub(outcome.purpose),
                    "query_id": outcome.query_id,
                    "rows": len(outcome.rows),
                    "sql_hash": sql_hash(outcome.trace_sql),
                    "executed_sql_hash": sql_hash(outcome.sql),  # audit: the statement run
                }
            )
            if not outcome.rows:
                outcome.hint = EMPTY_HINT_FIRST if turn.empty_results == 0 else EMPTY_HINT_FINAL
                turn.empty_results += 1
            elif outcome.truncated:
                outcome.hint = TRUNCATED_HINT
            return outcome
        if outcome.code in (GIVE_UP, DUPLICATE_QUERY, TOOL_BUSY):
            return outcome
        if not outcome.retryable:
            return outcome
        turn.consecutive_failures += 1
        turn.last_error = outcome.error_class or outcome.code
        if turn.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            turn.gave_up = True
            return self._give_up(turn)
        return outcome

    # -- envelopes, trace, audit --

    def _envelope_failure(self, failure: _Failure, start: float) -> dict[str, Any]:
        env = failure.envelope()
        env["meta"] = {"tool": TOOL_NAME, "duration_ms": self._ms(start), "cache_hit": False}
        return env

    def _ms(self, start: float) -> int:
        return max(0, int((self.clock() - start) * 1000))

    def _observe(
        self,
        outcome: _Failure | _Success,
        parsed: object,
        args: object,
        session: RunSqlSession,
        turn: RunSqlTurn,
        start: float,
        *,
        model_hash: str | None,
    ) -> None:
        ok = isinstance(outcome, _Success)
        code = None if ok else outcome.code  # type: ignore[union-attr]
        rule = None if ok else outcome.rule  # type: ignore[union-attr]
        error_class = None if ok else outcome.error_class  # type: ignore[union-attr]
        rows = len(outcome.rows) if ok else None  # type: ignore[union-attr]
        if self.tracer is not None:
            sql_fields: dict[str, Any] = {
                "status": "ok" if ok else "error",
                "turn_id": turn.turn_id,
                "duration_ms": self._ms(start),
                "policy_verdict": "refuse" if code == SQL_POLICY else "allow",
                "rule": rule,
                "error_class": error_class or code,
                "rows": rows,
            }
            if ok:
                sql_fields.update(
                    sql_text=outcome.trace_sql,  # type: ignore[union-attr] # tracer strips literals
                    purpose=self.scrub(outcome.purpose),  # type: ignore[union-attr]
                    dry_run_bytes=outcome.bytes_estimated,  # type: ignore[union-attr]
                    bytes_billed=outcome.bytes_billed,  # type: ignore[union-attr]
                    truncated=outcome.truncated,  # type: ignore[union-attr]
                    suppressed_groups=outcome.suppressed_groups,  # type: ignore[union-attr]
                    cache_hit=outcome.cache_hit,  # type: ignore[union-attr]
                )
            elif model_hash is not None:
                sql_fields["sql_hash"] = model_hash
            keys = sorted(str(k) for k in args) if isinstance(args, Mapping) else []
            with contextlib.suppress(Exception):
                self.tracer.record("sql", TOOL_NAME, **sql_fields)
            with contextlib.suppress(Exception):
                self.tracer.record(
                    "tool",
                    TOOL_NAME,
                    status="ok" if ok else "error",
                    turn_id=turn.turn_id,
                    tool=TOOL_NAME,
                    outcome="ok" if ok else "error",
                    error_code=code,
                    args_keys=keys[:10],
                    rows=rows,
                    truncated=outcome.truncated if ok else None,  # type: ignore[union-attr]
                )
        if self.audit is not None:
            record = {
                "event": "tool.run_sql",
                "outcome": "ok" if ok else "error",
                "code": code,
                "rule": rule,
                "class": error_class,
                "stage": None if ok else outcome.stage,  # type: ignore[union-attr]
                "sql_hash": sql_hash(outcome.trace_sql) if ok else model_hash,  # type: ignore[union-attr]
                "executed_sql_hash": sql_hash(outcome.sql) if ok else None,  # type: ignore[union-attr]
                "turn_id": turn.turn_id,
                "session_id": session.session_id,
                "rows": rows,
                "bytes_billed": outcome.bytes_billed if ok else None,  # type: ignore[union-attr]
            }
            with contextlib.suppress(Exception):
                self.audit(record)


@dataclass
class _Success:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
    suppressed_groups: str | None
    bytes_estimated: int
    bytes_billed: int
    query_id: str | None
    scope_label: str
    cache_hit: bool
    sql: str  # the scoped statement that ran (``executed_sql_hash`` only, never shown)
    trace_sql: str  # the scoped statement before differencing injection (trace and ledger)
    purpose: str
    statement_key: str
    hint: str | None = None

    def envelope(self, elapsed_s: float) -> dict[str, Any]:
        data: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "truncated": self.truncated,
            "suppressed_groups": self.suppressed_groups,
            "bytes_estimated": self.bytes_estimated,
            "bytes_billed": self.bytes_billed,
            "query_id": self.query_id,
            "scope_label": self.scope_label,
        }
        if self.hint is not None:
            data["hint"] = self.hint
        return {
            "ok": True,
            "data": data,
            "meta": {
                "tool": TOOL_NAME,
                "duration_ms": max(0, int(elapsed_s * 1000)),
                "cache_hit": self.cache_hit,
            },
        }


def _statement_key(statement: ScopedQuery) -> str:
    return sql_hash(statement.scope_key + "\n" + statement.sql)


def _scope_label(scope: ProductScope) -> str:
    if scope.all_products:
        return "all products"
    return "brands: " + ", ".join(scope.brands)
