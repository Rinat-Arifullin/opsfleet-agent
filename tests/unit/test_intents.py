"""D-152/D-155: the `memory` and `comment` router labels (offline, fake LLMs, synthetic text).

D-155 removed the English-only regex detectors: the router labels these turns in any
language, and the graph answers them with the code-owned texts."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from opsfleet_agent.graph import graph as gr
from opsfleet_agent.graph import intents
from opsfleet_agent.graph.context import HISTORY_TURNS
from opsfleet_agent.graph.fixed_replies import FIXED_KEY
from opsfleet_agent.graph.intents import COMMENT_FALLBACK_TEXT, MEMORY_TEXT
from opsfleet_agent.graph.llm import LLMResponse
from opsfleet_agent.guards.input import NON_ENGLISH, refusal_for
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.roles.light_path import CAPABILITIES_TEXT
from opsfleet_agent.roles.router import LABELS, LIGHT_LABELS
from tests.unit.test_graph import SIMPLE, Router, Scripted, SeqRouter, _state, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)
from tests.unit.test_reports import TurnRouter

ANSWER = "There were 3 complete orders."  # synthetic previous answer (grounded by SIMPLE)
COMMENT_ROUTE = ("router", "intent", {"label": "comment", "route": "comment"})
MEMORY_QUESTIONS = [
    "Do you see previous messages in our session?",
    "Is our chat saved?",
]
COMMENTS = ["So it is worth promoting this category", "I think that brand deserves more stock"]


class LangRouter:
    """Labels every turn with ``label`` and the given ``is_english`` flag."""

    def __init__(self, label: str, *, is_english: bool = True) -> None:
        self.label, self.is_english = label, is_english
        self.calls: list[Any] = []

    def __call__(self, model, messages, timeout):
        self.calls.append((model, list(messages)))
        body = {"label": self.label, "is_english": self.is_english, "refusal_text": None}
        return LLMResponse(json.dumps(body), 5, 5)


class BrokenRouter:
    """Router outage: every call returns text that is not the JSON contract."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, model, messages, timeout):
        self.calls.append((model, list(messages)))
        return LLMResponse("I think this is a memory question.", 5, 5)


# --- labels and texts ---


def test_memory_and_comment_are_light_labels() -> None:
    assert {"memory", "comment"} <= set(LABELS)
    assert {"memory", "comment"} <= LIGHT_LABELS


def test_regex_detectors_are_gone() -> None:
    assert not hasattr(intents, "is_memory_question")
    assert not hasattr(intents, "is_comment_followup")
    assert gr.COMMENT_FALLBACK_TEXT is COMMENT_FALLBACK_TEXT  # one definition, two names


def test_memory_text_derives_turn_count() -> None:
    assert f"last {HISTORY_TURNS} questions" in MEMORY_TEXT
    assert not any(ch.isdigit() for ch in MEMORY_TEXT.replace(str(HISTORY_TURNS), ""))


# --- `memory`: the code-owned answer on the light path ---


@pytest.mark.parametrize("text", MEMORY_QUESTIONS)
def test_memory_label_answers_static_text(make_env, text: str) -> None:  # noqa: F811
    env = make_env(Router("memory"), Scripted(ModelTurn("unused")))
    out = env.ask(text)
    assert out.route == "light" and out.label == "memory" and out.text == MEMORY_TEXT
    assert CAPABILITIES_TEXT not in out.text
    assert env.analyst.calls == [] and len(env.router.calls) == 1  # router only, no light call
    reply = _state(env)["history"][-1]
    assert reply["role"] == "assistant" and reply[FIXED_KEY] == "memory"


def test_memory_label_after_an_answer_still_static(make_env) -> None:  # noqa: F811
    env = make_env(SeqRouter("simple", "memory"), Scripted(sql_call(SIMPLE), ModelTurn(ANSWER)))
    env.ask("How many complete orders are there?")
    calls_before = len(env.analyst.calls)
    out = env.ask("Do you remember what I asked?")
    assert out.text == MEMORY_TEXT and len(env.analyst.calls) == calls_before


def test_data_question_about_remembering_is_not_memory(make_env) -> None:  # noqa: F811
    # "Do you remember the revenue for 2023?" is labelled simple by the router: analyst loop.
    env = make_env(Router("simple"), Scripted(sql_call(SIMPLE), ModelTurn(ANSWER)))
    out = env.ask("Do you remember the revenue for 2023?")
    assert out.text != MEMORY_TEXT and ANSWER in out.text
    assert [c for c in env.analyst.calls if c[2] > 0]


def test_non_english_memory_question_is_refused(make_env) -> None:  # noqa: F811
    # FR-17 runs before the label: a non-English memory question gets the language refusal.
    env = make_env(LangRouter("memory", is_english=False), Scripted(ModelTurn("unused")))
    out = env.ask("Ты помнишь наш разговор?")
    assert out.outcome == "refused" and out.text == refusal_for(NON_ENGLISH)
    assert MEMORY_TEXT not in out.text and env.analyst.calls == []


def test_router_outage_fails_open_not_memory(make_env) -> None:  # noqa: F811
    env = make_env(BrokenRouter(), Scripted(ModelTurn(ANSWER)))
    out = env.ask("Do you see previous messages in our session?")
    assert out.label == "complex" and out.route == "full"
    assert out.text != MEMORY_TEXT and COMMENT_FALLBACK_TEXT not in out.text
    assert 1 <= len(env.router.calls) <= 3  # bounded


def test_capabilities_question_unchanged(make_env) -> None:  # noqa: F811
    env = make_env(Router("meta"))
    out = env.ask("What can you do?")
    assert out.route == "light" and out.text.startswith(CAPABILITIES_TEXT)
    assert MEMORY_TEXT not in out.text


# --- `comment`: one brief reply from the previous answer ---


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


@pytest.mark.parametrize("text", COMMENTS)
def test_comment_gets_one_reply_from_previous_answer(make_env, text: str) -> None:  # noqa: F811
    router = TurnRouter("simple")
    env = make_env(router, CommentModel())
    first = env.ask("How many complete orders are there?")
    assert first.outcome == "answered" and ANSWER in first.text
    calls_before = len(env.analyst.calls)

    router.label = "comment"
    out = env.ask(text)
    assert out.outcome == "answered" and "return rate" in out.text and out.label == "comment"
    assert len(env.analyst.calls) - calls_before == 1 and len(_force_calls(env)) == 1
    assert out.sql_queries == 0  # no SQL for a comment
    msgs = _force_calls(env)[0][1]
    assert any(m["role"] == "assistant" and ANSWER in m["content"] for m in msgs)
    assert "Comment reply" in msgs[0]["content"] and msgs[-1]["role"] == "user"
    assert COMMENT_ROUTE in env.spans


def test_comment_fallback_when_reply_fails(make_env) -> None:  # noqa: F811
    router = TurnRouter("simple")
    env = make_env(router, CommentModel(reply=""))
    env.ask("How many complete orders are there?")
    router.label = "comment"
    out = env.ask("I think that deserves a campaign")
    assert out.text == COMMENT_FALLBACK_TEXT and gr.UNAVAILABLE_TEXT not in out.text


def test_comment_without_previous_answer_gets_static_text(make_env) -> None:  # noqa: F811
    env = make_env(TurnRouter("comment"), CommentModel())
    out = env.ask("So it is worth promoting this category")
    assert out.route == "light" and out.text == COMMENT_FALLBACK_TEXT
    assert env.analyst.calls == [] and COMMENT_ROUTE not in env.spans
    reply = _state(env)["history"][-1]
    assert reply[FIXED_KEY] == "comment_fallback"


def test_non_english_comment_is_refused(make_env) -> None:  # noqa: F811
    env = make_env(LangRouter("comment", is_english=False), CommentModel())
    out = env.ask("Похоже, эту категорию стоит продвигать")
    assert out.outcome == "refused" and out.text == refusal_for(NON_ENGLISH)
    assert env.analyst.calls == []


def test_question_after_answer_is_not_a_comment(make_env) -> None:  # noqa: F811
    # A comment that holds a question is labelled simple/complex by the router: analyst loop.
    env = make_env(TurnRouter("simple"), CommentModel())
    env.ask("How many complete orders are there?")
    calls_before = len(env.analyst.calls)
    env.ask("Interesting, and what about 2023?")
    assert COMMENT_ROUTE not in env.spans
    assert [c for c in env.analyst.calls[calls_before:] if c[2] > 0]


# --- a budget-hit follow-up prefers the partial text with context ---


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
