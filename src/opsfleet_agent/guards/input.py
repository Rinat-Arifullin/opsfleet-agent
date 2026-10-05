"""Input guard (HLD §4.1 layer 1, §5.2; FR-70, AC-08.4 input half, AC-11.1/2/5, AC-23.4).

Runs on every user message before the router and before anything is stored:

1. Type and length check (``MAX_INPUT_CHARS``), before any scanning.
2. PII scrub with the 8a regex + 8b NER detector (:meth:`PiiDetector.mask`). Only the
   scrubbed text may be stored, logged or sent to a model.
3. Deterministic rule scan on normalised copies of the raw text (format characters and
   combining marks removed, casefolded, small capitals, homoglyphs and leetspeak folded,
   whitespace collapsed per line, markdown emphasis and in-word joiners as spaces,
   dotted, letter-spaced and line-split words rejoined):
   injection, prompt exfiltration, personal-data requests, clear off-topic requests,
   non-English input and encoded payloads.

A hit is refused **in code** with a fixed English template; the model never writes the
refusal. Any internal error fails closed (the message is refused, nothing is processed).
The router (``roles/router.py``) adds a model-based classification after this guard; it
can only add refusals, never remove one made here.

The scan is a deterministic first layer, not a complete defence: paraphrased injection is
left to the router, the output guard and the code guardrails downstream (D-47, D-50).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Final

from opsfleet_agent.guards.pii import PiiDetector, default_detector

__all__ = [
    "AUDIT_INJECTION",
    "AUDIT_PII_BLOCK",
    "AUDIT_REFUSED",
    "MAX_INPUT_CHARS",
    "PII_NOTICE",
    "REFUSALS",
    "InputDecision",
    "audit_event_for",
    "check_input",
    "refusal_for",
]

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS: Final = 4000

AUDIT_INJECTION: Final = "guardrail.injection"
AUDIT_REFUSED: Final = "guardrail.refused"
AUDIT_PII_BLOCK: Final = "guardrail.pii_block"

# Rule codes (also the trace `rule` value of the guard span).
INVALID_INPUT: Final = "invalid_input"
INPUT_TOO_LONG: Final = "input_too_long"
INPUT_GUARD_ERROR: Final = "input_guard_error"
INJECTION: Final = "injection"
PROMPT_EXFILTRATION: Final = "prompt_exfiltration"
ENCODED_PAYLOAD: Final = "encoded_payload"
PII_REQUEST: Final = "pii_request"
OFF_TOPIC: Final = "off_topic"
NON_ENGLISH: Final = "non_english"

_ANALYSIS_OFFER = (
    "I can help you analyse the store's orders, products, revenue and customer trends "
    "(in aggregate) within your access."
)

# Fixed templates. The model never writes a refusal shown to the user.
REFUSALS: Final[dict[str, str]] = {
    INVALID_INPUT: "I couldn't read that message. Please type your question as plain text.",
    INPUT_TOO_LONG: (
        f"Your message is too long (limit {MAX_INPUT_CHARS:,} characters). "
        "Please shorten it and ask again."
    ),
    INPUT_GUARD_ERROR: (
        "I couldn't check your message safely, so I didn't process it. Please try again."
    ),
    INJECTION: f"I can't change, ignore or bypass my rules. {_ANALYSIS_OFFER}",
    PROMPT_EXFILTRATION: (
        f"I can't share my internal instructions or configuration. {_ANALYSIS_OFFER}"
    ),
    ENCODED_PAYLOAD: (
        "I can't process encoded or obfuscated content. Please ask your question in plain English."
    ),
    PII_REQUEST: (
        "I can't share personal data such as customer names, emails, phone numbers or "
        "addresses. I can give you aggregated results or anonymised customer IDs instead, "
        'for example "top 5 customers by revenue, by customer ID".'
    ),
    OFF_TOPIC: (
        "I can only help with analysis of the store's e-commerce data. Try, for example: "
        '"What was revenue by product category last month?"'
    ),
    NON_ENGLISH: "I can only work in English. Please rephrase your question in English.",
}

_AUDIT: Final[dict[str, str]] = {
    INVALID_INPUT: AUDIT_REFUSED,
    INPUT_TOO_LONG: AUDIT_REFUSED,
    INPUT_GUARD_ERROR: AUDIT_REFUSED,
    INJECTION: AUDIT_INJECTION,
    PROMPT_EXFILTRATION: AUDIT_INJECTION,
    ENCODED_PAYLOAD: AUDIT_INJECTION,
    PII_REQUEST: AUDIT_PII_BLOCK,
    OFF_TOPIC: AUDIT_REFUSED,
    NON_ENGLISH: AUDIT_REFUSED,
}

PII_NOTICE: Final = (
    "Note: I removed personal data from your message before processing it. "
    "There is no need to share it."
)


def refusal_for(rule: str) -> str:
    """Fixed template for a rule code (generic error text for an unknown code)."""
    return REFUSALS.get(rule, REFUSALS[INPUT_GUARD_ERROR])


def audit_event_for(rule: str) -> str:
    """Audit event name for a rule code (rule ID only; never the message text)."""
    return _AUDIT.get(rule, AUDIT_REFUSED)


@dataclass(frozen=True)
class InputDecision:
    """Result of :func:`check_input`. ``scrubbed`` is the only form of the message that may
    be stored, logged or sent onward (None when scrubbing itself failed)."""

    allowed: bool
    scrubbed: str | None
    refusal: str | None = None
    rule: str | None = None
    audit_event: str | None = None
    pii_types: frozenset[str] = field(default_factory=frozenset)
    pii_notice: str | None = None

    @property
    def redaction_count(self) -> int:
        return len(self.pii_types)


# ---------------------------------------------------------------------------
# normalisation

_HOMOGLYPHS = str.maketrans(
    {
        # Cyrillic
        "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o",
        "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i",
        "ј": "j", "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ɡ": "g",
        # Greek
        "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o",
        "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ω": "w",
    }
)  # fmt: skip
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_SPACED_RUN = re.compile(r"(?<!\w)(?:\w[\s.\-_*/|]{1,3}){4,}\w(?!\w)")
_SQUASHED_NEEDLES: Final = (
    "ignoreyour", "ignoreall", "ignoreprevious", "ignorethe", "disregard", "forgetyour",
    "systemprompt", "jailbreak", "developermode", "yourinstructions", "yourprompt",
    "bypass", "override",
)  # fmt: skip


# Look-alikes outside Cyrillic/Greek, applied before casefolding (Cherokee capitals fold to
# a separate lowercase block).
_CONFUSABLES = str.maketrans(
    {
        # Armenian
        "օ": "o", "Օ": "o", "ս": "u", "Ս": "u", "ց": "g", "հ": "h", "ո": "n", "զ": "q",
        "ա": "w", "ե": "e", "ւ": "l", "Լ": "l", "ռ": "n", "ք": "p",
        # Cherokee
        "Ꭰ": "d", "Ꭱ": "r", "Ꭵ": "i", "Ꭺ": "a", "Ꭻ": "j", "Ꭼ": "e", "Ᏼ": "b", "Ꮯ": "c",
        "Ꮐ": "g", "Ꮋ": "h", "Ꮶ": "k", "Ꮮ": "l", "Ꮇ": "m", "Ꮲ": "p", "Ꮢ": "r", "Ꮪ": "s",
        "Ꮤ": "t", "Ꮩ": "v", "Ꮃ": "w", "Ꮓ": "z", "Ꮑ": "n", "Ꮻ": "o", "Ꮜ": "u", "Ꮍ": "y",
        # Latin letters with no decomposition
        "ı": "i", "ɨ": "i", "Ɨ": "i", "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "đ": "d",
        "Đ": "d",
    }
)  # fmt: skip
# Invisible characters that are not format (Cf) characters: braille blank, Hangul fillers.
_FILLERS: Final = "\u2800\u3164\u115f\u1160\uffa0"
_AS_SPACE = str.maketrans(dict.fromkeys(_FILLERS, " "))
_DROP_FILLERS = str.maketrans(dict.fromkeys(_FILLERS))
_HTML_TAG = re.compile(r"<[^<>\n]{0,64}>")
_MD_LINK = re.compile(r"\[([^\[\]\n]{0,200})\]\([^()\n]{0,200}\)")  # keep the text only
_MD_JOINERS = re.compile(r"[*_~`>|\[\]()<]+|(?<=\w)-(?=\w)")
_SPLIT_WORD = re.compile(r"(?<=\w)[\n\u00b7](?=\w)")  # line break or middle dot in a word
# Joined copy only: wider separators for letter runs ("i,g,n,o,r,e", "i+g+n", "i'g'n"), and
# commas, "+" and backslashes between words read as spaces.
_JOIN_RUN = re.compile(r"(?<!\w)(?:\w[\s.\-_*/|,+'\\]{1,3}){4,}\w(?!\w)")
_WORD_SEPARATORS = re.compile(r"[,+\\]+")


def _base(text: str) -> str:
    """NFKC, format characters (zero-width, bidi) removed, casefolded. Keeps diacritics and
    the script of each letter: the non-English check needs both."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return text.casefold()


def _small_capital(c: str) -> str:
    name = unicodedata.name(c, "")
    if name.startswith("LATIN LETTER SMALL CAPITAL "):
        letter = name.rsplit(" ", 1)[-1]
        if len(letter) == 1:
            return letter.lower()
    return c


def _fold(text: str) -> str:
    """Scan form: format characters removed, invisible fillers as spaces, combining marks
    stripped (NFKD, drop Mn, NFKC), small capitals and look-alike letters folded to Latin,
    casefolded."""
    text = unicodedata.normalize("NFKD", text.translate(_AS_SPACE))
    text = "".join(_small_capital(c) for c in text if unicodedata.category(c) not in ("Mn", "Cf"))
    text = unicodedata.normalize("NFKC", text).translate(_CONFUSABLES)
    return text.casefold().translate(_HOMOGLYPHS)


def _per_line(text: str) -> str:
    # Whitespace collapsed within each line; newlines kept so line-start rules still fire.
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


def _scan_copies(text: str) -> tuple[str, ...]:
    """Scan copies: plain, markdown/HTML/joiner symbols as spaces, and dotted or
    letter-spaced words and words split across lines rejoined; each also with leetspeak
    folded. Text with invisible fillers is also scanned with the fillers removed, so a
    filler inside a word ("ig\u2800nore") is caught as well as one used as a space."""
    variants = [text]
    if any(c in _FILLERS for c in text):
        variants.append(text.translate(_DROP_FILLERS))
    out: list[str] = []
    for variant in variants:
        lines = _per_line(_fold(variant))
        md = _HTML_TAG.sub(" ", _MD_LINK.sub(r" \1 ", lines))
        md = _per_line(_MD_JOINERS.sub(" ", md))
        joined = _JOIN_RUN.sub(_join_run, _SPLIT_WORD.sub("", md))
        joined = _per_line(_WORD_SEPARATORS.sub(" ", joined))
        for c in (lines, md, joined):
            for v in (c, c.translate(_LEET)):
                if v not in out:
                    out.append(v)
    return tuple(out)


def _join_run(m: re.Match[str]) -> str:
    return re.sub(r"[\W_]+", "", m.group(0))


def _squashed(base: str) -> str:
    """Letter-spaced runs ("i g n o r e  y o u r  r u l e s") joined into one word."""
    runs = [_join_run(m) for m in _SPACED_RUN.finditer(base)]
    return " ".join(runs)


# ---------------------------------------------------------------------------
# rules (matched on casefolded, normalised text)

_QUAL = (
    r"(?:all|any|every|your|the|my|our|its|previous|prior|above|earlier|preceding|system|"
    r"developer|safety|security|pii|privacy|data|internal|original|initial|hidden|existing|"
    r"default|current|these|those|of|such|personal|content|scope)"
)
_TARGET = (
    r"(?:instructions?|prompts?|rules?|directives?|guidelines?|guardrails?|polic(?:y|ies)|"
    r"restrictions?|safeguards?|programming|limitations?|protections?)"
)
_STRONG_INJECTION = tuple(
    re.compile(p)
    for p in (
        rf"\b(?:ignore|disregard|forget|override|overrule|bypass|circumvent)\s+"
        rf"(?:{_QUAL}\s+){{0,4}}{_TARGET}\b",
        r"\b(?:ignore|disregard|forget)\s+(?:everything|all|anything)\s+(?:above|before this|"
        r"you(?:'ve| have| were)? (?:been )?told|i said before)",
        r"\b(?:disable|deactivate|turn\s+off|switch\s+off|remove|lift)\s+(?:\w+\s+){0,2}"
        r"(?:(?:pii|privacy|safety|data\s+protection)\s+(?:rules?|filters?|protections?|"
        r"checks?|mode|settings)|guard\w*|safety|redaction|masking|content\s+filters?)\b",
        r"\b(?:bypass|circumvent|get\s+around|evade|work\s+around)\s+(?:\w+\s+){0,3}"
        r"(?:filter|guard|safety|polic|rule|restriction|redaction|masking|pii|scope|check)",
        r"\byou\s+are\s+now\s+(?:a|an|in|my|the|no\s+longer|free|unrestricted|dan)\b",
        r"\bfrom\s+now\s+on,?\s+you\s+(?:are|will\s+be|have\s+no|no\s+longer|must\s+ignore)\b",
        r"\bpretend\s+(?:to\s+be|you\s+are|you're|that\s+you)\b",
        r"\b(?:act|behave)\s+as\s+(?:if\s+you\s+(?:have|had|were)|an?\s+(?:unrestricted|"
        r"unfiltered|jailbroken|uncensored|different|new)|dan)\b",
        r"\brole-?\s?play\b",
        r"\b(?:developer|god|dan|jailbreak|admin|sudo|debug|maintenance|unrestricted|"
        r"unfiltered|root)\s+mode\b",
        r"\bjailbr[eo]a?k",
        r"\bdo\s+anything\s+now\b",
        r"\bwithout\s+(?:any\s+)?(?:guardrails|safety|censorship|redaction|masking)\b",
        r"<\s*/?\s*(?:system|assistant|developer|instructions?|im_start|im_end)\b[^>]*>",
        r"\[/?\s*(?:inst|system|sys)\s*\]",
        r"<<\s*/?\s*sys\s*>>",
        r"(?:^|\n)\s*#{0,6}\s*(?:(?:[-*+]|\d{1,3}[.)])\s*)?(?:system|developer|assistant)\s*"
        r"(?:message|prompt)?\s*:",
        r"\b(?:new|updated|revised|real|actual|override)\s+(?:system\s+)?(?:instructions?|"
        r"rules|prompt|directives?)\s*:",
    )
)
# Weak signals: each alone is often legitimate; two together (or one plus a personal-data
# request) are treated as an attack.
_WEAK_INJECTION = tuple(
    re.compile(p)
    for p in (
        r"\bi\s*(?:am|'m)\s+(?:the|an|your|a)\s+(?:system\s+)?(?:admin|administrator|"
        r"developer|creator|sysadmin|root|superuser|security\s+(?:team|officer))\b",
        r"\b(?:the\s+|your\s+|our\s+)?(?:pii\s+|privacy\s+|data\s+)?(?:polic(?:y|ies)|rules|"
        r"restrictions)\s+(?:(?:has|have)\s+)?(?:changed|been\s+(?:lifted|removed|updated|"
        r"changed|disabled|waived|relaxed))\b",
        r"\b(?:i\s+(?:am|'m)\s+)?(?:authori[sz]ed|allowed|permitted|cleared)\s+to\s+"
        r"(?:see|view|access|get)\b",
        r"\bfor\s+(?:the|an|our)\s+(?:audit|investigation|compliance\s+review)\b",
    )
)
_EXFILTRATION = tuple(
    re.compile(p)
    for p in (
        r"\b(?:system|developer|hidden|internal|secret)[\s-]+"
        r"(?:prompt|message|instructions?)\b",
        r"\bpre-?\s?prompt\b",
        r"\bsafety\s+core\b",
        r"\b(?:your|the)\s+(?:hidden|initial|original|internal|secret|full|exact|verbatim|"
        r"complete|underlying|entire|whole)\s+(?:prompt|instructions|directives|"
        r"configuration|rules|guidelines)\b",
        r"\b(?:show|print|reveal|repeat|output|display|dump|give|tell|share|leak|copy|paste|"
        r"list|recite|echo|spell\s+out|write\s+out|summari[sz]e|what\s+(?:are|were|is))\s+"
        r"(?:me\s+|us\s+)?(?:all\s+|each\s+|every\s+)?(?:of\s+)?your\s+(?:\w+\s+)?"
        r"(?:prompt|instructions|directives|configuration|guidelines)\b",
        r"\bwhat\s+(?:were|are|have)\s+you\s+(?:been\s+)?(?:told|instructed|programmed|"
        r"prompted)\b",
        r"\bhow\s+(?:were|are|have)\s+you\s+(?:been\s+)?(?:instructed|programmed|prompted)\b",
        r"\b(?:repeat|print|output|recite|echo|show)\s+(?:\w+\s+){0,3}(?:text|words|"
        r"everything|message|content|lines?)\s+(?:above|before\s+this|so\s+far)\b",
        r"\b(?:beginning|start)\s+of\s+(?:this|the|your)\s+(?:conversation|context|prompt)\b",
    )
)
# Lookbehinds keep "product names", "brand names" etc. out of the personal-name field.
_NOT_THING = (
    r"(?<!product )(?<!brand )(?<!category )(?<!department )(?<!segment )(?<!source )"
    r"(?<!column )(?<!field )(?<!file )(?<!center )(?<!centre )(?<!table )"
)
_PII_FIELD = (
    r"(?:e-?mails?(?:\s+address(?:es)?)?|phone(?:\s+numbers?)?|telephone(?:\s+numbers?)?|"
    r"mobile\s+numbers?|(?:home|street|postal|mailing|physical|delivery|shipping)\s+"
    r"address(?:es)?|address(?:es)?|full\s+names?|first\s+names?|last\s+names?|surnames?|"
    rf"{_NOT_THING}names?|contact\s+(?:details|info(?:rmation)?)|personal\s+(?:data|"
    r"details|information)|ip\s+address(?:es)?|date\s+of\s+birth|birthdays?)"
)
_PERSON_NOUN = r"(?:customers?|users?|buyers?|clients?|shoppers?|people|persons?|members?)"
# One optional filler word, but not a preposition ("users from Email" is a traffic source).
_FILLER = r"(?:(?!(?:from|via|through|by|with|in|on|who|that|using|per|and|or|to|at)\b)\w+\s+)?"
_PII_REQUEST = tuple(
    re.compile(p)
    for p in (
        rf"\b{_PERSON_NOUN}(?:'s|'|’s)?\s+{_FILLER}{_PII_FIELD}\b",
        rf"\b{_PII_FIELD}\s+(?:of|for|from)\s+(?:\w+\s+){{0,4}}{_PERSON_NOUN}\b",
        r"\b(?:unmasked|unredacted|de-?anonymi[sz]ed|raw)\s+(?:\w+\s+)?(?:pii|personal|"
        r"e-?mails?|names?|phones?|address(?:es)?|customer\s+data)\b",
        r"\b(?:de-?anonymi[sz]e|re-?identify|unmask)\b",
    )
)
_OFF_TOPIC = tuple(
    re.compile(p)
    for p in (
        r"\b(?:write|compose|create|generate|make\s+up|give\s+me|tell\s+me|sing)\s+"
        r"(?:\w+\s+){0,3}(?:poem|poetry|song|haiku|limerick|sonnet|lyrics|rap|joke|riddle|"
        r"fairy\s*tale|bedtime\s+story|short\s+story|novel|screenplay|cover\s+letter)s?\b",
        r"\b(?:poem|song|haiku|limerick|sonnet|joke)\s+about\b",
        r"\b(?:what'?s|what\s+is|how'?s|how\s+is|will\s+it\s+be)\s+the\s+weather"
        r"(?:\s+(?:in|for|like|today|tomorrow|now|outside)\b|\s*[?.!]|\s*$)",
        r"\bweather\s+(?:in|for|today|tomorrow|this\s+week|forecast|like)\b",
        r"\btranslate\b(?:\s+\S+){0,8}\s+(?:in)?to\s+(?:english|spanish|french|german|"
        r"italian|portuguese|chinese|japanese|korean|russian|hebrew|arabic|hindi|dutch|"
        r"polish|turkish|ukrainian)\b",
        r"\b(?:write|generate|create|give\s+me|build)\s+(?:\w+\s+){0,3}(?:python|javascript|"
        r"typescript|java|c\+\+|c#|rust|golang|bash|shell|html|css|php|ruby)\s+(?:code|"
        r"script|function|program|class|app)\b",
        r"\b(?:recipe|recipes)\s+for\b",
        r"\bwhat\s+is\s+the\s+capital\s+of\b",
        r"\bwho\s+(?:won|will\s+win)\s+the\b",
        r"\b(?:stock\s+price|share\s+price)\s+of\b",
        r"\bmeaning\s+of\s+life\b",
    )
)
_ANSWER_IN_LANGUAGE = re.compile(
    r"\b(?:answer|respond|reply|write|speak|talk|explain)\s+(?:\w+\s+){0,3}in\s+(?:spanish|"
    r"french|german|italian|portuguese|chinese|mandarin|japanese|korean|russian|hebrew|"
    r"arabic|hindi|dutch|polish|turkish|ukrainian)\b"
)
_ENCODED = tuple(
    re.compile(p)
    for p in (
        r"(?<![\w/+])(?![0-9a-f]{32}(?![\w/+]))(?=[a-z0-9+/]*[a-z])(?=[a-z0-9+/]*[0-9])[a-z0-9+/]{32,}={0,2}(?![\w/+])",
        r"\b(?:decode|decrypt|deobfuscate|rot13|base64)\b(?:\s+\S+){0,6}\s+(?:and|then)\s+"
        r"(?:follow|run|execute|do|obey|apply)\b",
        r"\b(?:base64|rot13|hex)[\s-]*(?:encoded|decode)\b",
        r"(?:\\u[0-9a-f]{4}){4,}|(?:\\x[0-9a-f]{2}){6,}|(?:%[0-9a-f]{2}){6,}",
    )
)

# Non-English (Latin script) function words and greetings. English-only, heuristic (D-50).
_FOREIGN_WORDS: Final = frozenset(
    """
    el la los las que de del por para con una uno unos cuántos cuantos cuántas cuantas cuál
    cuales cuáles qué cómo como dónde donde ventas pedidos mes año ingresos productos
    usuarios clientes hola gracias buenos buenas días dias muéstrame dame cuánto cuanto
    le les des du une est sont avec pour dans quel quelle quels quelles combien commandes
    mois année chiffre affaires clients bonjour salut merci montre-moi montrez
    der die das und ist sind mit für wie viele welche bestellungen umsatz monat jahr kunden
    hallo danke zeig zeige mir bitte guten
    o os um uma são não quantos quantas vendas pedidos receita mês olá obrigado obrigada
    il gli della delle sono quanti quante ordini fatturato mese anno ciao grazie buongiorno
    """.split()
)
_ENGLISH_WORDS: Final = frozenset(
    """
    the a an of and or to in on at by for with from what which who how many much is are was
    were be been do does did show me give list top last this that month year week revenue
    sales orders order product products customers customer users brand category by per
    average total count number compare trend vs versus between than more most less hi hello
    hey thanks thank you please can could would what's how's help my our your i it
    """.split()
)
_FOREIGN_GREETINGS: Final = frozenset(
    {"hola", "bonjour", "salut", "hallo", "ciao", "olá", "ola", "buongiorno", "gracias",
     "merci", "danke", "grazie", "obrigado", "obrigada", "privet", "shalom", "namaste",
     "konnichiwa", "nihao", "guten", "buenos", "buenas"}
)  # fmt: skip
_WORD = re.compile(r"[^\W\d_]+(?:[-'][^\W\d_]+)*")


def _non_latin_letters(text: str) -> tuple[int, int]:
    latin = other = 0
    for c in text:
        if c.isalpha():
            if unicodedata.name(c, "").startswith("LATIN"):
                latin += 1
            else:
                other += 1
    return latin, other


def _looks_non_english(base: str) -> bool:
    """`base` is NFKC + casefolded, *not* homoglyph-folded (the script check needs it)."""
    latin, other = _non_latin_letters(base)
    if other >= 3 and other >= 0.2 * (latin + other):
        return True
    if "¿" in base or "¡" in base:
        return True
    words = _WORD.findall(base)
    if not words:
        return False
    foreign = sum(1 for w in words if w in _FOREIGN_WORDS or w in _FOREIGN_GREETINGS)
    english = sum(1 for w in words if w in _ENGLISH_WORDS)
    if english == 0 and any(w in _FOREIGN_GREETINGS for w in words):
        return True
    return foreign >= 2 and foreign > 2 * english


def _any(patterns: tuple[re.Pattern[str], ...], texts: tuple[str, ...]) -> bool:
    return any(p.search(t) for p in patterns for t in texts)


def _scan(raw: str) -> str | None:
    """Return the first rule code hit, or None. Order: most specific attack first."""
    base = _base(raw)
    copies = _scan_copies(raw)
    squashed = _squashed(copies[0])
    if squashed and any(n in squashed.replace(" ", "") for n in _SQUASHED_NEEDLES):
        return INJECTION
    if _any(_STRONG_INJECTION, copies):
        return INJECTION
    if _any(_EXFILTRATION, copies):
        return PROMPT_EXFILTRATION
    if _any(_ENCODED, (base,)):
        return ENCODED_PAYLOAD
    weak = sum(1 for p in _WEAK_INJECTION if any(p.search(t) for t in copies))
    if _any(_PII_REQUEST, copies):
        return PII_REQUEST
    if weak >= 2:
        return INJECTION
    if _looks_non_english(base) or _ANSWER_IN_LANGUAGE.search(copies[0]):
        return NON_ENGLISH
    if _any(_OFF_TOPIC, copies):
        return OFF_TOPIC
    return None


def _refuse(
    rule: str, scrubbed: str | None, pii_types: frozenset[str] = frozenset()
) -> InputDecision:
    return InputDecision(
        allowed=False,
        scrubbed=scrubbed,
        refusal=refusal_for(rule),
        rule=rule,
        audit_event=_AUDIT[rule],
        pii_types=pii_types,
        pii_notice=PII_NOTICE if pii_types else None,
    )


def check_input(raw: object, *, detector: PiiDetector | None = None) -> InputDecision:
    """Scrub and scan one user message. Never raises; fails closed.

    The scan runs on the raw text (masking could hide an attack, e.g. NER tagging
    "Ignore Previous Instructions" as a name), but nothing from the raw text leaves this
    function: refusals are fixed templates and ``scrubbed`` is the masked text.
    """
    if not isinstance(raw, str):
        return _refuse(INVALID_INPUT, None)
    if len(raw) > MAX_INPUT_CHARS:
        # Not scrubbed (the scrubber has its own cap); nothing of it is kept.
        return _refuse(INPUT_TOO_LONG, None)
    try:
        masked = (detector or default_detector()).mask(raw)
        if masked.truncated:
            return _refuse(INPUT_GUARD_ERROR, None)
        scrubbed = masked.text
        pii_types = frozenset(masked.types())
        rule = _scan(raw)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything (PiiDetectorError too)
        logger.error("input guard failed closed: %s", type(exc).__name__)
        return _refuse(INPUT_GUARD_ERROR, None)
    if rule is not None:
        return _refuse(rule, scrubbed, pii_types)
    if not scrubbed.strip():
        return _refuse(INVALID_INPUT, scrubbed, pii_types)
    return InputDecision(
        allowed=True,
        scrubbed=scrubbed,
        pii_types=pii_types,
        pii_notice=PII_NOTICE if pii_types else None,
    )


def scan_injection(text: str) -> str | None:
    """Public alias of the rule scan: the first rule code hit by ``text``, or None."""
    return _scan(text)
