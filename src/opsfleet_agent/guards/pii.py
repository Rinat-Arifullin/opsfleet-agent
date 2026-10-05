r"""Typed-PII detector: regex scrubber + Presidio NER + brand allowlist (iteration 8b; HLD §5.4).

``PiiDetector.mask(text)`` runs the deterministic regex scrubber (``pii_regex``, 8a)
first, then Presidio over the result, and masks what it finds with typed placeholders:
``<EMAIL>``, ``<PHONE>``, ``<CARD>``, ``<ID>`` (regex) and ``<PERSON>``, ``<ADDRESS>``
(NER and local pattern recognizers). ``findings`` carries entity types and spans only,
never the values. ``scrub_output(text)`` is the output-guard entry point (iteration 12).

Everything runs locally
-----------------------
Presidio is built over the already-installed spaCy model ``en_core_web_sm``. The model is
loaded with ``spacy.load`` and injected into ``SpacyNlpEngine`` directly; Presidio's own
``load()`` is never called, because it would ``spacy.cli.download`` a missing model over
the network. The recognizer registry holds only local recognizers (spaCy NER and the
regex-based recognizers below), no remote ones. A missing model is a startup error
(``ensure_model_available()`` raises ``PiiModelMissing``), never a regex-only fallback.
The model is loaded once per process (lazy, thread-safe module singleton).

Recognizers and labels
----------------------
* **spaCy NER**: the spaCy label ``PERSON`` (and ``PER``) maps to ``PERSON`` and is masked.
  ``GPE``, ``LOC`` and ``FAC`` map to ``LOCATION``, which is *not* requested: a country or
  city on its own is an analytics dimension ("revenue in France", "orders from Tokyo"),
  not PII. ``ORG``, ``NORP``, ``DATE``, ``CARDINAL``, ``MONEY`` and the rest are ignored.
* **Street address** (``ADDRESS``): house number + 1-4 words + a street suffix, with an
  optional unit (Apt/Unit/Suite/#), city, state and ZIP or UK postcode; a PO box; and an
  ``address:`` cue followed by a line containing a digit. Unambiguous suffixes
  (Street/St/Avenue/Road/Lane/Terrace/...) match in any case ("742 evergreen terrace",
  "742 EVERGREEN TERR."); suffixes that are also ordinary words (Way/Place/Row/Walk/...)
  must be capitalised or upper case. Street words never include function words or
  analytics nouns, so "742 orders on the road" is not an address. The house number is
  what separates a street address from a place name: "Springfield" or "Germany" is kept.
* **Name cues** (``PERSON``), because the small spaCy model misses names that a person
  would read as obvious: a title (``Mr``/``Ms``/``Dr``... + capitalised words); a
  self-introduction ("my name is ...", any case, also across a line break); a ``Name:``
  label at the start of a line, with the value on the same or the next lines ("Name:
  Gertrude\nPlumbottom"); a greeting or sign-off + capitalised name ("Hey Fenwick"); a
  JSON / dict / YAML name key (``"name": ...``, ``'customer'=...``, ``full_name: ...``).
* **Name columns**: in a pipe-, tab- or comma-separated table whose header has a name
  column (``name``, ``first_name``, ``last_name``, ``customer``, ``contact``...), every
  cell of that column is masked, in every row (there is no row cap; the input size
  bound below is the only limit).
* spaCy runs over three same-length copies of the text, so spans line up: the original;
  a flattened copy with line breaks, pipes, backticks, quotes and brackets replaced by
  spaces (a name split across rows or wrapped in code/JSON punctuation is seen as one
  name); and a recased copy where ALL-CAPS runs and all-lower-case segments are
  title-cased ("ZORBINA QUANDLEWORTH", "the buyer was bartholomew fizzlewick").
* A name spaCy finds only in a recased, ALL-CAPS or all-lower-case piece is kept only
  when it has two or more words and either follows a person cue in the 40 characters
  before it ("customer", "buyer", "named", "dear", "name:"...) or has a word outside the
  English lexicon (the spaCy lemmatizer tables, with simple inflection stripping). This
  keeps "RETURNED SHARE" or "small dip" unmasked.

Score threshold
---------------
``MIN_SCORE = 0.5``. spaCy NER results carry Presidio's fixed ``ner_strength`` of 0.85.
The threshold only drops results that Presidio itself scores as weak; the
recall/precision trade-off is set by which labels and patterns are used, not by tuning
this number. Cue recognizers run outside Presidio and are always kept.

Span hygiene
------------
A spaCy ``PERSON`` span is widened to whole tokens, absorbs a following surname glued to
digits ("Mortimer Plinkett2"), loses leading role/greeting words ("Customer", "Hi"),
connectors and trailing bare numbers ("Marlowe Finch 120k" -> "Marlowe Finch"), and is
split into pieces at inner connectors and at commas, semicolons, pipes and line breaks
("Ab Cd,1" -> "Ab Cd"). A token containing a letter is never dropped from the middle or
end of a name. A piece that will be masked is then widened over up to two adjacent
capitalised words on each side, so a name spaCy tagged only in part is masked whole.
Spans never cut into a placeholder the regex layer already wrote. Overlapping spans
merge; ``ADDRESS`` wins over ``PERSON``.

Brand allowlist
---------------
``build_allowlist(brands, categories, departments)`` builds an immutable allowlist from
injected iterables (production: the distinct values of the catalogue). Matching is
case-insensitive, Unicode-folded the same way ``pii_regex`` folds (format characters
dropped, full-width forms and dashes folded), tolerant of punctuation between words
("Ray-Ban" = "Ray Ban") and of a possessive "'s". ``SCHEMA_TERMS`` (order statuses
such as "Shipped", traffic sources, event types, month and weekday names) are always
part of it. A spaCy ``PERSON`` piece is exempt only when **one** occurrence of **one**
allowlisted term contains the whole piece ("Calvin Klein", "Levi" inside "Levi's");
two adjacent terms do not combine into an exemption ("Search Organic" is not covered by
"Search" + "Organic"). Cue hits are never allowlisted: "Name: Marlowe Finch" is masked
even when Marlowe Finch is a brand. The only exception is a title cue whose whole span
is itself one term ("Dr. Pimbleton"). The allowlist exempts ``PERSON`` only, never an
address, e-mail, phone, card or id.

Product names
-------------
A spaCy ``PERSON`` span, or a JSON name-key value, that contains an apparel/product noun
("Classic Denim Jacket", "Wrap Dress") is treated as a product name and dropped. This
never applies to the other cue recognizers.

Precision trade-off
-------------------
Recall wins (fail safe). Known over-masking: a person-looking word spaCy tags as
``PERSON`` that is not in the allowlist (a product named after someone, a brand missing
from the catalogue); capitalised words after a greeting ("Hi Team"); the word after a
title ("Dr Rainfall" style product names); a cue-labelled brand ("Name: <brand>").
Known gaps: a single lower-case or ALL-CAPS name word, a lower-case name made only of
dictionary words with no person cue ("john smith"), a name spaCy does not tag even after
recasing, and a name in a table without a header. A brand equal to a person's full name
exempts that person wherever the brand is allowlisted, and a spaCy name that contains a
product noun ("Jacket") is dropped.

Bounds and failure
------------------
Input is capped at ``pii_regex.MAX_SCRUB_CHARS`` (the regex layer truncates first, and
NER runs on that result). When the input was truncated, the partial last line is dropped
before NER, so a cut cannot leave half a name ("Zorbina Quand") that spaCy no longer
recognises. Input that is not ``str`` raises ``PiiDetectorError`` from ``mask``,
``detect`` and ``scrub_output``. Every exception inside the detector is raised as
``PiiDetectorError`` (``PiiModelMissing`` for a missing model); the caller never gets
unscrubbed text back (fail closed).
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opsfleet_agent.guards import pii_regex
from opsfleet_agent.guards.pii_regex import _FOLD, CARD, EMAIL, ID, MAX_SCRUB_CHARS, PHONE

if TYPE_CHECKING:
    from presidio_analyzer import AnalyzerEngine

logger = logging.getLogger(__name__)

PERSON = "PERSON"
ADDRESS = "ADDRESS"
ENTITY_TYPES: tuple[str, ...] = (EMAIL, PHONE, CARD, ID, PERSON, ADDRESS)
TOKENS: dict[str, str] = {t: f"<{t}>" for t in ENTITY_TYPES}

SPACY_MODEL = "en_core_web_sm"
SPACY_MODEL_WHEEL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
)
MIN_SCORE = 0.5
NER_SCORE = 0.85
#: spaCy labels mapped into Presidio entities. Only PERSON is requested for masking.
SPACY_LABEL_MAP: dict[str, str] = {
    "PERSON": PERSON,
    "PER": PERSON,
    "GPE": "LOCATION",
    "LOC": "LOCATION",
    "FAC": "LOCATION",
}
MAX_ALLOWLIST_TERMS = 50_000
#: D-153: derived (scope-brand) detectors kept per base detector.
MAX_DERIVED_DETECTORS = 32
MAX_TERM_CHARS = 200

__all__ = [
    "CATALOGUE_CATEGORIES",
    "CATALOGUE_DEPARTMENTS",
    "REPORT_TERMS",
    "ADDRESS",
    "ENTITY_TYPES",
    "MIN_SCORE",
    "PERSON",
    "SPACY_MODEL",
    "TOKENS",
    "BrandAllowlist",
    "Finding",
    "MaskResult",
    "PiiDetector",
    "PiiDetectorError",
    "PiiModelMissing",
    "build_allowlist",
    "default_detector",
    "ensure_model_available",
    "extend_allowlist",
    "scrub_output",
    "set_default_detector",
]


class PiiDetectorError(RuntimeError):
    """The detector failed. The text must be treated as unsafe (fail closed)."""


class PiiModelMissing(PiiDetectorError):
    """The spaCy model is not installed. A startup error, never a regex-only fallback."""


@dataclass(frozen=True)
class Finding:
    """One masked entity: its type and the span of its placeholder in the masked text."""

    type: str
    start: int
    end: int


@dataclass(frozen=True)
class MaskResult:
    """Masked text plus typed findings. Never holds a raw value."""

    text: str
    findings: tuple[Finding, ...] = ()
    truncated: bool = False

    @property
    def redacted(self) -> bool:
        return bool(self.findings)

    def types(self) -> set[str]:
        return {f.type for f in self.findings}


# --- allowlist ----------------------------------------------------------------------

_WORD = re.compile(r"\w+")
_POSSESSIVE = re.compile(r"['\u2019]s\b", re.IGNORECASE)


def _words(s: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(_POSSESSIVE.sub("", s.translate(_FOLD)))]


@dataclass(frozen=True)
class BrandAllowlist:
    """Immutable allowlist of catalogue terms. Build it with ``build_allowlist``."""

    terms: tuple[str, ...]
    _patterns: tuple[re.Pattern[str], ...]
    _index: dict[str, frozenset[int]]
    _keys: frozenset[tuple[str, ...]] = frozenset()

    def __len__(self) -> int:
        return len(self.terms)

    def covers(self, text: str, start: int, end: int) -> bool:
        """True when the word characters of ``text[start:end]`` lie inside ONE whole-word
        occurrence of ONE allowlisted term in ``text``. Terms are never combined: with
        "Marlowe" and "Finch" allowlisted separately, "Marlowe Finch" is not covered."""
        while start < end and not (text[start].isalnum() or text[start] == "_"):
            start += 1
        while end > start and not (text[end - 1].isalnum() or text[end - 1] == "_"):
            end -= 1
        span_words = _words(text[start:end])
        if not span_words:
            return False
        candidates: set[int] | frozenset[int] = self._index.get(span_words[0], frozenset())
        for w in span_words[1:]:
            candidates = candidates & self._index.get(w, frozenset())
        for idx in candidates:
            reach = len(self.terms[idx]) + 8
            lo, hi = max(0, start - reach), min(len(text), end + reach)
            for m in self._patterns[idx].finditer(text, lo, hi):
                if m.start() <= start and m.end() >= end:
                    return True
        return False

    def is_term(self, span: str) -> bool:
        """True when ``span`` as a whole equals one allowlisted term (folded, any case)."""
        return tuple(_words(span)) in self._keys


def _term_pattern(words: Sequence[str]) -> re.Pattern[str]:
    body = r"\W{0,3}".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?:['\u2019]s)?(?!\w)", re.IGNORECASE)


#: Always-allowed analytics vocabulary of the dataset (order statuses, traffic sources,
#: event types) plus month and weekday names; spaCy tags some of them as PERSON
#: ("Shipped", "April"). Part of every allowlist, including the empty one.
SCHEMA_TERMS: tuple[str, ...] = (
    "Complete", "Shipped", "Processing", "Cancelled", "Returned",
    "Search", "Organic", "Facebook", "Email", "Display", "Adwords", "YouTube",
    "home", "department", "product", "cart", "purchase", "cancel",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)  # fmt: skip


#: D-171: section headings of an analytical answer. spaCy tags some of them as PERSON
#: ("Takeaways:" in a live run), and the masked heading then leaks into later turns through
#: the history. Part of every allowlist, like ``SCHEMA_TERMS``.
REPORT_TERMS: tuple[str, ...] = (
    "Takeaway", "Takeaways", "Key Takeaway", "Key Takeaways", "Summary", "Executive Summary",
    "Insight", "Insights", "Key Insights", "Note", "Notes", "Highlight", "Highlights",
    "Key Highlights", "Observation", "Observations", "Finding", "Findings", "Key Findings",
    "Recommendation", "Recommendations", "Conclusion", "Conclusions", "Overview", "Trend",
    "Trends", "Caveat", "Caveats", "Next Steps", "Breakdown", "Bottom Line",
)  # fmt: skip


#: D-167: the product categories and departments of thelook_ecommerce (a fixed public
#: catalogue). spaCy tags some of them as PERSON ("Swim"), so every CLI allowlist has them.
CATALOGUE_CATEGORIES: tuple[str, ...] = (
    "Accessories", "Active", "Blazers & Jackets", "Clothing Sets", "Dresses",
    "Fashion Hoodies & Sweatshirts", "Intimates", "Jeans", "Jumpsuits & Rompers", "Leggings",
    "Maternity", "Outerwear & Coats", "Pants", "Pants & Capris", "Plus", "Shorts", "Skirts",
    "Sleep & Lounge", "Socks", "Socks & Hosiery", "Suits", "Suits & Sport Coats", "Sweaters",
    "Swim", "Tops & Tees", "Underwear",
)  # fmt: skip
CATALOGUE_DEPARTMENTS: tuple[str, ...] = ("Men", "Women")


def build_allowlist(
    brands: Iterable[str] = (),
    categories: Iterable[str] = (),
    departments: Iterable[str] = (),
) -> BrandAllowlist:
    """Build the allowlist from catalogue values. Pure: no I/O, the inputs are injected.

    ``SCHEMA_TERMS`` and ``REPORT_TERMS`` are always included. Values are folded like
    ``pii_regex`` folds and compared case-insensitively. Empty or word-less values are
    skipped. More than ``MAX_ALLOWLIST_TERMS`` distinct terms, or a term longer than
    ``MAX_TERM_CHARS``, raises ``ValueError`` (a catalogue that large is a bug, not
    something to truncate silently).
    """
    seen: dict[tuple[str, ...], str] = {}
    for source in (SCHEMA_TERMS, REPORT_TERMS, brands, categories, departments):
        for raw in source:
            if not isinstance(raw, str):
                continue
            term = " ".join(raw.translate(_FOLD).split())
            if len(term) > MAX_TERM_CHARS:
                raise ValueError(f"allowlist term longer than {MAX_TERM_CHARS} characters")
            words = tuple(_words(term))
            if not words or (len(words) == 1 and len(words[0]) < 2):
                continue
            seen.setdefault(words, term)
            if len(seen) > MAX_ALLOWLIST_TERMS:
                raise ValueError(f"allowlist larger than {MAX_ALLOWLIST_TERMS} terms")
    keys = list(seen)
    index: dict[str, set[int]] = defaultdict(set)
    for i, words in enumerate(keys):
        for w in words:
            index[w].add(i)
    return BrandAllowlist(
        terms=tuple(seen[k] for k in keys),
        _patterns=tuple(_term_pattern(k) for k in keys),
        _index={w: frozenset(ix) for w, ix in index.items()},
        _keys=frozenset(keys),
    )


EMPTY_ALLOWLIST = build_allowlist()


def extend_allowlist(base: BrandAllowlist, brands: Iterable[str]) -> BrandAllowlist:
    """D-153: ``base`` plus ``brands`` (same folding and limits as ``build_allowlist``)."""
    return build_allowlist(brands=(*base.terms, *brands))


# --- model singleton ----------------------------------------------------------------

_MODEL_LOCK = threading.Lock()
_nlp: Any = None


def _load_spacy_model() -> Any:
    """Load the installed model by package name. ``spacy.load`` never downloads."""
    import spacy

    return spacy.load(SPACY_MODEL)


def _get_nlp() -> Any:
    global _nlp
    if _nlp is None:
        with _MODEL_LOCK:
            if _nlp is None:
                try:
                    _nlp = _load_spacy_model()
                except (OSError, ImportError) as exc:
                    raise PiiModelMissing(
                        f"spaCy model {SPACY_MODEL!r} is not installed; install it with "
                        f"`uv sync` or `pip install {SPACY_MODEL_WHEEL}`"
                    ) from exc
                except Exception as exc:
                    raise PiiDetectorError(
                        f"loading spaCy model failed: {type(exc).__name__}"
                    ) from exc
    return _nlp


def ensure_model_available() -> None:
    """Startup check: load the model once or raise ``PiiModelMissing``."""
    _get_nlp()


# --- local recognizers --------------------------------------------------------------

_CAP = r"[A-Z][a-zA-Z'\u2019\-]{1,30}"
# Street words: any case, but never a function word or an analytics noun, so
# "742 orders on the road" style text cannot form an address.
_ADDR_WORD = (
    r"(?!(?i:the|a|an|of|and|or|in|at|on|for|to|from|by|per|via|with|than|vs|top|new"
    r"|orders?|items?|units?|users?|products?|sales|customers?|days?|weeks?|months?"
    r"|years?|times?|rows?|pairs?|brands?|categories|returns?|sessions?|events?)\b)"
    r"[A-Za-z][A-Za-z'\u2019\-]{1,30}"
)
# Unambiguous suffixes match in any case ("742 evergreen terrace", "742 EVERGREEN TERR.").
_STRICT_SUFFIX = (
    r"(?i:street|st|avenue|ave|road|rd|lane|ln|boulevard|blvd|terrace|terr|parkway|pkwy"
    r"|highway|hwy|crescent|cres|mews)"
)
# Suffixes that are also ordinary words ("way", "place", "row", "walk") must be
# Capitalised or upper case.
_LOOSE_SUFFIX = (
    r"(?:Drive|Dr|Way|Court|Ct|Place|Pl|Terrace|Ter|Close|Square|Sq|Row|Alley|Circle|Cir"
    r"|Trail|Gardens|Grove|Walk|Hill|Path|DRIVE|DR|WAY|COURT|CT|PLACE|PL|TER|CLOSE|SQUARE"
    r"|SQ|ROW|ALLEY|CIRCLE|CIR|TRAIL|GARDENS|GROVE|WALK|HILL|PATH)"
)
_UK_POSTCODE = r"(?i:[A-Z]{1,2}\d[A-Z\d]?[ \t]*\d[A-Z]{2})"
_STREET_ADDRESS = re.compile(
    rf"(?<![\w.,])\d{{1,6}}[A-Za-z]?,?[ \t]+(?:{_ADDR_WORD}[ \t]+){{0,3}}{_ADDR_WORD}[ \t]+"
    rf"(?:{_STRICT_SUFFIX}|{_LOOSE_SUFFIX})\b\.?"
    r"(?:,?[ \t]*(?i:apt|apartment|unit|suite|ste|flat|floor|#)\.?[ \t]*#?\w{1,6})?"
    rf"(?:,[ \t]*{_CAP}\b(?:[ \t]+{_CAP}\b){{0,2}})?"
    rf"(?:,?[ \t]*[A-Z]{{2}}\b)?"
    rf"(?:,?[ \t]*(?:\d{{5}}(?:-\d{{4}})?|{_UK_POSTCODE})\b)?"
)
_PO_BOX = re.compile(r"\b(?i:p\.?\s?o\.?\s{0,2}box)\s{1,3}\d{1,8}\b")
_ADDRESS_CUE = re.compile(
    r"(?im:^[ \t]*(?:home |street |postal |mailing |shipping |billing )?"
    r"(?:address|addr)[ \t]*[:=][ \t]*\n?[ \t]*([^\n]{0,120}\d[^\n]{0,120}))"
)
_NAME_WORD_ANY = (
    r"(?!(?:and|or|but|the|a|an|i|from|in|at|to|with|my|is|for|please)\b)[^\W\d_]{2,30}"
)
_TITLE_NAME = re.compile(
    rf"\b(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Sir|Dame|Madam)\.?[ \t]+{_CAP}(?:[ \t]+{_CAP}){{0,2}}"
)
_SELF_INTRO = re.compile(
    rf"(?i:\bmy\s{{1,3}}name\s{{1,3}}is|\bi\s{{1,3}}am\s{{1,3}}called|\bcall\s{{1,3}}me)"
    rf"[ \t:,]*\n?[ \t]*({_NAME_WORD_ANY}(?:[ \t]*\n?[ \t]*{_NAME_WORD_ANY}){{0,2}})"
)
_NAME_LABEL = re.compile(
    r"(?im:^[ \t]*(?:full |first |last |given |family |customer |contact |user )?"
    r"(?:name|surname)[ \t]*[:=][ \t]*\n?[ \t]*)"
    rf"({_NAME_WORD_ANY}(?:[ \t]*\n?[ \t]*{_NAME_WORD_ANY}){{0,2}})"
)
_GREETING = re.compile(
    r"\b(?:Hi|Hello|Hey|Dear|Thanks|Regards|Cheers|Sincerely|Best)[ \t]*,?[ \t]+"
    rf"(?!(?:Team|All|There|Everyone|Folks|Guys|Again|Claude)\b)({_CAP}(?:[ \t]+{_CAP})?)"
)
_NAME_KEYS = (
    r"full[_ -]?name|first[_ -]?name|last[_ -]?name|customer(?:[_ -]?name)?|buyer(?:[_ -]?name)?"
    r"|user[_ -]?name|contact[_ -]?name|name|user|contact"
)
_NAME_VALUE = rf"({_NAME_WORD_ANY}(?:[ \t]+{_NAME_WORD_ANY}){{0,2}})"
# JSON / dict / YAML keys. Quoted key: any of the keys, value quoted or not
# ({"name": "...", 'customer'='...'}). Unquoted key: only after a line start, "{" or ","
# (YAML, JS objects), and not the bare "user"/"contact" keys, so a chat transcript line
# ("User: what were ...") is not read as a name.
_KEY_NAME_QUOTED = re.compile(
    rf"(?i:[\"'`]({_NAME_KEYS})[\"'`][ \t]*[:=][ \t]*[\"'`]?){_NAME_VALUE}"
)
_KEY_NAME_BARE = re.compile(
    rf"(?im:(?:^|[{{,])[ \t\-]*(full[_ -]?name|first[_ -]?name|last[_ -]?name"
    rf"|customer(?:[_ -]?name)?|buyer(?:[_ -]?name)?|user[_ -]?name|contact[_ -]?name|name)"
    rf"[ \t]*[:=][ \t]*[\"'`]?){_NAME_VALUE}"
)
_NAME_HEADERS = frozenset(
    {
        "name", "full name", "first name", "last name", "surname", "given name",
        "family name", "customer", "customer name", "person", "contact", "contact name",
        "user name", "username", "recipient", "employee", "employee name", "buyer",
    }
)  # fmt: skip


_TITLE = "title"


def _cue_results(text: str) -> list[tuple[str, int, int, str]]:
    """Local pattern recognizers: (type, start, end, cue kind). Run outside Presidio so
    that no spaCy result can absorb or replace a cue hit."""
    out: list[tuple[str, int, int, str]] = []
    for pat in (_STREET_ADDRESS, _PO_BOX):
        out += [(ADDRESS, m.start(), m.end(), "address") for m in pat.finditer(text)]
    out += [(ADDRESS, m.start(1), m.end(1), "address") for m in _ADDRESS_CUE.finditer(text)]
    out += [(PERSON, m.start(), m.end(), _TITLE) for m in _TITLE_NAME.finditer(text)]
    for pat in (_SELF_INTRO, _NAME_LABEL, _GREETING):
        out += [(PERSON, m.start(1), m.end(1), "cue") for m in pat.finditer(text)]
    for pat in (_KEY_NAME_QUOTED, _KEY_NAME_BARE):
        for m in pat.finditer(text):
            # a bare "name" key is also the products-table column: skip product names
            kind = "name_key" if m.group(1).casefold() == "name" else "cue"
            out.append((PERSON, m.start(2), m.end(2), kind))
    out += [(PERSON, a, b, "cell") for _, a, b, _s in _table_name_cells(text)]
    return [r for r in out if r[2] > r[1]]


def _split_cells(line: str, sep: str) -> list[tuple[int, int]]:
    """Cell spans (start, end) of one table line, stripped of whitespace."""
    cells, pos = [], 0
    for part in line.split(sep):
        a = pos + len(part) - len(part.lstrip())
        b = pos + len(part.rstrip())
        cells.append((a, max(a, b)))
        pos += len(part) + 1
    return cells


def _table_name_cells(text: str) -> list[tuple[str, int, int, float]]:
    """Mask every cell under a name-like header in pipe/tab/comma tables."""
    out: list[tuple[str, int, int, float]] = []
    lines = text.split("\n")  # no cap: linear, and the input is bounded by MAX_SCRUB_CHARS
    offset, cols, sep = 0, (), ""
    for line in lines:
        if cols and sep in line:
            cells = _split_cells(line, sep)
            for c in cols:
                if c < len(cells):
                    a, b = cells[c]
                    if any(ch.isalpha() for ch in line[a:b]):
                        out.append((PERSON, offset + a, offset + b, 0.6))
        elif cols and line.strip() and sep not in line:
            cols = ()
        if not cols:
            for candidate in ("|", "\t", ","):
                if candidate in line:
                    heads = [
                        " ".join(line[a:b].replace("_", " ").replace("-", " ").split()).casefold()
                        for a, b in _split_cells(line, candidate)
                    ]
                    found = tuple(i for i, h in enumerate(heads) if h in _NAME_HEADERS)
                    if found:
                        cols, sep = found, candidate
                        break
        offset += len(line) + 1
    return out


def _build_analyzer(nlp: Any) -> AnalyzerEngine:
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NerModelConfiguration, SpacyNlpEngine
    from presidio_analyzer.predefined_recognizers import SpacyRecognizer

    config = NerModelConfiguration(
        model_to_presidio_entity_mapping=dict(SPACY_LABEL_MAP),
        labels_to_ignore=["ORG", "NORP", "DATE", "TIME", "CARDINAL", "ORDINAL", "MONEY",
                          "PERCENT", "QUANTITY", "PRODUCT", "EVENT", "WORK_OF_ART", "LAW",
                          "LANGUAGE"],
        default_score=NER_SCORE,
        low_score_entity_names=[],
    )  # fmt: skip
    engine = SpacyNlpEngine(
        models=[{"lang_code": "en", "model_name": SPACY_MODEL}],
        ner_model_configuration=config,
    )
    engine.nlp = {"en": nlp}  # injected: SpacyNlpEngine.load() would try to download
    registry = RecognizerRegistry(
        recognizers=[
            SpacyRecognizer(supported_entities=[PERSON], ner_strength=NER_SCORE),
        ],
        global_regex_flags=re.MULTILINE,
        supported_languages=["en"],
    )
    return AnalyzerEngine(registry=registry, nlp_engine=engine, supported_languages=["en"])


# --- detector -----------------------------------------------------------------------

_PLACEHOLDER = re.compile("|".join(re.escape(t) for t in TOKENS.values()))
_LEADING_NOISE = frozenset(
    {"customer", "client", "user", "buyer", "shopper", "employee", "contact", "hi",
     "hello", "hey", "dear", "thanks", "regards", "cheers", "the", "by", "for"}
)  # fmt: skip
# Words that never begin or end a name; spaCy sometimes glues them onto a PERSON span
# ("pemberton lacey vs"), which would defeat whole-span allowlist matching.
_EDGE_CONNECTORS = frozenset(
    {"vs", "versus", "v", "and", "or", "of", "with", "to", "from", "than", "a", "an",
     "in", "at", "on", "per", "via", "plus", "minus", "is", "was", "were", "are"}
)  # fmt: skip
# spaCy span tokens: split on whitespace and list separators only, so a surname glued
# to a digit ("Plinkett2") stays one token and is never dropped.
_SPAN_TOKEN = re.compile(r"[^\s,;|]+")
# A trailing token that is a number or a number with a unit ("120k", "12%", "3x").
_NUMERIC_TOKEN = re.compile(r"[(]?[$\u20ac\u00a3]?\d[\d.,]*(?:[a-z]{1,3}|%)?[)]?", re.IGNORECASE)
# After a spaCy name: a capitalised word glued to digits ("Mortimer Plinkett2").
_GLUED_SURNAME = re.compile(r"[ \t]+[A-Z][^\W\d_]*\d\w*")
# Same-length copy for a second spaCy pass: line breaks, table pipes and code/JSON
# punctuation become spaces, so a name split across rows or wrapped in backticks or
# quotes is seen as one name.
_FLATTEN = str.maketrans({c: " " for c in '\n|\t\r`"{}[]*'})
_NEXT_CAP = re.compile(r"[ \t]+([A-Z][^\W\d_]{1,30})(?![\w'\u2019])")
_PREV_CAP = re.compile(r"(?<![\w'\u2019])([A-Z][^\W\d_]{1,30})[ \t]+$")
_PIECE_BREAK = re.compile(r"[,;|\n\t]")
_ALPHA_WORD = re.compile(r"[^\W\d_]+")
_SEGMENT = re.compile(r"[^.!?\n]+")
_CAPS_RUN = re.compile(r"(?<!\w)[A-Z][A-Z'\u2019\-]+(?:[ \t]+[A-Z][A-Z'\u2019\-]+)+(?!\w)")
# D-215: a saved-report id as the agent shows it (pii_regex.REPORT_DISPLAY_ID). spaCy tags
# some random ids as PERSON (about 1 in 10 in a short sentence), which would show the user
# "<PERSON>" instead of the id. An id is never a name, so it is cut out of every spaCy
# piece; the rest of the piece (a real name next to the id) is judged as usual. Cue hits
# are unaffected.
_REPORT_ID = pii_regex.REPORT_DISPLAY_ID


_SPACY = "SpacyRecognizer"
_PRODUCT_NOUNS = frozenset(
    {"jacket", "jeans", "coat", "dress", "shirt", "tshirt", "tee", "top", "sweater",
     "hoodie", "sweatshirt", "cardigan", "blazer", "suit", "pants", "trousers", "chinos",
     "chino", "shorts", "skirt", "leggings", "socks", "sock", "boots", "boot", "shoes",
     "sneakers", "sandals", "bra", "briefs", "boxers", "underwear", "lingerie", "pajamas",
     "swimsuit", "bikini", "hat", "cap", "beanie", "scarf", "gloves", "belt", "bag",
     "wallet", "sunglasses", "watch", "vest", "parka", "denim", "fleece", "polo",
     "tank", "romper", "jumpsuit", "outerwear", "activewear", "sleepwear", "swimwear",
     "accessories", "intimates", "plus", "size", "slim", "fit", "classic"}
)  # fmt: skip


def _looks_like_product(span: str) -> bool:
    """A spaCy PERSON span that contains an apparel/product noun is a product name
    ("Classic Denim Jacket"), not a person. Not applied to cue-based PERSON findings."""
    return any(w in _PRODUCT_NOUNS or w.rstrip("s") in _PRODUCT_NOUNS for w in _words(span))


# Person cue right before a recased (lower-case or ALL-CAPS) name.
_RECASED_CUE = re.compile(
    r"\b(?i:customer|buyer|shopper|client|recipient|user|named|called|dear|hey|hi|hello"
    r"|thanks|name)\b(?:[ \t]+(?i:is|was))?[ \t]*[:=]?[ \t]*$"
)
# Suffix rewrites used to find the base form of an inflected word in the lexicon.
_INFLECTIONS = (
    ("ies", "y"),
    ("ied", "y"),
    ("es", ""),
    ("s", ""),
    ("ed", ""),
    ("ed", "e"),
    ("ing", ""),
    ("ing", "e"),
    ("est", ""),
    ("er", ""),
    ("ly", ""),
)


def _lexicon(nlp: Any) -> frozenset[str]:
    """English base forms from the pipeline's lemmatizer tables (empty if absent)."""
    words: set[str] = set()
    try:
        lookups = nlp.get_pipe("lemmatizer").lookups
        for name in ("lemma_index", "lemma_exc"):
            if lookups.has_table(name):
                table = lookups.get_table(name)
                for key in table.keys():
                    value = table[key]
                    words.update(value if name == "lemma_index" else value.keys())
    except Exception:  # noqa: BLE001 - no lexicon only weakens the recased pass
        return frozenset()
    return frozenset(w.casefold() for w in words if isinstance(w, str))


def _recase(text: str, stop_words: frozenset[str]) -> str:
    """Same-length copy with names in ALL-CAPS runs and all-lower-case segments
    title-cased ("ZORBINA QUANDLEWORTH", "the buyer was zorbina quandleworth"), for a
    spaCy pass that would otherwise not see them. In a lower-case segment stop words
    stay lower case (a fully title-cased sentence reads as a headline to spaCy).
    A character whose case change alters its length (German sharp s) is left as is."""
    chars = list(text)

    def title(lo: int, hi: int, keep_stop: bool) -> None:
        for m in _ALPHA_WORD.finditer(text, lo, hi):
            if keep_stop and m.group(0).casefold() in stop_words:
                continue
            for i, ch in enumerate(m.group(0)):
                c = ch.upper() if i == 0 else ch.lower()
                if len(c) == 1:
                    chars[m.start() + i] = c

    for m in _CAPS_RUN.finditer(text):
        title(m.start(), m.end(), False)
    for m in _SEGMENT.finditer(text):
        seg = m.group(0)
        if seg == seg.lower() and any(c.isalpha() for c in seg):
            title(m.start(), m.end(), True)
    out = "".join(chars)
    if len(out) != len(text):  # pragma: no cover - guarded per character above
        raise PiiDetectorError("recasing changed the text length")
    return out


def _spacy_pieces(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Clean a spaCy PERSON span: widen it to whole tokens, absorb a following surname
    glued to digits, drop leading role/greeting words and connectors, drop trailing
    connectors and bare numbers ("Marlowe Finch 120k" -> "Marlowe Finch"), and split it
    at inner connectors and at commas, semicolons, pipes and line breaks
    ("pemberton lacey vs Ellery Vane" -> two pieces). A token that contains a letter is
    never dropped from the middle or the end of a name."""
    while start > 0 and text[start - 1].isalnum():
        start -= 1
    while end < len(text) and text[end].isalnum():
        end += 1
    glued = _GLUED_SURNAME.match(text, end)
    if glued:
        end = glued.end()
    toks = [(m.start() + start, m.end() + start) for m in _SPAN_TOKEN.finditer(text[start:end])]

    def word(t: tuple[int, int]) -> str:
        return text[t[0] : t[1]].strip(".:!?()\"'").casefold()

    def droppable_tail(t: tuple[int, int]) -> bool:
        raw = text[t[0] : t[1]].strip(".:!?\"'")
        return (
            not any(c.isalpha() for c in raw)
            or _NUMERIC_TOKEN.fullmatch(raw) is not None
            or word(t) in _EDGE_CONNECTORS
        )

    groups: list[list[tuple[int, int]]] = [[]]
    for t in toks:
        if word(t) in _EDGE_CONNECTORS:
            groups.append([])
            continue
        if groups[-1] and _PIECE_BREAK.search(text, groups[-1][-1][1], t[0]):
            groups.append([])  # a cell or list separator ends a name: "Ab Cd,1\nEf Gh"
        groups[-1].append(t)
    pieces: list[tuple[int, int]] = []
    for g in groups:
        while g and (word(g[0]) in _LEADING_NOISE or word(g[0]) in _EDGE_CONNECTORS):
            g.pop(0)
        while g and droppable_tail(g[-1]):
            g.pop()
        if not g:
            continue
        a, b = g[0][0], g[-1][1]
        while b > a and text[b - 1] in ".:!?)\"'":
            b -= 1
        while a < b and text[a] in "(\"'":
            a += 1
        if b > a:
            pieces.append((a, b))
    return pieces


def _minus_report_ids(text: str, a: int, b: int) -> list[tuple[int, int, bool]]:
    """The parts of ``text[a:b]`` outside report-id tokens (D-215) that still contain a
    letter, trimmed of the separators left at their edges, as (start, end, was_cut)."""
    ids = [(m.start(), m.end()) for m in _REPORT_ID.finditer(text, max(0, a - 34), b + 34)]
    ids = [(x, y) for x, y in ids if x < b and y > a]
    if not ids:
        return [(a, b, False)]
    out: list[tuple[int, int]] = []
    cur = a
    for x, y in [*ids, (b, b)]:
        lo, hi = cur, min(x, b)
        while lo < hi and not text[lo].isalnum():
            lo += 1
        while hi > lo and not text[hi - 1].isalnum():
            hi -= 1
        if hi > lo and any(c.isalpha() for c in text[lo:hi]):
            out.append((lo, hi, True))
        cur = max(cur, y)
    return out


def _outside_placeholders(
    spans: list[tuple[str, int, int]], text: str
) -> list[tuple[str, int, int]]:
    ph = [(m.start(), m.end()) for m in _PLACEHOLDER.finditer(text)]
    out: list[tuple[str, int, int]] = []
    for t, a, b in spans:
        pieces = [(a, b)]
        for pa, pb in ph:
            nxt = []
            for x, y in pieces:
                if pb <= x or pa >= y:
                    nxt.append((x, y))
                    continue
                if x < pa:
                    nxt.append((x, pa))
                if pb < y:
                    nxt.append((pb, y))
            pieces = nxt
        out += [(t, x, y) for x, y in pieces if text[x:y].strip(" \t\n,.:;-|")]
    return out


def _merge(spans: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    merged: list[tuple[str, int, int]] = []
    for t, a, b in sorted(spans, key=lambda s: (s[1], -s[2])):
        if merged and a < merged[-1][2]:
            pt, pa, pb = merged[-1]
            merged[-1] = (ADDRESS if ADDRESS in (pt, t) else pt, pa, max(pb, b))
        else:
            merged.append((t, a, b))
    return merged


class PiiDetector:
    """Regex scrubber + Presidio NER with typed masks. Thread-safe; build once, reuse.

    ``allowlist`` exempts PERSON findings that are catalogue terms (brands, categories,
    departments). ``nlp`` lets a caller inject a loaded spaCy pipeline; by default the
    process-wide model singleton is used (raises ``PiiModelMissing`` if not installed).
    """

    def __init__(self, allowlist: BrandAllowlist | None = None, *, nlp: Any = None) -> None:
        self.allowlist = allowlist if allowlist is not None else EMPTY_ALLOWLIST
        model = nlp if nlp is not None else _get_nlp()
        try:
            self._analyzer = _build_analyzer(model)
        except Exception as exc:
            raise PiiDetectorError(f"building the analyzer failed: {type(exc).__name__}") from exc
        self._lock = threading.Lock()
        defaults = getattr(model, "Defaults", None)
        self._stop_words = frozenset(getattr(defaults, "stop_words", ()) or ())
        self._lexicon = _lexicon(model)
        self._derived: dict[frozenset[str], PiiDetector] = {}
        self._derived_lock = threading.Lock()

    def with_brands(self, brands: Iterable[str]) -> PiiDetector:
        """D-153: a detector whose allowlist also holds ``brands`` (the session's scope
        brands). Brands are matched exactly, case-insensitively and as a whole phrase, like
        every allowlist term; terms never combine, so a person name that only shares a word
        with a brand ("Marlowe Klein" next to "Calvin Klein") is still masked. Shares the
        loaded model; returns ``self`` when nothing is new. Bounded cache of derived
        detectors (``MAX_DERIVED_DETECTORS``)."""
        if isinstance(brands, str):
            raise TypeError("brands must be an iterable of brand names, not a string")
        if "_derived" not in vars(self):
            # A wrapper subclass that never ran PiiDetector.__init__ (it delegates mask and
            # detect elsewhere) has no allowlist of its own to extend: use it as is.
            return self
        new = frozenset(
            b for b in brands if isinstance(b, str) and b.strip() and not self.allowlist.is_term(b)
        )
        if not new:
            return self
        with self._derived_lock:
            hit = self._derived.get(new)
            if hit is not None:
                return hit
        derived = copy.copy(self)  # shares the analyzer and its lock (one model, one lock)
        derived.allowlist = extend_allowlist(self.allowlist, sorted(new))
        derived._derived = {}
        derived._derived_lock = threading.Lock()
        with self._derived_lock:
            if len(self._derived) >= MAX_DERIVED_DETECTORS:
                self._derived.clear()
            return self._derived.setdefault(new, derived)

    def _spacy_results(self, text: str) -> list[tuple[int, int, bool]]:
        """spaCy PERSON spans as (start, end, from_recased) over the text, a flattened
        copy and a recased copy (all three have the same length as ``text``)."""
        out: list[tuple[int, int, bool]] = []
        flat = text.translate(_FLATTEN)
        recased = _recase(flat, self._stop_words)
        variants = [(text, False)]
        if flat != text:
            variants.append((flat, False))
        if recased != flat:
            variants.append((recased, True))
        with self._lock:
            for variant, is_recased in variants:
                for r in self._analyzer.analyze(
                    text=variant, language="en", entities=[PERSON], score_threshold=MIN_SCORE
                ):
                    meta = r.recognition_metadata or {}
                    if r.entity_type == PERSON and meta.get("recognizer_name") == _SPACY:
                        out.append((r.start, r.end, is_recased))
        return out

    def _widen(self, text: str, a: int, b: int) -> tuple[int, int]:
        """Grow a masked spaCy name over up to two adjacent capitalised words on each
        side ("Thistlewood Marlowe Finch Bramble"), so a name partly tagged by spaCy is
        masked whole. Only applied to a span that is masked anyway, never to an exempt
        one, so it cannot turn a brand into a finding."""

        def ok(w: str) -> bool:
            f = w.casefold()
            return not (f in self._stop_words or f in _LEADING_NOISE or f in _EDGE_CONNECTORS)

        for _ in range(2):
            m = _NEXT_CAP.match(text, b)
            if not m or not ok(m.group(1)):
                break
            b = m.end()
        for _ in range(2):
            m = _PREV_CAP.search(text[max(0, a - 40) : a])
            if not m or not ok(m.group(1)):
                break
            a = a - len(m.group(0))
        return a, b

    def _common(self, word: str) -> bool:
        w = word.casefold()
        if w in self._stop_words or w in self._lexicon:
            return True
        for suffix, repl in _INFLECTIONS:
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                if w[: len(w) - len(suffix)] + repl in self._lexicon:
                    return True
        return False

    def _id_neighbour(self, piece: str) -> bool:
        """D-215: what is left of a spaCy span after a report id is cut out of it is dropped
        when it is one English word ("Renamed R-<id>"): spaCy tagged the id, not a name.
        Two or more words, or a word outside the lexicon ("Fenwick"), are still judged."""
        words = _ALPHA_WORD.findall(piece)
        return len(words) == 1 and self._common(words[0])

    def _recased_name(self, text: str, a: int, piece: str) -> bool:
        """A name found only after recasing a lower-case or ALL-CAPS segment is kept
        when it has two or more words and either follows a person cue ("the buyer was
        ...") or has a word outside the English lexicon. Analytics prose ("RETURNED
        SHARE", "small dip") is made of dictionary words and stays unmasked."""
        words = _ALPHA_WORD.findall(piece)
        if len(words) < 2:
            return False  # a single recased word is too weak a signal
        if _RECASED_CUE.search(text[max(0, a - 40) : a]):
            return True
        return not all(self._common(w) for w in words)

    def _ner_spans(self, text: str) -> list[tuple[str, int, int]]:
        spans: list[tuple[str, int, int]] = []
        # Cue-based hits are never trimmed and never allowlisted: a cue ("Name:",
        # "Hey", a name column, a JSON name key) says the value is a person.
        for t, a, b, kind in _cue_results(text):
            if kind == _TITLE and self.allowlist.is_term(text[a:b]):
                continue  # the whole titled span is one catalogue term ("Dr. Pimbleton")
            if kind == "name_key" and _looks_like_product(text[a:b]):
                continue  # {"name": "Classic Denim Jacket"} is the products column
            spans.append((t, a, b))
        # spaCy-only hits: allowlist (one term covering the whole piece) and the
        # product-noun filter apply here and only here.
        for start, end, recased in self._spacy_results(text):
            for a, b, cut in (
                p for s, e in _spacy_pieces(text, start, end) for p in _minus_report_ids(text, s, e)
            ):
                piece = text[a:b]
                if cut and self._id_neighbour(piece):
                    continue  # D-215: "Renamed" was tagged only together with the id
                caseless = recased or piece.isupper() or piece.islower()
                if caseless and not self._recased_name(text, a, piece):
                    continue
                if self.allowlist.covers(text, a, b) or _looks_like_product(piece):
                    continue
                spans.append((PERSON, *self._widen(text, a, b)))
        return _merge(_outside_placeholders(spans, text))

    def _bridged_by_id(self, text: str) -> tuple[str, list[tuple[str, int, int]]] | None:
        """D-227: NER rescan with every report id taken out ("Zorbina R-<id> Quandleworth"
        becomes "Zorbina Quandleworth"). If a name found there spans a place where an id
        was, the id exemption is withdrawn for this text: the text without ids and its
        spans are returned (fail closed). ``None`` when the ids hide nothing."""
        if _REPORT_ID.search(text) is None:
            return None
        parts: list[str] = []
        joins: list[int] = []
        cur = 0
        length = 0
        for m in _REPORT_ID.finditer(text):
            lo, hi = m.start(), m.end()
            while lo > cur and text[lo - 1] in " \t":
                lo -= 1
            while hi < len(text) and text[hi] in " \t":
                hi += 1
            parts.append(text[cur:lo])
            length += lo - cur
            joins.append(length)
            parts.append(" ")
            length += 1
            cur = hi
        parts.append(text[cur:])
        without = "".join(parts)
        spans = self._ner_spans(without)
        if any(a < j and b > j + 1 for _, a, b in spans for j in joins):
            return without, spans
        return None

    def mask(self, text: str) -> MaskResult:
        """Mask PII in ``text``. Raises ``PiiDetectorError`` on any failure, including
        input that is not ``str`` (fail closed: the caller never gets text back)."""
        if not isinstance(text, str):
            raise PiiDetectorError(f"mask() expects str, got {type(text).__name__}")
        try:
            base = pii_regex.scrub(text)
            out = base.text
            if base.truncated:
                # The cut can bisect a name ("Zorbina Quand|leworth"): drop the whole
                # partial last line, not only the partial token, before NER.
                body = out[: len(out) - len(pii_regex.TRUNCATION_MARKER)]
                cut = body.rfind("\n")
                out = (body[:cut] if cut > 0 else body) + pii_regex.TRUNCATION_MARKER
            spans = self._ner_spans(out)
            joined = self._bridged_by_id(out)
            if joined is not None:
                out, spans = joined
            for t, a, b in sorted(spans, key=lambda s: -s[1]):
                out = out[:a] + TOKENS[t] + out[b:]
            findings = tuple(
                Finding(m.group(0)[1:-1], m.start(), m.end()) for m in _PLACEHOLDER.finditer(out)
            )
            if len(out) > MAX_SCRUB_CHARS + 64 and not base.truncated:  # pragma: no cover
                raise PiiDetectorError("masked text exceeds the input bound")
            return MaskResult(text=out, findings=findings, truncated=base.truncated)
        except PiiDetectorError:
            raise
        except Exception as exc:
            logger.error("pii detector failed: %s", type(exc).__name__)
            raise PiiDetectorError(f"pii detection failed: {type(exc).__name__}") from exc

    def detect(self, text: str) -> list[Finding]:
        """Typed findings (type + span in the masked text), never the values."""
        return list(self.mask(text).findings)


_DEFAULT_LOCK = threading.Lock()
_default: PiiDetector | None = None


def default_detector() -> PiiDetector:
    """Process-wide detector (lazy, thread-safe). Empty allowlist until configured."""
    global _default
    if _default is None:
        with _DEFAULT_LOCK:
            if _default is None:
                _default = PiiDetector()
    return _default


def set_default_detector(detector: PiiDetector | None) -> None:
    """Install the process-wide detector (startup wiring passes the catalogue allowlist)."""
    global _default
    with _DEFAULT_LOCK:
        _default = detector


def scrub_output(text: str, detector: PiiDetector | None = None) -> MaskResult:
    """Output-guard entry point (iteration 12): regex + NER masking, fail closed."""
    if not isinstance(text, str):
        raise PiiDetectorError(f"scrub_output() expects str, got {type(text).__name__}")
    return (detector or default_detector()).mask(text)
