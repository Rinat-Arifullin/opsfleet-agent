"""Code-owned static replies in history (D-156).

A reply the code wrote (the capabilities text, the memory answer, the "show me the SQL" reply,
the greeting template, error and fallback templates) is a fact about the conversation, not an
answer the model should build on. In every LLM prompt such a history turn is replaced by a
short marker, so a model cannot copy it into a later answer.

* :func:`fixed_kind` names the static reply a text is (or starts with), or returns ``None``.
  ``_finalize`` stores that kind in the history entry (``"fixed"``) at write time; for
  checkpoints written before D-156 the context builder falls back to the same matcher.
* :func:`marker` is the text that stands in for such a turn in a prompt.
* :func:`static_texts` lists the static texts the echo check compares answers against.

The texts are imported lazily: this module is imported by ``graph.context``, which the
modules that own the texts import in turn.
"""

from __future__ import annotations

import functools
import re
from typing import Final

__all__ = [
    "CAPABILITIES_KIND",
    "FIXED_KEY",
    "MARKER_PREFIX",
    "contains_marker",
    "fixed_kind",
    "is_marker",
    "marker",
    "static_texts",
]

FIXED_KEY: Final = "fixed"  # history entry key: the kind of code-owned reply
CAPABILITIES_KIND: Final = "capabilities"
MARKER_PREFIX: Final = "[assistant "
_CAPABILITIES_MARKER: Final = "[assistant described its capabilities]"
_FIXED_MARKER: Final = "[assistant gave a fixed reply: {kind}]"
# A static text this long or longer also matches as a prefix (a scope line or a query list may
# follow it); shorter ones must match the whole text.
_MIN_PREFIX_CHARS: Final = 40
_MAX_KIND_CHARS: Final = 40
_MARKER_RE: Final = re.compile(
    r"\[assistant (?:described its capabilities|gave a fixed reply: \w{1,40})\]", re.IGNORECASE
)


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


@functools.cache
def _registry() -> tuple[tuple[str, str], ...]:
    """(kind, static text) pairs, longest text first so a prefix match picks the most specific."""
    from opsfleet_agent.graph import graph as g
    from opsfleet_agent.graph.degraded import AI_UNAVAILABLE_TEXT
    from opsfleet_agent.graph.intents import MEMORY_TEXT
    from opsfleet_agent.guards import input as input_guard
    from opsfleet_agent.guards.output import REFUSAL_TEXT
    from opsfleet_agent.guards.plain_language import SQL_NOT_SHOWN_TEXT
    from opsfleet_agent.roles.light_path import CAPABILITIES_TEXT, GREETING_TEMPLATE

    pairs = [
        (CAPABILITIES_KIND, CAPABILITIES_TEXT),
        ("memory", MEMORY_TEXT),
        ("sql_request", SQL_NOT_SHOWN_TEXT),
        ("greeting", GREETING_TEMPLATE),
        ("error", g.ERROR_TEXT),
        ("blocked", REFUSAL_TEXT),
        ("unavailable", g.UNAVAILABLE_TEXT),
        ("partial", g.PARTIAL_WITH_CONTEXT_TEXT),
        ("comment_fallback", g.COMMENT_FALLBACK_TEXT),
        ("ai_unavailable", AI_UNAVAILABLE_TEXT),
        *(("refusal", t) for t in input_guard.REFUSALS.values() if isinstance(t, str)),
    ]
    out = [(k, _norm(t)) for k, t in pairs if _norm(t)]
    return tuple(sorted(out, key=lambda p: -len(p[1])))


def static_texts() -> tuple[str, ...]:
    """Every code-owned static reply, whitespace-normalised and case-folded."""
    return tuple(t for _, t in _registry())


def fixed_kind(text: object) -> str | None:
    """The kind of code-owned reply ``text`` is, or ``None`` for any other text."""
    if not isinstance(text, str):
        return None
    t = _norm(text)
    if not t:
        return None
    for kind, static in _registry():
        if t == static or (len(static) >= _MIN_PREFIX_CHARS and t.startswith(static)):
            return kind
    return None


def marker(kind: str) -> str:
    """The prompt text that replaces a code-owned reply of this kind."""
    if kind == CAPABILITIES_KIND:
        return _CAPABILITIES_MARKER
    safe = "".join(c for c in str(kind) if c.isalnum() or c == "_")[:_MAX_KIND_CHARS] or "other"
    return _FIXED_MARKER.format(kind=safe)


def is_marker(text: object) -> bool:
    """True for a marker this module wrote (an assistant turn with no answer content)."""
    return isinstance(text, str) and text.startswith(MARKER_PREFIX) and text.endswith("]")


def contains_marker(text: object) -> bool:
    """True when ``text`` quotes a marker: an answer that does is copying history (D-156)."""
    return isinstance(text, str) and _MARKER_RE.search(text[:20_000]) is not None
