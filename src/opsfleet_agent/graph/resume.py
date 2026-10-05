"""Narrow crash resume: finish ONLY the interrupted turn of a session (HLD §4.0.6, FR-76).

:func:`resume_turn` reads the session's last checkpoint and decides, in this order:

1. the checkpoint store cannot be opened (missing or invalid ``LANGGRAPH_AES_KEY``) or read
   (a wrong key fails the AES MAC check): refuse with one actionable line. Nothing is decrypted
   into the turn and nothing is replayed;
2. no checkpoint at all: nothing to resume (one line);
3. the stored ``owner`` is not the current profile's ``user_id`` (or is missing): no replay,
   a new empty session. Checked BEFORE "no pending node", so another user's finished and
   unfinished sessions get the same reply (no state oracle, iteration 19 OD-1). Then no
   pending node: nothing to resume (one line). Then the stored ``scope_snapshot`` differs
   from the current profile's scope (or is missing): no replay, a new empty session
   (FR-76, fail closed);
4. the turn stopped before ``input_guard`` finished: the raw text is never stored, so the
   question cannot be replayed; the user asks again (no replay);
5. otherwise ``invoke(None)`` finishes the interrupted turn with its TurnBudget counters and
   SQL ledger restored from state (completed queries are not re-run). A new turn never starts.

Bounded, not exact (R2-m6): a crash inside a node re-runs that node, so at most one node's
calls and queries can repeat.

Seams (later iterations): a pending delete expires and a pending draft is re-shown at step 5
(iterations 22a and 17); they add their own branches on ``PendingTurn.next``.

Never raises and never shows a traceback: every failure is a typed :class:`ResumeOutcome`.

:func:`close_interrupted_turn` makes a Ctrl-C cancel durable: the cancelled turn is closed
in the checkpoint, so a later ``--resume`` never replays it (iteration 19).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from opsfleet_agent.config import ConfigError
from opsfleet_agent.graph.graph import AES_KEY_ENV, AgentGraph, TurnResult, scope_snapshot
from opsfleet_agent.guards.scope import ProductScope
from opsfleet_agent.session import Profile, Session

__all__ = ["ResumeKind", "ResumeOutcome", "close_interrupted_turn", "resume_turn"]

logger = logging.getLogger(__name__)

NOTHING_PENDING_TEXT: Final = "Nothing to resume: the last turn of this session had finished."
KEY_REFUSED_TEXT: Final = (
    f"Cannot resume: {AES_KEY_ENV} is missing or is not the key this session was saved with; "
    "set the original key, or start a new session."
)
SCOPE_DRIFT_TEXT: Final = (
    "Your product access changed since this session was saved, so it was not resumed; "
    "starting a new session."
)
ASK_AGAIN_TEXT: Final = (
    "The interrupted question was not saved (your raw text is never stored); please ask it again."
)
FAILED_TEXT: Final = "Could not resume this session; starting a new session."
OWNER_TEXT: Final = (
    "This session was not saved under the current profile, so it was not resumed; "
    "starting a new session."
)
STORE_REFUSED_TEXT: Final = (
    "Cannot resume: the saved session store could not be opened; check the data directory, "
    "or start a new session."
)
_MAC_FAILURE: Final = "MAC check failed"  # pycryptodome AES-EAX: a wrong LANGGRAPH_AES_KEY
_INPUT_NODE: Final = "input_guard"
_FINAL_NODE: Final = "finalize"


class ResumeKind(StrEnum):
    NOTHING_PENDING = "nothing_pending"
    RESUMED = "resumed"
    NEW_SESSION = "new_session"  # scope drift (FR-76) or an unreadable profile/state
    KEY_REFUSED = "key_refused"
    STORE_REFUSED = "store_refused"  # the checkpoint store cannot be opened (not a key issue)
    ASK_AGAIN = "ask_again"


@dataclass(frozen=True)
class ResumeOutcome:
    """What the CLI shows and does. ``start_new_session``: open a new empty session."""

    kind: ResumeKind
    text: str
    result: TurnResult | None = None
    start_new_session: bool = False
    turn_id: str | None = None  # the finished turn (RESUMED), for /feedback and /trace


def resume_turn(
    agent: AgentGraph | Callable[[], AgentGraph], session_id: str, profile: Profile
) -> ResumeOutcome:
    """Finish the interrupted turn of ``session_id`` under ``profile``, or say why not.

    ``agent`` may be a factory, so a checkpoint store that cannot be opened (a missing or
    invalid key raises :class:`ConfigError` in ``build_checkpointer``) becomes the one-line
    refusal instead of a traceback.
    """
    try:
        current = scope_snapshot(ProductScope.from_profile(profile))
    except Exception as exc:  # an invalid profile never resumes (fail closed)
        logger.error("resume: profile scope invalid: %s", type(exc).__name__)
        return ResumeOutcome(ResumeKind.NEW_SESSION, FAILED_TEXT, start_new_session=True)
    try:
        graph = agent if isinstance(agent, AgentGraph) else agent()
    except ConfigError as exc:  # fixed texts only: the exception text is never echoed
        if AES_KEY_ENV in str(exc):
            return ResumeOutcome(ResumeKind.KEY_REFUSED, KEY_REFUSED_TEXT)
        logger.error("resume: checkpoint store unavailable: %s", type(exc).__name__)
        return ResumeOutcome(ResumeKind.STORE_REFUSED, STORE_REFUSED_TEXT)
    except Exception as exc:
        logger.error("resume: agent build failed: %s", type(exc).__name__)
        return ResumeOutcome(ResumeKind.NEW_SESSION, FAILED_TEXT, start_new_session=True)
    try:
        pending = graph.open_resume(Session(session_id, profile))
    except ValueError as exc:
        logger.error("resume: checkpoint read failed: %s", type(exc).__name__)
        if _MAC_FAILURE in str(exc):  # a wrong key, never silently empty state
            return ResumeOutcome(ResumeKind.KEY_REFUSED, KEY_REFUSED_TEXT)
        return ResumeOutcome(ResumeKind.STORE_REFUSED, STORE_REFUSED_TEXT)
    except Exception as exc:
        logger.error("resume: checkpoint read failed: %s", type(exc).__name__)
        return ResumeOutcome(ResumeKind.NEW_SESSION, FAILED_TEXT, start_new_session=True)
    if pending._built is None:  # no checkpoint at all for this id
        return ResumeOutcome(ResumeKind.NOTHING_PENDING, NOTHING_PENDING_TEXT)
    if pending.values.get("owner") != profile.user_id:  # missing owner: never resumed
        return ResumeOutcome(ResumeKind.NEW_SESSION, OWNER_TEXT, start_new_session=True)
    if not pending.next:
        return ResumeOutcome(ResumeKind.NOTHING_PENDING, NOTHING_PENDING_TEXT)
    if pending.values.get("scope_snapshot") != current:
        return ResumeOutcome(ResumeKind.NEW_SESSION, SCOPE_DRIFT_TEXT, start_new_session=True)
    if _INPUT_NODE in pending.next:
        return ResumeOutcome(ResumeKind.ASK_AGAIN, ASK_AGAIN_TEXT)
    # seam (22a/17): a pending delete expires and a draft is re-shown here, before finish()
    turn_id = str(pending.values.get("turn_id") or "") or None
    result = pending.finish()
    return ResumeOutcome(ResumeKind.RESUMED, result.text, result=result, turn_id=turn_id)


def close_interrupted_turn(agent: object, session: Session, turn_id: str | None) -> bool:
    """Close the pending turn of ``session`` in its checkpoint after a Ctrl-C cancel.

    Only the session's own pending turn is closed: the stored ``owner`` must be the
    session's user and, when ``turn_id`` is given, the stored ``turn_id`` must match it.
    The close is a state write "as if ``finalize`` ran" (outcome ``cancelled``), so the
    checkpoint has no pending node and :func:`resume_turn` returns NOTHING_PENDING instead
    of replaying the cancelled turn. Local only (no LLM, no BigQuery). Never raises;
    returns whether a turn was closed. Anything other than an :class:`AgentGraph` is a
    no-op (test fakes).
    """
    if not isinstance(agent, AgentGraph):
        return False
    try:
        pending = agent.open_resume(session)
        if pending._built is None or not pending.next:
            return False
        if pending.values.get("owner") != session.profile.user_id:
            return False
        if turn_id is not None and pending.values.get("turn_id") != turn_id:
            return False
        compiled = pending._built[1]
        config = agent._config(session.session_id)
        compiled.update_state(config, {"outcome": "cancelled"}, as_node=_FINAL_NODE)
        return True
    except Exception as exc:  # best effort: the cancel itself already happened
        logger.error("cancel close failed: %s", type(exc).__name__)
        return False
