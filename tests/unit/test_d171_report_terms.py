"""D-171: analytical section headings are in every PII allowlist, so NER does not mask the
heading "Takeaways:" as a person (live run smoke-9080868)."""

from __future__ import annotations

import copy
import re

import pytest

from opsfleet_agent.guards.pii import EMPTY_ALLOWLIST, REPORT_TERMS, PiiDetector, build_allowlist


class HeadingNer(PiiDetector):
    """The live NER miss: spaCy tags a heading word (and a name) as a PERSON."""

    pattern = re.compile(r"\b(?:Key Takeaways|Takeaways|Summary Jones|Summary)\b")

    def _spacy_results(self, text: str):
        extra = [(m.start(), m.end(), False) for m in self.pattern.finditer(text)]
        return sorted([*super()._spacy_results(text), *extra])


def _heading_ner(allowlist) -> PiiDetector:
    det = copy.copy(PiiDetector(allowlist))
    det.__class__ = HeadingNer
    return det


def test_report_terms_cover_the_live_miss() -> None:
    assert {"Takeaways", "Key Takeaways", "Summary"} <= set(REPORT_TERMS)


@pytest.mark.parametrize("allow", [EMPTY_ALLOWLIST, build_allowlist(brands=("Acme",))])
@pytest.mark.parametrize(
    "text",
    ["**Takeaways:**\n- Jeans led revenue.", "Key Takeaways: Jeans led.", "Summary: revenue rose."],
)
def test_heading_tagged_as_person_is_not_masked(allow, text) -> None:
    assert _heading_ner(allow).mask(text).text == text


def test_real_person_next_to_a_heading_still_masked() -> None:
    out = _heading_ner(EMPTY_ALLOWLIST).mask("Takeaways: Name: Marlowe Finch bought Jeans.").text
    assert "Takeaways" in out and "Marlowe" not in out


def test_name_sharing_a_heading_word_still_masked() -> None:
    out = _heading_ner(EMPTY_ALLOWLIST).mask("Contact Summary Jones about it.").text
    assert "Jones" not in out
