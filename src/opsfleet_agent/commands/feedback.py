"""`/feedback up|down [reason] [comment]` (FR-46, AC-25.1, AC-25.2). Plain function; wired in 19.

No LLM call. The comment is PII-scrubbed (injectable `scrubber`, default: the regex scrubber;
swap in the NER detector in 12/14a) and secret-scrubbed before storage, then capped at 500
characters. The trace event carries allowlisted fields only and never the comment text.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from opsfleet_agent.guards import pii_regex
from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.store.feedback import MAX_COMMENT, REASONS, FeedbackStore

log = logging.getLogger(__name__)

USAGE = f"Usage: /feedback up|down [{'|'.join(REASONS)}] [comment]"
NOTHING = "Nothing to rate yet."
_RAW_CAP = 4 * MAX_COMMENT  # bound the work before scrubbing


def regex_scrubber(text: str) -> str:
    return pii_regex.scrub(text).text


@dataclass(frozen=True)
class ParsedFeedback:
    rating: str
    reason: str | None
    comment: str | None


def parse_args(args: str) -> ParsedFeedback:
    """Parse `up|down [reason] [comment]`. Raises ValueError with a user-facing message."""
    parts = (args or "").strip().split(None, 1)
    if not parts:
        raise ValueError(USAGE)
    rating = parts[0].lower()
    if rating not in ("up", "down"):
        raise ValueError(f"Rating must be 'up' or 'down'. {USAGE}")
    rest = parts[1].strip() if len(parts) > 1 else ""
    reason = None
    if rest:
        head, _, tail = rest.partition(" ")
        if head.lower() in REASONS:
            reason, rest = head.lower(), tail.strip()
    if reason and rating != "down":
        raise ValueError("A reason can only be given with 'down'.")
    return ParsedFeedback(rating, reason, rest or None)


def handle_feedback(
    args: str,
    *,
    store: FeedbackStore,
    user_id: str,
    session_id: str,
    last_turn_id: str | None,
    last_trace_id: str | None = None,
    turn_id: str | None = None,
    trace_id: str | None = None,
    tracer: tr.Tracer | None = None,
    scrubber: Callable[[str], str] = regex_scrubber,
) -> str:
    """Store feedback for the last answered turn (or an explicit `turn_id`/`trace_id`).

    `last_turn_id` is the last *answered* turn (None -> "nothing to rate yet").
    Returns the one-line reply.
    """
    target = turn_id or last_turn_id
    if not target:
        return NOTHING
    try:
        p = parse_args(args)
    except ValueError as exc:
        return str(exc)
    target_trace = trace_id if turn_id else (trace_id or last_trace_id)

    comment = None
    note = ""
    if p.comment:
        try:
            comment = scrubber(p.comment[:_RAW_CAP])  # PII first, then secrets + length cap
            comment = tr.scrub_text(comment, MAX_COMMENT)
        except Exception as exc:  # noqa: BLE001 - fail closed: never store the raw comment
            log.warning("feedback comment scrub failed: %s", tr.format_error(exc))
            comment, note = None, " (comment dropped: it could not be redacted)"

    rec, replaced = store.add(
        user_id=user_id,
        session_id=session_id,
        turn_id=target,
        trace_id=target_trace,
        rating=p.rating,
        comment=comment,
        reason=p.reason,
    )
    if tracer is not None:
        try:
            # No feedback span type exists in tracer.SPAN_TYPES; the allowlisted "tool" span
            # carries it. Only ids, rating and reason: never the comment.
            tracer.record(
                "tool",
                "feedback",
                tool="feedback",
                outcome=rec.rating,
                error_code=rec.reason,
                turn_id=rec.turn_id,
                trace_id=rec.trace_id,
                status="ok",
            )
        except Exception as exc:  # noqa: BLE001 - tracing must not fail the command
            log.warning("feedback trace failed: %s", tr.format_error(exc))
    verb = "updated" if replaced else "recorded"
    return f"Thanks, feedback ({rec.rating}) {verb} for turn {rec.turn_id}.{note}"
