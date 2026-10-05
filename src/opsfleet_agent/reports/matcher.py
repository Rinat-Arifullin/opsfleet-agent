"""The deterministic report matcher (A-16, AC-21.4, AC-21.6 matcher half, AC-21.11).

One matcher serves both the list filter (iteration 18) and the delete preview (22a), so a
"list my reports about X" shows exactly the set a delete would preview. It is a bounded,
case-insensitive literal substring match over title and body: no regex, no wildcards, no SQL
LIKE, no model. Every character of a query is a literal (``*``, ``%``, ``?``, ``_`` and
brackets included), because nothing here interprets patterns.

* Both haystack and needle are compared in the same canonical, rendered-equivalent form
  (:func:`canon`: NFKC, whitespace collapsed, Cc/Cf dropped, casefolded), so hidden or
  compatibility characters can neither hide a match nor fake one.
* A query needs at least :data:`MIN_ALNUM` letters or digits, so no query can match "everything".
* ROW SOURCE (OD-9): list and delete both scan the SAME bounded set, the owner's newest
  :data:`MAX_LIST` reports (:func:`owner_rows`), and both carry the ``truncated`` flag. Delete
  therefore never acts on a row the list could not have shown.
* Only reports whose ``scope_snapshot`` the CURRENT scope covers are matched by content
  (AC-21.5): a drifted report is never matched by content.
* Search results (iteration 18's :func:`library.search_reports`) are NOT inputs here:
  :func:`delete_candidates` takes a phrase string only (AC-21.11).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from opsfleet_agent.graph.context import covers
from opsfleet_agent.guards.scope import ProductScope
from opsfleet_agent.store.reports import MAX_LIST, SavedReport

__all__ = [
    "MAX_QUERY_CHARS",
    "MIN_ALNUM",
    "MatchError",
    "MatchSet",
    "canon",
    "delete_candidates",
    "in_scope",
    "match_reports",
    "matches_text",
    "normalize_query",
    "owner_matches",
    "owner_rows",
    "session_candidates",
]

MAX_QUERY_CHARS: Final = 100
MIN_ALNUM: Final = 3
_MAX_RAW_CHARS: Final = 4 * MAX_QUERY_CHARS  # bound on the work before canonicalisation
_DROP: Final = frozenset({"Cc", "Cf"})


class MatchError(ValueError):
    """An unusable query. The message is static text and never echoes the query."""


@dataclass(frozen=True)
class MatchSet:
    rows: tuple[SavedReport, ...]  # newest first, from the bounded scan
    truncated: bool  # the owner has more reports than the scan covered


def canon(text: object) -> str:
    """Rendered-equivalent comparison form: NFKC, whitespace collapsed, Cc/Cf dropped, casefold."""
    t = " ".join(unicodedata.normalize("NFKC", str(text)).split())
    t = "".join(c for c in t if unicodedata.category(c) not in _DROP)
    return " ".join(t.split()).casefold()


def normalize_query(query: object) -> str:
    """The canonical literal needle, or :class:`MatchError` (empty, too short, too long)."""
    if not isinstance(query, str):
        raise MatchError("the search text must be text")
    if len(query) > _MAX_RAW_CHARS:
        raise MatchError(f"the search text is limited to {MAX_QUERY_CHARS} characters")
    q = canon(query)
    if not q:
        raise MatchError("give some search text")
    if len(q) > MAX_QUERY_CHARS:
        raise MatchError(f"the search text is limited to {MAX_QUERY_CHARS} characters")
    if sum(c.isalnum() for c in q) < MIN_ALNUM:
        raise MatchError(f"the search text needs at least {MIN_ALNUM} letters or digits")
    return q


def in_scope(report: SavedReport, scope: ProductScope | None) -> bool:
    """True when the current scope covers the report's snapshot (None scope: fail closed)."""
    return scope is not None and covers(scope, report.scope_snapshot)


def matches_text(report: SavedReport, needle: str) -> bool:
    """``needle`` (already normalised) is a substring of the canonical title or body."""
    return needle in canon(report.title) or needle in canon(report.body_markdown)


def owner_rows(store: Any, owner: str) -> tuple[list[SavedReport], bool]:
    """The one row source for list, search and delete: the owner's newest ``MAX_LIST`` reports
    and whether the owner has more than that (OD-9)."""
    rows = store.list(owner, MAX_LIST)
    truncated = len(rows) >= MAX_LIST and store.count(owner) > len(rows)
    return rows, truncated


def match_reports(
    reports: Sequence[SavedReport], query: str, scope: ProductScope | None
) -> list[SavedReport]:
    """The in-scope reports matching ``query``, in the input order."""
    needle = normalize_query(query)
    return [r for r in reports if in_scope(r, scope) and matches_text(r, needle)]


def owner_matches(store: Any, owner: str, scope: ProductScope | None, phrase: str) -> MatchSet:
    """Row source plus match, shared by ``/reports <words>`` and the delete preview."""
    rows, truncated = owner_rows(store, owner)
    return MatchSet(tuple(match_reports(rows, phrase, scope)), truncated)


def delete_candidates(store: Any, owner: str, phrase: str, scope: ProductScope | None) -> MatchSet:
    """The delete matcher (22a): a stated phrase only, over the same rows the list scans. A
    search result, a listing or any non-text value is refused."""
    if not isinstance(phrase, str):
        raise TypeError("delete matching takes a stated phrase, never a search result")
    return owner_matches(store, owner, scope, phrase)


def session_candidates(reports: Sequence[SavedReport], session_id: str) -> list[SavedReport]:
    """AC-21.6: reports CREATED in ``session_id`` (the row's own session). A report that was
    merely viewed or opened in that session is not created there and is excluded."""
    if not isinstance(session_id, str) or not session_id:
        raise TypeError("a session id is required")
    return [r for r in reports if r.session_id == session_id]
