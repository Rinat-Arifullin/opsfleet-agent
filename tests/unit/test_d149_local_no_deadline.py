"""D-149: the local provider has no wall-clock limits; Gemini keeps the deadline (offline)."""

from __future__ import annotations

import dataclasses
import math

import pytest

from opsfleet_agent.config import LMSTUDIO
from opsfleet_agent.graph import graph as gr
from opsfleet_agent.graph import providers as P
from opsfleet_agent.graph.budget import BudgetExhausted, ExhaustedReason, TurnBudget, TurnKind
from opsfleet_agent.graph.llm import (
    MAX_ATTEMPT_TIMEOUT_S,
    Limiters,
    LLMResponse,
    LLMSuccess,
    LLMWrapper,
)
from opsfleet_agent.roles.analyst import ModelTurn
from tests.unit.test_graph import SIMPLE, Router, Scripted, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)

FAR = 10_000.0  # far past every deadline (120 s / 180 s)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _wrapper(*, time_bounded: bool, kind: TurnKind = TurnKind.QA):
    clock = Clock()
    budget = TurnBudget(kind, clock=clock, time_bounded=time_bounded)
    w = LLMWrapper(budget, Limiters({}, 0.8, clock=clock, sleep=lambda s: None), clock=clock,
                   sleep=lambda s: None, jitter=lambda b: 0.0)  # fmt: skip
    return clock, budget, w


@pytest.mark.parametrize("kind", list(TurnKind))
def test_unbounded_budget_never_hits_the_deadline(kind: TurnKind) -> None:
    clock = Clock()
    b = TurnBudget(kind, clock=clock, time_bounded=False)
    clock.t = FAR
    assert math.isinf(b.remaining_s()) and not b.deadline_hit()
    assert b.check_attempt("quick_analyst") is None
    assert not b.force_answer().template_only


def test_unbounded_budget_keeps_every_count_bound() -> None:
    clock = Clock()
    b = TurnBudget(TurnKind.QA, clock=clock, time_bounded=False)
    clock.t = FAR
    for _ in range(b.caps.sql_queries):
        assert b.consume_sql() is None
    assert b.consume_sql() == BudgetExhausted(ExhaustedReason.SQL)
    while b.check_attempt("deep_analyst") is None:
        b.consume_attempt("deep_analyst")
    assert b.calls <= b.caps.llm_calls


def test_unbounded_restore_fails_closed_on_counts_without_nan() -> None:
    clock = Clock()
    b = TurnBudget(TurnKind.QA, clock=clock, time_bounded=False)
    assert b.restore({"kind": "bogus"}) is False
    assert not math.isnan(b.remaining_s())
    assert b.check_attempt("quick_analyst") is not None  # counts exhausted: still stops
    assert b.force_answer().template_only  # fail closed: template only, as before


def test_gemini_budget_unchanged() -> None:
    clock = Clock()
    b = TurnBudget(TurnKind.QA, clock=clock)
    assert b.time_bounded and b.deadline_s == 120.0
    clock.t = 121.0
    assert b.deadline_hit()
    exhausted = BudgetExhausted(ExhaustedReason.DEADLINE, "quick_analyst")
    assert b.check_attempt("quick_analyst") == exhausted


def test_local_wrapper_passes_no_timeout_and_runs_past_120s() -> None:
    clock, budget, w = _wrapper(time_bounded=False)
    seen: list[float | None] = []

    def slow(timeout):
        seen.append(timeout)
        clock.t += 300.0  # a very slow local call
        return LLMResponse("ok")

    for _ in range(3):
        assert isinstance(w.call("quick_analyst", "local", slow), LLMSuccess)
    assert seen == [None, None, None] and clock.t == 900.0


def test_local_wrapper_retries_after_a_long_failure() -> None:
    clock, _, w = _wrapper(time_bounded=False)
    n = {"calls": 0}

    def flaky(timeout):
        n["calls"] += 1
        clock.t += 200.0
        if n["calls"] == 1:
            raise TimeoutError("synthetic")
        return LLMResponse("ok")

    out = w.call("quick_analyst", "local", flaky)
    assert isinstance(out, LLMSuccess) and out.attempts == 2


def test_gemini_wrapper_timeout_and_deadline_unchanged() -> None:
    clock, _, w = _wrapper(time_bounded=True)
    seen: list[float | None] = []

    def call(timeout):
        seen.append(timeout)
        clock.t += 70.0
        return LLMResponse("ok")

    assert isinstance(w.call("quick_analyst", "m", call), LLMSuccess)
    assert seen == [MAX_ATTEMPT_TIMEOUT_S]
    out = w.call("quick_analyst", "m", call)  # 50 s left - 10 s reserve -> 40 s timeout
    assert isinstance(out, LLMSuccess) and seen[-1] == pytest.approx(40.0)
    assert w.call("quick_analyst", "m", call) == BudgetExhausted(
        ExhaustedReason.DEADLINE, "quick_analyst"
    )


def test_local_chat_model_has_no_client_timeout() -> None:
    assert P.LOCAL_CHAT_TIMEOUT_S is None


class SlowScripted(Scripted):
    def __init__(self, clock: list[float], *steps) -> None:
        super().__init__(*steps)
        self.clock = clock
        self.timeouts: list[float | None] = []

    def __call__(self, model, messages, specs, timeout):
        self.timeouts.append(timeout)
        self.clock[0] += 130.0  # each call takes 130 s: one call alone passes 120 s
        return super().__call__(model, messages, specs, timeout)


def _run(make_env, settings, provider: str):  # noqa: F811
    clock = [0.0]
    analyst = SlowScripted(clock, sql_call(SIMPLE), ModelTurn("There were 3 complete orders."))
    env = make_env(Router("simple"), analyst)
    env.graph.services = dataclasses.replace(
        env.graph.services,
        clock=lambda: clock[0],
        settings=dataclasses.replace(settings, llm_provider=provider),
    )
    return env.ask("How many complete orders are there?"), analyst


def test_local_turn_answers_far_past_the_deadline(make_env, settings) -> None:  # noqa: F811
    out, analyst = _run(make_env, settings, LMSTUDIO)
    assert out.outcome == "answered" and "3 complete orders" in out.text
    assert analyst.timeouts and all(t is None for t in analyst.timeouts)
    assert gr.UNAVAILABLE_TEXT not in out.text


def test_gemini_turn_still_hits_the_deadline(make_env, settings) -> None:  # noqa: F811
    out, analyst = _run(make_env, settings, settings.llm_provider)
    assert "3 complete orders" not in out.text  # the second analyst call is past the deadline
    assert all(t is not None and t <= MAX_ATTEMPT_TIMEOUT_S for t in analyst.timeouts)
