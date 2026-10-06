"""Echo check on model-written answers (D-156).

An answer is an echo when it repeats, instead of answering, an earlier reply:

* it is near-identical (difflib ratio >= :data:`ECHO_RATIO` after whitespace normalisation)
  to an earlier assistant answer **and** states the same numbers (answers shorter than
  :data:`MIN_PREVIOUS_ECHO_CHARS` are not compared with earlier answers), or
* it is near-identical to a code-owned static text, or contains a long one.

An earlier answer to the same question (the user asked it again) is not compared: repeating
that answer is correct. The check is pure and bounded: at most :data:`MAX_COMPARED` earlier
answers, each compared over at most :data:`MAX_ECHO_CHARS` characters.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher
from typing import Final

__all__ = ["ECHO_RATIO", "ECHO_REJECTED", "ECHO_RETRY_RULE", "is_echo", "normalise"]

ECHO_REJECTED: Final = "ECHO_REJECTED"  # trace code
ECHO_RATIO: Final = 0.9
MIN_CONTAINED_STATIC_CHARS: Final = 120  # a static text this long counts when merely contained
# A shorter answer is not compared with earlier answers: a one-line answer ("There were 3
# complete orders.") can rightly repeat; the failure mode is a long earlier reply copied whole.
MIN_PREVIOUS_ECHO_CHARS: Final = 80
MAX_ECHO_CHARS: Final = 4000
MAX_COMPARED: Final = 24
ECHO_RETRY_RULE: Final = (
    "Answer the current question from the query results; do not repeat earlier replies."
)

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def normalise(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()[:MAX_ECHO_CHARS]


def _ratio(a: str, b: str) -> float:
    m = SequenceMatcher(None, a, b, autojunk=False)
    if m.real_quick_ratio() < ECHO_RATIO or m.quick_ratio() < ECHO_RATIO:
        return 0.0
    return m.ratio()


def _numbers(text: str) -> list[str]:
    return sorted(_NUMBER.findall(text))


def is_echo(answer: object, previous: Sequence[str], statics: Iterable[str] = ()) -> bool:
    """True when ``answer`` repeats one of ``previous`` (earlier answers) or a static text."""
    a = normalise(answer)
    if not a:
        return False
    for s in statics:
        st = normalise(s)
        if not st:
            continue
        if len(st) >= MIN_CONTAINED_STATIC_CHARS and st in a:
            return True
        if _ratio(a, st) >= ECHO_RATIO:
            return True
    if len(a) < MIN_PREVIOUS_ECHO_CHARS:
        return False
    nums = _numbers(a)
    for p in list(previous)[-MAX_COMPARED:]:
        pt = normalise(p)
        if pt and _numbers(pt) == nums and _ratio(a, pt) >= ECHO_RATIO:
            return True
    return False
