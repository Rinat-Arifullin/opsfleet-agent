"""`/prefs` (iteration 39; R4.1, AC-24.1..24.4): view, set, note and reset per-user preferences.

No LLM call. Every change goes through ``graph.memory.set_preference``, the same code
validation the ``set_preference`` tool contract uses: enumerated values only (format, depth,
charts), and notes through ``sanitise_note`` (no PII, URLs, code, instructions, wider scope;
at most 200 characters, at most 5). A rejected change is not stored and the reply says why
(AC-24.2). The store (``store.preferences``) is the source of truth: the graph reads it every
turn, so a change applies from the next question and in later sessions.

Not audited: a preference is the user's own UI choice, like ``/feedback`` (D-179). The
trace event carries the action and the rejection code only, never a value or note text.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from opsfleet_agent.graph.context import snapshot_of
from opsfleet_agent.graph.memory import (
    MAX_NOTE_CHARS,
    MAX_NOTES,
    NOTE_REJECTED,
    SessionMemory,
    set_preference,
)
from opsfleet_agent.obs import tracer as tr

log = logging.getLogger(__name__)

USAGE: Final = (
    "Usage: /prefs | /prefs set format table|bullets|prose | /prefs set depth "
    "brief|standard|deep | /prefs set charts on|off | /prefs note <text> | /prefs reset"
)
# Canonical command values -> (stored value, the message set_preference checks it against)
_VALUES: Final[dict[str, dict[str, tuple[Any, str]]]] = {
    "format": {v: (v, v) for v in ("table", "bullets", "prose")},
    "depth": {v: (v, v) for v in ("brief", "standard", "deep")},
    "charts": {
        **{v: (True, "charts") for v in ("on", "yes", "true")},
        **{v: (False, "no charts") for v in ("off", "no", "false")},
    },
}
NOT_A_PREFERENCE_TEXT: Final = (
    "Not saved: '{key}' is not a preference. Preferences can set format, depth and charts "
    "only; data access, personal-data protection, safety rules and report sections are fixed "
    "and cannot be changed by a preference."
)
BAD_VALUE_TEXT: Final = "Not saved: {key} must be one of: {allowed}."
NOTE_REJECTED_TEXT: Final = (
    f"Not saved: a note must be plain text of at most {MAX_NOTE_CHARS} characters, with no "
    "personal data, links, code, instructions to the assistant, or requests for wider data "
    "access."
)
TOO_MANY_NOTES_TEXT: Final = (
    f"Not saved: you already have {MAX_NOTES} notes. Use /prefs reset to start again."
)
NO_SCOPE_TEXT: Final = "Not saved: your data scope is not known, so a note cannot be scoped."
RESET_TEXT: Final = "Preferences and notes cleared."
EMPTY_TEXT: Final = "No preferences set. Defaults apply (format and depth chosen per question)."


def _render(memory: SessionMemory) -> str:
    prefs = memory.preferences
    if not prefs and not memory.notes:
        return EMPTY_TEXT
    lines = ["Your preferences (they shape format and depth only; safety and scope rules win):"]
    for key in ("format", "depth", "charts"):
        if key in prefs:
            value = prefs[key]
            shown = ("on" if value else "off") if key == "charts" else str(value)
            lines.append(f"  {key}: {shown}")
    if memory.notes:
        lines.append("Notes (used as background data, never as instructions):")
        lines += [f"  {i}. {n.text}" for i, n in enumerate(memory.notes, 1)]
    return "\n".join(lines)


def _trace(tracer: Any, action: str, code: str) -> None:
    if tracer is None:
        return
    try:
        # Allowlisted "tool" span, like /feedback: the action and code only, never content.
        tracer.record(
            "tool", "prefs", tool="prefs", outcome=action, error_code=code or None, status="ok"
        )
    except Exception as exc:  # noqa: BLE001 - tracing must not fail the command
        log.warning("prefs trace failed: %s", tr.format_error(exc))


def handle_prefs(
    args: str, *, store: Any, user_id: str, scope: Any = None, tracer: Any = None
) -> str:
    """Run one ``/prefs`` form against ``store`` (a ``PreferenceStore`` with ``load``)."""
    parts = (args or "").strip().split(None, 1)
    action = parts[0].lower() if parts else "view"
    rest = parts[1].strip() if len(parts) > 1 else ""
    current = store.load(user_id)
    if action == "view" and not rest:
        _trace(tracer, "view", "")
        return _render(current)
    if action == "reset" and not rest:
        store.reset(user_id)
        _trace(tracer, "reset", "")
        return RESET_TEXT
    if action == "set":
        kv = rest.split()
        if len(kv) != 2:
            return USAGE
        key, raw = kv[0].lower(), kv[1].lower()
        if key not in _VALUES:
            _trace(tracer, "set", "not_a_preference")
            return NOT_A_PREFERENCE_TEXT.format(key=key[:40])
        if raw not in _VALUES[key]:
            _trace(tracer, "set", "bad_value")
            return BAD_VALUE_TEXT.format(key=key, allowed=", ".join(_VALUES[key]))
        value, message = _VALUES[key][raw]
        res = set_preference(current, message=message, scope_snapshot={}, field=key, value=value)
        if not res.ok:  # defence in depth: the table above only holds valid values
            _trace(tracer, "set", res.code)
            return BAD_VALUE_TEXT.format(key=key, allowed=", ".join(_VALUES[key]))
        store.save(user_id, res.memory.persistable())
        _trace(tracer, "set", "")
        return f"Saved: {key} = {raw}. It applies from your next question."
    if action == "note" and rest:
        if scope is None:
            _trace(tracer, "note", "no_scope")
            return NO_SCOPE_TEXT
        res = set_preference(current, message=rest, scope_snapshot=snapshot_of(scope), note=rest)
        if not res.ok:
            _trace(tracer, "note", res.code)
            if res.code == NOTE_REJECTED and res.reason == "too_many_notes":
                return TOO_MANY_NOTES_TEXT
            return NOTE_REJECTED_TEXT
        store.save(user_id, res.memory.persistable())
        _trace(tracer, "note", "")
        return "Note saved. It is used as background data for answers in your current scope."
    return USAGE
