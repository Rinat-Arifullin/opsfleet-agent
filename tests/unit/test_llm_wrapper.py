import random

import pytest

from opsfleet_agent.graph.budget import (
    BudgetExhausted,
    ExhaustedReason,
    ForceAnswer,
    LLMCallRecord,
    TurnBudget,
    TurnKind,
)
from opsfleet_agent.graph.llm import (
    Limiters,
    LLMFailure,
    LLMResponse,
    LLMSuccess,
    LLMWrapper,
    TokenBucket,
    TransientLLMError,
    build_chat_model,
    classify_error,
)


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class Sink:
    def __init__(self) -> None:
        self.recs: list[LLMCallRecord] = []

    def record(self, rec: LLMCallRecord) -> None:
        self.recs.append(rec)


def make(kind=TurnKind.QA, *, rpm=None, sink=None, role_subcap=6, jitter=lambda b: 0.0):
    ft = FakeTime()
    budget = TurnBudget(kind, clock=ft.clock, role_subcap=role_subcap, sink=sink)
    limiters = Limiters(rpm or {}, 0.8, clock=ft.clock, sleep=ft.sleep)
    w = LLMWrapper(budget, limiters, clock=ft.clock, sleep=ft.sleep, jitter=jitter)
    return ft, budget, w


class Model:
    """Fake provider callable: fails `fail_times` times with a transient error, then succeeds."""

    def __init__(self, ft, fail_times=0, always=False, latency=0.5, exc=None):
        self.ft, self.fail_times, self.always, self.latency = ft, fail_times, always, latency
        self.exc = exc or TransientLLMError("http_503")
        self.calls = 0
        self.timeouts: list[float] = []

    def __call__(self, timeout):
        self.calls += 1
        self.timeouts.append(timeout)
        self.ft.t += self.latency
        if self.always or self.calls <= self.fail_times:
            raise self.exc
        return LLMResponse("ok", 10, 5)


def test_retry_wrapper_bounded():
    ft, budget, w = make()
    primary, fb = Model(ft, always=True), Model(ft, always=True)
    out = w.call("quick_analyst", "m1", primary, "m2", fb)
    assert isinstance(out, ForceAnswer)
    assert primary.calls == 3  # 1 + 2 retries (D-6)
    assert fb.calls == 1  # fallback exactly once
    assert ft.sleeps == [1.0, 2.0]  # backoff schedule (D-6)
    assert budget.calls == 4 and budget.retries == 2
    assert budget.elapsed() < budget.caps.deadline_s
    assert w.call("quick_analyst", "m1", primary, "m2", fb) is not None  # still typed, no loop


def test_retry_then_fallback():
    ft, budget, w = make()
    primary, fb = Model(ft, always=True), Model(ft)
    out = w.call("quick_analyst", "m1", primary, "m2", fb)
    assert isinstance(out, LLMSuccess)
    assert out.used_fallback and out.model == "m2" and out.attempts == 4
    assert primary.calls == 3 and fb.calls == 1


def test_failed_primary_is_skipped_for_the_rest_of_the_turn():
    ft, budget, w = make(role_subcap=10)
    primary, fb = Model(ft, always=True), Model(ft)
    assert w.call("deep_analyst", "m1", primary, "m2", fb).used_fallback
    ft.sleeps.clear()
    out = w.call("deep_analyst", "m1", primary, "m2", fb)
    assert isinstance(out, LLMSuccess) and out.used_fallback and out.attempts == 1
    assert primary.calls == 3 and fb.calls == 2 and ft.sleeps == []
    assert budget.calls == 5  # 3 primary + 2 fallback, not 3 + 1 + 3 + 1
    other = Model(ft)
    assert not w.call("router", "m3", other, "m2", fb).used_fallback  # other primaries untouched


def test_skipped_primary_without_fallback_is_still_tried():
    ft, _, w = make(role_subcap=10)
    primary = Model(ft, fail_times=3)
    w.call("deep_analyst", "m1", primary, "m2", Model(ft))
    out = w.call("verifier", "m1", primary, None, None)
    assert isinstance(out, LLMSuccess) and out.model == "m1" and primary.calls == 4


def test_primary_is_not_skipped_after_success_or_rate_limit():
    ft, _, w = make(rpm={"m1": 1})
    w.call("r", "m1", Model(ft, latency=0.0), None, None)  # empties m1 bucket
    fb = Model(ft)
    assert w.call("r", "m1", Model(ft), "m2", fb).used_fallback  # limiter, not the provider
    ft.t += 80.0  # m1 bucket refills (0.8 rpm), deadline not yet reached
    p = Model(ft)
    out = w.call("r", "m1", p, "m2", fb)
    assert isinstance(out, LLMSuccess) and not out.used_fallback and p.calls == 1


def test_retry_succeeds_without_fallback():
    ft, _, w = make()
    primary, fb = Model(ft, fail_times=2), Model(ft)
    out = w.call("router", "m1", primary, "m2", fb)
    assert isinstance(out, LLMSuccess) and not out.used_fallback and out.attempts == 3
    assert fb.calls == 0


def test_non_retryable_not_retried_or_fallen_back():
    ft, _, w = make()
    primary, fb = Model(ft, always=True, exc=ValueError("400 bad")), Model(ft)
    out = w.call("router", "m1", primary, "m2", fb)
    assert isinstance(out, LLMFailure) and primary.calls == 1 and fb.calls == 0


def test_jitter_is_added_and_seedable():
    rng = random.Random(7)
    ft, _, w = make(jitter=lambda b: rng.uniform(0, 0.25 * b))
    w.call("router", "m1", Model(ft, fail_times=1), None, None)
    assert 1.0 <= ft.sleeps[0] <= 1.25


def test_sdk_single_attempt():
    m = build_chat_model("gemini-3.1-flash-lite", "test-key-not-real")
    # 1 = only the initial request; 0 would mean "Google default" (retries stay on).
    assert m.max_retries == 1


def test_role_subcap_counts_retries_and_fallback():
    ft, budget, w = make(role_subcap=3)
    primary, fb = Model(ft, always=True), Model(ft)
    out = w.call("quick_analyst", "m1", primary, "m2", fb)
    # 3 attempts used the whole sub-cap: no fallback, no more retries.
    assert isinstance(out, ForceAnswer)
    assert out.detail == BudgetExhausted(ExhaustedReason.ROLE_CALLS, "quick_analyst")
    assert primary.calls == 3 and fb.calls == 0
    assert budget.role_calls["quick_analyst"] == 3
    # The role is now blocked up front with a typed result.
    assert w.call("quick_analyst", "m1", primary, "m2", fb) == BudgetExhausted(
        ExhaustedReason.ROLE_CALLS, "quick_analyst"
    )
    # Another role is unaffected.
    assert isinstance(w.call("router", "m1", Model(ft), None, None), LLMSuccess)


def test_turn_retry_budget_of_six_goes_straight_to_fallback():
    ft, budget, w = make(TurnKind.REPORT, role_subcap=99)
    for _ in range(3):  # 2 retries each (D-6) -> 6 used, 9 calls
        w.call("a", "m1", Model(ft, fail_times=2), "m2", Model(ft))
    assert budget.retries == 6
    p, fb = Model(ft, always=True), Model(ft)
    out = w.call("b", "m1", p, "m2", fb)
    assert isinstance(out, LLMSuccess) and out.used_fallback
    assert p.calls == 1 and fb.calls == 1


def test_deadline_stops_retries():
    ft, budget, w = make()
    primary, fb = Model(ft, always=True), Model(ft)
    ft.t = 108.0  # 12 s left: usable 2 s, a first attempt fits but no retry or fallback
    out = w.call("router", "m1", primary, "m2", fb)
    # 12 s left: the reserved force_answer call is still allowed, so not template-only.
    assert isinstance(out, ForceAnswer) and not out.template_only
    assert primary.calls == 1 and fb.calls == 0
    assert ft.sleeps == []


def test_deadline_stops_retries_before_first_attempt():
    ft, _, w = make()
    ft.t = 121.0
    primary, fb = Model(ft, always=True), Model(ft)
    out = w.call("router", "m1", primary, "m2", fb)
    assert out == BudgetExhausted(ExhaustedReason.DEADLINE, "router")
    assert primary.calls == 0 and fb.calls == 0 and ft.sleeps == []


def test_slow_attempts_never_overrun_deadline():
    ft, budget, w = make()
    primary, fb = Model(ft, always=True, latency=50.0), Model(ft, latency=50.0)
    out = w.call("router", "m1", primary, "m2", fb)
    assert isinstance(out, ForceAnswer)
    assert budget.elapsed() <= budget.caps.deadline_s
    assert primary.calls == 2 and fb.calls == 0
    assert all(t <= 60.0 for t in primary.timeouts)


def test_rate_limited_goes_straight_to_fallback_without_retries():
    ft, budget, w = make(rpm={"m1": 1})
    w.call("r", "m1", Model(ft, latency=0.0), None, None)  # empties m1 bucket
    ft.sleeps.clear()
    sink = Sink()
    budget.usage.sink = sink
    p, fb = Model(ft), Model(ft)
    out = w.call("r", "m1", p, "m2", fb)
    assert isinstance(out, LLMSuccess) and out.used_fallback and out.attempts == 1
    assert p.calls == 0 and ft.sleeps == []  # no backoff sleeps, no retry loop
    assert [r.outcome for r in sink.recs] == ["rate_limited", "ok"]
    assert budget.retries == 0 and budget.calls == 2


def test_rate_limited_everywhere_counts_no_attempts():
    ft, budget, w = make(rpm={"m1": 1, "m2": 1})
    w.call("r", "m1", Model(ft, latency=0.0), None, None)
    w.call("r", "m2", Model(ft, latency=0.0), None, None)
    before = budget.calls
    out = w.call("r", "m1", Model(ft), "m2", Model(ft))
    assert isinstance(out, ForceAnswer)
    assert budget.calls == before and ft.sleeps == []


def test_limiter_wait_eating_deadline_returns_budget_exhausted():
    # rpm 15 * 0.8 = 12/min: after the burst the next call waits 5 s. force_answer has no reserve.
    ft, budget, w = make(TurnKind.REPORT, rpm={"m1": 15}, role_subcap=99)
    ft.t = 180.0 - 5.5  # 5.5 s left, the 5 s wait leaves 0.5 s (< 1 s floor)
    for _ in range(12):
        w.call("r", "m1", Model(ft, latency=0.0), None, None)
    ft.t = 180.0 - 5.5
    p = Model(ft)
    out = w.call("force_answer", "m1", p, None, None)
    assert isinstance(out, ForceAnswer) and out.template_only
    assert out.detail == BudgetExhausted(ExhaustedReason.DEADLINE, "force_answer")
    assert p.calls == 0
    assert budget.elapsed() <= budget.caps.deadline_s


def test_force_answer_after_deadline_is_template_only():
    ft, budget, w = make()
    ft.t = 130.0
    out = w.call("force_answer", "m1", Model(ft), None, None)
    assert isinstance(out, ForceAnswer) and out.template_only
    assert budget.force_answer().template_only is True
    fresh = TurnBudget(TurnKind.QA, clock=lambda: 0.0)
    assert fresh.force_answer().template_only is False


def test_configurable_turn_retries_and_role_subcap():
    ft = FakeTime()
    budget = TurnBudget(TurnKind.QA, clock=ft.clock, role_subcap=2, max_turn_retries=1, sink=None)
    assert budget.subcap("x") == 2
    w = LLMWrapper(budget, Limiters({}, 0.8), clock=ft.clock, sleep=ft.sleep, jitter=lambda b: 0)
    p = Model(ft, always=True)
    out = w.call("x", "m1", p, None, None)
    assert isinstance(out, ForceAnswer) and p.calls == 2
    b2 = TurnBudget(TurnKind.QA, clock=ft.clock, max_turn_retries=0)
    assert b2.check_retry("x") == BudgetExhausted(ExhaustedReason.RETRIES, "x")


def test_llm_failure_omits_provider_message():
    ft, _, w = make()
    secret = "SENTINEL-PROVIDER-MESSAGE-do-not-leak"
    out = w.call("r", "m1", Model(ft, always=True, exc=ValueError(secret)), "m2", Model(ft))
    assert isinstance(out, LLMFailure)
    assert secret not in repr(out)
    assert all(secret not in str(v) for v in vars(out).values())


def test_trace_records_turn_usage():
    sink = Sink()
    ft, budget, w = make(rpm={"m1": 5}, sink=sink)
    w.call("router", "m1", Model(ft, fail_times=1, latency=0.5), None, None)
    assert [r.outcome for r in sink.recs] == ["transient_error", "ok"]
    assert [r.is_retry for r in sink.recs] == [False, True]
    assert all(r.latency_ms == pytest.approx(500.0) for r in sink.recs)
    ok = sink.recs[-1]
    assert (ok.tokens_in, ok.tokens_out, ok.role, ok.model) == (10, 5, "router", "m1")
    totals = budget.usage.totals()
    assert totals["llm_calls_total"] == 2 and totals["retries_total"] == 1
    assert totals["tokens_in_total"] == 10 and totals["tokens_out_total"] == 5


def test_limiter_wait_recorded_apart_from_latency():
    sink = Sink()
    # rpm 15 * 0.8 = 12 per minute, burst of 12: the 13th call waits 5 s.
    ft, budget, w = make(TurnKind.REPORT, rpm={"m1": 15}, sink=sink, role_subcap=99)
    for _ in range(13):
        w.call("r", "m1", Model(ft, latency=0.0), None, None)
    waits = [r.limiter_wait_ms for r in sink.recs]
    assert waits[:12] == [0.0] * 12
    assert waits[12] == pytest.approx(5000.0, rel=0.01)
    assert sink.recs[12].latency_ms == 0.0
    assert budget.usage.limiter_wait_ms == pytest.approx(waits[12])


def test_token_bucket_rate_and_cap():
    ft = FakeTime()
    b = TokenBucket(rpm=5, fraction=0.8, clock=ft.clock, sleep=ft.sleep)
    assert b.rate_per_s == pytest.approx(4 / 60)
    for _ in range(4):
        assert b.acquire(10) == 0.0
    assert b.acquire(10) is None  # 15 s wait exceeds the 10 s cap, nothing consumed or slept
    assert ft.sleeps == []
    ft.t += 60
    assert b.acquire(10) == 0.0


def test_limiter_timeout_does_not_consume_a_call():
    ft, budget, w = make(rpm={"m1": 1})  # burst of 1, then a 75 s wait
    w.call("r", "m1", Model(ft, latency=0.0), None, None)
    p, fb = Model(ft), Model(ft)
    out = w.call("r", "m1", p, "m2", fb)  # m1 bucket empty -> fallback m2 (unlimited)
    assert isinstance(out, LLMSuccess) and out.used_fallback and p.calls == 0
    assert budget.calls == 2


def test_classify_error():
    class E(Exception):
        code = 429

    assert isinstance(classify_error(E()), TransientLLMError)
    assert isinstance(classify_error(TimeoutError()), TransientLLMError)
    assert not isinstance(classify_error(ValueError()), TransientLLMError)


def test_classify_error_httpx_and_genai_by_name():
    class TimeoutException(Exception):  # noqa: N818
        pass

    class ReadTimeout(TimeoutException):
        pass

    class ConnectError(Exception):
        pass

    class APIError(Exception):
        def __init__(self, code):
            self.code = code

    assert isinstance(classify_error(ReadTimeout()), TransientLLMError)
    assert isinstance(classify_error(ConnectError()), TransientLLMError)
    for code in (429, 500, 503):
        assert isinstance(classify_error(APIError(code)), TransientLLMError)
    for code in (400, 401, 403):
        assert not isinstance(classify_error(APIError(code)), TransientLLMError)


def test_afc_advice_is_dropped_and_other_sdk_warnings_pass(caplog):
    import logging

    from opsfleet_agent.graph.llm import AFC_ADVICE_PREFIX, GENAI_MODELS_LOGGER, silence_afc_advice

    logger = logging.getLogger(GENAI_MODELS_LOGGER)
    before = list(logger.filters)
    logger.filters[:] = []  # another test may have built a chat model already
    try:
        silence_afc_advice()
        silence_afc_advice()
        assert len(logger.filters) == 1
        with caplog.at_level(logging.WARNING, logger=GENAI_MODELS_LOGGER):
            logger.warning(AFC_ADVICE_PREFIX + " in Models.generate_content is not recommended.")
            logger.warning("quota exceeded")
        assert [r.getMessage() for r in caplog.records] == ["quota exceeded"]
    finally:
        logger.filters[:] = before
