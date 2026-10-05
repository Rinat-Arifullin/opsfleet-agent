"""D-166: delete accepts the displayed ``R-`` report id. D-167: catalogue categories and
departments are in the CLI's PII allowlist, so NER does not mask "Swim" as a person."""

from __future__ import annotations

import copy
import re
from types import SimpleNamespace

import pytest

from opsfleet_agent import cli
from opsfleet_agent.delete.flow import parse_delete_request
from opsfleet_agent.guards.pii import (
    CATALOGUE_CATEGORIES,
    CATALOGUE_DEPARTMENTS,
    PiiDetector,
    build_allowlist,
)

RID = "0123456789abcdef0123456789abcdef"
RID2 = "fedcba9876543210fedcba9876543210"


# --- D-166 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [f"delete R-{RID}", f"delete report R-{RID}", f"delete r-{RID}", f"delete R-{RID.upper()}"],
)
def test_delete_accepts_display_id(text) -> None:
    req = parse_delete_request(text)
    assert req is not None and req.kind == "ids" and req.ids == (RID,) and req.error is None


def test_delete_display_and_bare_ids_select_the_same_report() -> None:
    assert parse_delete_request(f"delete R-{RID}") == parse_delete_request(f"delete {RID}")


def test_delete_command_accepts_display_ids() -> None:
    req = parse_delete_request(f"R-{RID}, {RID2}", command=True)
    assert req is not None and req.ids == (RID, RID2)


@pytest.mark.parametrize("text", ["delete R-short", "delete R-", f"delete XR-{RID}"])
def test_delete_prefix_alone_is_not_an_id(text) -> None:
    req = parse_delete_request(text)
    assert req is None or req.kind != "ids" or req.ids != (RID,)


@pytest.mark.parametrize("rid", [RID, f"R-{RID}"])
def test_negated_delete_handled_like_a_bare_id(rid) -> None:
    for text in (f"don't delete {rid}", f"delete don't remove {rid}"):
        assert parse_delete_request(text) is None, text


# --- D-167 ---------------------------------------------------------------------------------------


def test_catalogue_terms_cover_the_live_miss() -> None:
    assert "Swim" in CATALOGUE_CATEGORIES and set(CATALOGUE_DEPARTMENTS) == {"Men", "Women"}


def test_cli_allowlist_has_categories_and_departments(monkeypatch) -> None:
    installed: list[PiiDetector] = []
    monkeypatch.setattr(cli, "ensure_model_available", lambda: None)
    monkeypatch.setattr(cli, "set_default_detector", installed.append)
    cli._install_pii_detector([SimpleNamespace(brands=("Acme",))])
    allow = installed[0].allowlist
    for term in ("Acme", "Swim", "Sleep & Lounge", "Tops & Tees", "Women"):
        assert allow.is_term(term), term


class SwimNer(PiiDetector):
    """The live NER miss: spaCy tags "Swim" as a PERSON."""

    def _spacy_results(self, text: str):
        extra = [(m.start(), m.end(), False) for m in re.finditer(r"\bSwim\b", text)]
        return sorted([*super()._spacy_results(text), *extra])


def _swim_ner(allowlist) -> PiiDetector:
    det = copy.copy(PiiDetector(allowlist))
    det.__class__ = SwimNer
    return det


def test_category_tagged_as_person_is_not_masked() -> None:
    allow = build_allowlist(categories=CATALOGUE_CATEGORIES, departments=CATALOGUE_DEPARTMENTS)
    text = "Swim led revenue growth in Q2."
    assert _swim_ner(allow).mask(text).text == text


def test_without_the_catalogue_the_miss_is_masked() -> None:
    assert "Swim" not in _swim_ner(build_allowlist()).mask("Swim led revenue growth.").text


def test_real_person_next_to_a_category_still_masked() -> None:
    allow = build_allowlist(categories=CATALOGUE_CATEGORIES)
    out = _swim_ner(allow).mask("Name: Marlowe Finch bought Swim items.").text
    assert "Marlowe" not in out and "Swim" in out
