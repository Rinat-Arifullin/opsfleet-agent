"""Grounding: yearless month-day dates and "quarter of <year>" contexts (D-260)."""

from __future__ import annotations

from datetime import date

import pytest

from opsfleet_agent.graph.grounding import ESTIMATE_LABEL, check_grounding, extract_figures

WINDOW = (date(2019, 1, 1), date(2026, 9, 30))
# synthetic top-5 result: product name, revenue, units
ROWS = [
    ["Acme Jacket", 260.0, 4],
    ["Acme Jeans", 256.0, 8],
    ["Acme Hoodie", 240.5, 3],
    ["Acme Socks", 198.0, 2],
    ["Acme Cap", 150.25, 1],
]
FIGS = [extract_figures("q1", ["product_name", "revenue", "units"], ROWS)]
# figures that ground nothing below (no sums or differences hit the probed days)
NONE = [extract_figures("q0", ["n"], [[100.0]])]

Q2_DRAFT = """Here are the top 5 products by revenue for the second quarter of 2026 \
(April 1 to June 30):

| Product | Revenue |
|---|---|
| Acme Jacket | $260.00 |
| Acme Jeans | $256.00 |
| Acme Hoodie | $240.50 |
| Acme Socks | $198.00 |
| Acme Cap | $150.25 |

All dates are in UTC."""


def _u(draft: str, figs=NONE) -> tuple[str, ...]:
    return check_grounding(draft, figs, window=WINDOW).unmatched


def test_grounding_q2_answer_with_yearless_range_is_not_labelled() -> None:
    out = check_grounding(Q2_DRAFT, FIGS, window=WINDOW)
    assert out.unmatched == () and not out.label_added and ESTIMATE_LABEL not in out.text


def test_grounding_q2_answer_with_an_unrelated_figure_is_still_labelled() -> None:
    out = check_grounding(
        Q2_DRAFT.replace("All dates", "About $999. All dates"), FIGS, window=WINDOW
    )
    assert out.unmatched == ("$999",) and out.text.count(ESTIMATE_LABEL) == 1


@pytest.mark.parametrize(
    "draft",
    [
        "Sales peaked on June 30.",
        "Sales peaked on Jun 30th.",
        "Sales peaked on Jun. 30, then fell.",
        "Sales peaked on 30 June.",
        "Sales peaked on the 30th of June.",
        "From April 1 to June 30, sales rose.",
        "Between April 1 and June 30 sales rose.",
        "Sales rose over June 1-30.",
        "Sales rose over 1–30 June.",
        "Sales peaked on June 30th orders-wise.",
        "Sales peaked on Feb 29.",
        "Sales peaked on 30 June 2026 overall.",
    ],
)
def test_grounding_month_day_is_a_date(draft: str) -> None:
    assert _u(draft) == ()


@pytest.mark.parametrize(
    "draft",
    [
        "Q2 of 2026 was strong.",
        "In the second quarter of 2026 sales rose.",
        "The first half of 2026 was strong.",
        "The 3rd quarter of 2025 was flat.",
    ],
)
def test_grounding_quarter_of_year_is_a_year(draft: str) -> None:
    assert _u(draft) == ()


@pytest.mark.parametrize(
    "draft",
    [
        "Sales peaked on June 31.",
        "Sales peaked on Feb 30.",
        "Sales peaked on Sep 31st.",
        "Sales peaked on 31 June.",
        "Sales peaked on 31 June 2026.",
    ],
)
def test_grounding_impossible_month_day_is_labelled(draft: str) -> None:
    assert _u(draft) == ("date",)


def test_grounding_quarter_of_year_outside_window_is_labelled() -> None:
    assert _u("Q2 of 2093 was strong.") == ("date",)


@pytest.mark.parametrize(
    ("draft", "unmatched"),
    [
        # a day before a plain word may be a count: it stays a figure (fail closed)
        ("In June 30 orders shipped.", ("30",)),
        ("We sold 30 June orders.", ("30",)),
        ("June 1-30 orders shipped.", ("-30",)),  # only "June 1" is a date
        # lowercase month is prose, not a date
        ("Sales may 30 rise.", ("30",)),
        # a unit or decimal makes it a figure
        ("June 30% of orders.", ("30%",)),
        ("June 30.5 average.", ("30.5",)),
        # a bare "half of" is a count context, not a year
        ("Returns were half of 2093 orders.", ("2093",)),
    ],
)
def test_grounding_month_day_ambiguous_stays_a_figure(draft: str, unmatched: tuple) -> None:
    assert _u(draft) == unmatched


def test_grounding_month_day_before_a_word_grounds_when_in_ledger() -> None:
    assert _u("In June 30 orders shipped.", [extract_figures("q", ["n"], [[30]])]) == ()
