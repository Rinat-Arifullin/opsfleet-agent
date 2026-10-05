"""TurnBudget: per-turn call, SQL, retry and wall-clock bounds enforced in code.

Numbers are the approved ones of plan iteration 3 (HLD section 4.1). Exceeding a bound
returns a typed BudgetExhausted; nothing here raises into the agent loop.

D-149 (owner decision): a budget built with ``time_bounded=False`` (the dev-only local
provider) has no wall-clock deadline: ``deadline_s`` is ``math.inf``, so ``remaining_s`` is
infinite and ``deadline_hit`` is never true. Every count bound (calls, role sub-caps, retries,
SQL queries) is unchanged.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

FORCE_ANSWER_ROLE = "force_answer"
RECURSION_LIMIT = 60  # backstop only (HLD 4.1): 2 x the 14-call cap + parent nodes + margin
MAX_TURN_RETRIES = 6
ROLE_SUBCAP = 6

Clock = Callable[[], float]


class TurnKind(StrEnum):
    QA = "qa"
    REPORT = "report"
    LIGHT = "light"
    RETRY_REPORT = "retry_report"


@dataclass(frozen=True)
class TurnCaps:
    llm_calls: int
    sql_queries: int
    deadline_s: float


TURN_CAPS: dict[TurnKind, TurnCaps] = {
    TurnKind.QA: TurnCaps(llm_calls=10, sql_queries=6, deadline_s=120.0),
    TurnKind.REPORT: TurnCaps(llm_calls=14, sql_queries=6, deadline_s=180.0),
    TurnKind.LIGHT: TurnCaps(llm_calls=3, sql_queries=0, deadline_s=120.0),
    TurnKind.RETRY_REPORT: TurnCaps(llm_calls=8, sql_queries=0, deadline_s=180.0),
}


class ExhaustedReason(StrEnum):
    TURN_CALLS = "turn_calls"
    ROLE_CALLS = "role_calls"
    RETRIES = "retries"
    SQL = "sql"
    DEADLINE = "deadline"


@dataclass(frozen=True)
class BudgetExhausted:
    """Typed 'budget exhausted' result. The caller routes to force_answer or a template."""

    reason: ExhaustedReason
    role: str | None = None


@dataclass(frozen=True)
class ForceAnswer:
    """Typed instruction: stop the role and produce a partial answer from the ledger.

    If `template_only` is False the caller may spend its reserved force_answer LLM call; if True
    the budget (deadline or turn cap) forbids it and the caller renders a template fallback.
    """

    reason: str
    detail: BudgetExhausted | None = None
    # True when the budget no longer allows even the force_answer LLM call (deadline passed or
    # turn cap spent): the caller must render a deterministic template answer from the ledger
    # instead of calling the model again.
    template_only: bool = False


@dataclass(frozen=True)
class PartialAnswer:
    """Typed outcome of a caught recursion_limit hit (or another hard stop)."""

    reason: str
    message: str = "The analysis was cut short; here is what was found so far."


@dataclass(frozen=True)
class LLMCallRecord:
    """One provider attempt, as written to the trace (limiter wait is NOT part of latency)."""

    role: str
    model: str
    outcome: str  # ok | transient_error | fatal_error | rate_limited
    attempt: int
    is_retry: bool
    is_fallback: bool
    latency_ms: float
    limiter_wait_ms: float
    tokens_in: int = 0
    tokens_out: int = 0


class UsageSink(Protocol):
    """Where per-attempt records go. Iteration 4's tracer implements this."""

    def record(self, rec: LLMCallRecord) -> None: ...


@dataclass
class TurnUsage:
    """Aggregate usage for the trace's turn totals (llm_calls_total, tokens, limiter wait)."""

    calls: int = 0
    retries: int = 0
    fallback_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    limiter_wait_ms: float = 0.0
    sink: UsageSink | None = field(default=None, repr=False)

    def record(self, rec: LLMCallRecord) -> None:
        if rec.outcome != "rate_limited":  # a limiter timeout never reached the provider
            self.calls += 1
            self.retries += int(rec.is_retry)
            self.fallback_calls += int(rec.is_fallback)
        self.tokens_in += rec.tokens_in
        self.tokens_out += rec.tokens_out
        self.latency_ms += rec.latency_ms
        self.limiter_wait_ms += rec.limiter_wait_ms
        if self.sink is not None:
            self.sink.record(rec)

    def totals(self) -> dict[str, Any]:
        return {
            "llm_calls_total": self.calls,
            "retries_total": self.retries,
            "fallback_calls_total": self.fallback_calls,
            "tokens_in_total": self.tokens_in,
            "tokens_out_total": self.tokens_out,
            "latency_ms_total": self.latency_ms,
            "limiter_wait_ms_total": self.limiter_wait_ms,
        }


class TurnBudget:
    """Mutable per-turn budget. Every provider attempt (retry, fallback) counts as a call."""

    def __init__(
        self,
        kind: TurnKind,
        *,
        clock: Clock = time.monotonic,
        role_subcap: int = ROLE_SUBCAP,
        max_turn_retries: int = MAX_TURN_RETRIES,
        role_subcaps: dict[str, int] | None = None,
        sink: UsageSink | None = None,
        time_bounded: bool = True,
    ) -> None:
        self.kind = kind
        self.caps = TURN_CAPS[kind]
        # D-149: the one switch for "no time limits" (local provider). Counts stay bounded.
        self.time_bounded = time_bounded
        self.deadline_s = self.caps.deadline_s if time_bounded else math.inf
        self._clock = clock
        self._start = clock()
        self._role_subcap = role_subcap
        self._role_subcaps = dict(role_subcaps or {})
        self._max_turn_retries = max_turn_retries
        self.role_calls: Counter[str] = Counter()
        self.calls = 0
        self.sql_queries = 0
        self.retries = 0
        self.escalated = False
        self.usage = TurnUsage(sink=sink)

    # time
    def elapsed(self) -> float:
        return self._clock() - self._start

    def remaining_s(self) -> float:
        if not self.time_bounded:
            return math.inf  # never inf - inf (NaN) after a fail-closed restore
        return self.deadline_s - self.elapsed()

    def deadline_hit(self) -> bool:
        return self.remaining_s() <= 0

    # LLM calls
    def subcap(self, role: str) -> int:
        return self._role_subcaps.get(role, self._role_subcap)

    def check_attempt(self, role: str) -> BudgetExhausted | None:
        """Can this role make one more provider attempt? Does not consume."""
        if self.deadline_hit():
            return BudgetExhausted(ExhaustedReason.DEADLINE, role)
        # One call is reserved for force_answer: other roles stop one short of the cap.
        reserve = 0 if role == FORCE_ANSWER_ROLE else 1
        if self.calls >= self.caps.llm_calls - reserve:
            return BudgetExhausted(ExhaustedReason.TURN_CALLS, role)
        if self.role_calls[role] >= self.subcap(role):
            return BudgetExhausted(ExhaustedReason.ROLE_CALLS, role)
        return None

    def force_answer(
        self, detail: BudgetExhausted | None = None, reason: str = "llm_unavailable"
    ) -> ForceAnswer:
        """Typed force_answer instruction; template_only when force_answer itself is blocked.

        check_attempt(FORCE_ANSWER_ROLE) is blocked after the deadline, so a late caller gets
        ForceAnswer(template_only=True) and renders the template fallback without an LLM call.
        """
        blocked = self.check_attempt(FORCE_ANSWER_ROLE)
        return ForceAnswer(reason=reason, detail=detail, template_only=blocked is not None)

    def consume_attempt(self, role: str, *, is_retry: bool = False) -> None:
        self.calls += 1
        self.role_calls[role] += 1
        if is_retry:
            self.retries += 1

    def check_retry(self, role: str) -> BudgetExhausted | None:
        if self.retries >= self._max_turn_retries:
            return BudgetExhausted(ExhaustedReason.RETRIES, role)
        return self.check_attempt(role)

    # SQL
    def consume_sql(self) -> BudgetExhausted | None:
        if self.sql_queries >= self.caps.sql_queries:
            return BudgetExhausted(ExhaustedReason.SQL)
        self.sql_queries += 1
        return None

    # Quick -> Deep hand-off, at most once per turn
    def try_escalate(self) -> bool:
        if self.escalated:
            return False
        self.escalated = True
        return True

    # crash resume (HLD 4.0.6, R2-m6): the counters live in graph state, so a resumed turn
    # continues the same budget instead of getting a fresh one
    def snapshot(self) -> dict[str, Any]:
        """JSON-safe counters for the checkpoint (no usage records, no clock)."""
        return {
            "kind": self.kind.value,
            "calls": self.calls,
            "sql_queries": self.sql_queries,
            "retries": self.retries,
            "escalated": self.escalated,
            "role_calls": dict(self.role_calls),
            "elapsed_s": round(max(0.0, self.elapsed()), 3),
        }

    def restore(self, snap: Any) -> bool:
        """Continue from a snapshot. Never lowers a counter; values are clamped to the caps.

        A malformed snapshot (wrong kind or types) fails closed: the budget is exhausted, so
        the resumed turn can only reach force_answer's template. Returns False in that case.
        """
        caps = self.caps
        ok = isinstance(snap, dict) and snap.get("kind") == self.kind.value
        calls = _count(snap.get("calls"), caps.llm_calls) if ok else None
        sql = _count(snap.get("sql_queries"), caps.sql_queries) if ok else None
        retries = _count(snap.get("retries"), self._max_turn_retries) if ok else None
        escalated = snap.get("escalated") if ok else None
        elapsed = snap.get("elapsed_s") if ok else None
        roles = snap.get("role_calls") if ok else None
        valid = (
            None not in (calls, sql, retries)
            and isinstance(escalated, bool)
            and isinstance(elapsed, int | float)
            and not isinstance(elapsed, bool)
            and math.isfinite(elapsed)
            and isinstance(roles, dict)
            and len(roles) <= _MAX_ROLES
            and all(isinstance(k, str) and _count(v, 10**6) is not None for k, v in roles.items())
        )
        if not valid:
            self.calls, self.sql_queries = caps.llm_calls, caps.sql_queries
            self.retries, self.escalated = self._max_turn_retries, True
            if self.time_bounded:  # unbounded: the exhausted counts above already stop the turn
                self._start = self._clock() - caps.deadline_s
            return False
        self.calls = max(self.calls, calls)
        self.sql_queries = max(self.sql_queries, sql)
        self.retries = max(self.retries, retries)
        self.escalated = self.escalated or escalated
        for role, n in roles.items():
            self.role_calls[role] = max(self.role_calls[role], min(n, self.subcap(role)))
        lost = min(max(0.0, float(elapsed)), caps.deadline_s)
        self._start = min(self._start, self._clock() - lost)
        return True


_MAX_ROLES = 32


def _count(value: Any, cap: int) -> int | None:
    """A non-negative int clamped to ``cap``; None when it is not an int (bools rejected)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, cap)


def run_with_recursion_guard[T](invoke: Callable[[], T]) -> T | PartialAnswer:
    """Run a graph invocation; a recursion_limit hit becomes a typed PartialAnswer."""
    from langgraph.errors import GraphRecursionError

    try:
        return invoke()
    except GraphRecursionError:
        return PartialAnswer(reason="recursion_limit")
