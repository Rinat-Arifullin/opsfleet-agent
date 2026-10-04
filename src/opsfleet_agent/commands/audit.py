"""`/audit [--session|--user]` viewer (FR-28, AC-28.5, SEC-17; HLD §6.3.3). Plain function.

CLI-only: this module is reached from the CLI command dispatcher and is not a tool in any
role's registry (``tools/registry.py``), so the agent can neither read nor change the log.
No LLM call. It shows the current user's own events only: ``--session`` (the default) for
this session, ``--user`` for all of this user's sessions. Newest first, one line per event:
time, event type, owner, session, turn, pending action, report ids and count, rule, outcome
and the allowlisted detail codes. The rows hold ids and codes only, so there is no report
body or user text to show; every value is still passed through the shared secret scrub.
"""

from __future__ import annotations

from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.store.audit import AuditError, AuditEvent, AuditLog

USAGE = "Usage: /audit [--session|--user]"
EMPTY = "No audit events."
UNAVAILABLE = "The audit log is unavailable."
DEFAULT_LIMIT = 50
MAX_IDS_SHOWN = 10
_DETAIL_KEYS = ("category", "code", "class", "stage", "cause", "source", "error_type")


def parse_args(args: str) -> str:
    """Return ``"session"`` or ``"user"``. Raises ValueError with the usage line."""
    parts = (args or "").split()
    if not parts:
        return "session"
    if len(parts) == 1 and parts[0] in ("--session", "--user"):
        return parts[0][2:]
    raise ValueError(USAGE)


def _ids(event: AuditEvent) -> str:
    ids = list(event.target_ids[:MAX_IDS_SHOWN])
    more = len(event.target_ids) - len(ids)
    return ",".join(ids) + (f",+{more}" if more > 0 else "")


def format_event(event: AuditEvent) -> str:
    parts = [
        event.ts,
        event.event_type,
        f"owner={event.actor_user_id}",
        f"session={event.session_id}",
        f"turn={event.turn_id}",
    ]
    if event.pending_action_id:
        parts.append(f"action={event.pending_action_id}")
    if event.target_ids:
        parts.append(f"reports={_ids(event)}")
    if event.count is not None:
        parts.append(f"count={event.count}")
    for name in ("rule", "outcome"):
        value = getattr(event, name)
        if value:
            parts.append(f"{name}={value}")
    for key in _DETAIL_KEYS:
        value = event.details.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    return tr.scrub_text("  ".join(parts), 1000)


def render_audit(
    args: str,
    *,
    log: AuditLog,
    user_id: str,
    session_id: str,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Render the viewer text for ``/audit``. Never raises for bad input or a broken store."""
    try:
        mode = parse_args(args)
    except ValueError as err:
        return str(err)
    try:
        events = log.events(
            user_id=user_id,
            session_id=session_id if mode == "session" else None,
            newest_first=True,
            limit=limit,
        )
    except AuditError:
        return UNAVAILABLE
    if not events:
        return EMPTY
    scope = "this session" if mode == "session" else "all your sessions"
    header = f"Audit events for {scope} (newest first, up to {limit}):"
    return "\n".join([header, *(format_event(e) for e in events)])
