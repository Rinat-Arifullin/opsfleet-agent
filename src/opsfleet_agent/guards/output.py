"""Output guard (iteration 12, FR-75, AC-10.4/10.5/10.6, AC-08.2, HLD §4.0.5 and §5.2 layer 8).

The last code layer between a draft answer and the user. Pure and deterministic: no
LLM, no network, no I/O except the injected PII detector. One entry point,
:func:`check_output`, returns an :class:`OutputVerdict`.

Pipeline (each step sees the output of the previous one)
--------------------------------------------------------
1. **Action allowlist** (AC-10.4). The role that wrote the draft must be on the route of
   the router label, and every tool call recorded in the turn must be in the union of the
   tools of the roles on that route. Unknown role, label or tool fails closed.
   Result: **block** (``unexpected_action``).
   A draft longer than :data:`MAX_DRAFT_CHARS` is **blocked** (``output_too_long``).
2. **Normalise.** HTML entities (``&#47;``, ``&lt;``) and Markdown backslash escapes
   (``\\/``) are decoded, terminal control is dropped (ESC/C1 sequences such as OSC 8
   hyperlinks, OSC 52 clipboard writes and CSI, with their payload, then every C0/C1
   control and DEL except newline and tab; ``control_stripped``), then NFKC, then
   invisible format characters (Unicode ``Cf``: zero-width spaces, bidi controls) are
   removed; repeated until stable, so ``h​ttp``, ``https:&#47;&#47;`` or full-width dots
   cannot hide a URL or a phrase from the regexes below. A draft not stable after
   :data:`_MAX_DECODE_PASSES` passes is **blocked** (``output_encoding``): the renderer
   would decode the layer no check saw. The emitted text is the decoded one; at the end
   any ``&`` opening a character reference and any ``\\`` before punctuation are escaped
   again, so nothing can decode downstream.
3. **Injection scan** (AC-10.5/10.6): "ignore previous instructions"-style text, requests
   for the user's credentials or personal data, system-prompt leakage (disclosure phrasing
   plus the caller's protected snippets, matched punctuation-insensitively and per
   sentence) and a call to action pointing at a URL or domain ("visit evil.example").
   The scan also folds common Cyrillic/Greek homoglyphs to Latin.
   Result: **block** (``output_injection``).
4. **Images, links and HTML** (layer 8): Markdown images (inline, reference and shortcut)
   and ``<img>`` tags become ``<IMAGE>``. Markdown links keep their label, and the target
   is dropped unless it is allowed; reference definitions with a disallowed target are
   removed. Every other HTML tag (and comment marker) is removed (``html_stripped``),
   whatever the renderer does with HTML. Finally every remaining ``](`` and ``]:`` is
   neutralised with a space, so no Markdown link or reference definition the regexes
   missed (nested brackets, titles, a definition in a quote or list) can still render.
   Result: **redact** (``image_stripped``, ``url_stripped``).
5. **PII before URLs** (re-review N2): exact values from this turn's tool results (and any
   ``known_pii_values``), emails with an IP domain, and the 8a regex scrubber run first,
   so ``name@www.example.org`` becomes ``<EMAIL>``, not ``name@<URL>``.
6. **URLs**: any URL left (scheme URLs including ``hxxp``, ``http[:]//``, ``http:/host``,
   ``http:host`` and a scheme split across a line break, protocol-relative ``//host``,
   ``///host`` and ``\\\\host``, autolinks,
   ``mailto:``/``javascript:``/``data:`` URIs, ``www.``, IPv4 hosts, bare ``domain.tld``
   with ``[.]``/``(dot)`` obfuscation, and any ``label.tld`` followed by a path, port,
   query or fragment) becomes ``<URL>`` unless it is allowed. Host patterns are atomic and
   capped at :data:`_MAX_LABELS` labels, so obfuscated-dot runs stay linear (N3). URLs go
   before the NER so it never sees them (review L4). Result: **redact** (``url_stripped``).
7. **PII detector** (layer 8, AC-08.2): the exact-value masks of step 5 use typed tokens
   (D-9/D-13), so a value the detector would miss in the answer's context is still caught.
   Then :func:`opsfleet_agent.guards.pii.scrub_output` runs (8a regex scrubber again, then
   8b typed detector). Result: **redact** (``pii_redacted``).

Block vs. redact (owner decision pending)
-----------------------------------------
HLD §4.0.5 says an injection hit withholds the answer; the §6 failure table says the span
is removed. This module **blocks** on injection text (step 3), because an answer that
repeats instructions from the data was produced by a model that read them, and the
rest of it cannot be trusted either. URLs and images are **redacted** (layer 8 says
"strip"), since a stray link in otherwise good analysis is not by itself a sign that the
model was steered.

URL allowlist
-------------
A URL is allowed only when its host is in the operator's :data:`STATIC_URL_ALLOWLIST`
(empty in the prototype). URLs in tool results are **not** trusted: thelook has no URL
columns, so any URL in a result was produced by the model's own SQL (review HIGH-1,
owner decision OD-1). Bare domains with nothing after them use a
fixed TLD list, not "any word after a dot", so decimals (``12.5``), ``e.g.``, ``U.S.``
and ``table.column`` references survive; ``thelook.com`` is a domain and is stripped.

Fail closed
-----------
Any exception (``PiiDetectorError``, ``TypeError``, a bad argument, anything else) gives
:data:`REFUSAL_TEXT` and an ``output_guard_error`` event, never the draft. A non-``str``
draft does the same. Events and logs carry rule codes, roles, labels, tool names, types
and counts only, never text from the draft or the tool results.

Out of scope here: the scope-label and definitions checks (FR-16) and grounding; the
graph node, audit record and alert wiring land in iteration 14a.
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from opsfleet_agent.guards import pii, pii_regex
from opsfleet_agent.guards.pii import MaskResult, PiiDetector

__all__ = [
    "IMAGE_TOKEN",
    "LABEL_ROUTES",
    "REFUSAL_TEXT",
    "ROLE_TOOLS",
    "STATIC_URL_ALLOWLIST",
    "URL_TOKEN",
    "OutputEvent",
    "OutputVerdict",
    "check_output",
]

logger = logging.getLogger(__name__)

# HLD §6 failure table; the caller appends the trace id ("(trace <id>)") if it has one.
REFUSAL_TEXT: Final = "Something went wrong preparing the answer. Please ask again."
URL_TOKEN: Final = "<URL>"
IMAGE_TOKEN: Final = "<IMAGE>"

# Rule codes (AC-16.1 / AC-28.3).
OUTPUT_TOO_LONG: Final = "output_too_long"
HTML_STRIPPED: Final = "html_stripped"
UNEXPECTED_ACTION: Final = "unexpected_action"
OUTPUT_INJECTION: Final = "output_injection"
OUTPUT_ENCODING: Final = "output_encoding"  # still encoded after the decode cap (P1)
CONTROL_STRIPPED: Final = "control_stripped"  # terminal control characters removed (T1)
OUTPUT_GUARD_ERROR: Final = "output_guard_error"
PII_REDACTED: Final = "pii_redacted"
URL_STRIPPED: Final = "url_stripped"
IMAGE_STRIPPED: Final = "image_stripped"

# --- action allowlist (HLD §4.0.2) ---------------------------------------------------

_SQL_TOOLS: Final = frozenset({"list_tables", "get_schema", "run_sql"})
_LIBRARY_TOOLS: Final = frozenset(
    {
        "save_report",
        "list_reports",
        "search_reports",
        "view_report",
        "rename_report",
        "export_report",
        "delete_reports",
        "set_preference",
    }
)

# Model-callable tools per answer-writing role. Internal code paths (search_golden,
# save_generated_report, confirm_delete/execute_delete) are not model tool calls and are
# not accepted here: if they are ever recorded as one, the turn fails closed.
ROLE_TOOLS: Final[Mapping[str, frozenset[str]]] = {
    "light_path": frozenset(),
    "quick_analyst": _SQL_TOOLS,
    "deep_analyst": _SQL_TOOLS,
    "force_answer": frozenset(),
    "report_writer": frozenset(),
    "report_verifier": frozenset(),
    "library_agent": _LIBRARY_TOOLS,
}

_QA_ROUTE: Final = frozenset({"quick_analyst", "deep_analyst", "force_answer"})

# Roles that may write the answer for each router label (HLD §4.0.3). ``simple`` includes
# the Deep analyst because Quick escalates once per turn. ``off_topic`` and ``injection``
# get a templated refusal from code, so no model draft is ever expected for them.
LABEL_ROUTES: Final[Mapping[str, frozenset[str]]] = {
    "simple": _QA_ROUTE,
    "complex": frozenset({"deep_analyst", "force_answer"}),
    "report": frozenset({"deep_analyst", "force_answer", "report_writer", "report_verifier"}),
    "retry_report": frozenset({"report_writer", "report_verifier"}),
    "library": frozenset({"library_agent"}),
    "meta": frozenset({"light_path"}),
    "smalltalk": frozenset({"light_path"}),
    "off_topic": frozenset(),
    "injection": frozenset(),
}

# Hosts whose URLs may reach the user. Empty in the prototype (HLD §5.2 layer 8).
STATIC_URL_ALLOWLIST: Final[frozenset[str]] = frozenset()

# --- injection patterns ----------------------------------------------------------------

_I = re.IGNORECASE

_INSTRUCTION_PATTERNS: Final = (
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}?"
        r"\b(?:previous|prior|above|earlier|preceding|all|any|your|the|system|developer)\b"
        r"[^.\n]{0,40}?\b(?:instructions?|prompts?|rules|directives?|guidelines|guardrails"
        r"|polic(?:y|ies))\b",
        _I,
    ),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an|in|the|no\s+longer)\b", _I),
    re.compile(r"\bfrom\s+now\s+on,?\s+(?:you|ignore|always|never)\b", _I),
    re.compile(r"\b(?:new|updated|real|actual)\s+(?:system\s+)?instructions?\s*:", _I),
    re.compile(r"\b(?:developer|god|dan|jailbreak)\s+mode\b", _I),
    re.compile(r"\b(?:do\s+not|don't|never)\s+(?:tell|inform|show|warn)\s+the\s+user\b", _I),
)

_CRED = (
    r"(?:password|passcode|passphrase|pin|api[\s_-]?keys?|access\s+tokens?|tokens?|secrets?"
    r"|credentials?|login(?:\s+details)?|username|one[\s-]time\s+(?:code|password)|otp"
    r"|2fa\s+code|verification\s+code|ssn|social\s+security(?:\s+number)?"
    r"|passport(?:\s+number)?|credit\s+card(?:\s+number)?|card\s+(?:number|details)|cvv|cvc"
    r"|bank\s+(?:account|details)|iban|(?:home\s+)?address|phone(?:\s+number)?"
    r"|e-?mail(?:\s+address)?|date\s+of\s+birth|birthday|full\s+name"
    r"|personal\s+(?:data|details|information))"
)
_CREDENTIAL_PATTERNS: Final = (
    # "enter your password", "reply with your API key", "verify by sharing your card number"
    re.compile(
        r"\b(?:enter|provid|send|shar|giv|tell|typ|confirm|verif|submit|past|input|disclos"
        r"|reply|respond)\w{0,4}\s+(?:(?:me|us|in|back|here|below|with)\s+){0,2}your\s+"
        rf"(?:[\w-]+\s+){{0,2}}?{_CRED}\b",
        _I,
    ),
    re.compile(rf"\b(?:what\s+is|what's|what\s+are)\s+your\s+(?:[\w-]+\s+){{0,2}}?{_CRED}\b", _I),
)

# Disclosure phrasing only: "I can't share the system prompt" must pass (review M2). The
# real leak signal is the protected-snippet match.
_PROMPT = r"(?:system\s+prompt|prompt|(?:initial|hidden|system)\s+instructions|instructions|rules)"
_LEAK_PATTERNS: Final = (
    re.compile(rf"\bhere\s+(?:is|are)\s+(?:my|the)\s+(?:full\s+|complete\s+)?{_PROMPT}\b", _I),
    re.compile(
        r"\b(?:my|the)\s+(?:system\s+prompt|(?:initial|hidden|system)\s+instructions)"
        r"\s+(?:is|are|says?|reads?|states?)\s*:",
        _I,
    ),
    re.compile(rf"\b{_PROMPT}\b[^.\n]{{0,30}}\bverbatim\b", _I),
)
MIN_PROTECTED_SNIPPET_CHARS: Final = 20

_CALL_TO_ACTION: Final = re.compile(
    r"(?:visit|go\s+to|click|open|navigate\s+to|browse\s+to|download|log\s*in|sign\s*in"
    r"|head\s+(?:over\s+)?to|follow)\b[^.\n]{0,30}$",
    _I,
)
_CTA_WINDOW: Final = 64  # the pattern above spans at most ~50 chars before the URL
# Homoglyphs folded to Latin for the injection scan only (review L1). Not a full
# confusables table: the common Cyrillic and Greek look-alikes.
_CONFUSABLES: Final = str.maketrans(
    "аеорсухіјѕԁԛԝАВЕКМНОРСТХІЈЅαβεικνορτυχΑΒΕΖΗΙΚΜΝΟΡΤΥΧ",
    "aeopcyxijsdqwABEKMHOPCTXIJSabeiknoptuxABEZHIKMNOPTYX",
)

# --- URL and image patterns ------------------------------------------------------------

_DOT = r"(?:\.|\s?(?:\[\s?\.\s?\]|\(\s?\.\s?\)|\{\s?\.\s?\}|\[\s?dot\s?\]|\(\s?dot\s?\))\s?)"
# A fixed TLD list (not "any word") keeps decimals, "e.g.", "U.S." and table.column intact.
# Two-letter TLDs that are common English words (in, is, it, at, to, me, us, ...) are left
# out on purpose.
_TLDS = (
    r"com|net|org|info|biz|io|ai|app|dev|xyz|top|site|online|link|click|shop|store|live"
    r"|page|cloud|tech|pro|ru|cn|uk|de|fr|eu|tk|ml|ga|cf|gq|ly|gl|gg|cc|tv|ws|co|su|pw"
    r"|onion|edu|gov|mil|example|test|invalid|localhost|local"
)
# Any label.tld (2-24 letters, IDN included, or an xn-- label) is a URL when a path, port,
# query or fragment follows (review M1); "zip", "mov" and "sh" are only caught this way.
_LABEL = r"[^\W_](?:[\w\-]{0,62}[^\W_])?"
_ANY_TLD = r"(?:[^\W\d_]{2,24}|xn--[\w\-]{1,59})"
# Label repetition is capped and atomic: "a[.]a[.]..." no longer backtracks across the
# whole run from every start position (review N3, quadratic before).
_MAX_LABELS = 10
_HOST = rf"(?>{_LABEL}{_DOT}){{1,{_MAX_LABELS}}}{_ANY_TLD}"
_URL_TAIL = r"[^\s<>\"'`]*"
_URL_PATTERNS: Final = (
    # autolink <scheme:...> / <www...> (whole, brackets included)
    re.compile(rf"<(?:[a-z][a-z0-9+.\-]{{1,15}}:|www{_DOT})[^\s<>]*>", _I),
    # scheme://..., including hxxp://, http[:]//, a scheme split across a line break and
    # backslashes (browsers read "\" as "/" in http URLs)
    re.compile(
        rf"\b[a-z][a-z0-9+.\-]{{1,15}}(?::|\[:\])\s{{0,3}}[/\\]\s{{0,3}}[/\\]{_URL_TAIL}", _I
    ),
    # "http:/host": browsers resolve one slash after a special scheme to a host (N1)
    re.compile(rf"\b(?:h[tx]{{2}}ps?|ftps?|wss?|file)(?::|\[:\])[/\\](?=\w){_URL_TAIL}", _I),
    # "http:evil.bank" with no slash at all: browsers resolve it too (re-review L2)
    re.compile(rf"\b(?:h[tx]{{2}}ps?|ftps?|wss?|file)(?::|\[:\])(?=\w){_URL_TAIL}", _I),
    # protocol-relative //host, ///host, \\host (review HIGH-2, N1)
    re.compile(rf"(?<![\w:/\\])[/\\]{{2,}}{_HOST}(?::\d{{1,5}})?{_URL_TAIL}", _I),
    re.compile(rf"\b(?:mailto|javascript|vbscript)\s{{0,3}}:{_URL_TAIL}", _I),
    re.compile(rf"\bdata\s{{0,3}}:\s{{0,3}}[a-z]+/{_URL_TAIL}", _I),
    re.compile(rf"\bwww{_DOT}{_URL_TAIL}", _I),
    re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])(?::\d{1,5})?(?:/[^\s<>\"'`]*)?"),
    re.compile(
        rf"(?<![@\w.\-/])(?>[a-z0-9](?:[a-z0-9\-]{{0,62}}[a-z0-9])?{_DOT}){{1,{_MAX_LABELS}}}"
        rf"(?:{_TLDS})\b"
        rf"(?::\d{{1,5}})?(?:/{_URL_TAIL})?",
        _I,
    ),
    re.compile(rf"(?<![@\w.\-/]){_HOST}(?=[/?#]|:\d)(?::\d{{1,5}})?{_URL_TAIL}", _I),
)
# "Navigate to evil.bank" with no path: a domain with any TLD right after a navigation verb.
_CTA_DOMAIN: Final = re.compile(
    rf"\b(?:visit|go\s+to|navigate\s+to|browse\s+to|head\s+(?:over\s+)?to"
    rf"|(?:log|sign)\s*in\s+(?:at|on|to))\s+(?:the\s+)?(?:site\s+|page\s+)?{_HOST}\b",
    _I,
)
_TRAILING_PUNCT = ".,;:!?)]}'\""

_MD_IMAGES: Final = (
    re.compile(r"!\[[^\]\n]{0,500}\]\([^)\n]{0,2000}\)"),  # ![alt](url)
    re.compile(r"!\[[^\]\n]{0,500}\]\[[^\]\n]{0,200}\]"),  # ![alt][ref]
    re.compile(r"!\[[^\]\n]{0,500}\]"),  # ![ref] shortcut
    re.compile(r"<img\b[^<>]*>", _I),
)
# HTML tags other than <img> (already an image) and autolinks (<scheme:...>, <www....>):
# a tag name followed by whitespace, "/" or ">" (review HIGH-2, OD-8). No length cap on the
# attributes (a long title must not hide an href, re-review N1); "[^<>]*" stops at the next
# "<", so the scan stays linear. Comment, CDATA and declaration markers are removed on
# their own: an unclosed "<!--" would otherwise hide the rest of the answer, and matching
# open to close is quadratic on "<!--" runs.
_HTML_TAG: Final = re.compile(
    r"<!--|--!?>|<!\[CDATA\[|\]\]>|<[!?][^<>]*>|</?[a-z][a-z0-9\-]{0,30}(?=[\s/>])[^<>]*>", _I
)
# Whatever link syntax survives the two patterns below (nested brackets, titles,
# balanced parens, a definition in a quote or list, a target on the next line) is
# neutralised structurally: CommonMark needs "](" for an inline link and "]:" for a
# definition, with nothing in between (re-review N1).
_MD_LINK_OPENER: Final = re.compile(r"\](?=[(:])")
# Backslash escapes CommonMark honours (any ASCII punctuation).
_MD_ESCAPE: Final = re.compile(r"\\([!-/:-@\[-`{-~])")
_MAX_DECODE_PASSES: Final = 4
# "local@1.2.3.4": the regex scrubber needs a letter TLD, so an IP-host email is masked here.
_EMAIL_IP: Final = re.compile(
    r"(?<![\w.+%-])[\w.+%-]{1,64}@\[?\d{1,3}(?:\.\d{1,3}){3}\]?(?!\w|\.\d)"
)
# CommonMark reference definition: [label]: target
_MD_REF_DEF: Final = re.compile(
    r"(?m)^[ \t]{0,3}\[[^\[\]\n]{1,500}\]:[ \t]*<?([^\s>]{1,2000})>?.*$"
)
# Neither label nor target can contain a bracket, so "[a](" runs stay linear (review M3);
# a target with brackets is not a link here and its URL is stripped by the URL step.
_MD_LINK: Final = re.compile(
    r"\[([^\[\]\n]{0,500})\]\(\s*<?([^()\[\]\s<>]{0,2000})>?(?:\s+\"[^\"\n]*\")?\s*\)"
)

# --- exact-value masking -----------------------------------------------------------------

MAX_DRAFT_CHARS: Final = pii_regex.MAX_SCRUB_CHARS  # longer drafts are blocked (review M3)
MIN_EXACT_VALUE_CHARS: Final = 3
MAX_EXACT_VALUES: Final = 5_000
_CHUNK_CHARS: Final = 90_000  # below pii_regex.MAX_SCRUB_CHARS so mask() never truncates
MAX_TOOL_RESULT_CHUNKS: Final = 64


@dataclass(frozen=True)
class OutputEvent:
    """One guard finding. ``detail`` never contains draft or tool-result text."""

    code: str
    detail: str = ""


@dataclass(frozen=True)
class OutputVerdict:
    allowed: bool
    text: str
    events: tuple[OutputEvent, ...] = ()

    def codes(self) -> set[str]:
        return {e.code for e in self.events}


def check_output(
    draft: str,
    *,
    role: str,
    label: str,
    tool_calls: Sequence[str] = (),
    tool_results: Sequence[str] = (),
    known_pii_values: Mapping[str, str] | None = None,
    protected_snippets: Iterable[str] = (),
    detector: PiiDetector | None = None,
) -> OutputVerdict:
    """Check a draft answer before it is shown. Never raises; fails closed.

    ``tool_calls`` are the tool names recorded in this turn (all roles). ``tool_results``
    are this turn's raw tool-result payloads as text; their PII values are masked exactly
    (their URLs are not trusted: only :data:`STATIC_URL_ALLOWLIST` hosts are kept).
    ``known_pii_values`` maps extra values to a type in
    :data:`opsfleet_agent.guards.pii.ENTITY_TYPES`. ``protected_snippets`` are system-
    prompt passages that must never be reproduced (each at least
    :data:`MIN_PROTECTED_SNIPPET_CHARS` characters; a shorter one is a configuration error
    and fails closed). ``detector`` defaults to :func:`pii.default_detector`.
    """
    try:
        return _check(
            draft,
            role=role,
            label=label,
            tool_calls=tool_calls,
            tool_results=tool_results,
            known_pii_values=known_pii_values or {},
            protected_snippets=protected_snippets,
            detector=detector,
        )
    except Exception as exc:  # fail closed on anything, including PiiDetectorError
        logger.error("output guard failed closed: %s", type(exc).__name__)
        return _block(OutputEvent(OUTPUT_GUARD_ERROR, type(exc).__name__))


def _block(*events: OutputEvent) -> OutputVerdict:
    return OutputVerdict(allowed=False, text=REFUSAL_TEXT, events=tuple(events))


def _check(
    draft: str,
    *,
    role: str,
    label: str,
    tool_calls: Sequence[str],
    tool_results: Sequence[str],
    known_pii_values: Mapping[str, str],
    protected_snippets: Iterable[str],
    detector: PiiDetector | None,
) -> OutputVerdict:
    if not isinstance(draft, str):
        raise TypeError("draft must be str")
    for value in (tool_calls, tool_results):
        if isinstance(value, (str, bytes, Mapping)):  # a dict would iterate its keys (L6)
            raise TypeError("tool_calls and tool_results must be sequences of str")
    if len(draft) > MAX_DRAFT_CHARS:
        logger.warning("output guard: %s chars=%d", OUTPUT_TOO_LONG, len(draft))
        return _block(OutputEvent(OUTPUT_TOO_LONG, f"max={MAX_DRAFT_CHARS}"))
    results = [_require_str(r, "tool_results") for r in tool_results]

    unexpected = _unexpected_actions(role, label, tool_calls)
    if unexpected:
        logger.warning("output guard: %s role=%s label=%s", UNEXPECTED_ACTION, role, label)
        return _block(*unexpected)

    text, stable, n_controls = _decode(draft)
    if not stable:
        logger.warning("output guard: %s passes=%d", OUTPUT_ENCODING, _MAX_DECODE_PASSES)
        return _block(OutputEvent(OUTPUT_ENCODING, f"max_passes={_MAX_DECODE_PASSES}"))
    if len(text) > MAX_DRAFT_CHARS:  # NFKC can expand; the scrubbers must not truncate
        logger.warning("output guard: %s chars=%d", OUTPUT_TOO_LONG, len(text))
        return _block(OutputEvent(OUTPUT_TOO_LONG, f"max={MAX_DRAFT_CHARS}"))
    hits = _injection_hits(text, _snippets(protected_snippets))
    if hits:
        logger.warning("output guard: %s kinds=%s", OUTPUT_INJECTION, ",".join(hits))
        return _block(*(OutputEvent(OUTPUT_INJECTION, kind) for kind in hits))

    events: list[OutputEvent] = []
    if n_controls:
        events.append(OutputEvent(CONTROL_STRIPPED, f"count={n_controls}"))
    text, n_images, n_tags, n_links = _strip_images_html_links(text, _url_allowed)
    if n_images:
        events.append(OutputEvent(IMAGE_STRIPPED, f"count={n_images}"))
    if n_tags:
        events.append(OutputEvent(HTML_STRIPPED, f"count={n_tags}"))

    # Exact values and the regex scrubber run before the URL strip, so the host of an
    # e-mail is not taken for a URL and its local part left behind (re-review N2). The NER
    # runs after it, so it never sees (and splits) a URL (review L4).
    detector = detector or pii.default_detector()
    values = _tool_result_pii(results, detector)
    for value, kind in known_pii_values.items():
        if kind not in pii.ENTITY_TYPES:
            raise ValueError("unknown PII type in known_pii_values")
        values.setdefault(_value_key(_require_str(value, "known_pii_values")), kind)
    text, exact_types = _mask_exact(text, values)
    text, n_ip_emails = _EMAIL_IP.subn(pii.TOKENS[pii.EMAIL], text)
    if len(text) > MAX_DRAFT_CHARS:  # neutralising "](" adds a space; never truncate
        logger.warning("output guard: %s chars=%d", OUTPUT_TOO_LONG, len(text))
        return _block(OutputEvent(OUTPUT_TOO_LONG, f"max={MAX_DRAFT_CHARS}"))
    scrubbed = pii_regex.scrub(text)
    text = scrubbed.text
    # 8a hits found here are reported with the detector's (scrub_output = 8a + 8b).
    regex_types = {k for k, n in scrubbed.findings.items() if n and k in pii.ENTITY_TYPES}
    if n_ip_emails:
        regex_types.add(pii.EMAIL)

    text, n_urls = _strip_urls(text, _url_allowed)
    n_urls += n_links
    if len(text) > MAX_DRAFT_CHARS:  # pragma: no cover - tokens are not longer than URLs
        raise ValueError("draft would be truncated by the scrubber")

    masked = pii.scrub_output(text, detector)
    text = masked.text
    detector_types = set(masked.types()) | regex_types
    for source, types in (("tool_result", exact_types), ("detector", detector_types)):
        if types:
            detail = f"source={source} types={','.join(sorted(types))}"
            events.append(OutputEvent(PII_REDACTED, detail))

    if n_urls:
        events.append(OutputEvent(URL_STRIPPED, f"count={n_urls}"))
    # Nothing the renderer could still decode survives (re-review P1, defence in depth).
    text = _RENDER_DECODABLE.sub(lambda m: "&amp;" if m.group() == "&" else "\\\\", text)
    if len(text) > MAX_DRAFT_CHARS:
        logger.warning("output guard: %s chars=%d", OUTPUT_TOO_LONG, len(text))
        return _block(OutputEvent(OUTPUT_TOO_LONG, f"max={MAX_DRAFT_CHARS}"))
    if events:
        logger.info("output guard redacted: %s", ",".join(sorted({e.code for e in events})))
    return OutputVerdict(allowed=True, text=text, events=tuple(events))


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} items must be str")
    return value


# --- step 1: action allowlist --------------------------------------------------------------


def _unexpected_actions(role: str, label: str, tool_calls: Sequence[str]) -> list[OutputEvent]:
    route = LABEL_ROUTES.get(label) if isinstance(label, str) else None
    if route is None:
        return [OutputEvent(UNEXPECTED_ACTION, "unknown_label")]
    if not isinstance(role, str) or role not in ROLE_TOOLS:
        return [OutputEvent(UNEXPECTED_ACTION, "unknown_role")]
    if role not in route:
        return [OutputEvent(UNEXPECTED_ACTION, f"role_not_on_route role={role} label={label}")]
    allowed = frozenset().union(*(ROLE_TOOLS[r] for r in route))
    events = []
    for call in tool_calls:
        if not isinstance(call, str):
            events.append(OutputEvent(UNEXPECTED_ACTION, "malformed_tool_call"))
        elif call not in allowed:
            # Tool names from the known set are safe to log; anything else is not echoed.
            name = call if call in _SQL_TOOLS | _LIBRARY_TOOLS else "unknown"
            events.append(OutputEvent(UNEXPECTED_ACTION, f"tool={name} role={role} label={label}"))
    return events


# --- step 2/3: normalise and scan --------------------------------------------------------


# Terminal control (re-review T1): ESC/C1 string sequences (OSC 8 hyperlinks, OSC 52
# clipboard writes, DCS/PM/APC/SOS) are dropped with their payload, CSI and other ESC
# sequences with their parameters, then every remaining C0/C1 control and DEL except
# "\n" and "\t". Each pattern is a single run of a negated class: linear.
_TERMINAL_CONTROL: Final = re.compile(
    r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f])[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|\x1b[ -/]*[0-~]?"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)
# What a Markdown renderer would still decode after the fixpoint (re-review P1, defence in
# depth): "&" opening a character reference and "\" before ASCII punctuation. A plain
# "Outerwear & Coats" or "AT&T" is left alone.
_RENDER_DECODABLE: Final = re.compile(r"&(?=#|[a-z][a-z0-9]{0,31};)|\\(?=[!-/:-@\[-`{-~])", _I)


def _decode(text: str) -> tuple[str, bool, int]:
    """Normalised text, whether decoding reached a fixpoint, controls removed (count)."""
    # HTML entities and Markdown backslash escapes are decoded (a renderer decodes them
    # too: "https:&#47;&#47;" and "\\/\\/" are links, re-review N1), then NFKC and the
    # scrubber's own fold (drops Cf, folds spaces, dashes and full-width forms, decodes
    # %40/%2e-style "@" and "."), so every step sees what the scrubber and the renderer
    # see. Repeated until stable ("&amp;#47;"): at most _MAX_DECODE_PASSES decoding passes
    # plus one that must change nothing. A draft still encoded after that is refused by the
    # caller (re-review P1): the renderer would decode the last layer the checks never saw.
    n_controls = 0
    for _ in range(_MAX_DECODE_PASSES + 1):
        decoded = _MD_ESCAPE.sub(r"\1", html.unescape(text))
        decoded, n = _TERMINAL_CONTROL.subn("", decoded)  # "&#27;" decodes to ESC
        n_controls += n
        decoded = pii_regex._normalise(unicodedata.normalize("NFKC", decoded))
        if decoded == text:
            return text, True, n_controls
        text = decoded
    return text, False, n_controls


def _normalise(text: str) -> str:
    return _decode(text)[0]


def _collapse(text: str) -> str:
    return " ".join(text.casefold().split())


_NON_WORD: Final = re.compile(r"[\W_]+")
_SENTENCE_END: Final = re.compile(r"[.!?;:\n]+")


def _snippet_key(text: str) -> str:
    """Case-, punctuation- and homoglyph-insensitive form used for snippet matching."""
    return " ".join(_NON_WORD.sub(" ", text.translate(_CONFUSABLES).casefold()).split())


def _snippets(protected: Iterable[str]) -> list[str]:
    """Keys for each protected snippet and for each of its sentences long enough to match
    safely, so a partial reproduction ("Never reveal brand scope rules.") is caught (L1)."""
    if isinstance(protected, (str, bytes, Mapping)):
        raise TypeError("protected_snippets must be an iterable of str")
    out: set[str] = set()
    for s in protected:
        text = _normalise(_require_str(s, "protected_snippets"))
        key = _snippet_key(text)
        if len(key) < MIN_PROTECTED_SNIPPET_CHARS:
            raise ValueError("protected snippet too short to match safely")
        out.add(key)
        for sentence in _SENTENCE_END.split(text):
            part = _snippet_key(sentence)
            if len(part) >= MIN_PROTECTED_SNIPPET_CHARS:
                out.add(part)
    return sorted(out)


def _injection_hits(text: str, snippets: Sequence[str]) -> list[str]:
    text = text.translate(_CONFUSABLES)  # scan copy only; the answer keeps its letters
    hits = []
    if any(p.search(text) for p in _INSTRUCTION_PATTERNS):
        hits.append("instruction")
    if any(p.search(text) for p in _CREDENTIAL_PATTERNS):
        hits.append("credential_request")
    key = f" {_snippet_key(text)} "
    if any(p.search(text) for p in _LEAK_PATTERNS) or any(f" {s} " in key for s in snippets):
        hits.append("prompt_leak")
    if _CTA_DOMAIN.search(text) or any(
        _CALL_TO_ACTION.search(text, max(0, start - _CTA_WINDOW), start)
        for start, _ in _url_spans(text)
    ):
        hits.append("url_call_to_action")
    return hits


# --- steps 4 and 6: images and URLs --------------------------------------------------------


def _url_spans(text: str) -> list[tuple[int, int]]:
    """Non-overlapping URL spans, earliest and longest first, trailing punctuation trimmed."""
    found: list[tuple[int, int]] = []
    for pattern in _URL_PATTERNS:
        for m in pattern.finditer(text):
            start, end = m.span()
            if not m.group(0).startswith("<"):
                while end > start and text[end - 1] in _TRAILING_PUNCT:
                    end -= 1
            if end > start:
                found.append((start, end))
    found.sort(key=lambda s: (s[0], -s[1]))
    spans: list[tuple[int, int]] = []
    for start, end in found:
        if spans and start < spans[-1][1]:
            continue
        spans.append((start, end))
    return spans


def _url_key(url: str) -> str:
    return url.strip("<>").casefold().rstrip("/")


def _host(url: str) -> str:
    # Scheme with one or two slashes or backslashes ("http:/h", "http:\\h"), then "//h".
    rest = re.sub(r"^[a-z][a-z0-9+.\-]*(?::|\[:\])\s*[/\\]", "", _url_key(url), flags=_I)
    return re.split(r"[/\\:?#\s]", rest.lstrip("/\\ "), maxsplit=1)[0]


def _url_allowed(url: str) -> bool:
    """Only operator-allowlisted hosts survive. URLs from tool results are not trusted: a
    row value is attacker-controllable and can carry data out in its query (HIGH-1/OD-1)."""
    return bool(STATIC_URL_ALLOWLIST) and _host(url) in STATIC_URL_ALLOWLIST


_KEEP_TAGS: Final = frozenset({URL_TOKEN, IMAGE_TOKEN, *pii.TOKENS.values()})


def _strip_images_html_links(
    text: str, is_allowed: Callable[[str], bool]
) -> tuple[str, int, int, int]:
    n_images = 0
    for pattern in _MD_IMAGES:
        text, n = pattern.subn(IMAGE_TOKEN, text)
        n_images += n
    n_tags = 0

    def tag(m: re.Match[str]) -> str:
        nonlocal n_tags
        if m.group(0).upper() in _KEEP_TAGS:  # our own placeholders look like tags
            return m.group(0)
        n_tags += 1
        return ""

    text = _HTML_TAG.sub(tag, text)
    n_links = 0

    def ref_def(m: re.Match[str]) -> str:
        nonlocal n_links
        if is_allowed(m.group(1)):
            return m.group(0)
        n_links += 1
        return ""

    def link(m: re.Match[str]) -> str:
        nonlocal n_links
        label, target = m.group(1), m.group(2)
        if target and is_allowed(target):
            return m.group(0)
        n_links += 1
        return label

    text = _MD_REF_DEF.sub(ref_def, text)
    text = _MD_LINK.sub(link, text)
    text, n = _MD_LINK_OPENER.subn("] ", text)  # fail closed: no link syntax survives
    return text, n_images, n_tags, n_links + n


def _strip_urls(text: str, is_allowed: Callable[[str], bool]) -> tuple[str, int]:
    parts: list[str] = []
    last = 0
    count = 0
    for start, end in _url_spans(text):
        if is_allowed(text[start:end]):
            continue
        parts.append(text[last:start])
        parts.append(URL_TOKEN)
        last = end
        count += 1
    parts.append(text[last:])
    return "".join(parts), count


# --- step 5: exact-value PII masking --------------------------------------------------------


def _value_key(value: str) -> str:
    return _collapse(_normalise(value))


def _chunks(text: str) -> list[str]:
    """Split on line ends into pieces ``mask()`` will not truncate (bounded)."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        while len(line) > _CHUNK_CHARS:  # one huge line: hard split
            chunks.append(line[:_CHUNK_CHARS])
            line = line[_CHUNK_CHARS:]
        if size + len(line) > _CHUNK_CHARS:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
        if len(chunks) > MAX_TOOL_RESULT_CHUNKS:
            raise ValueError("tool results too large for exact-value masking")
    if current:
        chunks.append("".join(current))
    return chunks


def _tool_result_pii(results: Sequence[str], detector: PiiDetector) -> dict[str, str]:
    values: dict[str, str] = {}
    chunks = [c for r in results for c in _chunks(r)]
    if len(chunks) > MAX_TOOL_RESULT_CHUNKS:
        raise ValueError("tool results too large for exact-value masking")
    for chunk in chunks:
        for value, kind in _recover_values(pii_regex._normalise(chunk), detector.mask(chunk)):
            values.setdefault(_value_key(value), kind)
    return values


def _recover_values(source: str, masked: MaskResult) -> list[tuple[str, str]]:
    """Recover the raw values behind the placeholders of ``masked`` (findings only carry
    placeholder spans). ``source`` is the text ``mask()`` worked on, so the text between
    placeholders is a literal copy of it. Linear: one ``find`` per placeholder run.
    Adjacent placeholders cannot be split and are taken as one value of the first type.
    A literal that also occurs inside the value (e.g. one space between two names) makes
    the split ambiguous: the left value is cut short and the right one starts too early
    (a known gap; the detector still runs on the answer).
    Anything that does not line up raises (fail closed)."""
    runs: list[tuple[int, int, str]] = []
    for f in sorted(masked.findings, key=lambda f: f.start):
        if runs and f.start == runs[-1][1]:
            runs[-1] = (runs[-1][0], f.end, runs[-1][2])
        else:
            runs.append((f.start, f.end, f.type))
    out: list[tuple[str, str]] = []
    text = masked.text
    pos_src = pos_masked = 0
    for i, (start, end, kind) in enumerate(runs):
        literal = text[pos_masked:start]
        if not source.startswith(literal, pos_src):
            raise ValueError("cannot align masked tool result")
        value_start = pos_src + len(literal)
        nxt = text[end : runs[i + 1][0] if i + 1 < len(runs) else len(text)]
        if i + 1 == len(runs):
            value_end = len(source) - len(nxt)
            if not source.endswith(nxt):
                raise ValueError("cannot align masked tool result")
        else:
            value_end = source.find(nxt, value_start + 1)
        if value_end <= value_start:
            raise ValueError("cannot align masked tool result")
        out.append((source[value_start:value_end], kind))
        pos_src, pos_masked = value_end, end
    return out


def _mask_exact(text: str, values: Mapping[str, str]) -> tuple[str, set[str]]:
    keys = [k for k in values if len(k) >= MIN_EXACT_VALUE_CHARS]
    if not keys:
        return text, set()
    if len(keys) > MAX_EXACT_VALUES:
        raise ValueError("too many PII values for exact-value masking")
    keys.sort(key=len, reverse=True)
    alternation = "|".join(r"\s+".join(re.escape(w) for w in k.split()) for k in keys)
    pattern = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", _I)
    seen: set[str] = set()

    def repl(m: re.Match[str]) -> str:
        # Keys and draft share one normalisation; the PERSON fallback only covers an
        # exotic case-folding mismatch and still masks the value.
        kind = values.get(_value_key(m.group(0)), pii.PERSON)
        seen.add(kind)
        return pii.TOKENS[kind]

    return pattern.sub(repl, text), seen
