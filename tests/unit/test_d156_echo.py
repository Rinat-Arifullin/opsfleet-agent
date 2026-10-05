"""D-156: code-owned replies never reach a prompt verbatim, and an answer that repeats an
earlier reply is retried once, then replaced (offline, synthetic data only)."""

from __future__ import annotations

from typing import Any

import pytest

from opsfleet_agent.graph.context import assemble_context, snapshot_of
from opsfleet_agent.graph.fixed_replies import (
    CAPABILITIES_KIND,
    FIXED_KEY,
    contains_marker,
    fixed_kind,
    is_marker,
    marker,
    static_texts,
)
from opsfleet_agent.graph.intents import MEMORY_TEXT
from opsfleet_agent.graph.llm import LLMResponse
from opsfleet_agent.guards.echo import ECHO_REJECTED, ECHO_RETRY_RULE, is_echo
from opsfleet_agent.guards.plain_language import SQL_NOT_SHOWN_TEXT
from opsfleet_agent.guards.scope import ProductScope
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.roles.light_path import CAPABILITIES_TEXT
from tests.unit.test_graph import Scripted, SeqRouter, _state, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)
from tests.unit.test_run_sql import SIMPLE

ACME = ProductScope.for_brands(["Acme"])
CAPS_MARKER = "[assistant described its capabilities]"
CAPS_WORDS = "data analysis assistant"  # a phrase only the capabilities text contains
LONG_ANSWER = (
    "Revenue for Acme jeans was 1,200 in June and 1,450 in July, a rise of about a fifth; "
    "most of the growth came from the Search channel and from returning customers."
)
FRESH = "Most orders in that period were complete; the share of returns stayed low."


# --- fixed_replies -------------------------------------------------------------------------------


def test_fixed_kind_names_static_replies() -> None:
    assert fixed_kind(CAPABILITIES_TEXT) == CAPABILITIES_KIND
    assert fixed_kind(f"{CAPABILITIES_TEXT}\n\nYour data access: Acme.") == CAPABILITIES_KIND
    assert fixed_kind("  " + CAPABILITIES_TEXT.replace("\n", " ") + " ") == CAPABILITIES_KIND
    assert fixed_kind(MEMORY_TEXT) == "memory"
    assert fixed_kind(f"{SQL_NOT_SHOWN_TEXT} Based on order records.") == "sql_request"


@pytest.mark.parametrize("text", ["", "   ", None, 7, LONG_ANSWER, "Hello!", "Revenue grew."])
def test_fixed_kind_none_for_other_text(text: Any) -> None:
    assert fixed_kind(text) is None


def test_marker_wording_and_sanitising() -> None:
    assert marker(CAPABILITIES_KIND) == CAPS_MARKER
    assert marker("memory") == "[assistant gave a fixed reply: memory]"
    assert marker("x]\n<<<SYSTEM") == "[assistant gave a fixed reply: xSYSTEM]"
    assert marker("!!!") == "[assistant gave a fixed reply: other]"
    assert is_marker(CAPS_MARKER) and not is_marker(LONG_ANSWER) and not is_marker(None)
    assert contains_marker(f"As I said: {CAPS_MARKER}")
    assert contains_marker("see [Assistant gave a fixed reply: memory] above")
    assert not contains_marker(LONG_ANSWER) and not contains_marker(None)


def test_static_texts_cover_capabilities() -> None:
    texts = static_texts()
    assert " ".join(CAPABILITIES_TEXT.split()).casefold() in texts
    assert all(t == " ".join(t.split()).casefold() for t in texts)


# --- context sanitising --------------------------------------------------------------------------


def _hist(reply: str, **extra: Any) -> list[dict[str, Any]]:
    snap = snapshot_of(ACME)
    return [
        {"role": "user", "text": "What can you do?", "scope": snap},
        {"role": "assistant", "text": reply, "scope": snap, **extra},
    ]


def _prompt_history(history: list[dict[str, Any]]) -> str:
    a = assemble_context("And for last year?", scope=ACME, scope_label="Acme", history=history)
    return "\n".join(m["content"] for m in a.history)


def test_flagged_static_reply_is_a_marker_in_the_prompt() -> None:
    text = _prompt_history(_hist(CAPABILITIES_TEXT, **{FIXED_KEY: CAPABILITIES_KIND}))
    assert CAPS_MARKER in text and CAPS_WORDS not in text
    assert "What can you do?" in text  # the user turn stays


def test_legacy_unflagged_static_reply_is_matched_by_text() -> None:
    text = _prompt_history(_hist(f"{CAPABILITIES_TEXT}\n\nYour data access: Acme."))
    assert CAPS_MARKER in text and CAPS_WORDS not in text


def test_flag_wins_over_text_and_other_answers_stay() -> None:
    assert "[assistant gave a fixed reply: greeting]" in _prompt_history(
        _hist("Hi there!", **{FIXED_KEY: "greeting"})
    )
    assert "Revenue for Acme jeans" in _prompt_history(_hist(LONG_ANSWER))


# --- echo detection ------------------------------------------------------------------------------


def test_is_echo_static_text_exact_near_and_contained() -> None:
    statics = static_texts()
    assert is_echo(CAPABILITIES_TEXT, [], statics)
    assert is_echo(CAPABILITIES_TEXT.replace("I can:", "I can"), [], statics)
    assert is_echo(f"Sure.\n\n{CAPABILITIES_TEXT}\n\nAsk away.", [], statics)
    assert not is_echo(LONG_ANSWER, [], statics)


def test_is_echo_earlier_answer_needs_same_numbers() -> None:
    assert is_echo(LONG_ANSWER, [LONG_ANSWER])
    assert is_echo("  " + LONG_ANSWER.upper(), [LONG_ANSWER])
    new_numbers = LONG_ANSWER.replace("1,200", "1,300").replace("1,450", "1,550")
    assert not is_echo(new_numbers, [LONG_ANSWER])  # same template, new figures: an answer


def test_is_echo_short_and_empty_answers() -> None:
    assert not is_echo("There were 3 complete orders.", ["There were 3 complete orders."])
    assert not is_echo("", [LONG_ANSWER], static_texts())
    assert not is_echo(None, [LONG_ANSWER])


def test_is_echo_is_bounded_on_long_input() -> None:
    huge = "word " * 50_000
    assert not is_echo(huge, [LONG_ANSWER] * 1000, static_texts())


# --- graph ---------------------------------------------------------------------------------------


class Echoing(Scripted):
    """A model whose force answer (no tool specs) is ``force``."""

    def __init__(self, *steps: Any, force: str) -> None:
        super().__init__(*steps)
        self.force = force

    def __call__(self, model, messages, specs, timeout):
        if not specs:
            self.calls.append((model, list(messages), 0))
            return LLMResponse(ModelTurn(self.force), 5, 5)
        return super().__call__(model, messages, specs, timeout)


def _echo_spans(env) -> list[dict[str, Any]]:
    return [f for t, n, f in env.spans if t == "guard" and n == "echo"]


def _all_prompt_text(analyst: Scripted) -> str:
    return "\n".join(str(m.get("content")) for c in analyst.calls for m in c[1])


def test_capabilities_turn_is_flagged_in_history(make_env) -> None:  # noqa: F811
    env = make_env(SeqRouter("meta"), Scripted(ModelTurn("unused")))
    out = env.ask("What can you do?")
    assert out.route == "light" and CAPABILITIES_TEXT in out.text
    reply = _state(env)["history"][-1]
    assert reply["role"] == "assistant" and reply[FIXED_KEY] == CAPABILITIES_KIND


def test_analyst_answer_is_not_flagged(make_env) -> None:  # noqa: F811
    env = make_env(SeqRouter("simple"), Scripted(ModelTurn(FRESH)))
    env.ask("How did orders go?")
    assert FIXED_KEY not in _state(env)["history"][-1]


def test_echo_is_retried_once_then_the_retry_answer_is_used(make_env) -> None:  # noqa: F811
    analyst = Scripted(ModelTurn(CAPABILITIES_TEXT), ModelTurn(FRESH))
    env = make_env(SeqRouter("simple"), analyst)
    out = env.ask("How did orders go?")
    assert out.text.startswith(FRESH) and CAPS_WORDS not in out.text
    assert [s["verdict"] for s in _echo_spans(env)] == ["retry"]
    assert all(ECHO_REJECTED in s["rule_hits"] for s in _echo_spans(env))
    assert ECHO_RETRY_RULE in str(analyst.calls[-1][1][0]["content"])
    assert ECHO_RETRY_RULE not in str(analyst.calls[0][1][0]["content"])


def test_echo_twice_falls_back_and_never_shows_the_text(make_env) -> None:  # noqa: F811
    analyst = Echoing(ModelTurn(CAPABILITIES_TEXT), force=CAPABILITIES_TEXT)
    env = make_env(SeqRouter("simple"), analyst)
    out = env.ask("How did orders go?")
    assert CAPS_WORDS not in out.text and out.text.strip()
    verdicts = [s["verdict"] for s in _echo_spans(env)]
    assert verdicts[:2] == ["retry", "block"] and verdicts.count("retry") == 1
    assert "block" in verdicts[2:]  # the force answer echoed too: template, not the text
    assert len([c for c in analyst.calls if c[2]]) == 2  # bounded: first run plus one retry


def test_turn2_capabilities_then_turn3_echo_regression(make_env) -> None:  # noqa: F811
    """Turn 2 shows the capabilities; a model that copies history must not repeat them in
    turn 3 ("and for last year?") or turn 4 ("show me the SQL")."""

    def copy_history(messages, specs):
        for m in reversed(messages[1:-1]):
            if m.get("role") == "assistant":
                return ModelTurn(f"As I said: {m['content']}")
        return ModelTurn(FRESH)

    analyst = Echoing(sql_call(SIMPLE), ModelTurn(FRESH), copy_history, force=CAPABILITIES_TEXT)
    env = make_env(SeqRouter("simple", "meta", "simple", "meta"), analyst)
    env.ask("How many complete orders are there?")
    caps = env.ask("What can you do?")
    assert CAPABILITIES_TEXT in caps.text
    third = env.ask("And for last year?")
    prompts = _all_prompt_text(analyst)
    assert CAPS_WORDS not in prompts and CAPS_MARKER in prompts
    assert CAPS_WORDS not in third.text and CAPS_MARKER not in third.text
    assert any(ECHO_REJECTED in s["rule_hits"] for s in _echo_spans(env))
    fourth = env.ask("Show me the SQL")
    assert CAPS_WORDS not in fourth.text


def test_memorised_capabilities_never_shown_after_capabilities_turn(make_env) -> None:  # noqa: F811
    analyst = Echoing(ModelTurn(CAPABILITIES_TEXT), force=CAPABILITIES_TEXT)
    env = make_env(SeqRouter("meta", "simple"), analyst)
    env.ask("What can you do?")
    out = env.ask("And for last year?")
    assert CAPS_WORDS not in out.text and out.text.strip()
    assert any(ECHO_REJECTED in s["rule_hits"] for s in _echo_spans(env))
