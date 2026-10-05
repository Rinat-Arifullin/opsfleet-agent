"""D-152: memory-question and comment-follow-up intents (offline, fake LLMs, synthetic text)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from opsfleet_agent.graph import graph as gr
from opsfleet_agent.graph.context import HISTORY_TURNS
from opsfleet_agent.graph.intents import (
    MAX_INTENT_CHARS,
    MEMORY_TEXT,
    is_comment_followup,
    is_memory_question,
)
from opsfleet_agent.graph.llm import LLMResponse
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.roles.light_path import CAPABILITIES_TEXT
from tests.unit.test_graph import SIMPLE, Router, Scripted, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)
from tests.unit.test_reports import TurnRouter

ANSWER = "There were 3 complete orders."  # synthetic previous answer (grounded by SIMPLE)
COMMENT_ROUTE = ("router", "intent", {"label": "simple", "route": "comment"})

# --- pure intent checks ---


@pytest.mark.parametrize(
    "text",
    [
        "Do you see previous messages in our session?",
        "can you remember our earlier conversation",
        "Do you have memory?",
        "do you remember what I asked",
        "Is our chat saved?",
        "How many messages do you remember?",
        "DO YOU SEE THE PREVIOUS MESSAGES?",
    ],
)
def test_memory_question_detected(text: str) -> None:
    assert is_memory_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "Do you remember the revenue for 2023?",
        "can you see previous orders for Acme",
        "show me the top products",
        "hello",
        "",
        "do you see previous messages " * 20,  # over MAX_INTENT_CHARS
    ],
)
def test_memory_question_not_detected(text: str) -> None:
    assert not is_memory_question(text)


def test_memory_text_derives_turn_count() -> None:
    assert f"last {HISTORY_TURNS} questions" in MEMORY_TEXT
    assert not any(ch.isdigit() for ch in MEMORY_TEXT.replace(str(HISTORY_TURNS), ""))


@pytest.mark.parametrize(
    "text",
    [
        "So it is worth promoting this category",
        "I think that brand deserves more stock",
        "Looks like a good candidate for a discount.",
        "interesting, definitely worth a campaign",
    ],
)
def test_comment_detected(text: str) -> None:
    assert is_comment_followup(text)


@pytest.mark.parametrize(
    "text",
    [
        "Is it worth promoting?",
        "show me the sales for last year",
        "and for 2023",
        "compare it with the previous year, I think it is worth it",
        "what about returns",
        "count orders",
        "",
        "worth " * (MAX_INTENT_CHARS // 5 + 1),
    ],
)
def test_comment_not_detected(text: str) -> None:
    assert not is_comment_followup(text)


# --- Issue A: the memory question gets the code-owned answer on the light path ---


@pytest.mark.parametrize("label", ["meta", "smalltalk", "simple", "complex"])
def test_memory_question_answers_static_text(make_env, label: str) -> None:  # noqa: F811
    env = make_env(Router(label), Scripted(ModelTurn("unused")))
    out = env.ask("Do you see previous messages in our session?")
    assert out.route == "light" and out.text == MEMORY_TEXT
    assert str(HISTORY_TURNS) in out.text and CAPABILITIES_TEXT not in out.text
    assert env.analyst.calls == [] and len(env.router.calls) == 1  # router only, no light call
    if label not in ("meta", "smalltalk"):
        assert ("router", "intent", {"label": "meta", "route": "light"}) in env.spans


def test_capabilities_question_unchanged(make_env) -> None:  # noqa: F811
    env = make_env(Router("meta"))
    out = env.ask("What can you do?")
    assert out.route == "light" and out.text.startswith(CAPABILITIES_TEXT)


# --- Issue B: a comment after an answer gets one brief reply from that answer ---


class CommentModel(Scripted):
    """Answers the first turn with SQL; a force-style call (no tool specs) returns ``reply``."""

    def __init__(self, reply: str = "Agreed; it may be worth checking the return rate.") -> None:
        super().__init__(sql_call(SIMPLE), ModelTurn(ANSWER))
        self.reply = reply

    def __call__(self, model, messages, specs, timeout):
        if not specs:
            self.calls.append((model, list(messages), 0))
            return LLMResponse(ModelTurn(self.reply), 5, 5)
        return super().__call__(model, messages, specs, timeout)


def _force_calls(env) -> list[Any]:
    return [c for c in env.analyst.calls if c[2] == 0]


def test_comment_gets_one_reply_from_previous_answer(make_env) -> None:  # noqa: F811
    env = make_env(TurnRouter("simple"), CommentModel())
    first = env.ask("How many complete orders are there?")
    assert first.outcome == "answered" and ANSWER in first.text
    calls_before = len(env.analyst.calls)

    out = env.ask("So it is worth promoting this category")
    assert out.outcome == "answered" and "return rate" in out.text
    assert len(env.analyst.calls) - calls_before == 1 and len(_force_calls(env)) == 1
    assert out.sql_queries == 0  # no SQL for a comment
    msgs = _force_calls(env)[0][1]
    assert any(m["role"] == "assistant" and ANSWER in m["content"] for m in msgs)
    assert "Comment reply" in msgs[0]["content"] and msgs[-1]["role"] == "user"
    assert COMMENT_ROUTE in env.spans


def test_comment_fallback_when_reply_fails(make_env) -> None:  # noqa: F811
    env = make_env(TurnRouter("simple"), CommentModel(reply=""))
    env.ask("How many complete orders are there?")
    out = env.ask("I think that deserves a campaign")
    assert out.text == gr.COMMENT_FALLBACK_TEXT and gr.UNAVAILABLE_TEXT not in out.text


def test_statement_without_previous_answer_takes_normal_path(make_env) -> None:  # noqa: F811
    env = make_env(TurnRouter("simple"), CommentModel())
    env.ask("So it is worth promoting this category")
    assert COMMENT_ROUTE not in env.spans
    assert [c for c in env.analyst.calls if c[2] > 0]  # the analyst loop ran


def test_question_after_answer_is_not_a_comment(make_env) -> None:  # noqa: F811
    env = make_env(TurnRouter("simple"), CommentModel())
    env.ask("How many complete orders are there?")
    env.ask("Is it worth promoting?")
    assert not [s for s in env.spans if s[:2] == ("router", "intent")]


# --- Issue B: a budget-hit follow-up prefers the partial text with context ---


class SlowModel(CommentModel):
    """After the first answer, every analyst call advances the fake clock by 70 s and returns
    no answer; the force-style call returns nothing, so the template is used."""

    def __init__(self, clock: list[float]) -> None:
        super().__init__(reply="")
        self.clock = clock
        self.answered = False

    def __call__(self, model, messages, specs, timeout):
        if self.answered and specs:
            self.clock[0] += 70.0
            self.calls.append((model, list(messages), len(specs)))
            return LLMResponse(ModelTurn(""), 10, 10)
        out = super().__call__(model, messages, specs, timeout)
        if isinstance(out.value, ModelTurn) and out.value.text == ANSWER:
            self.answered = True
        return out


def test_budget_hit_follow_up_points_to_previous_answer(make_env) -> None:  # noqa: F811
    clock = [0.0]
    model = SlowModel(clock)
    env = make_env(TurnRouter("complex"), model)
    env.graph.services = dataclasses.replace(env.graph.services, clock=lambda: clock[0])
    first = env.ask("How many complete orders are there?")
    assert first.outcome == "answered"
    out = env.ask("And how does that compare with the year before?")
    assert out.text.startswith(gr.PARTIAL_WITH_CONTEXT_TEXT)
    assert gr.UNAVAILABLE_TEXT not in out.text


def test_budget_hit_first_question_keeps_unavailable_text(make_env) -> None:  # noqa: F811
    model = CommentModel(reply="")
    model.steps = [ModelTurn("")]  # no answer -> force_answer -> template
    env = make_env(Router("complex"), model)
    out = env.ask("compare revenue across years")
    assert out.text.startswith(gr.UNAVAILABLE_TEXT)
