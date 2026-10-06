"""`/prefs` (iteration 39; R4.1, AC-24.1..24.4): view, set, note and reset per-user preferences.

No LLM call. Every change goes through ``graph.memory.set_preference``, the same code
validation the ``set_preference`` tool contract uses: enumerated values only (format, depth,
charts) or a whole number 1..50 (rows, iteration 39b), and notes through ``sanitise_note``
(no PII, URLs, code, instructions, wider scope; at most 200 characters, at most 5). A
rejected change is not stored and the reply says why (AC-24.2). ``/prefs <free text>`` that
is not a subcommand ("/prefs give me min 10 rows in tables") goes through the
chat-preference path (D-241): mapped onto the fields where it can be, otherwise stored as a
note through the same sanitiser as ``/prefs note``. The store (``store.preferences``) is the
source of truth: the graph reads it every turn, so a change applies from the next question
and in later sessions.

Not audited: a preference is the user's own UI choice, like ``/feedback`` (D-179). The
trace event carries the action and the rejection code only, never a value or note text.
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any, Final

from opsfleet_agent.graph.context import snapshot_of
from opsfleet_agent.graph.memory import (
    MAX_NOTE_CHARS,
    MAX_NOTES,
    NOTE_REJECTED,
    ROWS_MAX,
    ROWS_MIN,
    SessionMemory,
    clamp_rows,
    set_preference,
)
from opsfleet_agent.graph.nl_preferences import detect_preference
from opsfleet_agent.obs import tracer as tr

log = logging.getLogger(__name__)

USAGE: Final = (
    "Usage: /prefs | /prefs set format table|bullets|prose | /prefs set depth "
    "brief|standard|deep | /prefs set charts on|off | /prefs set rows 1-50 | /prefs note <text> "
    "| /prefs reset | /prefs <what you prefer, in your words>"
)
PREFERENCE_FIELDS: Final = ("format", "depth", "charts", "rows")
# Canonical command values -> (stored value, the message set_preference checks it against)
_VALUES: Final[dict[str, dict[str, tuple[Any, str]]]] = {
    "format": {v: (v, v) for v in ("table", "bullets", "prose")},
    "depth": {v: (v, v) for v in ("brief", "standard", "deep")},
    "charts": {
        **{v: (True, "charts") for v in ("on", "yes", "true")},
        **{v: (False, "no charts") for v in ("off", "no", "false")},
    },
}
_ROWS_ALLOWED: Final = f"a whole number from {ROWS_MIN} to {ROWS_MAX}"


def canonical_value(key: str, raw: Any) -> tuple[Any, str] | None:
    """``(stored value, the message set_preference checks it against)`` for a ``/prefs set``
    value, or None. ``rows`` takes any whole number and clamps it to 1..50 (D-240)."""
    if key == "rows":
        text = str(raw).strip()
        if isinstance(raw, bool) or not re.fullmatch(r"\d{1,6}", text):
            return None
        rows = clamp_rows(int(text))
        return rows, f"{rows} rows"
    return _VALUES.get(key, {}).get(str(raw).strip().lower())


def allowed_values(key: str) -> str:
    return _ROWS_ALLOWED if key == "rows" else ", ".join(_VALUES.get(key, {}))


NOT_A_PREFERENCE_TEXT: Final = (
    "Not saved: '{key}' is not a preference. Preferences can set format, depth, charts "
    "and rows only; data access, personal-data protection, safety rules and report sections "
    "are fixed and cannot be changed by a preference."
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
    for key in PREFERENCE_FIELDS:
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
        if not kv:
            return USAGE
        key = kv[0].lower()
        if key not in PREFERENCE_FIELDS:
            return _lenient_set(None, rest, store=store, user_id=user_id, tracer=tracer)
        canonical = canonical_value(key, kv[1]) if len(kv) == 2 else None
        if canonical is None:  # "set format reports table", "set charts none": read the words
            return _lenient_set(key, " ".join(kv[1:]), store=store, user_id=user_id, tracer=tracer)
        value, message = canonical
        res = set_preference(current, message=message, scope_snapshot={}, field=key, value=value)
        if not res.ok:  # defence in depth: canonical_value only returns valid values
            _trace(tracer, "set", res.code)
            return BAD_VALUE_TEXT.format(key=key, allowed=allowed_values(key))
        store.save(user_id, res.memory.persistable())
        _trace(tracer, "set", "")
        return f"Saved: {key} = {_raw_of(key, value)}. It applies from your next question."
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
    if action in _SUBCOMMANDS:  # a known subcommand in the wrong form: the usage line
        return USAGE
    return _free_text(args.strip(), store=store, user_id=user_id, scope=scope, tracer=tracer)


_SUBCOMMANDS: Final = frozenset({"view", "reset", "set", "note", "help"})


def _lenient_set(key: str | None, words: str, *, store: Any, user_id: str, tracer: Any) -> str:
    """``/prefs set`` that is not exactly ``<field> <value>`` (D-242): read the words with the
    chat detector and save field values only, never a note. ``key`` (when the user named a
    field) limits what may be saved to that field; nothing found gives the allowed values of
    that field (or the not-a-preference line), not the whole usage line."""
    if key == "rows":
        numbers = re.findall(r"(?<![\w.-])\d{1,6}(?![\w.])", words)
        found = canonical_value("rows", numbers[0]) if len(set(numbers)) == 1 else None
        values = [("rows", found[0])] if found else []
        ambiguous: tuple[str, ...] = ()
    else:
        text = f"{key} {words}" if key else words
        detected = detect_preference(text, standing=True) if text.strip() else None
        values = [
            (k, v) for k, v in (detected.settings if detected else ()) if key in (None, k)
        ]
        if key == "charts" and values == [("charts", True)]:  # the field word alone is no "on"
            said_on = any(canonical_value("charts", w) == (True, "charts") for w in words.split())
            values = values if said_on else []
        ambiguous = tuple(a for a in (detected.ambiguous if detected else ()) if key in (None, a))
    if not values and not ambiguous:
        if key is None:
            _trace(tracer, "set", "not_a_preference")
            first = words.split()[0] if words.split() else ""
            return NOT_A_PREFERENCE_TEXT.format(key=first[:40])
        _trace(tracer, "set", "bad_value")
        return BAD_VALUE_TEXT.format(key=key, allowed=allowed_values(key))
    picked = SimpleNamespace(settings=tuple(values), notes=(), ambiguous=ambiguous)
    reply, _saved, _rejected = apply_nl_preference(
        picked, store=store, user_id=user_id, tracer=tracer
    )
    return reply


def _free_text(text: str, *, store: Any, user_id: str, scope: Any, tracer: Any) -> str:
    """``/prefs <free text>`` (D-241): the user invoked /prefs, so the text is a standing
    preference by definition. Fields where the chat detector maps it, otherwise each sentence
    as a note through the ``/prefs note`` sanitiser. A single word (most likely a mistyped
    subcommand) and questions give the usage line."""
    if len(text.split()) < 2:
        return USAGE
    detected = detect_preference(text, standing=True)
    if detected is None:  # only questions or punctuation: nothing to save
        return USAGE
    reply, _saved, _rejected = apply_nl_preference(
        detected, store=store, user_id=user_id, scope=scope, tracer=tracer
    )
    return reply or USAGE


# --- natural-language preferences (iteration 39b, D-235..D-238) -----------------------------

NL_UNDO_TEXT: Final = "To undo: /prefs reset (or /prefs to view what is saved)."
NL_AMBIGUOUS_TEXT: Final = (
    "Not saved: '{key}' was named more than once with different values. Say one, for "
    "example /prefs set {key} {example}."
)
NL_UNAVAILABLE_TEXT: Final = "Not saved: preferences are unavailable right now."
_NL_EXAMPLE: Final = {"format": "table", "depth": "brief", "charts": "off", "rows": "10"}


def _raw_of(key: str, value: Any) -> str:
    if key == "charts" and isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def apply_nl_preference(
    detected: Any, *, store: Any, user_id: str, scope: Any = None, tracer: Any = None
) -> tuple[str, bool, bool]:
    """Persist a preference the user stated in chat (``graph.nl_preferences.NLPreference``).

    The same code path as ``/prefs set`` and ``/prefs note``: each value goes through its
    canonical ``/prefs`` message into ``set_preference`` and each note through
    ``sanitise_note`` (D-238). ``detected`` comes only from the user's own message of this
    turn. Returns ``(confirmation text, saved anything, rejected anything)``.
    """
    if store is None:
        _trace(tracer, "nl", "unavailable")
        return NL_UNAVAILABLE_TEXT, False, True
    try:
        current = store.load(user_id)
    except Exception as exc:  # noqa: BLE001 - a store failure must not fail the turn
        log.warning("nl prefs load failed: %s", tr.format_error(exc))
        _trace(tracer, "nl", "unavailable")
        return NL_UNAVAILABLE_TEXT, False, True
    saved: list[str] = []
    problems: list[str] = []
    for key, value in detected.settings:
        found = canonical_value(key, _raw_of(key, value))
        if found is None:
            problems.append(BAD_VALUE_TEXT.format(key=key[:40], allowed=allowed_values(key)))
            continue
        canonical, message = found
        res = set_preference(
            current, message=message, scope_snapshot={}, field=key, value=canonical
        )
        if not res.ok:
            _trace(tracer, "nl_set", res.code)
            problems.append(BAD_VALUE_TEXT.format(key=key, allowed=allowed_values(key)))
            continue
        current = res.memory
        saved.append(f"{key} = {_raw_of(key, canonical)}")
        _trace(tracer, "nl_set", "")
    for note in detected.notes:
        if scope is None:
            _trace(tracer, "nl_note", "no_scope")
            problems.append(NO_SCOPE_TEXT)
            continue
        res = set_preference(current, message=note, scope_snapshot=snapshot_of(scope), note=note)
        if not res.ok:
            _trace(tracer, "nl_note", res.code)
            too_many = res.code == NOTE_REJECTED and res.reason == "too_many_notes"
            problems.append(TOO_MANY_NOTES_TEXT if too_many else NOTE_REJECTED_TEXT)
            continue
        current = res.memory
        saved.append("a note")
        _trace(tracer, "nl_note", "")
    for key in detected.ambiguous:
        _trace(tracer, "nl_set", "ambiguous")
        problems.append(NL_AMBIGUOUS_TEXT.format(key=key, example=_NL_EXAMPLE.get(key, "")))
    if saved:
        try:
            store.save(user_id, current.persistable())
        except Exception as exc:  # noqa: BLE001 - say so instead of claiming it was saved
            log.warning("nl prefs save failed: %s", tr.format_error(exc))
            _trace(tracer, "nl", "unavailable")
            return NL_UNAVAILABLE_TEXT, False, True
    lines: list[str] = []
    if saved:
        lines.append(
            f"Saved preference: {', '.join(saved)}. It applies from now on, in this and later "
            f"sessions. {NL_UNDO_TEXT}"
        )
    lines += problems
    return "\n".join(lines), bool(saved), bool(problems)
