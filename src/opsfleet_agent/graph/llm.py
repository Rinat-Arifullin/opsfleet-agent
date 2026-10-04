"""The single LLM call wrapper: limiter, bounded retry, one fallback, then force_answer.

Provider-agnostic: callers pass one callable per model (primary, fallback). Each callable makes
exactly one provider request and receives the attempt timeout in seconds. The ladder is
primary -> up to 3 retries (backoff 1 s, 2 s, 4 s plus jitter) -> fallback once -> ForceAnswer.
Every attempt draws from the TurnBudget (turn cap, role sub-cap, 6 retries per turn, deadline).
Clock, sleep and jitter are injectable so tests never sleep.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from opsfleet_agent.graph.budget import (
    FORCE_ANSWER_ROLE,
    BudgetExhausted,
    Clock,
    ExhaustedReason,
    ForceAnswer,
    LLMCallRecord,
    TurnBudget,
)

BACKOFFS_S: tuple[float, ...] = (1.0, 2.0, 4.0)  # at most 3 retries per call
MAX_CALL_RETRIES = len(BACKOFFS_S)
MAX_ATTEMPT_TIMEOUT_S = 60.0
MIN_USEFUL_TIMEOUT_S = 10.0
FORCE_RESERVE_S = 10.0  # kept for force_answer and finalize
MAX_LIMITER_WAIT_S = 10.0
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

TRANSIENT_CLASS_NAMES = frozenset(
    {
        "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
        "ConnectError", "ReadError", "RemoteProtocolError",
    }
)  # fmt: skip
MIN_ATTEMPT_TIMEOUT_S = 1.0  # less usable time than this: no attempt (no deadline overrun)

RATE_LIMITED: object = object()  # sentinel: limiter timeout, no provider call was made
Sleep = Callable[[float], None]
Jitter = Callable[[float], float]


class TransientLLMError(Exception):
    """429, 5xx, timeout or connection reset: retryable."""


class NonRetryableLLMError(Exception):
    """400, auth, safety block: never retried and never sent to the fallback."""


def classify_error(exc: BaseException) -> Exception:
    """Map a provider/SDK error to Transient or NonRetryable (small adapter, no SDK import)."""
    if isinstance(exc, (TransientLLMError, NonRetryableLLMError)):
        return exc
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TransientLLMError(type(exc).__name__)
    # httpx timeout/connect errors, matched by class name so httpx need not be imported.
    if any(c.__name__ in TRANSIENT_CLASS_NAMES for c in type(exc).__mro__):
        return TransientLLMError(type(exc).__name__)
    # google-genai APIError (and subclasses) expose the HTTP status as `.code`.
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in TRANSIENT_STATUS:
        return TransientLLMError(f"http_{code}")
    return NonRetryableLLMError(type(exc).__name__)


@dataclass(frozen=True)
class LLMResponse:
    value: Any
    tokens_in: int = 0
    tokens_out: int = 0


Attempt = Callable[[float], LLMResponse]  # arg: attempt timeout in seconds


@dataclass(frozen=True)
class LLMSuccess:
    response: LLMResponse
    model: str
    attempts: int
    used_fallback: bool


@dataclass(frozen=True)
class LLMFailure:
    """Non-retryable provider error; the role fails (no retry, no fallback)."""

    error_class: str


LLMOutcome = LLMSuccess | LLMFailure | BudgetExhausted | ForceAnswer


class TokenBucket:
    """Per-model token bucket at limiter_fraction x RPM."""

    def __init__(self, rpm: float, fraction: float, clock: Clock, sleep: Sleep) -> None:
        self.rate_per_s = rpm * fraction / 60.0
        self.capacity = max(1.0, rpm * fraction)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate_per_s)
        self._last = now

    def acquire(self, max_wait_s: float) -> float | None:
        """Take one token. Returns seconds waited, or None if it would wait longer than max."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        wait = (1.0 - self._tokens) / self.rate_per_s
        if wait > max_wait_s:
            return None
        self._sleep(wait)
        self._refill()
        self._tokens = max(0.0, self._tokens - 1.0)
        return wait


class Limiters:
    """One bucket per model id; models without a configured limit are not limited."""

    def __init__(
        self,
        rpm_by_model: Mapping[str, float],
        fraction: float,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._buckets = {
            m: TokenBucket(rpm, fraction, clock, sleep) for m, rpm in rpm_by_model.items()
        }

    def acquire(self, model: str, max_wait_s: float) -> float | None:
        bucket = self._buckets.get(model)
        return 0.0 if bucket is None else bucket.acquire(max_wait_s)

    @classmethod
    def from_settings(cls, settings: Any, **kw: Any) -> Limiters:
        rpm = {m: lim.rpm for m, lim in settings.limits.items()}
        return cls(rpm, settings.limiter_fraction, **kw)


def default_jitter(base_s: float) -> float:
    return random.uniform(0.0, 0.25 * base_s)  # noqa: S311 (not security relevant)


class LLMWrapper:
    def __init__(
        self,
        budget: TurnBudget,
        limiters: Limiters,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
        jitter: Jitter = default_jitter,
    ) -> None:
        self.budget = budget
        self.limiters = limiters
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter

    def _usable_s(self, role: str) -> float:
        reserve = 0.0 if role == FORCE_ANSWER_ROLE else FORCE_RESERVE_S
        return self.budget.remaining_s() - reserve

    def call(
        self,
        role: str,
        primary_model: str,
        primary: Attempt,
        fallback_model: str | None = None,
        fallback: Attempt | None = None,
    ) -> LLMOutcome:
        blocked = self.budget.check_attempt(role)
        if blocked:
            if role == FORCE_ANSWER_ROLE:  # force_answer blocked: caller renders a template
                return self.budget.force_answer(blocked)
            return blocked
        attempts = 0
        last_block: BudgetExhausted | None = None
        for n in range(MAX_CALL_RETRIES + 1):
            if n > 0:
                backoff = BACKOFFS_S[n - 1]
                blocked = self.budget.check_retry(role)
                if blocked:
                    last_block = blocked
                    break
                if self._usable_s(role) < backoff + MIN_USEFUL_TIMEOUT_S:
                    last_block = BudgetExhausted(ExhaustedReason.DEADLINE, role)
                    break
                self._sleep(backoff + self._jitter(backoff))
            res = self._attempt(role, primary_model, primary, attempts + 1, n > 0, False)
            if res is RATE_LIMITED:  # limiter timeout: no retry loop, straight to the fallback
                break
            if isinstance(res, BudgetExhausted):
                last_block = res
                break
            attempts += 1
            if isinstance(res, (LLMSuccess, LLMFailure)):
                return _with_attempts(res, attempts)
        # Retries are used up (or not allowed): the fallback gets exactly one attempt.
        if fallback is not None and fallback_model is not None:
            blocked = self.budget.check_attempt(role)
            if blocked is None and self._usable_s(role) < MIN_USEFUL_TIMEOUT_S:
                blocked = BudgetExhausted(ExhaustedReason.DEADLINE, role)
            if blocked is None:
                res = self._attempt(role, fallback_model, fallback, attempts + 1, False, True)
                if isinstance(res, BudgetExhausted):
                    last_block = res
                elif res is not RATE_LIMITED:
                    attempts += 1
                    if isinstance(res, (LLMSuccess, LLMFailure)):
                        return _with_attempts(res, attempts)
            else:
                last_block = blocked
        fa = self.budget.force_answer(last_block)
        # force_answer itself failed: nothing is left to ask the model, render the template.
        return replace(fa, template_only=True) if role == FORCE_ANSWER_ROLE else fa

    def _attempt(
        self,
        role: str,
        model: str,
        fn: Attempt,
        attempt_no: int,
        is_retry: bool,
        is_fallback: bool,
    ) -> LLMSuccess | LLMFailure | BudgetExhausted | object | None:
        """One provider attempt: None = retryable failure, RATE_LIMITED = limiter timeout
        (no model call made), BudgetExhausted = no usable time left after the limiter wait."""
        max_wait = max(0.0, min(MAX_LIMITER_WAIT_S, self._usable_s(role)))
        waited = self.limiters.acquire(model, max_wait)
        if waited is None:
            self._record(role, model, "rate_limited", attempt_no, is_retry, is_fallback, 0, 0)
            return RATE_LIMITED
        wait_ms = waited * 1000.0
        usable = self._usable_s(role)  # re-check: the limiter wait may have eaten the deadline
        if usable < MIN_ATTEMPT_TIMEOUT_S:
            return BudgetExhausted(ExhaustedReason.DEADLINE, role)
        timeout = min(MAX_ATTEMPT_TIMEOUT_S, usable)
        self.budget.consume_attempt(role, is_retry=is_retry)
        t0 = self._clock()
        try:
            resp = fn(timeout)
        except Exception as exc:  # noqa: BLE001 - provider errors are classified, never leaked
            lat = (self._clock() - t0) * 1000.0
            err = classify_error(exc)
            transient = isinstance(err, TransientLLMError)
            outcome = "transient_error" if transient else "fatal_error"
            self._record(role, model, outcome, attempt_no, is_retry, is_fallback, lat, wait_ms)
            return None if transient else LLMFailure(error_class=type(exc).__name__)
        lat = (self._clock() - t0) * 1000.0
        self._record(
            role, model, "ok", attempt_no, is_retry, is_fallback, lat, wait_ms,
            resp.tokens_in, resp.tokens_out,
        )  # fmt: skip
        return LLMSuccess(resp, model, attempt_no, is_fallback)

    def _record(
        self,
        role: str,
        model: str,
        outcome: str,
        attempt_no: int,
        is_retry: bool,
        is_fallback: bool,
        latency_ms: float,
        limiter_wait_ms: float,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        rec = LLMCallRecord(
            role, model, outcome, attempt_no, is_retry, is_fallback,
            latency_ms, limiter_wait_ms, tokens_in, tokens_out,
        )  # fmt: skip
        self.budget.usage.record(rec)


def _with_attempts(res: LLMSuccess | LLMFailure, attempts: int) -> LLMSuccess | LLMFailure:
    if isinstance(res, LLMSuccess):
        return LLMSuccess(res.response, res.model, attempts, res.used_fallback)
    return res


def build_chat_model(
    model: str,
    api_key: str,
    *,
    thinking_level: str | None = None,
    thinking_budget: int | None = None,
) -> Any:
    """Real ChatGoogleGenerativeAI with SDK retries off.

    Verified in langchain_google_genai/_common.py: max_retries=1 means only the initial request
    (0 means 'use the Google default', i.e. retries stay on). Construction makes no network call.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    kwargs: dict[str, Any] = {"model": model, "api_key": api_key, "max_retries": 1}
    if thinking_level is not None:
        kwargs["thinking_level"] = thinking_level
    if thinking_budget is not None:
        kwargs["thinking_budget"] = thinking_budget
    return ChatGoogleGenerativeAI(**kwargs)
