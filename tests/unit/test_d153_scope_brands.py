"""D-153: the session's scope brands are never masked as PII (offline, synthetic names only)."""

from __future__ import annotations

import dataclasses

import pytest

from opsfleet_agent.graph import graph as gr
from opsfleet_agent.guards.pii import (
    MAX_DERIVED_DETECTORS,
    PERSON,
    TOKENS,
    PiiDetector,
    build_allowlist,
    extend_allowlist,
)
from opsfleet_agent.obs import langfuse_sink as lf
from opsfleet_agent.roles.analyst import ModelTurn
from opsfleet_agent.session import Profile, Session
from tests.unit.test_graph import SIMPLE, Router, Scripted, sql_call
from tests.unit.test_graph import detector as detector  # noqa: F401  (fixture)
from tests.unit.test_graph import make_env as make_env  # noqa: F401  (fixture)
from tests.unit.test_graph import settings as settings  # noqa: F401  (fixture)

BRAND = "Calvin Klein"
MASK = TOKENS[PERSON]
SCOPED = Profile("analyst_cb", "Analyst CB", brands=(BRAND,))
ALL = Profile("ceo_cb", "CEO CB", all_products=True)

BRAND_TEXTS = [
    "Calvin Klein had revenue of $12,400 in July.",
    "Revenue for Calvin Klein grew 12% month over month.",
    "In your scope (Calvin Klein), sales were flat.",
    "Calvin Klein's best month was July.",
    "| brand | revenue |\n|---|---|\n| Calvin Klein | 12400 |",
    "calvin klein revenue was 12400",
]


@pytest.fixture(scope="module")
def base() -> PiiDetector:
    return PiiDetector()  # SCHEMA_TERMS only, like a session before D-153


@pytest.fixture(scope="module")
def scoped(base: PiiDetector) -> PiiDetector:
    return base.with_brands([BRAND])


def test_precondition_brand_is_person_like(base: PiiDetector) -> None:
    """Without the scope allowlist spaCy tags the brand as a person (the D-152 trace)."""
    assert MASK in base.mask(BRAND_TEXTS[0]).text


@pytest.mark.parametrize("text", BRAND_TEXTS)
def test_scope_brand_is_kept(scoped: PiiDetector, text: str) -> None:
    out = scoped.mask(text)
    assert out.text == text and not out.findings


@pytest.mark.parametrize(
    "text",
    [
        "The order was placed by Marlowe Finch on Tuesday.",
        "Thanks, Thistlewood Bramble",
        "The buyer was Marlowe Finch, Calvin Klein orders were 3.",
    ],
)
def test_synthetic_person_is_still_redacted(scoped: PiiDetector, text: str) -> None:
    out = scoped.mask(text).text
    assert MASK in out
    assert "Marlowe" not in out and "Thistlewood" not in out


@pytest.mark.parametrize(
    "text",
    [
        "Marlowe Klein placed an order.",  # shares a word with the brand only
        "The order was placed by Calvin Bramblewood yesterday.",
    ],
)
def test_person_sharing_a_brand_word_is_redacted(scoped: PiiDetector, text: str) -> None:
    out = scoped.mask(text).text
    assert MASK in out and "Marlowe" not in out and "Bramblewood" not in out


def test_with_brands_is_cached_bounded_and_shares_the_model(base: PiiDetector) -> None:
    a = base.with_brands([BRAND])
    assert base.with_brands((BRAND,)) is a and base.with_brands([BRAND.upper()]) is not base
    assert a._analyzer is base._analyzer and a._lock is base._lock
    assert a.with_brands([BRAND]) is a  # nothing new: the same detector
    assert base.with_brands([]) is base and base.with_brands(["", "  ", None]) is base  # type: ignore[list-item]
    assert base.allowlist.is_term(BRAND) is False  # the base is never changed
    for i in range(MAX_DERIVED_DETECTORS + 5):
        base.with_brands([f"Brand{i:03d}"])
    assert len(base._derived) <= MAX_DERIVED_DETECTORS
    with pytest.raises(TypeError):
        base.with_brands(BRAND)  # type: ignore[arg-type]


def test_extend_allowlist_is_whole_phrase() -> None:
    al = extend_allowlist(build_allowlist(), ["Calvin Klein", "Levi's"])
    assert al.is_term("calvin klein") and al.is_term("LEVI'S") and al.is_term("Levi")
    assert not al.is_term("Klein") and not al.is_term("Calvin Klein Marlowe")
    text = "Marlowe Klein"
    assert not al.covers(text, 0, len(text))


# --- graph: output path --------------------------------------------------------------------------


def _env(make_env, profile: Profile, answer: str, **services):  # noqa: F811
    env = make_env(Router("simple"), Scripted(sql_call(SIMPLE), ModelTurn(answer)))
    if services:
        env.graph.services = dataclasses.replace(env.graph.services, **services)
    env.session = Session("sess-d153", profile)
    return env


def test_answer_keeps_the_scope_brand(make_env) -> None:  # noqa: F811
    env = _env(make_env, SCOPED, "Calvin Klein had 3 complete orders.")
    out = env.ask("How many complete orders are there?")
    assert out.outcome == "answered" and "Calvin Klein had 3 complete orders" in out.text
    assert MASK not in out.text


def test_answer_still_redacts_a_person_in_scope(make_env) -> None:  # noqa: F811
    env = _env(make_env, SCOPED, "Calvin Klein had 3 orders, one placed by Marlowe Finch.")
    out = env.ask("How many complete orders are there?")
    assert "Calvin Klein" in out.text and "Marlowe" not in out.text


def test_all_products_scope_uses_known_brands(make_env) -> None:  # noqa: F811
    env = _env(make_env, ALL, "Calvin Klein had 3 complete orders.", known_brands=(BRAND,))
    out = env.ask("How many complete orders are there?")
    assert "Calvin Klein had 3 complete orders" in out.text


def test_turn_detector_falls_back_for_duck_typed_detectors(make_env) -> None:  # noqa: F811
    env = _env(make_env, SCOPED, "ok")
    fake = object()
    env.graph.services = dataclasses.replace(env.graph.services, detector=fake)
    ctx = type("Ctx", (), {"services": env.graph.services})()
    assert gr._turn_detector(ctx) is fake


# --- trace masking (Langfuse) --------------------------------------------------------------------


def test_trace_cleaner_keeps_brand_and_masks_person(scoped: PiiDetector) -> None:
    clean = lf._Cleaner(scoped)
    out = clean.text("Calvin Klein revenue rose; the buyer was Marlowe Finch.")
    assert "Calvin Klein" in out and "Marlowe" not in out


def test_wrapper_subclass_without_init_is_used_as_is(base: PiiDetector) -> None:
    class _Wrapper(PiiDetector):
        def __init__(self, inner: PiiDetector) -> None:  # never runs PiiDetector.__init__
            self.inner = inner

        def mask(self, text):
            return self.inner.mask(text)

    w = _Wrapper(base)
    assert w.with_brands(["Calvin Klein"]) is w
