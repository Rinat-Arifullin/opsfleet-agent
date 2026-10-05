"""Degraded mode and quota gate around the agent graph (AC-15.1/15.2, AC-21.14, AC-22.8).

`DegradedGraph` wraps `AgentGraph` with the same `run_turn` signature and leaves `graph.py`
untouched:

* BEFORE every provider call: `LLMHealth.wrap` checks the per-user quota in code and raises
  `QuotaExceeded` (non-retryable: no retry, no fallback model) before the call. Each attempt is
  counted when it is made, so Ctrl-C or an exception cannot erase it (AC-22.8). The wrapped
  invokes are shared by resume, reports, the verifier and retries, so all are gated. Turns that
  need no LLM (save, cancel, the pending-delete refusal) are not blocked.
* BEFORE a turn: the session BigQuery byte budget is capped at the user's remaining daily bytes
  (`prepare`). Bytes are recorded in `finally`. A resumed turn goes through `finish`, which does
  the same (cap, per-turn reset, bytes recorded, reply degraded), so `--resume` is charged too.
* A "revise" reply to a pending draft while the user is already over quota is refused BEFORE the
  graph runs: the graph would close the old draft before its first gated call, so the draft
  stays pending and savable. A quota hit later in the revise still drops it (owner decision,
  docs/process/iter24-ods.md OD-7).
* AFTER a turn: if the LLM failed and never succeeded, the reply is the AI-unavailable message
  (keeping any draft status line the graph already produced) and the notice says that listing,
  viewing and searching saved reports still work (AC-15.2, AC-21.14). A quota hit replaces the
  reply with the quota text only when the graph had nothing but its generic failure text;
  otherwise the answer or draft is kept and the notice says the turn was cut short by quota
  (a draft may be unverified). Checkpointed graph state is not touched.

Slash commands (/reports, /search, /open) never reach this wrapper: they need no LLM.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from collections.abc import Callable
from typing import Any, Final

from opsfleet_agent.delete import flow as delete_flow
from opsfleet_agent.graph.budget import PartialAnswer
from opsfleet_agent.graph.graph import _DELETE_RE as _GRAPH_DELETE_RE
from opsfleet_agent.graph.graph import _REVISE_RE as _GRAPH_REVISE_RE
from opsfleet_agent.graph.graph import (
    COMMENT_FALLBACK_TEXT,
    CONFIRM_NODE,
    ERROR_TEXT,
    NOT_SAVED_TEXT,
    PARTIAL_WITH_CONTEXT_TEXT,
    REVISING_TEXT,
    UNAVAILABLE_TEXT,
    AgentGraph,
    PendingTurn,
    TurnResult,
)
from opsfleet_agent.graph.graph import _reply_forms as _graph_reply_forms
from opsfleet_agent.graph.llm import NonRetryableLLMError
from opsfleet_agent.session import Session
from opsfleet_agent.store.db import StoreError
from opsfleet_agent.store.quota import QuotaStore

log = logging.getLogger(__name__)

AI_UNAVAILABLE_TEXT: Final = "The AI service is temporarily unavailable, please try again shortly."
DEGRADED_NOTICE: Final = (
    "Analysis is unavailable right now. You can still use /reports, /search and /open "
    "on your saved reports."
)
QUOTA_CHECK_FAILED_TEXT: Final = (
    "Usage limits could not be checked, so I didn't run this. Please try again."
)
QUOTA_CHECK_FAILED: Final = "check_failed"  # QuotaExceeded.reason when the check itself failed
QUOTA_TEXT: Final = {
    QUOTA_CHECK_FAILED: QUOTA_CHECK_FAILED_TEXT,
    "llm_hour": "You have reached your hourly question limit. Please try again later.",
    "llm_day": "You have reached your daily question limit. Please try again tomorrow.",
    "bq_bytes_day": "You have reached your daily data-scan limit. Please try again tomorrow.",
}
QUOTA_NOTICE: Final = "Your saved reports are still available: /reports, /search and /open."
# a turn the quota cut short but that still produced an answer or a draft (Mn3/Mn4)
QUOTA_CUT_SHORT_NOTICE: Final = (
    "This turn was cut short by the limit, so the answer may be incomplete and a report "
    "draft may not have been checked."
)
QUOTA_DRAFT_KEPT_NOTICE: Final = "Your report draft is still pending: reply save to keep it."
# Status lines the graph puts before an answer; they stay when the answer itself fails.
_STATUS_PREFIXES: Final = (NOT_SAVED_TEXT, REVISING_TEXT)
_CUT_SHORT: Final = frozenset({"answered", "error"})
# the graph's own failure texts: nothing of value is lost when the quota text replaces them
_GENERIC_TEXTS: Final = (
    ERROR_TEXT,
    UNAVAILABLE_TEXT,
    PARTIAL_WITH_CONTEXT_TEXT,  # D-152 budget-hit template
    COMMENT_FALLBACK_TEXT,  # D-152 comment reply template
    PartialAnswer(reason="").message,
)


class QuotaExceeded(NonRetryableLLMError):
    """Raised before a provider call when the user is over quota (never retried)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LLMHealth:
    """Per-call quota gate and outcome counter (the CLI is single-user per process)."""

    def __init__(self, quota: QuotaStore | None = None, user_id: str = "") -> None:
        self.quota = quota
        self.user_id = user_id
        self.successes = 0
        self.failures = 0
        self.quota_reason = ""

    def reset(self) -> None:
        self.successes = 0
        self.failures = 0
        self.quota_reason = ""

    @property
    def all_failed(self) -> bool:
        return self.failures > 0 and self.successes == 0

    def _gate(self) -> None:
        if self.quota is None:
            return
        try:  # fail closed, visibly: a broken store must not silently pass or hide the refusal
            decision = self.quota.check(self.user_id)
        except (StoreError, sqlite3.Error) as exc:
            log.error("quota check failed: %s", type(exc).__name__)
            self.quota_reason = QUOTA_CHECK_FAILED
            raise QuotaExceeded(QUOTA_CHECK_FAILED) from exc
        if not decision.allowed:
            self.quota_reason = decision.reason
            raise QuotaExceeded(decision.reason)
        try:  # counted at attempt time: an interrupt after this point keeps the count
            self.quota.record_calls(self.user_id, 1)
        except Exception as exc:  # accounting never blocks an answer
            log.error("quota accounting failed: %s", type(exc).__name__)

    def wrap(self, invoke: Callable[..., Any]) -> Callable[..., Any]:
        def observed(*args: Any, **kwargs: Any) -> Any:
            self._gate()
            try:
                out = invoke(*args, **kwargs)
            except Exception:
                self.failures += 1
                raise
            self.successes += 1
            return out

        return observed


class DegradedGraph:
    def __init__(self, graph: AgentGraph, quota: QuotaStore, health: LLMHealth) -> None:
        self.inner = graph  # resume.py unwraps this (see `unwrap`)
        self._quota = quota
        self.health = health
        self._base_cap: dict[str, int] = {}

    def _budget(self, session: Session) -> Any:
        try:
            return self.inner._sql_session(session).bytes  # noqa: SLF001 - no graph.py edit
        except Exception as exc:
            log.error("byte budget unavailable: %s", type(exc).__name__)
            return None

    def prepare(self, session: Session) -> None:
        """Cap the session BigQuery budget at the user's remaining daily bytes (never raises)."""
        budget = self._budget(session)
        if budget is None:
            return
        try:
            base = self._base_cap.setdefault(session.session_id, budget.cap)
            left = self._quota.remaining_bytes(session.profile.user_id)
            budget.cap = max(0, min(base, budget.used + left))
        except Exception as exc:
            log.error("byte cap failed: %s", type(exc).__name__)

    def _begin(self, session: Session) -> tuple[Any, int]:
        """Per-turn reset (no stale `quota_reason`), the user, the byte cap; (budget, used)."""
        self.health.reset()
        self.health.user_id = session.profile.user_id
        self.prepare(session)
        budget = self._budget(session)
        return budget, getattr(budget, "used", 0)

    def run_turn(
        self, raw_text: str, *, session: Session, turn_id: str | None = None
    ) -> TurnResult:
        budget, before = self._begin(session)
        kept = self._keep_draft_at_quota(raw_text, session)
        if kept is not None:
            return kept
        try:
            result = self.inner.run_turn(raw_text, session=session, turn_id=turn_id)
        finally:  # bytes already scanned are counted even on Ctrl-C or an exception
            self._record_bytes(session, budget, before)
        return self._degrade(result)

    def start_delete(
        self, args: str, *, session: Session, turn_id: str | None = None
    ) -> TurnResult:
        """``/delete <selector>`` (22a): explicit delegation, same accounting as a turn.

        The preview is deterministic code (no provider call), so it is never blocked or counted
        by the LLM quota and an outage cannot change its text."""
        budget, before = self._begin(session)
        try:
            result = self.inner.start_delete(args, session=session, turn_id=turn_id)
        finally:
            self._record_bytes(session, budget, before)
        return self._degrade(result)

    def delete_reply_turn(self, session_id: str) -> str | None:
        """Explicit delegation (22a): the turn id of the reply that resumed a pending delete."""
        return self.inner.delete_reply_turn(session_id)

    def finish(self, pending: PendingTurn, session: Session) -> TurnResult:
        """Finish a resumed turn (`resume_turn`) under the same quota rules as `run_turn`:
        cap applied, bytes recorded even on Ctrl-C, reply degraded on a quota hit or outage."""
        budget, before = self._begin(session)
        try:
            result = pending.finish()
        finally:
            self._record_bytes(session, budget, before)
        return self._degrade(result)

    def _keep_draft_at_quota(self, raw_text: str, session: Session) -> TurnResult | None:
        """Mn5: refuse a revise of a pending draft up front when the user is over quota.

        The graph closes the old draft before the revise makes its first (gated) call, so at
        quota the draft would be lost for nothing. Only a plain revise reply is checked (a
        delete request is refused by the graph itself); any doubt returns None (graph runs).
        """
        if self.health.quota is None:
            return None
        try:
            reply, folded = _graph_reply_forms(raw_text)
            if _GRAPH_DELETE_RE.search(folded) or not _GRAPH_REVISE_RE.match(reply):
                return None
            decision = self.health.quota.check(session.profile.user_id)
            if decision.allowed:
                return None
            pending = self.inner.open_resume(session)
            if CONFIRM_NODE not in pending.next:
                return None
            if pending.values.get("owner") != session.profile.user_id:
                return None
        except Exception as exc:  # the graph decides; this pre-check never breaks a turn
            log.error("quota draft check failed: %s", type(exc).__name__)
            return None
        self.health.quota_reason = decision.reason
        notice = f"{QUOTA_DRAFT_KEPT_NOTICE} {QUOTA_NOTICE}"
        return TurnResult(
            QUOTA_TEXT[decision.reason], label="report", route="report", outcome="refused",
            notice=notice,
        )  # fmt: skip

    def _record_bytes(self, session: Session, budget: Any, before: int) -> None:
        try:
            if budget is not None:
                self._quota.record_bytes(session.profile.user_id, budget.used - before)
        except Exception as exc:
            log.error("quota accounting failed: %s", type(exc).__name__)

    def _degrade(self, result: TurnResult) -> TurnResult:
        h = self.health
        prefix, body = _split_status(result.text)
        closed = "" if prefix else _closed_delete_head(result.text)  # 22a: kept in front
        head = prefix or closed
        if closed:
            body = result.text[len(closed) :].lstrip("\n")
        if h.quota_reason:
            # Mn3: the quota text replaces only the graph's generic failure text; a real answer
            # (or a draft) is kept and the notice says the limit cut the turn short (Mn4)
            if result.outcome in _CUT_SHORT and body.startswith(_GENERIC_TEXTS):
                text, outcome, notice = QUOTA_TEXT[h.quota_reason], "refused", QUOTA_NOTICE
            else:
                parts = (result.notice, QUOTA_TEXT[h.quota_reason], QUOTA_CUT_SHORT_NOTICE,
                         QUOTA_NOTICE)  # fmt: skip
                return dataclasses.replace(result, notice=" ".join(p for p in parts if p))
        elif h.all_failed:
            text, outcome, notice = AI_UNAVAILABLE_TEXT, "error", DEGRADED_NOTICE
        else:
            return result
        if head:
            text = f"{head}\n\n{text}"
        return dataclasses.replace(result, text=text, outcome=outcome, notice=notice)


def _split_status(text: str) -> tuple[str, str]:
    """(draft status line or "", the rest) of a reply."""
    for prefix in _STATUS_PREFIXES:
        if text.startswith(prefix):
            return prefix, text[len(prefix) :].lstrip("\n")
    return "", text


_DELETE_HEADS: Final = (
    delete_flow.CANCELLED_TEXT,
    delete_flow.EXPIRED_TEXT,
    delete_flow.UNSAFE_TEXT,
    delete_flow.UNAVAILABLE_TEXT,
    delete_flow.DELETE_PENDING_TEXT,
    delete_flow.STRANDED_TEXT,
)


def _closed_delete_head(text: str) -> str:
    """The delete-closing text the graph put before the reply of the turn (22a: a pending delete
    closed by a non-confirm reply or a stranded execute, then the turn answered). Matched by
    prefix, whatever follows (generic failure text, clarification, answer). The audit row is
    already written; the user must still be told nothing was deleted."""
    for head in _DELETE_HEADS:
        if text.startswith(head + "\n\n"):
            return head
    return ""


def unwrap(agent: Any, session: Session | None = None) -> Any:
    """The real graph behind a `DegradedGraph` (with a session, its byte cap is applied too).

    Resume finishes the turn through `DegradedGraph.finish` instead, which also records the
    bytes; this cap-only form is kept for callers that only need the inner graph."""
    if isinstance(agent, DegradedGraph):
        if session is not None:
            agent.prepare(session)
        return agent.inner
    return agent
