"""Numeric grounding of a drafted answer (HLD §4.1 step 6). Pure functions, no I/O, no model.

Every number in the draft must be explained by the *grounding set*: the scrubbed values of this
turn's ledger, prior in-scope session ledger entries (each figure group is tagged with its
``query_id``) and viewed-report figures (a seam: callers pass them as more figure groups).

A draft number is grounded when it matches a grounding value:

* a ledger value: after rounding to the precision it is shown with, or within 0.5% relative (a
  ``%`` number: the shown precision only, no relative floor); a derived value: shown precision
  only;
* with ``k`` / ``M`` / ``B`` / ``%`` / ``percent`` suffixes (glued, or after 1-3 blanks: "3.2 bn",
  "12.3 M", "9 K", "3.2 Mio", "3.2 MM", "3.2 mil"), ``million`` / ``thousand`` words,
  ``e`` exponents and thousands separators (comma, space, NBSP, apostrophe) normalised (a
  percentage also matches the underlying fraction); Indian grouping ("9,34,567") is read by its
  value. A digit run joined by a group mark that no grouping explains ("1,2345", "640,98765",
  "1,2,3") is unparseable and labelled whole: no piece of it grounds or is skipped on its own;
* as a derived value recomputed from ONE query (never across queries): a column total, and
  sum or difference of consecutive or first and last values of a column, or of two in a row; and,
  for a ``%`` number ONLY, the
  share of a column total, the growth between consecutive values and first-to-last growth (no
  all-pairs ratios: they made a percentage too easy to match, OD-1). Identifier-like columns
  (named ``id`` or ``*_id``, or 20+ all-distinct integers) are never used to derive anything;
* signs must agree (hyphen, en/figure/em dash, horizontal bar, minus sign, small and
  fullwidth hyphen directly before the number, a currency symbol ($ € £ ¥ ₹) or code, or
  right after a code ("USD-12,345", "-A$12,345", "-€12,345"), and "($1,234)" mean
  negative; a bare "(1,234)" is a list item as often as a loss and matches either sign).
  Limitation: direction words are not read, so "fell 12%" is
  matched like "12%" (a +12% growth grounds it); a bare number never matches a fraction x100.

Dates are not numbers: each must be a valid calendar date or month inside the data window,
otherwise it counts as unmatched. Small bare integers (10 or less, no unit) are prose ("top 5")
and are ignored. A four-digit number is a year after a strong date word (Q1, FY, a month) or in a
table key;
after a weak one ("in", "since") it passes as an in-window year OR when it is a grounded number.
A pure digit run glued to a letter ("A12345", "7f3a91c2") or a time ("10:30") is an identifier,
skipped, unless the glue is a known currency, magnitude or count ("eur98765", "98765bnUSD"); a
grouped or decimal number glued to letters ("usd12,345", "12,345kg") is always checked.
The draft is first decoded exactly as the output guard will show it (entities, Markdown
escapes, NFKC, invisibles); a draft over MAX_DRAFT_CHARS is labelled without scanning.
Spelled-out numbers
(eleven .. ninety, hundred, thousand, million, ...), superscripts and vulgar fractions are not
parsed and are labelled. Anything unmatched, a draft over the number cap or a check past its
deadline gets exactly ONE trailing "estimate" label, added by code (any label in the draft is
stripped first); the check is idempotent.

A figure group is ``{"query_id": str, "values": [float, ...], "columns": {name: [float, ...]},
"dates": [iso-date, ...]}``; only numbers from scrubbed result rows go in, never row text.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from opsfleet_agent.guards.output import decode_for_display

__all__ = [
    "ESTIMATE_LABEL",
    "MAX_RAW_VALUES",
    "GroundingResult",
    "check_grounding",
    "extract_figures",
    "merge_figures",
]

ESTIMATE_LABEL: Final = (
    "Note: some figures above are an estimate and are not taken directly from the query results."
)
REL_TOL: Final = 0.005
MAX_RAW_VALUES: Final = 20_000  # raw ledger values searched; filled newest query first
MAX_DRAFT_CHARS: Final = 20_000  # a longer draft is labelled unscanned
MAX_QUERIES: Final = 20
MAX_VALUES_PER_QUERY: Final = 2000
MAX_DRAFT_NUMBERS: Final = 200
_SMALL_INT_MAX: Final = 10
MAX_CANDIDATES: Final = 200_000
_ID_MIN_ROWS: Final = 20  # an all-distinct integer column this long is identifier-like
_MAX_EXP: Final = 30

_MONTHS: Final = (
    "january february march april may june july august september october november december"
).split()
_MONTH_RE: Final = "|".join(m[:3] + "(?:" + m[3:] + ")?" if len(m) > 3 else m for m in _MONTHS)
_DATE_RES: Final = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{4})-(\d{2})\b"),
    re.compile(
        rf"\b({_MONTH_RE})\.?(?:\s+(\d{{1,2}})(?:st|nd|rd|th)?,?)?\s+(\d{{4}})\b", re.IGNORECASE
    ),
)
_YEAR: Final = r"(?:19|20)\d{2}"
_YEAR_TAIL: Final = r"(?![\w%]|[.,]\d|\s?(?:k|m|b)\b)"
_STRONG: Final = rf"fy|fiscal|q[1-4]|h[12]|{_MONTH_RE}"
_WEAK: Final = "in|during|since|until|through|from|between|year"
# a four-digit number is a year only in a date context: after a date word, in brackets, or as a
# table or list key ("We sold 2023 units" stays a number)
_YEAR_RES: Final = (
    re.compile(
        rf"(?<![\w.,$])(?:{_STRONG})\s+(?P<y>{_YEAR})"
        rf"(?:\s*(?:-|–|to|and)\s*(?P<y2>{_YEAR}))?{_YEAR_TAIL}",
        re.IGNORECASE,
    ),
    re.compile(rf"(?<![\w.,$])fy(?P<y>{_YEAR}){_YEAR_TAIL}", re.IGNORECASE),
    re.compile(rf"\((?P<y>{_YEAR})\)"),
    re.compile(rf"(?m)^[ \t|*•-]*(?P<y>{_YEAR})[ \t]*(?:[|:]|-\s)"),
)
# after a weak date word the number is ambiguous ("in 2024 sales rose", "since 2023 customers
# placed 1,234"): it passes as an in-window year OR when it is a grounded number
_WEAK_YEAR_RE: Final = re.compile(
    rf"(?<![\w.,$])(?:{_WEAK})\s+(?P<y>{_YEAR})"
    rf"(?:\s*(?:-|–|to|and)\s*(?P<y2>{_YEAR}))?{_YEAR_TAIL}",
    re.IGNORECASE,
)
_SIGNS: Final = "-‒–—―−﹣－"
_SEP: Final = "   '’"
_GROUP_MARKS: Final = ",'’"  # a digit run joined by these is one numeral (or unparseable)
_CUR_SYMBOLS: Final = "$€£¥₹"
_PCT_SUFFIXES: Final = frozenset({"%", "percent", "percentagepoint", "percentagepoints", "pp"})
# A number glued to letters (either side) is read by its shape:
# * a comma-grouped or decimal number ("usd12,345", "12,345customers", "3.2bnUSD") is a figure
#   and checked, whatever the letters (an unknown glue reads as scale 1);
# * a pure digit run is an identifier ("A12345", "SKU12345", "7f3a91c2"), skipped, EXCEPT after
#   a currency prefix ("USD12345", "eur98765", "Rs12345", "US$12", "kr98765") or before a
#   magnitude, currency or count glue, possibly doubled ("5k", "3T", "98765bnUSD", "98765eur",
#   "12345units"), which are checked. Codes and glues are case-insensitive. A time ("10:30") is
#   skipped. Accepted false positives (fail closed): "ABC-12345" and "AUD2024" are checked.
# Matches run left to right, so a grouped number is read whole ("usd12,345" is never "345"); a
# number after a digit and a space ("1234 98765") is still scanned; a run joined by a group mark
# that no grouping explains ("640,98765", "1,2345") is unparseable and labelled (fail closed).
_ISO_CODES: Final = frozenset(
    (
        "usd eur gbp cad aud nzd jpy cny rmb inr chf rub rur brl mxn sek nok dkk pln uah kzt aed "
        "sgd hkd zar krw twd thb idr myr vnd ils sar qar kwd egp ngn clp czk huf ron bgn"
    ).split()
)
# Ambiguous ISO codes that are also English words ("try", "php", "pen", "ars"): a currency only in
# UPPERCASE ("TRY98765", "98765PEN"); lowercase forms stay identifiers or prose (case-sensitive).
_UPPER_CODES: Final = frozenset("TRY PHP COP ARS KES ISK PEN".split())
_PREFIXES: Final = "|".join(sorted(_ISO_CODES | {"rs", "rm", "rp", "kr"}, key=len, reverse=True))
_UPPER_PREFIXES: Final = "|".join(sorted(_UPPER_CODES))
# A sign may stand before a prefix ("-usd12345", "−US$12,345", "-A$12,345") or right after it
# ("USD-12,345", "Rs.-12345", "US$-12,345"); a letter-$ prefix ("A$", "HK$", "Mex$") is a
# currency. Elsewhere a sign is never read after a letter or a dot ("ABC-12345" is unsigned).
# A currency symbol ($ € £ ¥ ₹) carries a sign the same way ("-€12,345", "−£12,345").
# A spaced magnitude (1-3 blanks: "3.2 bn", "12.3 M", "9 K", "3.2 Mio", "3.2 MM") scales like
# "3.2 billion"; M/B/T as single uppercase letters only ("3.2 m", "3.2 MB" stay unscaled).
# Indian grouping ("9,34,567", "1,05,45,678") is read whole, by its value; a malformed group
# ("1,2345", "640,98765") makes the whole run unparseable (see _scan_numbers).
_NUM_RE: Final = re.compile(
    rf"(?:(?:(?<![\w.])(?P<lsign>[{_SIGNS}]))?(?<![A-Za-z])"
    rf"(?P<pre>(?:[A-Za-z]{{1,3}}\$|(?i:rs\.|{_PREFIXES})|{_UPPER_PREFIXES})"
    rf"(?=[{_SIGNS}{_CUR_SYMBOLS}\d])))?"
    rf"(?(pre)(?P<psign>[{_SIGNS}])?|(?:(?<![\w.])(?P<sign>[{_SIGNS}]))?)"
    rf"(?P<cur>[{_CUR_SYMBOLS}])?"
    r"(?<!\d)(?<!\d\.)(?<!\d:)"
    r"(?P<num>(?>\d{1,2},(?:\d{2},)++\d{3}(?!\d)(?:\.\d+)?"
    r"|\d{1,3}(?:,\d{3})+(?!\d)(?:\.\d+)?"
    rf"|\d{{1,3}}(?:[{_SEP}]\d{{3}})+(?!\d)(?:\.\d+)?"
    r"|\d+(?:\.\d+)?))(?!:\d)"
    r"(?P<exp>[eE][+-]?\d+)?"
    r"(?:(?P<suf>\s?%|\s?(?:percent|per\s?cent)\b|\s?percentage\s+points?\b|\s?pp\b"
    r"|\s{1,3}(?:(?i:million|billion|thousand|k|mln|bln|mn|bn|tn|mil)|[Mm]io|MM|mm|[MBT])\b)"
    r"|(?P<glue>[A-Za-z]++))?"
)
_COUNT_GLUES: Final = frozenset(
    "unit units pcs piece pieces item items order orders customer customers user users".split()
)
_CURRENCY_WORDS: Final = frozenset(
    "dollar dollars bucks euro euros yen pound pounds rupee rupees ruble rubles rouble roubles "
    "kr".split()
)
_MULT: Final = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "billion": 1e9,
    "bn": 1e9,
    "mn": 1e6,
    "mm": 1e6,
    "mln": 1e6,
    "mil": 1e6,
    "mio": 1e6,
    "bln": 1e9,
    "t": 1e12,
    "tn": 1e12,
    "x": 1.0,
}
_GLUE_PCT: Final = frozenset({"percent", "percentagepoint", "percentagepoints", "pp"})


def _is_prefix(glue: str) -> bool:
    """``glue`` is a currency prefix ("usd", "Rs", "TRY"): a figure glued after it is its own."""
    g = glue.lower()
    return g in _ISO_CODES or g in {"rs", "rm", "rp", "kr"} or glue in _UPPER_CODES


def _parse_glue(glue: str) -> tuple[str, str] | None:
    """Split a glued suffix into (magnitude, unit); None when it is not a known glue."""
    g = glue.lower()
    if g in _GLUE_PCT:  # glued "12percent"
        return g, ""
    for mult in sorted(_MULT, key=len, reverse=True):
        if g.startswith(mult):
            rest = g[len(mult) :]
            if rest == "" or (mult != "x" and _known_unit(rest, glue[len(mult) :])):
                return mult, rest
    return ("", g) if _known_unit(g, glue) else None


def _known_unit(u: str, shown: str = "") -> bool:
    """``u`` is the lowercased glue, ``shown`` its original case (ambiguous codes: UPPER only)."""
    return u in _ISO_CODES or u in _CURRENCY_WORDS or u in _COUNT_GLUES or shown in _UPPER_CODES


_NUMBER_WORDS: Final = re.compile(
    r"\b(?:eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty"
    r"|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundreds?|thousands?|millions?|billions?"
    r"|trillions?|dozens?|(?:one|two|three|four|five|six|seven|eight|nine|ten)\s+per\s?cent"
    r"|(?:one|two|three|four|five|six|seven|eight|nine|ten)\s+percent)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GroundingResult:
    text: str
    grounded: bool
    unmatched: tuple[str, ...]
    label_added: bool


@dataclass(frozen=True)
class _Num:
    raw: str
    value: float  # in base units (percent stays in percent points)
    tol: float  # half a unit of the shown precision, base units
    is_percent: bool
    either_sign: bool = False  # bare "(1,234)": a list item or a loss; read either way


@dataclass(frozen=True)
class _Scan:
    nums: list[_Num]
    truncated: bool
    bad: list[str]  # unparseable numerals: always unmatched
    residual: str  # the text with the matched numbers blanked out


_Date = tuple[int, int | None, int | None]  # (year, month or None, day or None)


def _month_index(name: str) -> int:
    n = name.lower()[:3]
    return next(i + 1 for i, m in enumerate(_MONTHS) if m.startswith(n))


def _dates_in(text: str) -> tuple[list[_Date], str]:
    """Return ((year, month, day), ...) found and the text with the dates blanked out."""
    found: list[_Date] = []

    def blank(m: re.Match[str]) -> str:
        return " " * len(m.group(0))

    out = text
    for rx in _DATE_RES[:2]:
        for m in rx.finditer(out):
            g = m.groups()
            found.append((int(g[0]), int(g[1]), int(g[2]) if len(g) > 2 else None))
        out = rx.sub(blank, out)
    for m in _DATE_RES[2].finditer(out):
        day = int(m.group(2)) if m.group(2) else None
        found.append((int(m.group(3)), _month_index(m.group(1)), day))
    out = _DATE_RES[2].sub(blank, out)
    for rx in _YEAR_RES:
        for m in rx.finditer(out):
            found.append((int(m.group("y")), None, None))
            if "y2" in rx.groupindex and m.group("y2"):
                found.append((int(m.group("y2")), None, None))
        out = rx.sub(blank, out)
    return found, out


def _in_window(d: _Date, window: tuple[date, date]) -> bool:
    year, month, day = d
    lo, hi = window
    if month is None:
        return lo.year <= year <= hi.year
    if not 1 <= month <= 12:
        return False
    if day is not None:
        if not 1 <= day <= 31:
            return False
        try:
            return lo <= date(year, month, day) <= hi
        except ValueError:
            return False
    return (lo.year, lo.month) <= (year, month) <= (hi.year, hi.month)


def _scan_numbers(text: str) -> _Scan:
    """Numbers shown in ``text`` (signed); see :class:`_Scan`. Capped at MAX_DRAFT_NUMBERS."""
    out: list[_Num] = []
    bad: list[str] = []
    spans: list[tuple[int, int]] = []
    truncated = False
    n = len(text)
    pos = 0
    # a manual search, not finditer: a malformed run is consumed whole and the scan resumes after
    # it, so a long "1,23,23,23..." is read once (linear), never re-entered at every group
    while (m := _NUM_RE.search(text, pos)) is not None:
        pos = end = m.end()
        raw_num = m.group("num")
        exp = m.group("exp")
        pre = m.group("pre")
        start = m.start("num")
        tail = m.end("exp") if exp else m.end("num")
        # a digit run joined by a group mark that no grouping explains ("1,2345", "7,1234",
        # "640,98765", "10:30,98765") is unparseable: the whole run is labelled, and no piece of
        # it is ever grounded or skipped as a small number on its own
        if (tail + 1 < n and text[tail] in _GROUP_MARKS and text[tail + 1].isdecimal()) or (
            start >= 2 and text[start - 1] in _GROUP_MARKS and text[start - 2].isdecimal()
        ):
            j = tail
            while j < n and (
                text[j].isdecimal()
                or (text[j] in _GROUP_MARKS + "." and j + 1 < n and text[j + 1].isdecimal())
            ):
                j += 1
            pos = end = max(j, m.end())
            bad.append(text[m.start() : end].strip())
            spans.append((m.start(), end))
            continue
        pure = raw_num.isdecimal()  # same as \d+ (Unicode digits)
        # "C$", "HK$" are currency prefixes, and a sign is never read after a letter
        signed = bool(m.group("lsign") or m.group("psign") or m.group("sign"))
        prefixed = bool(pre or m.group("cur") or signed)
        lead_glued = not prefixed and start > 0 and text[start - 1].isalpha()
        if lead_glued and pure:
            continue  # "A12345", "SKU12345": an identifier
        glue = m.group("glue")
        split = bool(glue) and end < n and text[end].isdecimal() and _is_prefix(glue)
        if split:
            # "640usd7": the letters open the next figure ("usd7"), not a unit of this one; the
            # number before them stays checked however small ("7USD2024": 7 is not prose)
            glue = None
            pos = end = m.start("glue")
        raw = text[m.start() : end].strip()
        suf = re.sub(r"\s", "", m.group("suf") or "").lower()
        unit = ""
        if glue:
            parsed = _parse_glue(glue)
            if parsed is None:
                if pure:
                    continue  # "7f3a91c2", "1990s", "10am": an identifier or prose
                parsed = ("", "?")  # grouped or decimal with an unknown glue: checked, scale 1
            suf, unit = parsed
        cur = bool(m.group("cur") or pre) or unit in _ISO_CODES or unit in _CURRENCY_WORDS
        cur = cur or unit.upper() in _UPPER_CODES  # only reached from an UPPERCASE glue
        unit_counts = bool(unit) and unit not in _COUNT_GLUES
        has_unit = (bool(suf) and suf != "x") or unit_counts or cur or bool(exp)
        digits = re.sub(rf"[,{_SEP}]", "", raw_num)
        try:
            base = Decimal(digits)
        except InvalidOperation:
            bad.append(raw)
            continue
        if not has_unit and not split and pure and base <= _SMALL_INT_MAX:
            continue
        if len(out) >= MAX_DRAFT_NUMBERS:
            truncated = True
            break
        spans.append((m.start(), end))
        e = int(exp[1:]) if exp else 0
        if abs(e) > _MAX_EXP or len(digits) > 40:
            bad.append(raw)
            continue
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        scale = _MULT.get(suf, 1.0) * 10.0**e
        value = float(base) * scale
        before = text[m.start() - 1] if m.start() else ""
        after = text[end] if end < n else ""
        is_pct = suf in _PCT_SUFFIXES
        # "($1,234)" is a loss; a bare "(1,234)" is as often a count in a list: either sign
        parens = before == "(" and after == ")" and not is_pct
        negative = signed or (parens and cur)
        out.append(
            _Num(
                raw,
                -value if negative else value,
                0.5 * 10 ** (-decimals) * scale,
                is_pct,
                parens and not negative,
            )
        )
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return _Scan(out, truncated, bad, "".join(chars))


def _finite(v: float) -> bool:
    return v == v and abs(v) != float("inf")


def _has(pool: Sequence[float], lo: float, hi: float) -> bool:
    i = bisect.bisect_left(pool, lo)
    return i < len(pool) and pool[i] <= hi


def _raw_pool(figures: Sequence[Mapping[str, Any]]) -> list[float]:
    """Finite ledger values, newest query first until MAX_RAW_VALUES, sorted for bisect."""
    vals: list[float] = []
    for f in reversed(figures):  # merge_figures keeps the newest last: a cap drops old turns
        for v in f.get("values", ()):
            if len(vals) >= MAX_RAW_VALUES:
                break
            if isinstance(v, int | float) and _finite(v):
                vals.append(float(v))
    vals.sort()
    return vals


def _raw_match(pool: Sequence[float], n: _Num) -> bool:
    """A ledger value shown as ``n``; x100 (a fraction) only when the draft carries ``%``."""
    if n.is_percent:
        t = n.tol * (1 + 1e-9)
        return _has(pool, n.value - t, n.value + t) or _has(
            pool, (n.value - t) / 100.0, (n.value + t) / 100.0
        )
    w = max(n.tol, abs(n.value) * REL_TOL) * (1 + 1e-9)
    return _has(pool, n.value - w, n.value + w)


def _id_like(name: str, col: Sequence[float]) -> bool:
    low = name.lower()
    if low == "id" or low.endswith("_id"):
        return True
    return (
        len(col) >= _ID_MIN_ROWS
        and all(float(v).is_integer() for v in col)
        and len(set(col)) == len(col)
    )


class _Derived:
    """Derived candidates of ONE check, computed once and sorted for bisect lookups.

    Scope (HLD 4.1 step 6): within one query only, identifier-like columns left out. Per column:
    a column total and the sum and difference of consecutive values and of first to last (no
    all-pairs: it made a number too easy to match, OD-1) in ``plain``; per row (same index
    across a query's columns) sum and difference. ``absolute`` holds the absolute value of
    differences (a drop is usually
    written as a positive number). For percentages ONLY: ``pct`` holds each value's share of the
    column total and the growth of consecutive values and of first to last (read x100 against
    the shown precision), ``pct_abs`` their absolute values.
    """

    def __init__(self, figures: Sequence[Mapping[str, Any]], expired: Callable[[], bool]) -> None:
        self.plain: list[float] = []
        self.absolute: list[float] = []
        self.pct: list[float] = []
        self.pct_abs: list[float] = []
        self.capped = False
        self.expired = False
        self._expired = expired
        for f in reversed(figures):  # newest last in the set: a cap drops the oldest turns
            if self._stop():
                break
            cols = [
                [v for v in col[:MAX_VALUES_PER_QUERY] if _finite(v)]
                for name, col in (f.get("columns") or {}).items()
                if not _id_like(str(name), [v for v in col[:MAX_VALUES_PER_QUERY] if _finite(v)])
            ]
            self._columns(cols)
            self._rows(cols)
        for pool in (self.plain, self.absolute, self.pct, self.pct_abs):
            pool.sort()

    def _stop(self) -> bool:
        if self._expired():
            self.expired = True
        return self.expired or self.capped

    def _room(self) -> bool:
        if len(self.plain) + len(self.absolute) + len(self.pct) >= MAX_CANDIDATES:
            self.capped = True
        return not self.capped

    def _pair(self, a: float, b: float) -> None:
        if self._room():
            self.plain += [a + b, a - b]
            self.absolute.append(abs(a - b))

    def _growth(self, new: float, old: float) -> None:
        if old != 0 and self._room():
            g = (new - old) / old
            self.pct.append(g)
            self.pct_abs.append(abs(g))

    def _columns(self, cols: Sequence[Sequence[float]]) -> None:
        for col in cols:
            if not col or self._stop():
                continue
            total = float(sum(col))
            self.plain.append(total)
            if total != 0:
                for v in col:
                    self.pct.append(v / total)
                    self.pct_abs.append(abs(v / total))
            for a, b in zip(col, col[1:], strict=False):
                self._pair(b, a)
                self._growth(b, a)
            self._pair(col[-1], col[0])
            self._growth(col[-1], col[0])

    def _rows(self, cols: Sequence[Sequence[float]]) -> None:
        for i in range(min((len(c) for c in cols), default=0)):
            if self._stop():
                return
            row = [c[i] for c in cols]
            for x, a in enumerate(row):
                for b in row[x + 1 :]:
                    self._pair(a, b)
                    self._pair(b, a)

    def match(self, n: _Num) -> bool:
        """Shown precision only: a derived value has no relative floor (OD-1)."""
        t = n.tol * (1 + 1e-9)
        if n.is_percent:
            pools = [self.pct] + ([self.pct_abs] if n.value > 0 else [])
            return any(_has(p, (n.value - t) / 100.0, (n.value + t) / 100.0) for p in pools)
        if _has(self.plain, n.value - t, n.value + t):
            return True
        return n.value > 0 and _has(self.absolute, n.value - t, n.value + t)


def _unparseable(text: str) -> list[str]:
    """Superscripts, subscripts and vulgar fractions are numerals this check cannot read."""
    return [c for c in text if unicodedata.category(c) in ("No", "Nl")]


def check_grounding(
    draft: str,
    figures: Sequence[Mapping[str, Any]],
    *,
    window: tuple[date, date],
    deadline_hit: Callable[[], bool] | None = None,
) -> GroundingResult:
    """Check ``draft`` against ``figures``; append ONE estimate label if anything is unmatched.

    Any estimate label the draft already carries is removed first and the label is decided from
    the unmatched numbers alone, so a model cannot pre-empt it. A draft with more than
    MAX_DRAFT_NUMBERS numbers, or a check that runs past ``deadline_hit``, is unverified and
    labelled (fail closed).
    """
    # ground what the user will see: the output guard decodes entities, Markdown escapes, NFKC
    # and invisibles, so "5\\,987" or "37.4\\%" must be read the same way here
    original = draft
    draft, stable = decode_for_display(draft)
    if not stable:
        # hand the ORIGINAL back (decoding it twice would hide the layers from the guard, which
        # blocks it as output_encoding)
        return GroundingResult(original, False, ("unstable_encoding",), False)
    draft = _strip_label(draft)
    if len(draft) > MAX_DRAFT_CHARS:
        return GroundingResult(draft.rstrip() + "\n\n" + ESTIMATE_LABEL, False, ("too_long",), True)
    expired = deadline_hit or (lambda: False)
    dates, remainder = _dates_in(draft)
    unmatched: list[str] = ["date" for d in dates if not _in_window(d, window)]
    weak = _weak_years(remainder, window)
    scan = _scan_numbers(remainder)
    unmatched += scan.bad
    unmatched += [f"unparseable:{c}" for c in _unparseable(draft)]
    unmatched += [m.group(0) for m in _NUMBER_WORDS.finditer(scan.residual)]
    if scan.truncated:
        unmatched.append("too_many_numbers")
    raw = _raw_pool(figures)
    derived: _Derived | None = None
    for n in scan.nums:
        if expired():
            unmatched.append("deadline")
            break
        if n.raw in weak or _raw_match(raw, n) or (n.either_sign and _raw_match(raw, _flip(n))):
            continue
        if derived is None:
            derived = _Derived(figures, expired)
            if derived.expired:
                unmatched.append("deadline")
                break
        if derived.match(n) or (n.either_sign and derived.match(_flip(n))):
            continue
        unmatched.append(n.raw)
    if not unmatched:
        return GroundingResult(draft, True, (), False)
    return GroundingResult(draft.rstrip() + "\n\n" + ESTIMATE_LABEL, False, tuple(unmatched), True)


def _flip(n: _Num) -> _Num:
    return replace(n, value=-n.value, either_sign=False)


def _weak_years(text: str, window: tuple[date, date]) -> set[str]:
    """In-window years after a weak date word ("in 2024 sales"): ambiguous, they pass as years."""
    out: set[str] = set()
    for m in _WEAK_YEAR_RE.finditer(text):
        for g in ("y", "y2"):
            y = m.group(g)
            if y and window[0].year <= int(y) <= window[1].year:
                out.add(y)
    return out


_LABEL_RE: Final = re.compile(
    r"\s+".join(re.escape(w) for w in ESTIMATE_LABEL.split()), re.IGNORECASE
)


def _strip_label(draft: str) -> str:
    """Remove every copy of the label (any case, any whitespace) and one adjacent bracket each.

    Linear: the label is found with one anchored pattern, the neighbouring blanks and brackets
    are trimmed by hand (a ``\\(?[ \\t]*LABEL`` regex is quadratic on a long run of blanks).
    """
    parts: list[str] = []
    pos = 0
    for m in _LABEL_RE.finditer(draft):
        start, end = m.span()
        lo = start
        while lo > pos and draft[lo - 1] in " \t":
            lo -= 1
        if lo > pos and draft[lo - 1] == "(":
            lo -= 1
            while lo > pos and draft[lo - 1] in " \t":
                lo -= 1
        hi = end
        while hi < len(draft) and draft[hi] in " \t":
            hi += 1
        if hi < len(draft) and draft[hi] == ")":
            hi += 1
        parts.append(draft[pos:lo])
        pos = hi
    if not parts:
        return draft
    parts.append(draft[pos:])
    return "".join(parts).rstrip()


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, int | float):
        return None
    return float(x)


def extract_figures(
    query_id: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> dict[str, Any]:
    """Figure group from a scrubbed result: numbers and ISO dates only, never strings."""
    values: list[float] = []
    cols: dict[str, list[float]] = {}
    dates: list[str] = []
    for row in rows[:MAX_VALUES_PER_QUERY]:
        for name, cell in zip(columns, row, strict=False):
            v = _num(cell)
            if v is not None:
                values.append(v)
                cols.setdefault(str(name), []).append(v)
            elif isinstance(cell, str) and re.fullmatch(r"\d{4}-\d{2}(-\d{2})?.*", cell):
                dates.append(cell[:10])
    return {
        "query_id": query_id,
        "values": values[:MAX_VALUES_PER_QUERY],
        "columns": {k: v[:MAX_VALUES_PER_QUERY] for k, v in list(cols.items())[:20]},
        "dates": dates[:MAX_VALUES_PER_QUERY],
    }


def merge_figures(
    old: Sequence[Mapping[str, Any]] | None, new: Sequence[Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """Reducer for the session figure set: replace by ``query_id``, keep the newest MAX_QUERIES."""
    merged: dict[str, dict[str, Any]] = {}
    for f in [*(old or ()), *(new or ())]:
        qid = str(f.get("query_id", ""))
        merged.pop(qid, None)
        merged[qid] = dict(f)
    return list(merged.values())[-MAX_QUERIES:]
