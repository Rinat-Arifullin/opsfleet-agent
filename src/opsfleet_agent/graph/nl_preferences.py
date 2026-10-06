"""Standing preferences stated in natural language (iteration 39b, D-235..D-241).

"From now on answer in tables", "keep answers short", "no charts", "отвечай таблицами впредь",
"remember that our team reviews Acme jeans weekly". :func:`detect_preference` is a pure,
bounded, deterministic check on the CURRENT user message (already scrubbed by the input
guard). There is no model call. Only the user's own message can set a preference: the graph
calls this on ``state["message"]`` only, never on tool, report or retrieved text.

What it returns is a proposal; nothing is stored here. The graph hands the proposal to
``commands.preferences.apply_nl_preference``, which stores it through exactly the code that
``/prefs set`` and ``/prefs note`` use (``graph.memory.set_preference``: enumerated values only,
and notes through ``sanitise_note``). So a "preference" that asks for PII or a wider scope is
rejected there, not here.

Rules (D-235, D-236):

* A sentence is a *standing statement* when it has a strong marker ("from now on", "going
  forward", "by default", "remember that", "I prefer", "впредь", "отныне", "по умолчанию",
  "запомни", "мне удобнее", ...) or a weak marker ("always", "never", "answer in ...",
  "keep answers ...", "всегда", "отвечай ...", or a sentence that is only a chart opt-out such
  as "no charts") with no data word and no question verb in it. A one-off formatting request
  inside a data question ("show sales by month as a table") has no marker and is never stored.
* Questions (a sentence ending in "?" or starting with a question word) are never standing.
* A churn definition ("churn means ...") is session memory (A-4), not a preference.
* Values map onto the allowlisted fields only: format (table, bullets, prose), depth (brief,
  standard, deep) and charts (on/off). Two formats or two depths in one message are ambiguous
  and are not stored; "X instead of Y" keeps X.
* A sentence with a strong marker and no allowlisted value becomes a note candidate (the
  sentence with the leading/trailing temporal marker removed). A note candidate that is really
  a data request ("from now on show me customer emails") is not a note: the message is
  answered (or refused) as a normal question.
* ``mixed`` is True when the message also asks a data question; the graph then saves the
  preference and goes on to answer it (D-237).
* Rows (D-240): "min 10 rows", "at least 10 rows", "показывай минимум 10 строк" set the
  ``rows`` field (clamped to 1..50 by the save path). A bare count ("show 10 rows",
  "top 10 products") is a one-off; a floor word (min, at least, минимум, не меньше) or a
  Russian imperfective verb ("показывай 10 строк") makes it standing.
* ``standing=True`` (``/prefs <free text>``, D-241): the user invoked ``/prefs``, so every
  sentence that is not a question is a standing statement (``mixed`` is always False), and
  one with no field value becomes a note candidate for the sanitiser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from opsfleet_agent.graph.memory import _CHURN_RE, _norm

__all__ = ["MAX_PREFERENCE_CHARS", "NLPreference", "detect_preference"]

MAX_PREFERENCE_CHARS: Final = 400  # longer messages are never treated as a preference statement
MAX_SENTENCES: Final = 8

_W: Final = r"(?<![\w-])"  # word start (a hyphen joins words)
_E: Final = r"(?![\w-])"  # word end

# --- markers ------------------------------------------------------------------------------
_STRONG_EN: Final = (
    r"from\s+now\s+on|going\s+forward|from\s+here\s+on(?:\s+out)?|"
    r"in\s+(?:the\s+)?future|by\s+default|(?:every|each)\s+time|"
    r"for\s+(?:all\s+)?(?:future|later|next)\s+(?:answers|questions|replies|responses)|"
    r"remember(?:\s+that|\s*:|(?=\s+(?:i|we|my|our)\b))|please\s+remember|"
    r"note\s+that|keep\s+in\s+mind(?:\s+that)?|"
    r"i\s+(?:always\s+|generally\s+|usually\s+|really\s+)?prefer|i'd\s+prefer|"
    r"i\s+would\s+prefer|my\s+preference\s+is|i\s+like\s+(?:my\s+)?(?:answers|responses|replies)|"
    r"(?:save|store|keep)\s+(?:this|it|that)\s+as\s+(?:my\s+|a\s+)?(?:permanent\s+)?preference|"
    r"permanent(?:ly)?"
)
_STRONG_RU: Final = (
    r"впредь|отныне|с\s+этого\s+момента|с\s+этого\s+дня|в\s+дальнейшем|дальше\s+всегда|"
    r"по\s+умолчанию|каждый\s+раз|запомни(?:те)?(?:\s*,?\s*что)?|"
    r"мне\s+(?:удобнее|удобней|больше\s+нравится|нравится)|(?:я\s+)?предпочитаю|"
    r"на\s+будущее|в\s+будущем"
)
_STRONG_RE: Final = re.compile(rf"{_W}(?:{_STRONG_EN}|{_STRONG_RU}){_E}")

_WEAK_RE: Final = re.compile(
    rf"{_W}(?:always|never|no\s+more|"
    r"stop\s+(?:showing|using|adding|including|drawing|giving|making)|"
    r"(?:keep|make)\s+(?:the\s+|all\s+|my\s+|your\s+)?(?:answers|responses|replies|them)|"
    r"(?:all|my|your)\s+(?:answers|responses|replies)|"
    r"всегда|никогда|больше\s+не|отвечай(?:те)?|пиши(?:те)?|"
    rf"(?:все\s+|мои\s+)?ответы){_E}"
    # "answer in tables" is standing; "answer in a table" is a one-off reformat
    r"|(?<![\w-])(?:answer|reply|respond)(?:\s+me)?\s+(?:in|with|using|as)\s+"
    r"(?!(?:a|an|the|this|that|it)\s)\w"
)
# The whole sentence is a chart opt-out ("no charts", "без графиков"): standing (D-236).
_BARE_CHART_OFF_RE: Final = re.compile(
    r"\W*(?:please\s+)?(?:no|without|skip(?:\s+the)?|no\s+more)\s+"
    r"(?:charts?|graphs?|plots?|visuali[sz]ations?)(?:\s*,?\s*please)?\W*"
    r"|\W*(?:пожалуйста\s+)?(?:без|никаких|не\s+надо|не\s+нужны)\s+"
    r"(?:графиков|графики|диаграмм|диаграммы|визуализаций)(?:\s*,?\s*пожалуйста)?\W*"
)

# Leading/trailing temporal markers removed from a note (the rest must stay verbatim).
_NOTE_LEAD_RE: Final = re.compile(
    r"^\W*(?:(?:please\s+|and\s+|also\s+)?(?:from\s+now\s+on|going\s+forward|"
    r"from\s+here\s+on(?:\s+out)?|in\s+(?:the\s+)?future|by\s+default|remember(?:\s+that)?|"
    r"please\s+remember(?:\s+that)?|note\s+that|keep\s+in\s+mind(?:\s+that)?|"
    r"впредь|отныне|с\s+этого\s+момента|в\s+дальнейшем|по\s+умолчанию|на\s+будущее|"
    r"запомни(?:те)?(?:\s*,?\s*что)?)(?![\w-])[\s,:;-]*)+",
    re.IGNORECASE,
)
_NOTE_TAIL_RE: Final = re.compile(
    r"[\s,;-]*(?:from\s+now\s+on|going\s+forward|by\s+default|in\s+(?:the\s+)?future|"
    r"впредь|отныне|в\s+дальнейшем|по\s+умолчанию|на\s+будущее)\W*$",
    re.IGNORECASE,
)

# --- data questions -----------------------------------------------------------------------
_QUESTION_START_RE: Final = re.compile(
    r"^\W*(?:what|which|how|who|whom|when|where|why|is|are|was|were|does|did|"
    r"do\s+(?:you|we|i|they)|can|could|will|would|should|"
    rf"что|какой|какая|какие|каких|сколько|почему|зачем|когда|где|кто){_E}"
)
_ASK_RE: Final = re.compile(
    rf"{_W}(?:show|list|give|tell|find|get|compare|break\s+down|calculate|count|plot|chart|"
    r"what|which|how|who|"
    rf"покажи|покажите|показывай|показывайте|выведи|выведите|выводи|дай|дайте|сравни|найди|посчитай|построй|"
    rf"расскажи|сколько|какие|какой|какая){_E}"
)
_DATA_RE: Final = re.compile(
    rf"{_W}(?:revenue|sales|orders?|profit|margin|prices?|costs?|aov|returns?|units?|"
    r"customers?|users?|buyers?|products?|brands?|categor(?:y|ies)|inventory|traffic|top|"
    r"total|average|count|emails?|names?|addresses?|phones?|"
    rf"выручк\w*|продаж\w*|заказ\w*|клиент\w*|покупател\w*|товар\w*|бренд\w*|"
    rf"категори\w*|прибыл\w*|возврат\w*|почт\w*|имен\w*|адрес\w*|телефон\w*){_E}"
    r"|\d"
)

# --- allowlisted values -------------------------------------------------------------------
_FORMATS: Final = {
    "table": rf"{_W}(?:tables?|tabular|таблиц\w*|табличн\w*){_E}",
    "bullets": (
        rf"{_W}(?:bullets?|bullet\s+points?|bulleted(?:\s+lists?)?|"
        r"(?:as|in)\s+(?:a\s+)?lists?|"
        rf"списк\w*|списком|пункт\w*|маркированн\w*|буллет\w*){_E}"
    ),
    "prose": (
        rf"{_W}(?:prose|paragraphs?|plain\s+text|narrative|"
        rf"текстом|абзац\w*|прозой){_E}"
    ),
}
_DEPTHS: Final = {
    "brief": (
        rf"{_W}(?:brief|short(?:er)?|concise|terse|to\s+the\s+point|"
        rf"кратк\w*|коротк\w*|короче|сжат\w*|лаконичн\w*){_E}"
    ),
    "deep": (
        rf"{_W}(?:detailed|in-depth|in\s+depth|deep(?:er)?|thorough|more\s+detail|"
        rf"подробн\w*|детальн\w*|развёрнут\w*|развернут\w*|глубже|глубок\w*){_E}"
    ),
    "standard": rf"{_W}(?:standard|normal|regular|обычн\w*|стандартн\w*){_E}",
}
_CHART_WORDS: Final = (
    r"(?:charts?|graphs?|plots?|visuals?|visuali[sz]ations?|"
    r"график\w*|диаграмм\w*|визуализаци\w*)"
)
_CHART_RE: Final = re.compile(rf"{_W}{_CHART_WORDS}{_E}")
_CHART_NEG_RE: Final = re.compile(
    rf"{_W}(?:don't|dont|do\s+not|not|no|without|skip|never|stop|no\s+more|"
    rf"без|не|нет|никаких|никогда)\s+(?:[\w']+\s+){{0,3}}?{_CHART_WORDS}{_E}"
)
# "tables instead of bullets", "tables, not bullets": the alternative is removed before mapping
_ALTERNATIVE_RE: Final = re.compile(
    rf"{_W}(?:instead\s+of|rather\s+than|over|not|вместо|а\s+не|но\s+не|не)\s+"
    r"(?:[\w'-]+\s+){0,2}?"
    rf"(?:tables?|tabular|bullets?|bullet\s+points?|lists?|prose|paragraphs?|plain\s+text|"
    rf"таблиц\w*|списк\w*|списком|пункт\w*|текстом|абзац\w*){_E}"
)
# "min 10 rows", "show at least 10 rows", "показывай минимум 10 строк" (D-240). The number is
# group "n"; a floor word ("floor") or a Russian imperfective verb ("ru_verb") makes it standing.
_ROWS_RE: Final = re.compile(
    rf"{_W}(?:(?:(?:show|list|give|return|display)(?:\s+me)?|"
    r"(?P<ru_verb>показывай(?:те)?|выводи(?:те)?|давай(?:те)?)(?:\s+мне)?|"
    r"покажи(?:те)?(?:\s+мне)?|выведи(?:те)?)\s+)?"
    r"(?P<floor>(?:a\s+)?min(?:imum)?(?:\s+of)?|at\s+least|no\s+(?:fewer|less)\s+than|"
    r"(?:как\s+)?минимум|не\s+меньше|не\s+менее|хотя\s+бы)?\s*"
    r"(?:top\s+|топ\s*-?\s*)?(?P<n>\d{1,6})\s+"
    r"(?:rows?|items?|lines?|results?|entries|records|"
    rf"строк\w*|строчк\w*|позици\w*|результат\w*|элемент\w*|записей|записи){_E}"
)
_SPLIT_RE: Final = re.compile(r"(?<=[.!?;\n])\s*")


@dataclass(frozen=True)
class NLPreference:
    """A proposal from one user message: settings and/or note candidates, and whether the
    message also asks a data question (the graph then answers it after saving)."""

    settings: tuple[tuple[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
    mixed: bool = False
    ambiguous: tuple[str, ...] = ()  # fields with two conflicting values (not stored)


def _sentences(message: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(message) if p and p.strip()]
    return parts[:MAX_SENTENCES]


def _is_question(sentence: str, norm: str) -> bool:
    return sentence.rstrip().endswith("?") or _QUESTION_START_RE.search(norm) is not None


def _asks_data(norm: str) -> bool:
    return _ASK_RE.search(norm) is not None and _DATA_RE.search(norm) is not None


def _one_of(table: dict[str, str], norm: str) -> list[str]:
    return [value for value, rx in table.items() if re.search(rx, norm)]


def _values(norm: str) -> tuple[list[tuple[str, Any]], list[str]]:
    """Allowlisted (field, value) pairs in one standing sentence, and ambiguous fields."""
    out: list[tuple[str, Any]] = []
    ambiguous: list[str] = []
    counts = {int(m.group("n")) for m in _ROWS_RE.finditer(norm)}
    if len(counts) == 1:
        out.append(("rows", counts.pop()))  # the save path clamps it to 1..50
    elif len(counts) > 1:
        ambiguous.append("rows")
    norm = _ROWS_RE.sub(" ", norm)
    stripped = _ALTERNATIVE_RE.sub(" ", norm)
    for field_name, table in (("format", _FORMATS), ("depth", _DEPTHS)):
        found = _one_of(table, stripped)
        if len(found) == 1:
            out.append((field_name, found[0]))
        elif len(found) > 1:
            ambiguous.append(field_name)
    if _CHART_NEG_RE.search(norm):
        out.append(("charts", False))
    elif _CHART_RE.search(norm):
        out.append(("charts", True))
    return out, ambiguous


def _note_text(sentence: str) -> str:
    text = _NOTE_LEAD_RE.sub("", sentence.strip())
    text = _NOTE_TAIL_RE.sub("", text)
    return text.strip(" \t,;:-").rstrip(".!").strip()


def detect_preference(message: str, *, standing: bool = False) -> NLPreference | None:
    """The standing preference stated in ``message``, or None (see the module docstring).

    ``standing`` is True only for ``/prefs <free text>``: every sentence counts as standing."""
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_PREFERENCE_CHARS:
        return None
    settings: dict[str, Any] = {}
    ambiguous: set[str] = set()
    notes: list[str] = []
    other = False
    for sentence in _sentences(message):
        norm = _norm(sentence)
        if not re.search(r"\w", norm):
            continue
        if _is_question(sentence, norm) or (not standing and _CHURN_RE.search(sentence)):
            other = True  # under ``standing`` a question is neither a value nor a note
            continue
        rows = [m for m in _ROWS_RE.finditer(norm) if m.group("floor") or m.group("ru_verb")]
        rest = _ROWS_RE.sub(" ", norm)  # "10 rows" is a list length, not a data word
        data = _DATA_RE.search(rest) is not None
        strong = standing or _STRONG_RE.search(norm) is not None
        asks = not standing and _asks_data(rest)
        weak = not asks and (
            _WEAK_RE.search(norm) is not None
            or _BARE_CHART_OFF_RE.fullmatch(norm) is not None
            or bool(rows)
        )
        if not (strong or weak):
            other = other or data  # "thanks" or "ok" alone is not a question to answer
            continue
        values, amb = _values(norm)
        ambiguous.update(amb)
        if values or amb:
            for field_name, value in values:
                if field_name in settings and settings[field_name] != value:
                    ambiguous.add(field_name)
                settings[field_name] = value
            other = other or asks  # "from now on show sales as a table": save and answer
            continue
        if standing:
            note = _note_text(sentence) or sentence.strip()
            notes.append(note)
            continue
        if strong and not asks:
            note = _note_text(sentence)
            if note:
                notes.append(note)
                continue
        other = True  # e.g. "from now on show me customer emails": a normal question
    for field_name in ambiguous:
        settings.pop(field_name, None)
    if not settings and not notes and not ambiguous:
        return None
    return NLPreference(
        settings=tuple(settings.items()),
        notes=tuple(notes),
        mixed=other and not standing,
        ambiguous=tuple(sorted(ambiguous)),
    )
