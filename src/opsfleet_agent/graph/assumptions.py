"""Deterministic assumptions footer on data answers (live eval iteration 1).

The analyst prompt asks the model to state the product scope and the definitions it used
(A-9 revenue, AOV, A-4 churn), but under budget pressure, and in the force answer, it often
did not. The live evals showed answers with no scope ("Calvin Klein") and no churn definition.
This module makes that statement a code guarantee: :func:`assumptions_footer` returns the lines
the answer is missing, and ``_finalize`` appends them after the output guard.

Rules:

* Pure and bounded: plain substring checks on the answer and the question, no LLM, no I/O.
* Only what is missing is added, so an answer that already states its scope or definition
  gets no duplicate line.
* The footer is code-owned constant text plus the profile's own brand names: no digits, no
  SQL, no PII. It never states a figure, so number grounding is unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

__all__ = [
    "ASSUMPTIONS_ADDED",
    "AOV_DEFINITION",
    "CHURN_DEFINITION",
    "CHURN_RESTATED",
    "FOOTER_HEADING",
    "REVENUE_DEFINITION",
    "SCOPE_ALL",
    "assumptions_footer",
    "scope_line",
]

ASSUMPTIONS_ADDED: Final = "assumptions_added"  # guard code when a footer is appended
FOOTER_HEADING: Final = "Assumptions:"
SCOPE_ALL: Final = "Scope: all products."
REVENUE_DEFINITION: Final = (
    "Revenue is the sum of item sale prices, excluding cancelled and returned items."
)
AOV_DEFINITION: Final = (
    "Average order value (AOV) is revenue divided by the number of orders."
)
CHURN_DEFINITION: Final = (
    "Churn: a customer who ordered in one month and placed no order in the next month. "
    "You can restate your own churn definition (for example a different window) and I will "
    "use it."
)
CHURN_RESTATED: Final = "Churn uses the definition you restated for this session."

_REVENUE_TERMS: Final = ("revenue", "sales", "aov", "average order value", "spend", "gmv")
_AOV_TERMS: Final = ("aov", "average order value")
_AOV_STATED: Final = ("divided by", "per order")
_REVENUE_STATED: Final = ("cancel",)
_CHURN_STATED: Final = ("no order in", "restate")
MAX_SCAN_CHARS: Final = 20_000


def _fold(text: str) -> str:
    # curly apostrophes fold to straight ones so "Levi’s" in an answer matches "Levi's"
    return text[:MAX_SCAN_CHARS].replace("’", "'").casefold()


def _join(names: Sequence[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def scope_line(brands: Sequence[str], all_products: bool) -> str:
    """The scope sentence for a profile."""
    if all_products or not brands:
        return SCOPE_ALL
    return f"Scope: {_join(list(brands))} products only."


def _scope_missing(answer: str, brands: Sequence[str], all_products: bool) -> bool:
    if all_products or not brands:
        return "all products" not in answer
    return not all(_fold(b) in answer for b in brands)


def assumptions_footer(
    answer: str,
    question: str,
    *,
    brands: Sequence[str],
    all_products: bool,
    churn_restated: bool = False,
) -> str:
    """The footer lines ``answer`` is missing, as one block, or "" when nothing is missing."""
    a = _fold(answer)
    topic = a + "\n" + _fold(question)
    lines: list[str] = []
    if _scope_missing(a, brands, all_products):
        lines.append(scope_line(brands, all_products))
    if any(t in topic for t in _REVENUE_TERMS) and not any(s in a for s in _REVENUE_STATED):
        lines.append(REVENUE_DEFINITION)
    if any(t in topic for t in _AOV_TERMS) and not any(s in a for s in _AOV_STATED):
        lines.append(AOV_DEFINITION)
    if "churn" in topic:
        if churn_restated:
            if "restate" not in a:
                lines.append(CHURN_RESTATED)
        elif not all(s in a for s in _CHURN_STATED):
            lines.append(CHURN_DEFINITION)
    if not lines:
        return ""
    return FOOTER_HEADING + "\n" + "\n".join(f"- {line}" for line in lines)
