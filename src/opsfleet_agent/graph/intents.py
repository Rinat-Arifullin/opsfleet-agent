"""Code-side intent checks that refine the router's label, and the code-owned intent replies.

* :data:`MEMORY_TEXT` / :data:`COMMENT_FALLBACK_TEXT` - D-155: the router labels a question
  about the agent's conversation memory ``memory`` and a statement or opinion about the
  previous answer ``comment`` (any language, ``prompts/router.md``). These texts are the
  code-owned replies for those labels. The English-only regex detectors of D-152 were removed:
  the router is the one source of truth, and a router failure fails open to ``complex``.
* :func:`is_sql_request` - the user asks to see the SQL ("show me the SQL you used"). D-151a:
  SQL is never shown, so the light path answers with a code-owned reply that describes the
  data used in business words (:func:`opsfleet_agent.guards.plain_language.sql_request_reply`).
* :func:`is_customer_ranking_request` - D-157: "who are our top 10 customers by spend?" or
  "list our customers". The router sometimes labels these ``injection``; they are data
  questions, so the graph answers them instead of refusing. D-159: the answer is spend bands
  with customer counts only (no individual customers, no customer IDs); ``run_sql`` enforces
  it for the turn and :func:`mentions_customer_id` checks the answer.
  :func:`asks_for_customer_pii` marks the ones that also ask for names, emails or addresses.

The checks are pure, bounded regex checks on the scan fold the input guard uses (no model
call). They are English-only. They only narrow what happens next; the guards, budget and
grounding still apply.
"""

from __future__ import annotations

import re
from typing import Final

from opsfleet_agent.graph.context import HISTORY_TURNS
from opsfleet_agent.guards.input import _fold
from opsfleet_agent.guards.small_cell import DEFAULT_K

__all__ = [
    "MAX_INTENT_CHARS",
    "COMMENT_FALLBACK_TEXT",
    "CUSTOMER_BANDS_NOTICE",
    "CUSTOMER_BANDS_RULE",
    "CUSTOMER_BANDS_SECTION",
    "MEMORY_TEXT",
    "asks_for_customer_pii",
    "is_customer_ranking_request",
    "is_sql_request",
    "mentions_customer_id",
]

MAX_INTENT_CHARS: Final = 300  # longer messages are never treated as these intents

MEMORY_TEXT: Final = (
    "Yes. Within this session I remember our recent conversation (the last "
    f"{HISTORY_TURNS} questions and answers), so you can ask follow-ups like "
    "'and for last year?'. I don't keep chat history between sessions; saved reports stay "
    "in your library."
)
# D-152 template for a `comment` turn (label from the router, D-155): the static reply when
# there is no previous answer, and the fallback when the brief contextual reply fails.
COMMENT_FALLBACK_TEXT: Final = (
    "Noted. I can check that against the data if you like, for example the sales trend over "
    "recent months or the return rate."
)

# Data words: a message that names figures ("show the SQL for revenue") is a data question.
_DATA_RE: Final = re.compile(
    r"\b(?:revenue|sales|orders?|profit|margin|price|cost|aov|churn|returns?|units?|"
    r"customers?|users?|products?|brands?|categor(?:y|ies)|inventory|traffic|top|total|"
    r"average|count|number\s+of|\d)"
)


# --- D-151a: "show me the SQL" ---------------------------------------------------------------
_SQL: Final = (
    r"(?:the\s+|your\s+|that\s+|this\s+|those\s+|these\s+|its\s+|an?\s+)?"
    r"(?:exact\s+|actual\s+|underlying\s+|raw\s+|full\s+|generated\s+|bigquery\s+|same\s+)?"
    r"(?:sql(?:\s+(?:query|queries|code|statements?|text))?|quer(?:y|ies)|query\s+text)"
    r"(?!\s*(?:results?|output|rows?)\b)"
)
# Refers back to SQL that already ran: always a SQL request, whatever data words it holds.
_SQL_BACKREF_RE: Final = re.compile(
    r"\b(?:"
    r"what\s+(?:sql|quer(?:y|ies))\s+(?:did|do|was|were|is|are|have|has|had)\b"
    rf"|{_SQL}\s+(?:that\s+|which\s+)?(?:you\s+|was\s+|were\s+)(?:used|use|ran|run|executed|"
    r"wrote|written|generated|made|behind)\b"
    rf"|{_SQL}\s+(?:behind|for|of)\s+(?:that|this|the\s+(?:last|previous|above))\b"
    r"|how\s+did\s+you\s+(?:query|write\s+the\s+(?:sql|query))\b"
    r")"
)
# A generic request to see or write SQL; a data question that mentions SQL goes to the analyst
# (the output check still strips any SQL from its answer).
_SQL_REQUEST_RE: Final = re.compile(
    r"(?:^|\b)(?:"
    r"(?:show|give|send|print|display|share|paste|reveal|post|output|provide|write|dump|list)"
    rf"\s+(?:me\s+|us\s+)?{_SQL}\b"
    rf"|(?:see|view|get|have(?!\s+an?\s)|read|check|look\s+at)\s+{_SQL}\b"
    r"|^(?:the\s+)?sql(?:\s+(?:please|pls))?\W*$"
    r")"
)


def is_sql_request(text: str) -> bool:
    """True when ``text`` asks to see (or write) the SQL / database query (D-151a)."""
    if not isinstance(text, str) or len(text) > MAX_INTENT_CHARS:
        return False
    folded = " ".join(_fold(text).split())
    if _SQL_BACKREF_RE.search(folded):
        return True
    return bool(_SQL_REQUEST_RE.search(folded)) and not _DATA_RE.search(folded)


# --- D-157: "top customers" is a data question, not an injection ------------------------------
# D-159 (owner 2026-10-05): it is answered with spend bands and customer counts only.
CUSTOMER_BANDS_NOTICE: Final = (
    "Note: I show customer spending as bands with customer counts, not individual customers."
)
CUSTOMER_BANDS_SECTION: Final = "Customer ranking"
CUSTOMER_BANDS_RULE: Final = (
    "This question asks for top customers or a customer ranking. Answer it with spend bands "
    "and customer counts only, never with individual customers. Write one query that computes "
    "each customer's total spend in a subquery, then groups those totals into spend bands with "
    "CASE (for example under $100, $100 to $499, $500 to $999, $1,000 and over) and returns per "
    "band the number of customers, the band's revenue and its share of total revenue. Do not "
    "return or mention customer IDs, user IDs or per-customer rows: such a query is refused. "
    f"A band with fewer than {DEFAULT_K} customers is merged into the next lower band; if that "
    "is not possible, leave it out and say that small bands were combined."
)
_CUSTOMERS: Final = r"(?:customers?|buyers?|clients?|shoppers?|purchasers?|spenders?|users?)"
_RANK: Final = (
    r"(?:top|best|biggest|largest|leading|heaviest|loyal(?:est)?|most\s+(?:valuable|loyal|"
    r"active|frequent)|highest[\s-]+(?:spending|value|paying|revenue)|big[\s-]+spending|"
    r"repeat|vip)"
)
_CUSTOMER_RANKING_RE: Final = re.compile(
    r"\b(?:"
    # "top 10 customers", "best repeat buyers", "most valuable clients by revenue"
    rf"{_RANK}\s+(?:\d{{1,4}}\s+)?(?:\w+\s+){{0,2}}?{_CUSTOMERS}"
    # "list (of) our customers", "list all buyers"
    rf"|list\s+(?:of\s+)?(?:(?:our|all|the|my)\s+)?(?:\w+\s+)?{_CUSTOMERS}"
    # "who are our customers", "who were the biggest buyers last year"
    rf"|who\s+(?:are|were|is)\s+(?:our|the|my)\s+(?:\w+\s+){{0,3}}?{_CUSTOMERS}"
    # "which customers spent the most"
    rf"|which\s+{_CUSTOMERS}\s+(?:spent|spend|spends|bought|buy|ordered|order|purchased)"
    r")\b"
)
# Words of a genuine injection: such a message keeps the router's refusal.
_INJECTION_HINT_RE: Final = re.compile(
    r"\b(?:ignore|disregard|forget|bypass|override|pretend|jailbreak|unrestricted|"
    r"developer\s+mode|admin|system\s+prompt|instructions?|rules?|polic(?:y|ies)|"
    r"act\s+as|you\s+are\s+now|label|classify|respond\s+with|output|reveal|print|"
    r"configuration|select\s|from\s+users)\b"
)
# Direct identifiers: these keep a refusal, with the PII text instead of the injection text.
_CUSTOMER_PII_RE: Final = re.compile(
    r"\b(?:names?|first[\s_-]*names?|last[\s_-]*names?|surnames?|e-?mails?|e-?mail\s+"
    r"address(?:es)?|phones?|phone\s+numbers?|addresses|address|street|contacts?|"
    r"contact\s+details)\b"
)


def _ranking_fold(text: str) -> str | None:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_INTENT_CHARS:
        return None
    folded = " ".join(_fold(text).split())
    if not _CUSTOMER_RANKING_RE.search(folded) or _INJECTION_HINT_RE.search(folded):
        return None
    return folded


def is_customer_ranking_request(text: str) -> bool:
    """True for a ranking or list of customers with no injection wording (D-157).

    D-159: the answer is spend bands with customer counts only. The turn's ``run_sql`` refuses
    a query at customer grain or one that returns an id, and the SQL policy and the output
    guard block direct identifiers whatever the label."""
    return _ranking_fold(text) is not None


def asks_for_customer_pii(text: str) -> bool:
    """True for a customer ranking or list that also asks for names, emails or addresses."""
    folded = _ranking_fold(text)
    return folded is not None and bool(_CUSTOMER_PII_RE.search(folded))


# An individual customer in an answer: "customer ID 12345", "user #881", "user_id", or a
# "Customer ID" table column. A sentence that only says IDs are not shown does not match.
_CUSTOMER_ID_RE: Final = re.compile(
    r"\b(?:customer|user|client|buyer|shopper)s?[\s_-]*(?:ids?|#|no\.?|numbers?)\s*[:#=]?\s*\d"
    r"|\b(?:customer|user|client|buyer|shopper)\s*#\s*\d"
    r"|\buser_?ids?\b"
    r"|\|\s*(?:customer|user|client|buyer)[\s_-]*id\s*\|",
    re.IGNORECASE,
)


def mentions_customer_id(text: str) -> bool:
    """True when an answer names individual customers by ID (D-159). Pure, linear regex."""
    if not isinstance(text, str) or not text:
        return False
    return bool(_CUSTOMER_ID_RE.search(text))
