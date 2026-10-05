"""Code-side intent checks that refine the router's label (D-152).

* :func:`is_memory_question` - the user asks whether the agent remembers or sees the earlier
  messages ("do you see previous messages in our session?"). The router labels these ``meta``
  (sometimes ``smalltalk`` or an analytic label), and the capabilities text does not answer
  them, so the light path returns the code-owned :data:`MEMORY_TEXT` instead.
* :func:`is_comment_followup` - a statement or opinion about the previous answer ("so it is
  worth promoting"), not a new data request. It gets one brief reply from the previous answer's
  context instead of a full analyst loop (which spends the turn budget looking for data).

Both are pure, bounded regex checks on the scan fold the input guard uses (no model call).
They only narrow what happens next; the guards, budget and grounding still apply.
"""

from __future__ import annotations

import re
from typing import Final

from opsfleet_agent.graph.context import HISTORY_TURNS
from opsfleet_agent.guards.input import _fold

__all__ = ["MAX_INTENT_CHARS", "MEMORY_TEXT", "is_comment_followup", "is_memory_question"]

MAX_INTENT_CHARS: Final = 300  # longer messages are never treated as these intents

MEMORY_TEXT: Final = (
    "Yes. Within this session I remember our recent conversation (the last "
    f"{HISTORY_TURNS} questions and answers), so you can ask follow-ups like "
    "'and for last year?'. I don't keep chat history between sessions; saved reports stay "
    "in your library."
)

_YOU: Final = r"(?:do|can|could|will|would|did)\s+you"
_PAST: Final = r"(?:previous|earlier|past|prior|old|older|last|recent|other|our|my|the|these)"
_MSGS: Final = r"(?:messages?|questions?|conversation|chat(?:\s+history)?|history|turns?|answers?)"
_MEMORY_RE: Final = re.compile(
    r"\b(?:"
    # "do you see / remember / keep previous messages", "can you see our chat history"
    rf"{_YOU}\s+(?:still\s+)?(?:see|remember|recall|keep|have|store|retain|save|track|read|access)"
    rf"\s+(?:\w+\s+){{0,2}}?{_PAST}\s+(?:\w+\s+)?{_MSGS}"
    # "do you have (a) memory", "do you remember things / what I said / what we talked about"
    rf"|{_YOU}\s+have\s+(?:a\s+|any\s+)?(?:memory|context)"
    rf"|{_YOU}\s+remember\s+(?:anything|things|what\s+(?:i|we)\s+(?:said|asked|discussed|"
    r"talked\s+about))"
    # "is our chat saved / remembered", "how many messages do you remember"
    rf"|(?:is|are)\s+(?:our|this|my|the)\s+{_MSGS}\s+(?:saved|kept|stored|remembered)"
    rf"|how\s+(?:many|much|far\s+back)\s+(?:\w+\s+)?(?:{_MSGS}\s+)?(?:do|can)\s+you\s+"
    r"(?:remember|see|recall|keep)"
    r")\b"
)
# A request for figures ("do you remember the revenue for 2023?") is a data question.
_DATA_RE: Final = re.compile(
    r"\b(?:revenue|sales|orders?|profit|margin|price|cost|aov|churn|returns?|units?|"
    r"customers?|users?|products?|brands?|categor(?:y|ies)|inventory|traffic|top|total|"
    r"average|count|number\s+of|\d)"
)


def is_memory_question(text: str) -> bool:
    """True when ``text`` asks about the agent's conversation memory, not about the data."""
    if not isinstance(text, str) or len(text) > MAX_INTENT_CHARS:
        return False
    folded = " ".join(_fold(text).split())
    return bool(_MEMORY_RE.search(folded)) and not _DATA_RE.search(folded)


# A question or a request: never a comment.
_ASK_START_RE: Final = re.compile(
    r"^(?:what|which|who|whom|whose|how|why|when|where|show|list|give|compare|break|plot|"
    r"chart|find|get|tell|calculate|compute|count|sum|can|could|would|will|do|does|did|is|"
    r"are|was|were|should\s+i|please|and|now|also|then|same|repeat|filter|only|exclude|"
    r"include|sort|rank|split|group|drill|top|bottom|by|for|per|in|from|between|since|"
    r"during|let'?s|lets|check|look|see|analy[sz]e|run|redo|try|write|make|create|save|"
    r"delete|open|search)\b"
)
_REQUEST_RE: Final = re.compile(
    r"\b(?:show|list|give\s+me|compare|break\s*down|look\s+at|check|analy[sz]e|calculate|"
    r"find|tell\s+me|split|drill|report|what\s+about|how\s+about|vs\.?|versus)\b"
)
# An opinion or conclusion about what was just shown.
_OPINION_RE: Final = re.compile(
    r"\b(?:worth|should|deserves?|i\s+(?:think|guess|believe|feel|suppose|reckon)|"
    r"seems?|looks?\s+like|sounds?|makes?\s+sense|good\s+(?:idea|candidate|sign)|"
    r"great|interesting|impressive|surprising|not\s+bad|that'?s\s+(?:a\s+lot|good|bad|low|high)|"
    r"promot\w*|invest\w*|prioriti[sz]\w*|definitely|probably|agree|so\s+(?:it|we|this|that))\b"
)


def is_comment_followup(text: str) -> bool:
    """True when ``text`` reads as a statement or opinion, not a question or a data request.

    The caller also requires a previous answer in this session's (scope-covered) history.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_INTENT_CHARS:
        return False
    folded = " ".join(_fold(text).split())
    if "?" in folded or _ASK_START_RE.match(folded) or _REQUEST_RE.search(folded):
        return False
    return bool(_OPINION_RE.search(folded))
