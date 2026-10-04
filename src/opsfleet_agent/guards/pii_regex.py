r"""Deterministic regex scrubber for structured PII (iteration 8a; HLD §5.2 layers 1, 7-9, §5.4).

``scrub(text)`` masks e-mail addresses (including obfuscated forms), phone numbers,
card-like numbers and id-like digit strings with typed placeholders (``<EMAIL>``,
``<PHONE>``, ``<CARD>``, ``<ID>``, HLD §5.4). It is pure and deterministic: no I/O, no
logging of values, and ``findings`` holds counts by type only, never the raw values.

The NER detector for person names and street addresses is added on top of this in 8b.

Digit policy (the analytics vs. PII trade-off)
---------------------------------------------
Analytics answers are full of numbers, so the scrubber only masks digit shapes that an
aggregate does not take:

* **Contiguous runs of 13 or more digits are always masked** (fail safe): ``<CARD>`` when
  13-19 digits pass the Luhn check, otherwise ``<ID>``. Aggregates of up to **12 digits**
  (``999999999999``) survive, as do larger values written with thousands separators
  (``1,234,567,890,123``), because commas are not treated as digit-group separators.
* **Digit-group separators** are a hyphen or dot with up to two spaces/tabs around it
  (``555 - 010 - 4477``) or one or two whitespace characters, including a tab or a line
  break (``555\n010\n4477``). Consequence (fail safe): bare 3-3-4 digit groups such as
  ``IDs 123 456 7890`` or a column ``312\n455\n1203`` are masked as ``<PHONE>``.
* **Card-shaped groups** (a 4-digit group followed by 2-4 groups of 3-6 digits) with 13+
  digits in total are masked: ``<CARD>`` when Luhn-valid; ``<ID>`` when hyphen/dot
  separated but not Luhn-valid. A whitespace-separated sequence that fails Luhn is
  **kept**, because it is far more likely a list of numbers, with one exception (8b
  review): exactly four groups of four digits (card layout) is masked as ``<ID>`` unless
  every group is a year (``2021 2022 2023 2024`` stays).
* **Phones** need a shape: an international prefix (``+`` or ``00``) with 7-15 digits in
  groups of up to 8 (``+49 30 1234567``), North-American ``3-3-4`` grouping, a
  leading-zero national ``0xx xxxx xxxx`` grouping,
  or a phone keyword (``phone``, ``tel``, ``cell``, ``mobile``, ``contact number``,
  ``call``, ``sms``, ``dial``, ``text me``, ``reach me``...) before the number, with at
  most one line break in between. An extension after a masked phone (``ext. 89``,
  ``x123``, ``#12``) is absorbed into the same ``<PHONE>``. Unseparated
  10-12 digit runs without a keyword and 7-digit local numbers (``555-0123``) are **not**
  masked here, because they collide with aggregates and ranges (``250-1000``); the NER
  layer (8b) is the second line for those.
* ``ddd-dd-dddd`` (SSN shape, also with one consistent space or dot separator) is masked
  as ``<ID>``. After an id keyword (``SSN``, ``social security number``, ``passport``,
  ``national id``, ``tax id``, ``driver's license``) any id with 6+ digits is ``<ID>``
  (``SSN 123456789``, ``passport X12345678``).
* An IBAN (compact or in groups of four) that passes the mod-97 check is one ``<ID>``.

Ordinary analytics text is left alone: years, ISO dates, prices (``$1,234.56``),
percentages, counts and short SKUs.

E-mail forms: plain, spaced or line-broken around ``@`` and ``.`` (``jane @ example . com``),
bracketed/defanged (``[at]``, ``[@]``, ``[dot]``, ``[.]``, ``(.)``), word forms
(``at``/``dot``), ``jane.doe at example.com`` when the local part is dotted, and a local
part with spaced dots (``zed . quux @ example . com``). Not masked by design:
``zed at example.com`` (undotted local, plain ``at``, literal dot), which cannot be told
apart from ``Revenue at thelook.com``.

Normalisation: every format character (``Cf``: zero-width, bidi controls, invisible
separators, soft hyphen...) is removed, every space separator (``Zs``) becomes an ASCII
space, full-width / compatibility forms of ASCII (``＠``, ``﹫``, full-width digits,
ideographic full stop) are folded to ASCII, and URL/HTML encodings of ``@`` and ``.`` only
(``%40``, ``%2540``, ``&#64;``, ``&commat;``, ``%2E``, ``&#46;``...) are decoded before
matching, so none of them can be used to dodge the patterns. No NFKC and no general
unescaping, so the rest of the text is unchanged.

Bounds: every pattern uses bounded quantifiers or a single character class per
repetition with a mandatory separator between groups, so matching is linear in the input
length (no nested ambiguous repetition). Input longer than ``MAX_SCRUB_CHARS`` is cut to
the cap, minus the trailing partial token (so a value bisected by the cut cannot slip
through), before scrubbing and flagged ``truncated``; the excess is dropped, never
returned unscrubbed.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

MAX_SCRUB_CHARS = 100_000
TRUNCATION_MARKER = " [truncated]"

EMAIL = "EMAIL"
PHONE = "PHONE"
CARD = "CARD"
ID = "ID"

TOKENS: Mapping[str, str] = MappingProxyType(
    {EMAIL: "<EMAIL>", PHONE: "<PHONE>", CARD: "<CARD>", ID: "<ID>"}
)

_AGGREGATE_MAX_DIGITS = 12  # contiguous digit runs up to this length survive


@dataclass(frozen=True)
class ScrubResult:
    """Scrubbed text plus counts of what was masked. Never holds a raw value."""

    text: str
    findings: Mapping[str, int] = field(default_factory=dict)
    truncated: bool = False

    @property
    def redacted(self) -> bool:
        return bool(self.findings)


# --- normalisation ------------------------------------------------------------------


def _build_fold() -> dict[int, int | None]:
    """Translate table, built once at import time (one pass over the code space, ~70 ms).

    * every format character (category ``Cf``: zero-width, bidi marks and isolates, the
      invisible separator/times, tags, soft hyphen, BOM...) plus U+034F and U+180E is
      dropped, so it cannot split a value;
    * every space separator (``Zs``: NBSP, figure, thin, ideographic...) becomes an ASCII
      space, line/paragraph separators (``Zl``/``Zp``) become a newline;
    * full-width / compatibility forms of ASCII and the dash family fold to ASCII.
    """
    fold: dict[int, int | None] = {cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)}
    fold.update(
        {
            0xFE6B: ord("@"),  # small commercial at
            0xFE52: ord("."),  # small full stop
            0x3002: ord("."),  # ideographic full stop
            0xFF61: ord("."),  # halfwidth ideographic full stop
            0x2024: ord("."),  # one dot leader
            0x2010: ord("-"),
            0x2011: ord("-"),
            0x2012: ord("-"),
            0x2013: ord("-"),
            0x2014: ord("-"),
            0x2212: ord("-"),  # minus sign
        }
    )
    for cp in range(sys.maxunicode + 1):
        cat = unicodedata.category(chr(cp))
        if cat == "Cf":
            fold[cp] = None
        elif cat == "Zs" and cp != 0x20:
            fold[cp] = ord(" ")
        elif cat in ("Zl", "Zp"):
            fold[cp] = ord("\n")
    fold[0x034F] = None  # combining grapheme joiner (Mn)
    fold[0x180E] = None  # Mongolian vowel separator (Cf in old Unicode, Zs before that)
    return fold


_FOLD = _build_fold()

# Bounded pre-pass: decode only the encodings of "@" and "." (URL, double-URL, HTML
# entities). No general unescaping, so the rest of the text is unchanged.
_ENC_AT = r"%(?:25){0,2}40|&#0{0,4}64(?:;|(?!\d))|&#x0{0,4}40(?:;|(?![0-9a-f]))|&commat;"
_ENC_DOT = r"%(?:25){0,2}2e|&#0{0,4}46(?:;|(?!\d))|&#x0{0,4}2e(?:;|(?![0-9a-f]))|&period;"
_ENCODED = re.compile(rf"(?i:(?P<at>{_ENC_AT})|(?P<dot>{_ENC_DOT}))")


def _decode(m: re.Match[str]) -> str:
    return "@" if m.group("at") else "."


def _normalise(text: str) -> str:
    return _ENCODED.sub(_decode, text.translate(_FOLD))


# --- patterns -----------------------------------------------------------------------

_LOCAL = r"(?<![\w.+%-])[\w.+%-]{1,64}"
# Local part with spaced dots ("zed . quux @ example . com"): up to four dotted pieces.
_LOCAL_SPACED = r"(?<![\w.+%-])[\w+%-]{1,64}(?:[ \t]{0,3}\.[ \t]{0,3}[\w+%-]{1,64}){0,4}"
_LABEL = r"[\w-]{1,63}"
_TLD = r"[^\W\d_]{2,24}(?![\w-]|\.[\w-])"
_BR_OPEN = r"[\[({<]"
_BR_CLOSE = r"[\])}>]"

# "@" (optionally spaced or line-broken) or a bracketed "at"/"@": strong separators,
# combined with any dot form.
_AT_STRONG = (
    rf"(?:\s{{0,3}}@\s{{0,3}}"
    rf"|\s{{0,3}}{_BR_OPEN}\s{{0,3}}(?:(?i:at)|@)\s{{0,3}}{_BR_CLOSE}\s{{0,3}})"
)
_DOT_WORD = (
    rf"(?:\s{{0,3}}{_BR_OPEN}\s{{0,3}}(?:(?i:dot)|\.)\s{{0,3}}{_BR_CLOSE}\s{{0,3}}"
    rf"|\s{{1,3}}(?i:dot)\s{{1,3}})"
)
# A literal dot, possibly spaced ("example . com", "example .com"). A dot followed by
# whitespace only (sentence end, "ops@acme. Thanks") is a dot only before a lower-case TLD.
_DOT_ANY = rf"(?:\s{{0,3}}\.(?:\s{{1,3}}(?=[^\W\dA-Z_]))?|{_DOT_WORD})"

# 1. Standard, spaced, line-broken and bracket-obfuscated e-mail: a@b.com, a @ b . com,
#    a [at] b [dot] com, a@b dot com, a [@] b [.] com, a@b(.)com.
_EMAIL_STRONG = re.compile(
    rf"(?:{_LOCAL_SPACED}|{_LOCAL}){_AT_STRONG}{_LABEL}(?:{_DOT_ANY}{_LABEL}){{0,8}}{_DOT_ANY}{_TLD}"
)

# 2. Plain-word "at" needs a word-form "dot" too ("name at domain dot com"). A plain "at"
#    followed by a literal dot ("revenue at thelook.com") is too common in analytics text.
_HOST = r"[\w-]{1,63}(?:\.[\w-]{1,63}){0,4}"
_EMAIL_WORD = re.compile(
    rf"{_LOCAL}\s{{1,3}}(?i:at)\s{{1,3}}{_HOST}(?:{_DOT_WORD}{_HOST}){{0,4}}{_DOT_WORD}{_TLD}"
)

# 3. Plain-word "at" with a literal-dot domain only when the local part is itself dotted
#    and starts with a letter ("jane.doe at example.com"). "revenue at thelook.com",
#    "Q4 at thelook.com" and "5.5 at thelook.com" do not qualify.
_EMAIL_AT_DOTTED = re.compile(
    r"(?<![\w.+%-])[^\W\d_][\w+%-]{0,30}(?:\.[\w+%-]{1,30}){1,3}"
    r"\s{1,3}(?i:at)\s{1,3}"
    rf"{_LABEL}(?:\.{_LABEL}){{0,4}}\.{_TLD}"
)

# Digit-group separator: one dot or hyphen with up to two spaces/tabs either side
# ("555 - 010 - 4477"), or a run of one or two whitespace characters, which covers a tab
# or a line break ("555\n010\n4477", "+1\t555"). Bounded, so matching stays linear.
_SEP = r"(?:[ \t]{0,2}[.\-][ \t]{0,2}|\s{1,2})"
_WS_ONLY = frozenset(" \t\r\n\v\f\x1c\x1d\x1e\x1f\x85")

# 4. Phone preceded by a keyword: "phone: 5551234567", "tel. +44...", "Mobile:\n+44...".
#    At most one line break between the keyword and the number.
_PHONE_KEYWORD = re.compile(
    r"(?i:\b(?:phone|telephone|tel|cellphone|cell|mobile|whatsapp|fax"
    r"|contact\s{1,3}number|call(?:ed)?|sms|dial|text[ \t]+me|reach[ \t]+me)\b)"
    r"(?:[^\d\n]{0,20}?\n)?[^\d\n]{0,20}?"
    r"(\+?\d[\d \-.()]{5,20}\d)(?!\d)"
)

# 5. International prefix (+ or 00) and country code, then 1-6 groups of 1-8 digits each
#    after a separator or a parenthesised trunk/area code: "+44 20 7946 0958",
#    "+49 30 1234567", "+49 (0)30 1234567", "+1 (555) 123-4567", "0049 30 1234567"; or an
#    unseparated "+15551234567". phone_min7 keeps matches outside 7-15 digits (E.164).
_PHONE_INTL = re.compile(
    rf"(?<![\w+])(?:(?:\+|\b00)\d{{1,4}}"
    rf"(?:{_SEP}?\(\d{{1,5}}\)|(?:{_SEP}|(?<=\)))\d{{1,8}}){{1,6}}"
    rf"|\+\d{{7,15}})(?!\w)"
)

# 6. Card-shaped groups: 4242 4242 4242 4242, 3782-822463-10005.
_CARD_GROUPED = re.compile(rf"(?<![\d.,])\d{{4}}(?:{_SEP}\d{{3,6}}){{2,4}}(?![\d])")

# 7. Long contiguous digit runs.
_LONG_RUN = re.compile(rf"(?<!\d)\d{{{_AGGREGATE_MAX_DIGITS + 1},}}(?!\d)")

# 8. North-American 3-3-4: (555) 123-4567, 555.123.4567, 1-555-123-4567.
_PHONE_NANP = re.compile(
    rf"(?<![\w+])(?:1{_SEP})?(?:\(\d{{3}}\){_SEP}?|\d{{3}}{_SEP})\d{{3}}{_SEP}\d{{4}}(?![\w])"
)

# 9. Leading-zero national: 020 7946 0958.
_PHONE_NATIONAL = re.compile(rf"(?<![\w.,])0\d{{2,4}}{_SEP}\d{{3,4}}{_SEP}\d{{3,4}}(?![\w])")

# 9b. Extension after a masked phone: "<PHONE> ext. 89", "<PHONE> x123", "<PHONE> #12".
_PHONE_EXT = re.compile(
    r"<PHONE>[ \t]{0,2},?[ \t]{0,2}(?i:ext(?:ension)?\.?|x|#)[ \t]{0,2}\d{1,6}(?!\w)"
)

# 9c. IBAN: two letters, two check digits, then 11-30 alphanumerics, compact or in groups
#     of four ("DE89 3704 0044 0532 0130 00"). Masked as one <ID> when the mod-97 check
#     passes, so the country prefix does not survive next to a partial mask.
_IBAN = re.compile(r"(?<![\w])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?(?![\w])")

# 9d. Government id after a keyword: "SSN 123456789", "passport X12345678",
#     "social security number: 123 45 6789". The id needs 6+ digits.
_ID_KEYWORD = re.compile(
    r"(?i:\b(?:ssn|social[ \t]+security(?:[ \t]+(?:number|no))?|passport(?:[ \t]+(?:number|no))?"
    r"|national[ \t]+id|tax[ \t]+id|driver'?s?[ \t]+licen[cs]e(?:[ \t]+(?:number|no))?)\b)"
    r"[^\w\n]{0,4}(?:(?i:is|was|number|no)[^\w\n]{0,4})?"
    r"([A-Za-z]{0,3}\d[\d \-]{4,14}\d[A-Za-z]?)(?![\w])"
)

# 10. SSN shape with one consistent separator: 123-45-6789, 123 45 6789, 123.45.6789.
_SSN = re.compile(r"(?<![\w.-])\d{3}([-. ])\d{2}\1\d{4}(?![\w-]|\.\d)")


_YEAR = re.compile(r"(?:19|20)\d\d")


def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdecimal())


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class _Scrubber:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def mask(self, kind: str) -> str:
        self.counts[kind] += 1
        return TOKENS[kind]

    def email(self, _m: re.Match[str]) -> str:
        return self.mask(EMAIL)

    def phone_keyword(self, m: re.Match[str]) -> str:
        if len(_digits(m.group(1))) < 7:
            return m.group(0)
        return m.group(0)[: m.start(1) - m.start(0)] + self.mask(PHONE)

    def phone_min7(self, m: re.Match[str]) -> str:
        return self.mask(PHONE) if 7 <= len(_digits(m.group(0))) <= 15 else m.group(0)

    def phone(self, _m: re.Match[str]) -> str:
        return self.mask(PHONE)

    def card_grouped(self, m: re.Match[str]) -> str:
        raw = m.group(0)
        digits = _digits(raw)
        if len(digits) <= _AGGREGATE_MAX_DIGITS:
            return raw
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return self.mask(CARD)
        if set(raw) - set(digits) <= _WS_ONLY:
            groups = raw.split()
            if len(groups) == 4 and all(len(g) == 4 for g in groups):
                if not all(_YEAR.fullmatch(g) for g in groups):
                    return self.mask(ID)  # 4-4-4-4 is card layout, even if Luhn-invalid
            return raw  # whitespace-separated, not a valid PAN: most likely a list of numbers
        return self.mask(ID)

    def iban(self, m: re.Match[str]) -> str:
        raw = m.group(0).replace(" ", "")
        if not 15 <= len(raw) <= 34:
            return m.group(0)
        moved = raw[4:] + raw[:4]
        number = "".join(str(int(c, 36)) for c in moved)
        return self.mask(ID) if int(number) % 97 == 1 else m.group(0)

    def id_keyword(self, m: re.Match[str]) -> str:
        if len(_digits(m.group(1))) < 6:
            return m.group(0)
        return m.group(0)[: m.start(1) - m.start(0)] + self.mask(ID)

    def phone_ext(self, _m: re.Match[str]) -> str:
        return TOKENS[PHONE]  # same finding, the extension joins the masked phone

    def long_run(self, m: re.Match[str]) -> str:
        digits = m.group(0)
        if len(digits) <= 19 and _luhn_ok(digits):
            return self.mask(CARD)
        return self.mask(ID)

    def ssn(self, _m: re.Match[str]) -> str:
        return self.mask(ID)


def _drop_partial_token(text: str) -> str:
    """Drop the trailing run of non-whitespace after a cut: it may be a bisected value
    whose remainder no longer matches any pattern. Linear scan, no regex backtracking."""
    i = len(text)
    while i and not text[i - 1].isspace():
        i -= 1
    return text[:i]


def scrub(text: str) -> ScrubResult:
    """Mask structured PII in ``text``. Pure, deterministic and idempotent."""
    if not isinstance(text, str):
        raise TypeError("scrub() expects str")
    truncated = len(text) > MAX_SCRUB_CHARS
    if truncated:
        text = _drop_partial_token(text[:MAX_SCRUB_CHARS])
    s = _Scrubber()
    out = _normalise(text)
    out = _EMAIL_STRONG.sub(s.email, out)
    out = _EMAIL_WORD.sub(s.email, out)
    out = _EMAIL_AT_DOTTED.sub(s.email, out)
    out = _IBAN.sub(s.iban, out)
    out = _ID_KEYWORD.sub(s.id_keyword, out)
    out = _PHONE_KEYWORD.sub(s.phone_keyword, out)
    out = _PHONE_INTL.sub(s.phone_min7, out)
    out = _CARD_GROUPED.sub(s.card_grouped, out)
    out = _LONG_RUN.sub(s.long_run, out)
    out = _PHONE_NANP.sub(s.phone, out)
    out = _PHONE_NATIONAL.sub(s.phone, out)
    out = _SSN.sub(s.ssn, out)
    out = _PHONE_EXT.sub(s.phone_ext, out)
    if truncated:
        out += TRUNCATION_MARKER
    return ScrubResult(text=out, findings=MappingProxyType(dict(s.counts)), truncated=truncated)


def scrub_for_persistence(text: str, sink: Callable[[str], object]) -> ScrubResult:
    """Input-guard entry point: scrub ``text``, then hand only the masked form to ``sink``.

    ``sink`` stands for anything that stores or forwards a user message: the router input,
    the LangGraph state and checkpoint, history, the summariser and traces (AC-10.7, R3-M8).
    The raw text is never passed to it; if scrubbing raises, ``sink`` is not called (fail
    closed). Iteration 14a re-asserts this against the real checkpointer
    (``test_user_typed_email_not_persisted``).
    """
    result = scrub(text)
    sink(result.text)
    return result
