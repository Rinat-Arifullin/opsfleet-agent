"""Report library: list, view and substring search over the user's OWN reports (iteration 18).

Rules enforced here, in code (AC-06.3, AC-21.3 to AC-21.5, AC-21.10, AC-21.11, AC-22.3):

* owner-only: every read goes through the owner-scoped :class:`ReportStore`; another user's
  report ID gets the same "not found" as a missing one (no existence oracle), and no count or
  message depends on other owners' rows;
* scope drift: a report whose ``scope_snapshot`` the CURRENT scope no longer covers shows only
  as ID, date and "created under a different product scope" (title, tags, preview masked), its
  body is withheld on open, and content matching never reaches it. A missing scope fails closed;
* untrusted text: titles and bodies are rendered back through the fence-on-render forms of
  ``graph.context`` (SEC-13, PII scrub, no ``<<``/``>>``); the CLI then runs ``terminal_safe``
  over every command output;
* the filter and the search are bounded substring matches (``reports.matcher``), 0 SQL on the
  data tables, 0 model calls. A search result is plain data: nothing here records it, and the
  delete matcher refuses anything but a phrase (AC-21.11).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from opsfleet_agent.graph.context import KIND_REPORT, _one_line, fence_untrusted
from opsfleet_agent.guards.plain_language import strip_sql
from opsfleet_agent.guards.scope import ProductScope
from opsfleet_agent.reports import fts
from opsfleet_agent.reports.matcher import (
    MatchError,
    in_scope,
    matches_text,
    normalize_query,
    owner_matches,
    owner_rows,
)
from opsfleet_agent.store.reports import MAX_BODY_CHARS, MAX_LIST, SavedReport

__all__ = [
    "DRIFT_LABEL",
    "MAX_RESULTS",
    "SEARCH_MODES",
    "NOT_FOUND_TEXT",
    "NO_ROW_TEXT",
    "LibraryError",
    "ListEntry",
    "ListResult",
    "OpenResult",
    "ViewResult",
    "count_text",
    "display_id",
    "list_reports",
    "open_report",
    "parse_search_args",
    "render_list",
    "search_reports",
    "strip_display_prefix",
    "view_report",
]

MAX_RESULTS: Final = 20
SEARCH_MODES: Final = ("substring", "ranked")
MAX_TAGS: Final = 5
MAX_ID_CHARS: Final = 64
TITLE_CHARS: Final = 120
DRIFT_LABEL: Final = "created under a different product scope"
NOT_FOUND_TEXT: Final = "No saved report found for that id or title."
DRIFT_VIEW_TEXT: Final = (
    "Report {rid} (created {date}) was created under a different product scope than your "
    "current one, so its content is withheld."
)
_DATE_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_ROW_RE: Final = re.compile(r"\d{1,2}", re.ASCII)
# live1: a saved report is announced as "R-<id>"; the stored id stays the bare hex
DISPLAY_PREFIX: Final = "R-"
_DISPLAY_RE: Final = re.compile(r"^[Rr]-(?=[0-9a-f]{32}$)", re.ASCII)


def display_id(report_id: str) -> str:
    """The id as shown when a report is saved ("R-" + the stored hex id)."""
    return f"{DISPLAY_PREFIX}{report_id}"


def strip_display_prefix(ref: str) -> str:
    """A shown id ("R-<hex>") back to the stored id; anything else unchanged."""
    return _DISPLAY_RE.sub("", ref)


class LibraryError(ValueError):
    """A bad request (an unusable search). The message is static and shown to the user."""


@dataclass(frozen=True)
class ListEntry:
    report_id: str
    created_at: str
    session_id: str
    title: str | None  # None: masked (scope drift)
    tags: tuple[str, ...]
    masked: bool


@dataclass(frozen=True)
class ListResult:
    entries: tuple[ListEntry, ...]  # at most the requested limit
    total: int  # matches among the scanned reports (the owner's own only)
    truncated_scan: bool  # the owner has more reports than were scanned
    # iteration 37: "ranked" (FTS5 bm25), "substring", or "substring_fallback" (ranked was
    # asked for but the index is missing or refused the query)
    path: str = "substring"


@dataclass(frozen=True)
class ViewResult:
    status: str  # "ok" | "not_found" | "scope_drift"
    text: str


def _line(text: str, n: int = TITLE_CHARS) -> str:
    return _one_line(text, n)


def _entry(r: SavedReport, scope: ProductScope | None) -> ListEntry:
    if not in_scope(r, scope):
        return ListEntry(r.report_id, r.created_at, r.session_id, None, (), True)
    return ListEntry(
        r.report_id, r.created_at, r.session_id, _line(r.title),
        tuple(_line(t, 40) for t in r.tags), False,
    )  # fmt: skip


def list_reports(
    store,
    owner: str,
    scope: ProductScope | None,
    query: str | None = None,
    limit: int = MAX_RESULTS,
) -> ListResult:
    """The owner's reports, newest first. With ``query`` the SAME matcher as delete (A-16)
    filters title and body over in-scope reports; without one, drifted reports appear masked.
    Raises ``MatchError`` for an unusable query."""
    if query is not None:
        found = owner_matches(store, owner, scope, query)
        rows, truncated = list(found.rows), found.truncated
    else:
        rows, truncated = owner_rows(store, owner)
    limit = max(1, min(int(limit), MAX_RESULTS))
    return ListResult(tuple(_entry(r, scope) for r in rows[:limit]), len(rows), truncated)


def _check_date(d: str | None) -> None:
    if d is None:
        return
    if not _DATE_RE.fullmatch(d):
        raise LibraryError("dates must look like 2026-01-31")
    try:
        date.fromisoformat(d)
    except ValueError:
        raise LibraryError("that is not a real calendar date") from None


def parse_search_args(args: str) -> dict[str, Any]:
    """``words [tag:x] [from:D] [to:D]`` to ``search_reports`` keyword arguments. A repeated
    ``from:`` or ``to:`` is refused rather than silently resolved."""
    words: list[str] = []
    tags: list[str] = []
    dates: dict[str, str] = {}
    for tok in args.split():
        low = tok.lower()
        if low.startswith("tag:") and len(tok) > 4:
            tags.append(tok[4:])
        elif (low.startswith("from:") and len(tok) > 5) or (low.startswith("to:") and len(tok) > 3):
            key = low.split(":", 1)[0]
            if key in dates:
                raise LibraryError(f"give only one {key}: date")
            dates[key] = tok.split(":", 1)[1]
        else:
            words.append(tok)
    return {
        "text": " ".join(words) or None,
        "tags": tags,
        "date_from": dates.get("from"),
        "date_to": dates.get("to"),
    }


def search_reports(
    store,
    owner: str,
    scope: ProductScope | None,
    *,
    text: str | None = None,
    tags: Sequence[str] = (),
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = MAX_RESULTS,
    mode: str = "substring",
) -> ListResult:
    """AC-21.10: all given filters must match (case-insensitive substring over title and body,
    exact tag, creation date within the inclusive range), newest first, at most 20 and a total.
    Drifted reports are never searched. Raises :class:`LibraryError` for an unusable request.

    Iteration 37 (AC-21.13): ``mode="ranked"`` with text uses the FTS5 index (every word must
    match, stemmed, over title, body and tags), best bm25 first, over the same owner rows; the
    scope, tag and date filters apply before the limit. Without an index it falls back to the
    substring search (``path="substring_fallback"``)."""
    if mode not in SEARCH_MODES:
        raise LibraryError("unknown search mode")
    try:
        needle = normalize_query(text) if text is not None else None
    except MatchError as exc:
        raise LibraryError(str(exc)) from None
    want_tags = {" ".join(str(t).split()).casefold() for t in tags if str(t).strip()}
    if len(want_tags) > MAX_TAGS:
        raise LibraryError(f"at most {MAX_TAGS} tags per search")
    for d in (date_from, date_to):
        _check_date(d)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise LibraryError("the from: date is after the to: date")
    if needle is None and not want_tags and date_from is None and date_to is None:
        raise LibraryError("give some search text, a tag or a date range")
    path, ranked = "substring", None
    if mode == "ranked" and needle is not None:
        try:
            match = fts.build_match(needle)
        except fts.FtsQueryError as exc:
            raise LibraryError(str(exc)) from None
        search = getattr(store, "ranked_search", None)
        ranked = search(owner, match) if callable(search) else None
        path = "ranked" if ranked is not None else "substring_fallback"
    if ranked is not None:
        rows, truncated = ranked, store.count(owner) > MAX_LIST
    else:
        rows, truncated = owner_rows(store, owner)
    hits = [
        r
        for r in rows
        if in_scope(r, scope)
        and (ranked is not None or needle is None or matches_text(r, needle))
        and want_tags <= {t.casefold() for t in r.tags}
        and (date_from is None or r.created_at[:10] >= date_from)
        and (date_to is None or r.created_at[:10] <= date_to)
    ]
    limit = max(1, min(int(limit), MAX_RESULTS))
    return ListResult(tuple(_entry(r, scope) for r in hits[:limit]), len(hits), truncated, path)


def view_report(store, owner: str, scope: ProductScope | None, report_id: str) -> ViewResult:
    """AC-21.3 / AC-21.5: the owner's own in-scope report, fenced, with its date and data
    window. Another user's id, a missing id and a malformed id all give :data:`NOT_FOUND_TEXT`."""
    rid = strip_display_prefix(str(report_id).strip())
    rec = store.get(rid, owner) if rid and len(rid) <= MAX_ID_CHARS else None
    if rec is None:
        return ViewResult("not_found", NOT_FOUND_TEXT)
    if not in_scope(rec, scope):
        text = DRIFT_VIEW_TEXT.format(rid=_line(rec.report_id, 80), date=rec.created_at[:10])
        return ViewResult("scope_drift", text)
    head = (
        f"{_line(rec.title)}\n"
        f"created {rec.created_at[:10]}, data window {_line(rec.data_window, 80)}"
    )
    # D-151a: SQL is never shown, also in reports saved before the "Data used" section
    body = fence_untrusted(
        KIND_REPORT, strip_sql(rec.body_markdown), item_id=rec.report_id, max_chars=MAX_BODY_CHARS
    )
    return ViewResult("ok", f"{head}\n{body}")


def _count(n: int, truncated: bool) -> str:
    return f"at least {n}" if truncated else str(n)


def count_text(result: ListResult) -> str:
    """The match count for a header: "at least N" when the scan was capped (OD-9)."""
    return _count(result.total, result.truncated_scan)


def render_list(result: ListResult, *, header: str, empty: str) -> str:
    """Plain text lines: id, date, title, tags, session. A masked entry shows no title."""
    if not result.entries:
        if result.truncated_scan:
            return f"{empty} (only your newest {MAX_LIST} reports were searched)"
        return empty
    lines = [header]
    for e in result.entries:
        if e.masked:
            lines.append(f"  {e.report_id}  {e.created_at[:10]}  ({DRIFT_LABEL})")
            continue
        tags = f"  [{', '.join(e.tags)}]" if e.tags else ""
        lines.append(
            f"  {e.report_id}  {e.created_at[:10]}  {e.title}{tags}  session {e.session_id[:12]}"
        )
    if result.total > len(result.entries):
        more = result.total - len(result.entries)
        lines.append(
            f"  ... and {_count(more, result.truncated_scan)} more "
            f"({count_text(result)} in total)"
        )
    if result.truncated_scan:
        lines.append(f"  (only your newest {MAX_LIST} reports were searched)")
    return "\n".join(lines)


@dataclass(frozen=True)
class OpenResult:
    status: str  # "ok" | "not_found" | "scope_drift" | "ambiguous" | "no_row"
    text: str
    listing: tuple[str, ...] = ()  # ids shown by an "ambiguous" answer, for "/open <n>"


NO_ROW_TEXT: Final = "No such row in the last list. Run /reports or /search first."
AMBIGUOUS_HEAD: Final = "Several of your reports match; open one by id or row number:"


def open_report(
    store: Any,
    owner: str,
    scope: ProductScope | None,
    ref: str,
    listing: Sequence[str] = (),
) -> OpenResult:
    """AC-21.3 / AC-21.11: open by id, by a bare row number of the last listing, or by a phrase
    (the matcher over the owner's in-scope reports). Owner and scope are re-checked here on
    every path; a number only picks an id from the listing, it never bypasses ``view_report``."""
    ref = strip_display_prefix(" ".join(str(ref).split()))
    if _ROW_RE.fullmatch(ref):
        n = int(ref)
        if not 1 <= n <= len(listing):
            return OpenResult("no_row", NO_ROW_TEXT)
        v = view_report(store, owner, scope, listing[n - 1])
        return OpenResult(v.status, v.text)
    if " " not in ref and len(ref) <= MAX_ID_CHARS and store.get(ref, owner) is not None:
        v = view_report(store, owner, scope, ref)
        return OpenResult(v.status, v.text)
    try:
        found = owner_matches(store, owner, scope, ref)
    except MatchError:
        return OpenResult("not_found", NOT_FOUND_TEXT)
    if not found.rows:
        return OpenResult("not_found", NOT_FOUND_TEXT)
    if len(found.rows) == 1:
        v = view_report(store, owner, scope, found.rows[0].report_id)
        return OpenResult(v.status, v.text)
    shown = found.rows[:MAX_RESULTS]
    res = ListResult(
        tuple(_entry(r, scope) for r in shown), len(found.rows), found.truncated
    )
    return OpenResult(
        "ambiguous",
        render_list(res, header=AMBIGUOUS_HEAD, empty=NOT_FOUND_TEXT),
        tuple(r.report_id for r in shown),
    )
