import pytest
from langgraph.errors import GraphRecursionError

from opsfleet_agent.graph.budget import (
    FORCE_ANSWER_ROLE,
    BudgetExhausted,
    ExhaustedReason,
    PartialAnswer,
    TurnBudget,
    TurnKind,
    run_with_recursion_guard,
)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def drain(b: TurnBudget, roles: list[str]) -> int:
    n = 0
    for role in roles:
        while b.check_attempt(role) is None:
            b.consume_attempt(role)
            n += 1
    return n


@pytest.mark.parametrize(
    ("kind", "calls", "sql"),
    [(TurnKind.QA, 10, 6), (TurnKind.REPORT, 14, 6), (TurnKind.LIGHT, 3, 0)],
)
def test_turn_caps_enforced(kind, calls, sql):
    b = TurnBudget(kind, clock=Clock(), role_subcap=99)
    # Non-force roles stop one short of the cap: one call is reserved for force_answer.
    assert drain(b, ["analyst"]) == calls - 1
    res = b.check_attempt("analyst")
    assert res == BudgetExhausted(ExhaustedReason.TURN_CALLS, "analyst")
    assert b.check_attempt(FORCE_ANSWER_ROLE) is None
    b.consume_attempt(FORCE_ANSWER_ROLE)
    assert b.check_attempt(FORCE_ANSWER_ROLE) is not None
    for _ in range(sql):
        assert b.consume_sql() is None
    assert b.consume_sql() == BudgetExhausted(ExhaustedReason.SQL)


def test_turn_deadline():
    clock = Clock()
    b = TurnBudget(TurnKind.QA, clock=clock)
    assert b.check_attempt("analyst") is None
    clock.t += 119.9
    assert b.check_attempt("analyst") is None
    clock.t += 0.2
    assert b.check_attempt("analyst") == BudgetExhausted(ExhaustedReason.DEADLINE, "analyst")
    assert TurnBudget(TurnKind.REPORT, clock=clock).caps.deadline_s == 180.0


def test_escalation_once():
    b = TurnBudget(TurnKind.QA, clock=Clock())
    assert b.try_escalate() is True
    assert b.try_escalate() is False
    assert b.try_escalate() is False


def test_recursion_limit_caught():
    def boom():
        raise GraphRecursionError("limit")

    res = run_with_recursion_guard(boom)
    assert isinstance(res, PartialAnswer)
    assert res.reason == "recursion_limit"
    assert run_with_recursion_guard(lambda: 42) == 42


def test_recursion_other_errors_propagate():
    def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        run_with_recursion_guard(boom)
