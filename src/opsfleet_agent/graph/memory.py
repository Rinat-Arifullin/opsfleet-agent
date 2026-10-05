"""Session-only memory (iteration 15; HLD §4.3 "User level", A-4, FR-42/43, R2-M6).

Two things live here, both only for the current session:

* **Restatements** (A-4): "churn = 90 days without an order" is captured from the user's
  message, honoured for the rest of the session and stated in each answer that uses it. It is
  never a stored preference: :meth:`SessionMemory.persistable` leaves it out.
  Only a definitional shape counts ("churn means|is|= X", "define churn as X", "by churn I
  mean X"); a restatement that is rejected (too long, unsafe, PII, not a definition) returns a
  fixed reason that the answer states, so the user is never silently ignored.
* **Preferences** set through ``set_preference``: enumerated values only (format, depth,
  charts), applied only when the value or a synonym appears in the CURRENT user message next to
  an intent phrase ("use tables", "keep answers short", "I do not want charts";
  ``VALUE_NOT_IN_MESSAGE`` otherwise), so text from a report or a tool result, or a word that
  merely contains the value ("orders table", "short-term"), cannot set one.
  Notes are sanitised (no URLs, code, instruction-like or policy-conflicting text), at most
  200 characters, at most 5 (``NOTE_REJECTED``). Persisting preferences across sessions is
  iteration 39: :class:`PreferenceStore` is the seam (``store/preferences.py`` implements it,
  ``/prefs`` writes through it), and :func:`render_preferences` turns validated values into
  fixed code sentences for the lowest-precedence prompt block.

A pending clarification (AC-23.2/23.3) is also session memory: the original question waits for
the user's answer and is completed with it once, without asking again.

The memory is an immutable value; every change returns a new one. :meth:`to_state` /
:meth:`from_state` convert to plain JSON-safe data for the checkpointed graph state, and
``from_state`` fails closed (anything malformed falls back to the defaults). No I/O.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol

from opsfleet_agent.guards.input import _fold  # the input guard's scan fold (R3-M1)
from opsfleet_agent.guards.pii_regex import scrub

__all__ = [
    "DEFAULT_CHURN",
    "MAX_NOTES",
    "MAX_NOTE_CHARS",
    "MAX_RESTATEMENT_CHARS",
    "NOTE_REJECTED",
    "VALUE_NOT_IN_MESSAGE",
    "INVALID_ARGS",
    "RESTATEMENT_CAPTURED",
    "RESTATEMENT_NONE",
    "RESTATEMENT_REASONS",
    "PendingClarification",
    "PreferenceNote",
    "PreferenceResult",
    "PreferenceStore",
    "SessionMemory",
    "capture_restatement",
    "message_contains",
    "sanitise_note",
    "validate_restatement",
    "set_preference",
]

DEFAULT_CHURN: Final = (
    "monthly: a customer who placed an order in month M-1 and no order in month M (A-4)"
)
MAX_NOTES: Final = 5
MAX_NOTE_CHARS: Final = 200
MAX_RESTATEMENT_CHARS: Final = 160
MAX_MESSAGE_CHARS: Final = 4000  # input_guard cap; longer text is never scanned past this
MAX_OPTIONS: Final = 3
MAX_OPTION_CHARS: Final = 240

RESTATEMENT_NONE: Final = ""  # no restatement in the message
RESTATEMENT_CAPTURED: Final = "captured"
# Fixed, user-facing reasons (never echo user text).
RESTATEMENT_REASONS: Final[dict[str, str]] = {
    "too_long": f"it is longer than {MAX_RESTATEMENT_CHARS} characters",
    "pii": "it contains personal data",
    "unsafe": "it contains instruction-like, code-like or policy-conflicting text",
    "not_a_definition": "it does not read as a definition (a time window and an activity)",
    "names_brand": "it names a brand outside your scope",
}

VALUE_NOT_IN_MESSAGE: Final = "VALUE_NOT_IN_MESSAGE"
NOTE_REJECTED: Final = "NOTE_REJECTED"
INVALID_ARGS: Final = "INVALID_ARGS"

# field -> value -> synonyms (matched as whole words/phrases, case-insensitive).
_SYNONYMS: Final[dict[str, dict[Any, tuple[str, ...]]]] = {
    "format": {
        "table": ("table", "tables", "tabular"),
        "bullets": ("bullets", "bullet", "bullet points", "bulleted"),
        "prose": ("prose", "paragraph", "paragraphs", "narrative", "plain text"),
    },
    "depth": {
        "brief": ("brief", "short", "shorter", "concise", "terse"),
        "standard": ("standard", "normal", "regular"),
        "deep": ("deep", "detailed", "in-depth", "in depth", "thorough", "more detail"),
    },
    "charts": {
        True: ("chart", "charts", "graph", "graphs", "plot", "plots"),
        False: (
            "no chart", "no charts", "without charts", "without a chart", "no graphs",
            "skip charts", "skip the charts", "don't want charts", "no plots",
        ),
    },
}  # fmt: skip
_FIELDS: Final = tuple(_SYNONYMS)

_URL_RE: Final = re.compile(r"(?i)(https?:|www\.|://|\bftp:|[a-z0-9-]+\.(com|net|org|io)\b)")
_CODE_RE: Final = re.compile(
    r"(?i)(`|[{}<>\[\];$\\]|\bselect\b|\bimport\b|\bdef\b|\beval\b|\bexec\b|\bsudo\b|=>|==)"
)
_INSTRUCTION_RE: Final = re.compile(
    r"(?i)\b(ignore|disregard|forget|override|bypass|jailbreak|pretend|act as|you are|"
    r"you must|from now on|system prompt|instructions?|rules?|safety|guardrails?|"
    r"developer mode|do anything)\b"
)
# AC-24.2: a "preference" that would widen scope or expose PII is not a preference.
_POLICY_RE: Final = re.compile(
    r"(?i)\b(e-?mails?|phone|phones|addresse?s?|names? of customers|customer names?|"
    r"whose names?|named|surnames?|first names?|last names?|full names?|"
    r"personal (data|details|info)|pii|passwords?|api keys?|all brands|every brand|"
    r"other brands|any brand|all products|unscoped|unfiltered|raw rows|full rows)\b"
)
_CTRL_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # C0, DEL, C1

# A-4 restatement: a definitional shape only ("churn means|is|= X", "define churn as X",
# "by churn I mean X"); a bare "churn:" is a topic label, not a definition. The capture runs to
# the end of the sentence (bounded by the input cap) so an over-long one is reported, not cut.
_CHURN_RE: Final = re.compile(
    r"(?i)(?:\bchurn\s+(?:means|should\s+mean|is\s+defined\s+as|counts\s+as)"
    r"|(?P<bare_is>\bchurn\s+is)\b(?!\s+defined)"
    r"|\bchurn\s*="
    r"|\bdefine\s+churn\s+as|\bby\s+churn\s+i\s+mean|\btreat\s+churn\s+as"
    r"|\bcount\s+churn\s+as|\bconsider\s+churn\s+(?:as|to\s+be))"
    rf"\s*(?P<def>[^.;\n?!]{{1,{MAX_MESSAGE_CHARS}}})"
)
# A definition names a window or an activity; "churn is high this month" names neither.
_DEFINITIONAL_RE: Final = re.compile(
    r"(?i)\b(customers?|users?|buyers?|shoppers?|accounts?|orders?|ordered|ordering|"
    r"purchases?|purchased|bought|buying|inactive|active|lapsed|cancel\w*|stopp?\w*|"
    r"returning|no|without|days?|weeks?|months?|quarters?|years?|\d+)\b"
)

# set_preference evidence (R2-M3): an intent phrase, then up to three filler words, then the
# value; or the value followed by a confirming tail; or a message that is only the value.
_FILLER: Final = (
    r"(?:the|a|an|my|your|our|it|them|me|us|answers?|results?|responses?|replies|reply|"
    r"output|everything|all|only|more|bit|please|really|very|as|in|of|form|format|style|"
    r"using|things|summaries|summary)"
)
_INTENT: Final = (
    r"(?:prefer|use|using|show(?:\s+(?:it|them|me|results?|answers?|everything))?\s+(?:as|in)|"
    r"show(?:\s+me)?|give\s+me|i\s+(?:want|need|'d\s+like|would\s+like)|"
    r"make\s+(?:it|them|answers?|results?|responses?)|"
    r"keep\s+(?:it|them|answers?|results?|responses?)|switch\s+to|answer\s+in|"
    r"format(?:ted)?\s+(?:as|in)|draw|include|add|with|in|as)"
)
# "churn is X" is a definition only with a window or a lapse ("churn is high this month" is not).
_BARE_IS_RE: Final = re.compile(
    r"(?i)\b(\d+|no|without|inactive|lapsed|stopped|cancel\w*|didn't|did not|not)\b"
)
_PLACEHOLDER_RE: Final = re.compile(r"<[A-Z][A-Z_]*>")
_TAIL: Final = r"(?:please|from now on|going forward|only|by default|instead|thanks)"
_CHART_WORDS: Final = r"(?:charts?|graphs?|plots?|visuals?|visuali[sz]ations?)"
_CHART_NEG_RE: Final = re.compile(
    r"(?<![\w'])(?:don't|dont|do\s+not|not|no|without|skip|never|stop)\s+"
    rf"(?:[\w']+\s+){{0,3}}?(?:the\s+)?{_CHART_WORDS}(?![\w-])"
)
_APOSTROPHES: Final = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})
_WORD_RE: Final = re.compile(r"\w+")


def _drop_format(text: str) -> str:
    """NFKC with format characters (Cf: zero-width space/joiners, bidi marks) removed."""
    t = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFKC", t)


def _norm(text: str) -> str:
    t = _drop_format(str(text)[:MAX_MESSAGE_CHARS]).translate(_APOSTROPHES)
    return " ".join(_CTRL_RE.sub(" ", t).split()).lower()


def _mixed_script(text: str) -> bool:
    """True when a word mixes Latin and non-Latin letters ("y\u043eu" with a Cyrillic o).

    A word wholly in one script passes; only the mix is a spoofing signal (R3-M1).
    """
    for word in _WORD_RE.findall(text):
        latin = other = False
        for c in word:
            if not c.isalpha():
                continue
            if unicodedata.name(c, "").startswith("LATIN"):
                latin = True
            else:
                other = True
            if latin and other:
                return True
    return False


def _phrase(p: str) -> str:
    return r"\s+".join(re.escape(w) for w in _norm(p).split())


def message_contains(message: str, phrase: str) -> bool:
    """True when ``phrase`` appears in ``message`` as whole words (case/space-insensitive).

    A hyphen counts as part of a word, so "short" is not found in "short-term".
    """
    p = _norm(phrase)
    if not p:
        return False
    rx = r"(?<![\w-])" + _phrase(p) + r"(?![\w-])"
    return re.search(rx, _norm(message)) is not None


def _stated_with_intent(message: str, phrase: str) -> bool:
    """``phrase`` appears in ``message`` next to an intent phrase (see ``_INTENT``)."""
    msg = _norm(message)
    val = r"(?<![\w-])" + _phrase(phrase) + r"(?![\w-])"
    if re.fullmatch(r"\W*" + val + rf"(?:[\s,]+{_TAIL})?\W*", msg):
        return True
    if re.search(rf"(?<![\w']){_INTENT}\s+(?:{_FILLER}\s+){{0,3}}?" + val, msg):
        return True
    return re.search(val + rf"[\s,]+{_TAIL}(?![\w-])", msg) is not None


@dataclass(frozen=True)
class PreferenceNote:
    text: str
    scope_snapshot: Mapping[str, Any]  # snapshot of the scope the note was written under


@dataclass(frozen=True)
class PendingClarification:
    original: str  # the scrubbed original question
    options: tuple[str, ...]
    # Snapshot of the scope the question was asked under (R3-L3): the answer is merged only
    # while the current scope still covers it. None (never produced by assemble_context) is
    # treated as "covers nothing" by the caller.
    scope_snapshot: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PreferenceResult:
    ok: bool
    memory: SessionMemory
    code: str = ""  # "" when ok, else VALUE_NOT_IN_MESSAGE / NOTE_REJECTED / INVALID_ARGS
    reason: str = ""  # short machine reason, never echoes user text


class PreferenceStore(Protocol):
    """Seam for persisted preferences (iteration 39; ``store.preferences`` implements it)."""

    def save(self, user_id: str, preferences: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class SessionMemory:
    churn_definition: str | None = None  # session restatement (A-4); None = default
    preferences: Mapping[str, Any] = field(default_factory=dict)  # format/depth/charts
    notes: tuple[PreferenceNote, ...] = ()
    pending_clarification: PendingClarification | None = None
    # Snapshot of the scope the restatement was captured under (R4-L4): assembly applies it
    # only while the current scope covers it, and from_state drops a restatement without one.
    churn_scope: Mapping[str, Any] | None = None

    @property
    def churn(self) -> str:
        return self.churn_definition or DEFAULT_CHURN

    def without_pending(self) -> SessionMemory:
        """The same memory with no pending clarification (any non-clarifying finalize)."""
        return replace(self, pending_clarification=None)

    def persistable(self) -> dict[str, Any]:
        """What iteration 39 persists: preferences and notes (with the scope snapshot each note
        was written under, so FR-76 filtering still works in a later session). Never a
        restatement and never a pending clarification."""
        return {
            "preferences": dict(self.preferences),
            "notes": [{"text": n.text, "scope": dict(n.scope_snapshot)} for n in self.notes],
        }

    def to_state(self) -> dict[str, Any]:
        pending = self.pending_clarification
        return {
            "churn_definition": self.churn_definition,
            "churn_scope": (
                _snapshot_state(self.churn_scope) if self.churn_scope is not None else None
            ),
            "preferences": dict(self.preferences),
            "notes": [{"text": n.text, "scope": dict(n.scope_snapshot)} for n in self.notes],
            "pending": (
                {
                    "original": pending.original,
                    "options": list(pending.options),
                    "scope": (
                        _snapshot_state(pending.scope_snapshot)
                        if pending.scope_snapshot is not None
                        else None
                    ),
                }
                if pending
                else None
            ),
        }

    @classmethod
    def from_state(cls, obj: Any) -> SessionMemory:
        """Rebuild from graph state; anything malformed is dropped (fail closed)."""
        if not isinstance(obj, Mapping):
            return cls()
        churn = obj.get("churn_definition")
        churn_scope = _valid_snapshot(obj.get("churn_scope"))
        if validate_restatement(churn)[0] != churn or churn_scope is None:
            churn, churn_scope = None, None
        prefs: dict[str, Any] = {}
        raw_prefs = obj.get("preferences")
        if isinstance(raw_prefs, Mapping):
            for k in _FIELDS:
                v = raw_prefs.get(k)
                if v is not None and _valid_value(k, v):
                    prefs[k] = v
        notes: list[PreferenceNote] = []
        raw_notes = obj.get("notes")
        if isinstance(raw_notes, list):
            for n in raw_notes[:MAX_NOTES]:
                if not isinstance(n, Mapping) or not isinstance(n.get("text"), str):
                    continue
                snap = _valid_snapshot(n.get("scope"))  # R4-L3: same shape check as pending
                if snap is not None and sanitise_note(n["text"]) == n["text"]:
                    notes.append(PreferenceNote(n["text"], snap))
        return cls(churn, prefs, tuple(notes), _pending_from_state(obj.get("pending")), churn_scope)


def _pending_from_state(raw: Any) -> PendingClarification | None:
    """A pending clarification re-validated from state; anything invalid drops it whole."""
    if not isinstance(raw, Mapping):
        return None
    original, opts = raw.get("original"), raw.get("options")
    if not isinstance(original, str) or not original.strip():
        return None
    if len(original) > MAX_MESSAGE_CHARS or scrub(original).text != original:
        return None
    if not isinstance(opts, list) or not 1 <= len(opts) <= MAX_OPTIONS:
        return None
    if any(sanitise_note(o, max_chars=MAX_OPTION_CHARS) != o for o in opts):
        return None
    snap = _valid_snapshot(raw.get("scope"))
    if snap is None:
        return None
    return PendingClarification(original, tuple(opts), snap)


def _snapshot_state(snap: Mapping[str, Any]) -> dict[str, Any]:
    return {"all": snap.get("all"), "brands": list(snap.get("brands") or ())}


def _valid_snapshot(raw: Any) -> dict[str, Any] | None:
    """The ``{"all": bool, "brands": [str, ...]}`` shape of ``context.snapshot_of``, or None.

    Kept local (context imports this module); ``context.covers`` re-parses it fail-closed.
    """
    if not isinstance(raw, Mapping) or set(raw) != {"all", "brands"}:
        return None
    all_, brands = raw.get("all"), raw.get("brands")
    if not isinstance(all_, bool) or not isinstance(brands, list | tuple):
        return None
    if len(brands) > 1000 or not all(isinstance(b, str) and b.strip() for b in brands):
        return None
    if all_ == bool(brands):  # all-scope has no brands; a brand scope has at least one
        return None
    return {"all": all_, "brands": sorted(brands)}


def validate_restatement(text: Any) -> tuple[str | None, str]:
    """``(definition, "")`` for an acceptable churn definition, else ``(None, reason)``.

    ``reason`` is a key of :data:`RESTATEMENT_REASONS`, or "" for text too short to be one.
    Used on capture and again in :meth:`SessionMemory.from_state` (defence in depth).
    """
    if not isinstance(text, str):
        return None, ""
    # R4-L1: normalised before the edge strip, so a fullwidth or small "-", ":" or "," left at
    # an edge is stripped on capture too, and re-validation in from_state is idempotent.
    cleaned = " ".join(_CTRL_RE.sub(" ", _drop_format(text[:MAX_MESSAGE_CHARS])).split())
    cleaned = cleaned.strip(" ,:-")
    if len(cleaned) < 3:
        return None, ""
    if len(cleaned) > MAX_RESTATEMENT_CHARS:
        return None, "too_long"
    if scrub(cleaned).redacted:
        return None, "pii"
    definition = sanitise_note(cleaned, max_chars=MAX_RESTATEMENT_CHARS)
    if definition is None:
        return None, "unsafe"
    if not _DEFINITIONAL_RE.search(definition):
        return None, "not_a_definition"
    return definition, ""


def capture_restatement(
    memory: SessionMemory, message: str, scope_snapshot: Mapping[str, Any] | None = None
) -> tuple[SessionMemory, str]:
    """Capture a churn restatement from the current message (A-4). Session-only.

    Returns ``(memory, status)``: :data:`RESTATEMENT_NONE` when the message holds no
    definitional shape (or the sentence is a question), :data:`RESTATEMENT_CAPTURED`, or a
    rejection reason (a key of :data:`RESTATEMENT_REASONS`) the answer must state. A rejected
    restatement leaves the memory unchanged (the default or the earlier restatement stays).
    A captured one records ``scope_snapshot`` (a malformed or missing snapshot records None,
    and assembly then never applies the restatement: fail closed).
    """
    # Matched on the scrubbed text (defence in depth): an e-mail's dots cannot cut the capture
    # short and hide it, and any masked value inside the definition rejects it as personal data.
    text = scrub(str(message)[:MAX_MESSAGE_CHARS]).text
    m = _CHURN_RE.search(text)
    if not m:
        return memory, RESTATEMENT_NONE
    if text[m.end() : m.end() + 1] == "?":  # "churn means what?" asks, it does not define
        return memory, RESTATEMENT_NONE
    if _PLACEHOLDER_RE.search(m.group("def")):
        return memory, "pii"
    definition, reason = validate_restatement(m.group("def"))
    if definition is not None and m.group("bare_is") and not _BARE_IS_RE.search(definition):
        definition, reason = None, "not_a_definition"
    if definition is None:
        # "churn is high this month" is a statement about churn, not a definition: stay silent.
        silent = not reason or (reason == "not_a_definition" and m.group("bare_is"))
        return memory, RESTATEMENT_NONE if silent else reason
    snap = _valid_snapshot(scope_snapshot)
    return replace(memory, churn_definition=definition, churn_scope=snap), RESTATEMENT_CAPTURED


def sanitise_note(text: Any, *, max_chars: int = MAX_NOTE_CHARS) -> str | None:
    """A cleaned note, or None when it must be rejected (AC-24.2, AC-24.3).

    Rejected: empty, longer than ``max_chars``, structured PII (``pii_regex.scrub``, defence in
    depth behind the input guard), a word mixing Latin and non-Latin letters, a URL, code-like
    characters, instruction-like wording, or a policy-conflicting request (PII, names, wider
    scope), checked on the text and on its homoglyph fold.
    """
    if not isinstance(text, str):
        return None
    # NFKC with zero-width/format characters removed, so the stored text is the folded form
    # ("\uff49\uff47\uff4e\uff4f\uff52\uff45" -> "ignore") and from_state re-validation is
    # idempotent.
    cleaned = " ".join(_CTRL_RE.sub(" ", _drop_format(text[: max_chars * 4])).split())
    if not cleaned or len(cleaned) > max_chars:
        return None
    if scrub(cleaned).redacted or _mixed_script(cleaned):
        return None
    # The rules also run on the input guard's fold (Cyrillic/Greek look-alikes, small capitals,
    # combining marks), so "y\u043eu must" cannot pass as an unknown word (R3-M1).
    folded = _fold(cleaned)
    for rx in (_URL_RE, _CODE_RE, _INSTRUCTION_RE, _POLICY_RE):
        if rx.search(cleaned) or rx.search(folded):
            return None
    return cleaned


def set_preference(
    memory: SessionMemory,
    *,
    message: str,
    scope_snapshot: Mapping[str, Any],
    action: str = "set",
    field: str | None = None,
    value: Any = None,
    note: str | None = None,
) -> PreferenceResult:
    """Apply a ``set_preference`` call to session memory (HLD §4.2), enforced in code.

    ``message`` is the CURRENT scrubbed user message; the value (or a synonym) must appear in it
    next to an intent phrase, and a note must appear in it verbatim.
    ``reset`` clears preferences and notes; ``view`` changes nothing.
    """
    if action == "view":
        return PreferenceResult(True, memory)
    if action == "reset":
        return PreferenceResult(True, replace(memory, preferences={}, notes=()))
    if action != "set" or (field is None) == (note is None):
        return PreferenceResult(False, memory, INVALID_ARGS, "bad_action_or_args")
    if note is not None:
        cleaned = sanitise_note(note)
        if cleaned is None:
            return PreferenceResult(False, memory, NOTE_REJECTED, "sanitiser")
        if not message_contains(message, cleaned):
            return PreferenceResult(False, memory, VALUE_NOT_IN_MESSAGE, "note_not_in_message")
        if len(memory.notes) >= MAX_NOTES:
            return PreferenceResult(False, memory, NOTE_REJECTED, "too_many_notes")
        new_note = PreferenceNote(cleaned, dict(scope_snapshot))
        return PreferenceResult(True, replace(memory, notes=(*memory.notes, new_note)))
    values = _SYNONYMS.get(field or "")
    if values is None or not _valid_value(field or "", value):
        return PreferenceResult(False, memory, INVALID_ARGS, "bad_field_or_value")
    if not _value_in_message(field or "", value, message):
        return PreferenceResult(False, memory, VALUE_NOT_IN_MESSAGE, "value_not_in_message")
    prefs = {**memory.preferences, field: value}
    return PreferenceResult(True, replace(memory, preferences=prefs))


def _valid_value(field: str, value: Any) -> bool:
    if field == "charts":
        return isinstance(value, bool)
    return isinstance(value, str) and value in _SYNONYMS[field]


def _value_in_message(field: str, value: Any, message: str) -> bool:
    if field == "charts":
        negated = _CHART_NEG_RE.search(_norm(message)) is not None or any(
            message_contains(message, p) for p in _SYNONYMS["charts"][False]
        )
        if value is False:
            return negated
        return not negated and any(
            _stated_with_intent(message, p) for p in _SYNONYMS["charts"][True]
        )
    return any(_stated_with_intent(message, p) for p in _SYNONYMS[field][value])


_FORMAT_SENTENCES = {
    "table": "Prefer a table for lists and comparisons.",
    "bullets": "Prefer bullet points.",
    "prose": "Prefer short prose paragraphs.",
}
_DEPTH_SENTENCES = {
    "brief": "Keep answers brief.",
    "standard": "Use standard answer depth.",
    "deep": "Give a deeper analysis with more breakdowns.",
}


def render_preferences(memory: SessionMemory) -> str:
    """Validated preferences as fixed code sentences, or "" (iteration 39, AC-24.4).

    Only enumerated values reach the prompt, and only through these sentences: no user text
    is copied here (notes go as fenced, scope-filtered data in the context section).
    """
    prefs = memory.preferences
    out: list[str] = []
    fmt, depth, charts = prefs.get("format"), prefs.get("depth"), prefs.get("charts")
    if fmt in _FORMAT_SENTENCES:
        out.append(_FORMAT_SENTENCES[fmt])
    if depth in _DEPTH_SENTENCES:
        out.append(_DEPTH_SENTENCES[depth])
    if charts is True:
        out.append("Suggest a chart when it helps.")
    elif charts is False:
        out.append("Do not suggest charts.")
    return "\n".join(f"- {line}" for line in out)
