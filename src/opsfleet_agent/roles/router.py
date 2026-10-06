"""Router: one flash-lite structured call that labels the turn (HLD §4.0.3, §4.1, ADR-010).

Structural guarantees enforced here, in code:

* The router sees **user messages only** (R2-M6). Its only input type is
  :class:`RouterInput`, which holds :class:`UserTurn` values; a ``UserTurn`` can be built only
  from an allowed input-guard decision (the scrubbed text) or from a message whose role is
  ``"user"``. Tool output, report bodies and assistant text have no path in.
* The router never sees the persona: its system prompt is ``prompts/router.md`` alone.
* Its output gets a strict schema parse. Any failure (provider down, budget, parse error)
  routes to the full path with label ``complex`` (HLD: router fail-open to the Deep analyst,
  logged ``router_unavailable``); it never routes to the light path.
* The model's ``refusal_text`` must be null or absent (anything else, or a duplicate JSON
  key, is a parse error). Refusals use the fixed templates in
  :mod:`opsfleet_agent.guards.input`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from opsfleet_agent.graph.budget import BudgetExhausted, ForceAnswer
from opsfleet_agent.graph.llm import LLMFailure, LLMResponse, LLMSuccess, LLMWrapper
from opsfleet_agent.guards import input as input_guard
from opsfleet_agent.guards.input import InputDecision

__all__ = [
    "LABELS",
    "LIGHT_LABELS",
    "LIGHT_ROLE_SUBCAPS",
    "ROUTER_ROLE",
    "ChatMessage",
    "Invoke",
    "RouterDecision",
    "RouterInput",
    "RouterOutput",
    "RouterParseError",
    "UserTurn",
    "build_router_messages",
    "load_router_prompt",
    "model_ids_from_settings",
    "route",
]

logger = logging.getLogger(__name__)

ROUTER_ROLE: Final = "router"
ROUTER_PROMPT_VERSION: Final = "router-v3"
ROUTER_PROMPT_PATH: Final = Path(__file__).resolve().parents[3] / "prompts" / "router.md"

LABELS: Final = (
    "simple",
    "complex",
    "report",
    "library",
    "meta",
    "smalltalk",
    "memory",
    "comment",
    "off_topic",
    "injection",
)
# D-155: `memory` (does the agent remember the conversation?) and `comment` (an opinion about
# the previous answer) are router labels, not English-only regex checks.
LIGHT_LABELS: Final = frozenset({"meta", "smalltalk", "memory", "comment"})
FULL_LABELS: Final = frozenset({"simple", "complex", "report", "library"})
FAIL_OPEN_LABEL: Final = "complex"

# Light turn (HLD §4.3): router <= 2 provider calls + light_reply 1, hard cap 3.
LIGHT_ROLE_SUBCAPS: Final[Mapping[str, int]] = {"router": 2, "light_path": 1}

MAX_RAW_OUTPUT_CHARS: Final = 4000
_OUTPUT_KEYS: Final = frozenset({"label", "is_english", "refusal_text"})
_REQUIRED_KEYS: Final = frozenset({"label", "is_english"})

Route = Literal["light", "full", "refuse"]
RouterStatus = Literal["ok", "unavailable", "parse_error"]


# ---------------------------------------------------------------------------
# input types (user messages only)


@dataclass(frozen=True)
class UserTurn:
    """The text of one **user** message, already scrubbed by the input guard."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("UserTurn text must be a non-empty str")
        if len(self.text) > input_guard.MAX_INPUT_CHARS:
            raise ValueError("UserTurn text exceeds the input cap")

    @classmethod
    def from_decision(cls, decision: InputDecision) -> UserTurn:
        """The current message: only an allowed decision's scrubbed text may be routed."""
        if not isinstance(decision, InputDecision):
            raise TypeError("expected an InputDecision")
        if not decision.allowed or decision.scrubbed is None:
            raise ValueError("a refused input cannot be routed")
        return cls(decision.scrubbed)

    @classmethod
    def from_message(cls, role: str, text: str) -> UserTurn:
        """A stored history message (already scrubbed when it was persisted). Any role other
        than ``user`` (assistant, tool, system, report) is rejected."""
        if role != "user":
            raise ValueError("the router only accepts user messages")
        return cls(text)


@dataclass(frozen=True)
class RouterInput:
    """The current and (optionally) the previous user message. Nothing else."""

    current: UserTurn
    previous: UserTurn | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.current, UserTurn):
            raise TypeError("current must be a UserTurn")
        if self.previous is not None and not isinstance(self.previous, UserTurn):
            raise TypeError("previous must be a UserTurn or None")


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user"]
    content: str


# (model, messages, attempt timeout seconds) -> LLMResponse. 14a binds this to the chat model.
Invoke = Callable[[str, Sequence[ChatMessage], float | None], LLMResponse]


# ---------------------------------------------------------------------------
# output schema


class RouterParseError(ValueError):
    """The router output does not match the strict schema."""


@dataclass(frozen=True)
class RouterOutput:
    label: str
    is_english: bool
    refusal_text: str | None

    @classmethod
    def parse(cls, value: Any) -> RouterOutput:
        """Strict parse: exact keys, known label, bool flag. Raises RouterParseError."""
        if isinstance(value, str):
            if len(value) > MAX_RAW_OUTPUT_CHARS:
                raise RouterParseError("output too long")
            try:
                value = json.loads(_strip_fence(value), object_pairs_hook=_no_duplicate_keys)
            except _DuplicateKey as exc:
                raise RouterParseError("duplicate key") from exc
            except (ValueError, RecursionError) as exc:
                raise RouterParseError("not JSON") from exc
        if not isinstance(value, Mapping):
            raise RouterParseError("not an object")
        keys = set(value.keys())
        if not _REQUIRED_KEYS <= keys <= _OUTPUT_KEYS:
            raise RouterParseError("unexpected or missing keys")
        label, is_english, refusal = value["label"], value["is_english"], value.get("refusal_text")
        if not isinstance(label, str) or label not in LABELS:
            raise RouterParseError("unknown label")
        if not isinstance(is_english, bool):
            raise RouterParseError("is_english must be a boolean")
        if refusal is not None:  # the prompt asks for null; anything else is off-contract
            raise RouterParseError("refusal_text must be null")
        return cls(label, is_english, None)


class _DuplicateKey(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _DuplicateKey(k)
        out[k] = v
    return out


_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*)\n\s*```\s*$", re.DOTALL)


def _strip_fence(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text


# ---------------------------------------------------------------------------
# prompt


@lru_cache(maxsize=4)
def load_router_prompt(path: Path = ROUTER_PROMPT_PATH) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("router prompt is empty")
    return text


# Any tag naming a user message: optional whitespace after "<" or "</", "-", "_" or spaces
# between the words, attributes and line breaks before ">".
_DELIM = re.compile(
    r"<\s*/?\s*(?:(?:current|previous)[\s_-]*)?user[\s_-]*message\b[^>]*>", re.IGNORECASE
)


def _fence_user_text(tag: str, text: str) -> str:
    # The user cannot close the delimiter early: any delimiter-like tag in the text is removed.
    return f"<{tag}>\n{_DELIM.sub(' ', text)}\n</{tag}>"


def build_router_messages(inp: RouterInput, *, prompt: str | None = None) -> list[ChatMessage]:
    """System prompt (router rules only, no persona) + one user message with the fenced
    current and previous user messages. Accepts nothing but a RouterInput."""
    if not isinstance(inp, RouterInput):
        raise TypeError("build_router_messages takes a RouterInput")
    parts = []
    if inp.previous is not None:
        parts.append(_fence_user_text("previous_user_message", inp.previous.text))
    parts.append(_fence_user_text("current_user_message", inp.current.text))
    return [
        ChatMessage("system", prompt if prompt is not None else load_router_prompt()),
        ChatMessage("user", "\n\n".join(parts)),
    ]


# ---------------------------------------------------------------------------
# decision


@dataclass(frozen=True)
class RouterDecision:
    label: str
    is_english: bool
    route: Route
    status: RouterStatus
    model: str | None = None
    refusal_rule: str | None = None
    refusal_text: str | None = None
    audit_event: str | None = None

    @property
    def escalation_reason(self) -> str | None:
        return None if self.status == "ok" else f"router_{self.status}"


def _fail_open(status: RouterStatus, model: str | None = None) -> RouterDecision:
    # HLD §4.1: router down -> rules only, label complex, Deep analyst. Never the light path.
    return RouterDecision(FAIL_OPEN_LABEL, True, "full", status, model)


def _refuse(out: RouterOutput, rule: str, model: str) -> RouterDecision:
    return RouterDecision(
        out.label,
        out.is_english,
        "refuse",
        "ok",
        model,
        refusal_rule=rule,
        refusal_text=input_guard.refusal_for(rule),
        audit_event=input_guard.audit_event_for(rule),
    )


def decide(out: RouterOutput, model: str) -> RouterDecision:
    """Map a parsed router output to a route. Code decides; the label is only an input."""
    if not out.is_english:
        return _refuse(out, input_guard.NON_ENGLISH, model)
    if out.label == "off_topic":
        return _refuse(out, input_guard.OFF_TOPIC, model)
    if out.label == "injection":
        return _refuse(out, input_guard.INJECTION, model)
    if out.label in LIGHT_LABELS:
        return RouterDecision(out.label, True, "light", "ok", model)
    return RouterDecision(out.label, True, "full", "ok", model)


def route(
    inp: RouterInput,
    *,
    llm: LLMWrapper,
    model: str,
    invoke: Invoke,
    fallback_model: str | None = None,
    tracer: Any = None,
    prompt: str | None = None,
) -> RouterDecision:
    """Run the router call and decide the route. Never raises for provider or parse errors."""
    try:
        messages = build_router_messages(inp, prompt=prompt)
    except (OSError, ValueError) as exc:  # prompt file missing or empty: rules only
        logger.error("router prompt unavailable: %s", type(exc).__name__)
        return _trace(tracer, _fail_open("unavailable"))

    def attempt_for(m: str) -> Callable[[float], LLMResponse]:
        return lambda timeout: invoke(m, messages, timeout)

    fallback = attempt_for(fallback_model) if fallback_model else None
    try:
        res = llm.call(ROUTER_ROLE, model, attempt_for(model), fallback_model, fallback)
    except Exception as exc:  # the wrapper should not raise; fail open regardless
        logger.error("router call raised: %s", type(exc).__name__)
        return _trace(tracer, _fail_open("unavailable"))
    if isinstance(res, (LLMFailure, BudgetExhausted, ForceAnswer)):
        logger.warning("router unavailable: %s", type(res).__name__)
        return _trace(tracer, _fail_open("unavailable"))
    if not isinstance(res, LLMSuccess):  # explicit, so it holds under python -O too
        logger.warning("router returned an unknown result: %s", type(res).__name__)
        return _trace(tracer, _fail_open("unavailable"))
    try:
        out = RouterOutput.parse(res.response.value)
    except RouterParseError as exc:
        logger.warning("router output rejected: %s", exc)
        return _trace(tracer, _fail_open("parse_error", res.model))
    return _trace(tracer, decide(out, res.model))


def _trace(tracer: Any, d: RouterDecision) -> RouterDecision:
    if tracer is not None:
        tracer.record(
            "router",
            ROUTER_ROLE,
            label=d.label,
            route=d.route,
            model=d.model,
            escalated=d.status != "ok",
            escalation_reason=d.escalation_reason,
        )
        if d.route == "refuse":
            tracer.record("guard", "router", verdict="refuse", rule=d.refusal_rule)
    return d


def model_ids_from_settings(settings: Any, role: str) -> tuple[str, str | None]:
    """(model, fallback) for a role from config (``config/models.yaml``), as in graph/llm."""
    rm = settings.roles[role]
    return rm.model, rm.fallback
