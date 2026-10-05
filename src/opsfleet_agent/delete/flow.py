"""Two-phase delete of the user's own saved reports (iteration 22a, ADR-007, HLD §6.3.3).

The flow is code, never the model:

1. **Preview** (:meth:`DeleteService.preview`): parse the stated selector, compute the
   owner-scoped match, write ``delete.previewed`` and return the pending action plus a prompt
   rendered by code (count, up to :data:`PREVIEW_SIZE` lines, "and M more", the backup
   notice). The pending action holds ``sha256(token)``, never the token (``delete.token``).
2. **Confirm** (:meth:`DeleteService.confirm`): only the NEXT user turn, only an exact
   confirmation word (or the typed count above :data:`PREVIEW_SIZE`), only with a valid
   HMAC proof for this pending action. Anything else cancels (or expires) and is audited.
   Confirm never deletes.
3. **Execute** (:meth:`DeleteService.execute`): needs the ``delete.confirmed`` record,
   recomputes ownership and ``ids_sha256``, and calls :func:`store.audit.audited_delete`,
   which writes ``delete.executed`` first in the same transaction as the DELETE. An audit
   failure deletes nothing and says so (AC-28.2).

Every public method returns a :class:`Step` and never raises: an unexpected error is an
"unsafe" step that deletes nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from opsfleet_agent.delete.token import (
    DeleteKey,
    derive_token,
    forget_secret,
    ids_sha256,
    make_proof,
    new_key,
    token_sha256,
    verify_proof,
)
from opsfleet_agent.obs.tracer import register_secret
from opsfleet_agent.reports.matcher import (
    MatchError,
    delete_candidates,
    in_scope,
    owner_rows,
    session_candidates,
)
from opsfleet_agent.store import audit as A

logger = logging.getLogger(__name__)

__all__ = [
    "BACKUP_NOTICE",
    "KIND",
    "PREVIEW_SIZE",
    "DeleteRequest",
    "DeleteService",
    "Step",
    "check_tool_request",
    "gate_step",
    "parse_delete_request",
    "setup_delete",
]

KIND: Final = "saved_report"
PREVIEW_SIZE: Final = 20  # lines shown; above this the user types the count (AC-21.7)
MAX_MATCHES: Final = 100  # one delete never targets more (audit MAX_TARGET_IDS is 1000)
EXPIRY_S: Final = 600  # 10 min (22b adds the other expiry paths)
EXECUTE_GRACE_S: Final = 60  # execute only this soon after delete.confirmed (OD-10)
HELD_MAX: Final = 256  # pending actions whose proof/secrets stay in memory (OD-11, OD-12)
MAX_REQUEST_CHARS: Final = 2000
TITLE_CHARS: Final = 80
DELETE_TOOL: Final = "delete_reports"
TAINT_TOOLS: Final = frozenset({"view_report", "search_reports"})

BACKUP_NOTICE: Final = (
    "Deleted reports may stay in backups for up to 7 days and are never restored."
)
SELECTOR_EMPTY: Final = "SELECTOR_EMPTY"
SELECTOR_EMPTY_TEXT: Final = (
    "Please name the reports to delete more specifically (at least 3 letters or digits, no "
    "wildcards) or by id."
)
NONE_TEXT: Final = "Nothing matched (not found among your saved reports), so nothing was deleted."
TOO_MANY_TEXT: Final = (
    f"More than {MAX_MATCHES} reports match, so nothing was deleted. Please name fewer reports."
)
UNSAFE_TEXT: Final = "The delete could not be completed safely; nothing was deleted."
CANCELLED_TEXT: Final = "Nothing was deleted."
EXPIRED_TEXT: Final = "That delete request expired, so nothing was deleted. Please ask again."
UNAVAILABLE_TEXT: Final = "Deleting reports is turned off right now, so nothing was deleted."
DELETE_PENDING_TEXT: Final = (
    "A delete was waiting for your confirmation; it was cancelled and nothing was deleted. "
    "Send the new delete request again."
)
SET_CHANGED: Final = "set_changed"  # a previewed report vanished or was re-owned (OD-14)
STRANDED_TEXT: Final = (
    "A confirmed delete was interrupted before it ran, so nothing was deleted. "
    "Ask again if you still want it."
)
INTERRUPTED: Final = "interrupted"  # a Ctrl-C at the confirm or execute stage (MJ-1)
STRANDED: Final = "stranded"  # an execute left pending, found by the next turn (MJ-1)

CONFIRM_WORDS: Final = frozenset({"yes", "y", "confirm", "delete"})
INTENT_RE: Final = re.compile(r"\b(?:delete|remove|erase)\b", re.I | re.A)
# Natural language: the request STARTS with the verb, and its direct object is "report(s)",
# a report id or "this session's reports" (M2). "remove the cancelled orders from the
# revenue report" names orders, not reports, so it is analysis, never a delete. The object
# ends the clause or is followed by a selector word (mn-2): "remove the report header",
# "delete the report's second chart" and "remove report-level duplicates" are analysis.
_OBJECT_END: Final = (
    r"(?=\s*(?:$|[.;!?,:\n])"
    r"|\s+(?:about|titled|named|called|from|with|on|matching|containing|created|that|which"
    r"|in\s+(?:this|current)\s+session"
    r"|not|except|excluding|exclude|other\s+than|but|without|besides|apart)\b"
    r"|\s+[0-9a-f]{32}\b|\s+[%_*?])"  # a wildcard reaches the parser to be refused
)
_NL_RE: Final = re.compile(
    r"^(?:please\s+)?(?:delete|remove|erase)\b"
    r"(?=\s+(?:"
    r"(?:(?:all\s+(?:of\s+)?)?(?:my|the|these|those|our)\s+|all\s+)?(?:saved\s+)?reports?"
    + _OBJECT_END
    + r"|(?:the\s+)?(?:this|current)\s+session(?:'s|s)?\s+(?:saved\s+)?reports?"
    + _OBJECT_END
    + r"|(?:reports?\s+)?[0-9a-f]{32}\b"
    r"))",
    re.I | re.A,
)
_ID_RE: Final = re.compile(r"\b[0-9a-f]{32}\b", re.I | re.A)  # stored lowercase (mn-3)
# mn-1: a negated or exclusive selector ("not from this session", "all except ...") is
# never inverted or narrowed by guesswork: it is refused as too broad
_NEGATION_RE: Final = re.compile(
    r"\b(?:not|except|excluding|exclude|other\s+than|but|without|besides|apart\s+from)\b"
    r"|n't\b",
    re.I | re.A,
)
_SESSION_RE: Final = re.compile(r"\b(?:this|current)\s+session\b", re.I | re.A)
_SENTENCE_RE: Final = re.compile(r"[.;!?\n]")
_CLAUSE_RE: Final = re.compile(r"[.;!?\n,]")
_WILDCARD_RE: Final = re.compile(r"[%_*?]")
_FILLER: Final = frozenset(
    "please all any every my the our saved report reports about on named called titled "
    "matching with for of from in created that which those these".split()
)
_ID_FILLER: Final = frozenset({"and", "id", "ids", ","})


# --- request parsing ------------------------------------------------------------------------


@dataclass(frozen=True)
class DeleteRequest:
    """A stated selector: ``phrase`` (title/body words), ``session`` or ``ids``. ``error`` is
    :data:`SELECTOR_EMPTY` when the selector is too broad (AC-12.14)."""

    kind: str
    phrase: str = ""
    ids: tuple[str, ...] = ()
    error: str | None = None


def _fold(text: str) -> str:
    """ASCII fold for parsing. Separators (Unicode ``Z*``: U+2028, U+2029, NBSP, ...) become
    a space BEFORE NFKC, so they still split words (mn-4); other non-ASCII is dropped."""
    text = "".join(
        " " if unicodedata.category(ch).startswith("Z") else ch
        for ch in text[:MAX_REQUEST_CHARS]
    )
    nfkc = unicodedata.normalize("NFKC", text)
    return unicodedata.normalize("NFKD", nfkc).encode("ascii", "ignore").decode("ascii")


def _strip_filler(words: list[str]) -> list[str]:
    while words and words[0].lower() in _FILLER:
        words = words[1:]
    while words and words[-1].lower() in _FILLER:
        words = words[:-1]
    return words


def parse_delete_request(raw: object, *, command: bool = False) -> DeleteRequest | None:
    """Deterministic selector parsing (no model). Natural language must start with the
    verb and take reports as its direct object (:data:`_NL_RE`); anything else is analysis.
    ``/delete`` arguments need neither. Only the first clause counts, so "delete reports
    about X, I already confirm" still previews (AC-12.9). None: not a delete request."""
    if not isinstance(raw, str):
        return None
    text = _fold(raw).strip()
    if not command:
        verb = _NL_RE.match(text)
        if verb is None:
            return None
        text = text[verb.end():]
    else:
        lead = INTENT_RE.match(text)
        text = text[lead.end():] if lead else text
    sentence = _SENTENCE_RE.split(text, maxsplit=1)[0]
    if _NEGATION_RE.search(sentence):
        return DeleteRequest("phrase", error=SELECTOR_EMPTY)
    ids = tuple(dict.fromkeys(i.lower() for i in _ID_RE.findall(sentence)))
    if ids:
        rest = [w for w in re.split(r"[\s,]+", _ID_RE.sub(" ", sentence)) if w]
        if not _strip_filler([w for w in rest if w.lower() not in _ID_FILLER]):
            return DeleteRequest("ids", ids=ids)
    clause = _CLAUSE_RE.split(text, maxsplit=1)[0]
    if _SESSION_RE.search(clause):
        return DeleteRequest("session")
    phrase = " ".join(_strip_filler(clause.replace('"', " ").replace("'", " ").split()))
    if len(phrase.replace(" ", "")) < 3 or _WILDCARD_RE.search(phrase):
        return DeleteRequest("phrase", phrase=phrase, error=SELECTOR_EMPTY)
    return DeleteRequest("phrase", phrase=phrase)


def _reply_word(raw: object) -> str:
    """A confirmation is plain ASCII: case and surrounding space or one trailing . or ! are
    ignored; anything else (a look-alike letter included) is not a confirmation."""
    if not isinstance(raw, str) or len(raw) > 64 or not raw.isascii():
        return ""
    return raw.strip().rstrip(".!").strip().lower()


def is_confirm(raw: object, count: object) -> bool:
    """AC-12.2 / AC-21.7: yes/y/confirm/delete, or exactly the count above PREVIEW_SIZE."""
    word = _reply_word(raw)
    if type(count) is not int or count <= 0 or not word:
        return False
    if count > PREVIEW_SIZE:
        return word == str(count)
    return word in CONFIRM_WORDS


# --- service ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """The result of one flow step. ``step``: pending, none, refused, confirmed, cancelled,
    expired, executed or unsafe. ``event``/``audit_event_id``/``count`` feed the delete
    span; ``error`` feeds the error span (a code, never free text)."""

    step: str
    text: str
    pending: dict[str, Any] | None = None
    event: str | None = None
    audit_event_id: str | None = None
    count: int = 0
    error: str | None = None

    @property
    def outcome(self) -> str:
        return f"delete_{self.step}"


def _title(report: Any, scope: Any) -> str:
    if not in_scope(report, scope):
        return "(created under a different product scope)"
    return " ".join(str(report.title).split())[:TITLE_CHARS]


def _fields(pending: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("pending_action_id", "ids_sha256", "owner", "session_id", "preview_turn",
            "expires_at")  # fmt: skip
    return {k: pending[k] for k in keys}


class DeleteService:
    """Process-wide delete flow over one audit log and report store. ``K_delete`` lives only
    in this object (``self._key``), and so do, per pending action, the confirmation proof,
    the confirm time and the secrets registered with the scrubber (``self._held``, the
    newest :data:`HELD_MAX`). A restart loses all of it: nothing staged before can confirm
    or execute (OD-10)."""

    def __init__(
        self,
        audit: A.AuditLog,
        store: Any,
        *,
        clock: Callable[[], float] = time.time,
        key: DeleteKey | None = None,
    ) -> None:
        self.audit = audit
        self.store = store
        self.clock = clock
        self._key = key or new_key()
        self._held: OrderedDict[str, dict[str, Any]] = OrderedDict()

    # -- in-memory secrets (never in state, a checkpoint, a trace or a log) ---------------

    def _hold(self, pid: str) -> dict[str, Any]:
        held = self._held.get(pid)
        if held is not None:
            self._held.move_to_end(pid)
            return held
        held = self._held[pid] = {"secrets": set(), "proof": None, "confirmed_at": None}
        while len(self._held) > HELD_MAX:  # bounded: the oldest action is forgotten
            _, old = self._held.popitem(last=False)
            for value in old["secrets"]:
                forget_secret(value)
        return held

    def _register(self, pid: str, value: str) -> None:
        """Register a token or proof with the scrubber once per pending action (m4)."""
        held = self._hold(pid)
        if value not in held["secrets"]:
            held["secrets"].add(value)
            register_secret(value)

    def _token(self, fields: Mapping[str, Any]) -> str:
        token = derive_token(self._key, fields, register=False)
        self._register(str(fields["pending_action_id"]), token)
        return token

    def _token_ok(self, pending: Mapping[str, Any]) -> bool:
        """The stored sha256(token) matches the token re-derived under this process's key.
        Raises on malformed binding fields (callers fail closed)."""
        stored = pending.get("token_sha256")
        token = self._token(_fields(pending))
        return isinstance(stored, str) and hmac.compare_digest(
            token_sha256(token).encode(), stored.encode()
        )

    # -- preview ------------------------------------------------------------------------

    def find(self, req: DeleteRequest, *, owner: str, scope: Any, session_id: str):
        """(rows, missing ids, scan truncated). Owner-scoped in every branch (AC-12.4)."""
        if req.kind == "ids":
            rows = [r for r in (self.store.get(i, owner) for i in req.ids) if r is not None]
            return rows, len(req.ids) - len(rows), False
        if req.kind == "session":  # AC-12.5 / AC-21.6: created in THIS session only
            all_rows, truncated = owner_rows(self.store, owner)
            return session_candidates(all_rows, session_id), 0, truncated
        found = delete_candidates(self.store, owner, req.phrase, scope)
        return list(found.rows), 0, found.truncated

    def preview(
        self, req: DeleteRequest | None, *, owner: str, scope: Any, session_id: str,
        turn_id: str, preview_turn: int,
    ) -> Step:  # fmt: skip
        if req is None or req.error:
            return Step("refused", SELECTOR_EMPTY_TEXT, error=SELECTOR_EMPTY)
        try:
            rows, missing, truncated = self.find(
                req, owner=owner, scope=scope, session_id=session_id
            )
        except MatchError:
            return Step("refused", SELECTOR_EMPTY_TEXT, error=SELECTOR_EMPTY)
        except Exception as exc:  # noqa: BLE001 - a store failure deletes nothing
            logger.error("delete preview failed: %s", type(exc).__name__)
            return Step("unsafe", UNSAFE_TEXT, error=type(exc).__name__)
        if not rows:
            return Step("none", NONE_TEXT)  # AC-12.8: no question asked
        if len(rows) > MAX_MATCHES:
            return Step("refused", TOO_MANY_TEXT)
        try:
            return self._stage(rows, owner=owner, scope=scope, session_id=session_id,
                               turn_id=turn_id, preview_turn=preview_turn, missing=missing,
                               truncated=truncated)  # fmt: skip
        except A.AuditError as exc:
            logger.error("delete preview audit failed: %s", type(exc).__name__)
            return Step("unsafe", UNSAFE_TEXT, error=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            logger.error("delete preview failed: %s", type(exc).__name__)
            return Step("unsafe", UNSAFE_TEXT, error=type(exc).__name__)

    def _stage(
        self, rows: Sequence[Any], *, owner: str, scope: Any, session_id: str, turn_id: str,
        preview_turn: int, missing: int, truncated: bool,
    ) -> Step:  # fmt: skip
        report_ids = [r.report_id for r in rows]
        pid = hashlib.sha256(f"{session_id}:{turn_id}:delete".encode()).hexdigest()
        fields = {
            "pending_action_id": pid,
            "ids_sha256": ids_sha256(report_ids),
            "owner": owner,
            "session_id": session_id,
            "preview_turn": int(preview_turn),
            "expires_at": int(self.clock()) + EXPIRY_S,
        }
        token = self._token(fields)
        pending = {
            **fields,
            "token_sha256": token_sha256(token),
            "report_ids": report_ids,
            "count": len(report_ids),
            "kind": KIND,
            "step": "preview",
        }
        ev = self.audit.record(  # idempotent: a replayed preview finds the first row
            A.DELETE_PREVIEWED, actor_user_id=owner, session_id=session_id, turn_id=turn_id,
            pending_action_id=pid, target_ids=report_ids, count=len(report_ids),
            outcome="previewed", details={"source": "delete", "kind": KIND},
        )  # fmt: skip
        if ev is None:
            ev = self.audit.get(pid, A.DELETE_PREVIEWED)
        text = render_prompt(rows, scope, missing=missing, truncated=truncated)
        return Step("pending", text, pending=pending, event=A.DELETE_PREVIEWED,
                    audit_event_id=ev.event_id if ev else None, count=len(report_ids))  # fmt: skip

    def is_live(self, pending: Mapping[str, Any]) -> bool:
        """Read-only: the pending action was staged under this process's key and has not
        expired (crash resume re-shows it, else says it expired). Never raises."""
        return self.lapse_reason(pending) is None

    def lapse_reason(self, pending: Mapping[str, Any]) -> str | None:
        """Read-only: why a pending action can no longer be confirmed: ``key_changed`` (a
        restart made a new K_delete, or tampered state) or ``timeout``; None if live."""
        try:
            if not self._token_ok(pending):
                return "key_changed"
            return None if self.clock() < pending["expires_at"] else "timeout"
        except Exception:  # noqa: BLE001
            return "key_changed"

    def _terminal(self, pid: object) -> Any:
        """The terminal audit row (EXECUTED, CANCELLED or EXPIRED) of an action, or None.
        The same lookup the replay check uses: one terminal row per action, on every path."""
        if not isinstance(pid, str) or not pid:
            return None
        for event in (A.DELETE_EXECUTED, A.DELETE_CANCELLED, A.DELETE_EXPIRED):
            row = self.audit.get(pid, event)
            if row is not None:
                return row
        return None

    @staticmethod
    def _reported(row: Any) -> Step:
        """Report an action's existing terminal row without writing a new one."""
        if row.event_type == A.DELETE_EXECUTED:
            return Step("executed", _deleted_text(row.count or 0), event=A.DELETE_EXECUTED,
                        audit_event_id=row.event_id, count=row.count or 0)  # fmt: skip
        cancelled = row.event_type == A.DELETE_CANCELLED
        return Step("cancelled" if cancelled else "expired",
                    CANCELLED_TEXT if cancelled else EXPIRED_TEXT, event=row.event_type,
                    audit_event_id=row.event_id, count=0,
                    error=str((row.details or {}).get("error_type") or "") or None)  # fmt: skip

    # -- confirm ------------------------------------------------------------------------

    def reply_payload(self, raw: object, pending: Mapping[str, Any]) -> dict[str, str]:
        """The resume value for a reply. The proof binds a confirmation to this pending
        action; it stays in memory and the resume value (which LangGraph writes to the
        checkpoint) carries only ``proof_sha256`` (OD-11). A non-confirmation carries ""."""
        pid = str(pending.get("pending_action_id") or "")
        word = _reply_word(raw)
        out = {"reply": word, "pending_action_id": pid, "proof_sha256": ""}
        if not is_confirm(word, pending.get("count")) or not pid:
            return out
        try:
            fields = _fields(pending)
            proof = make_proof(self._token(fields), word, fields, register=False)
        except (KeyError, TypeError):
            return out
        self._register(pid, proof)
        self._hold(pid)["proof"] = proof
        return {**out, "proof_sha256": token_sha256(proof)}

    def _proof_ok(self, pid: str, token: str, fields: Mapping[str, Any], pay: Mapping) -> bool:
        held = self._held.get(pid)
        proof = held["proof"] if held else None
        claimed = pay.get("proof_sha256")
        return (
            isinstance(proof, str)
            and isinstance(claimed, str)
            and hmac.compare_digest(token_sha256(proof).encode(), claimed.encode())
            and verify_proof(token, pay["reply"], fields, proof)
        )

    def confirm(
        self, pending: Mapping[str, Any], payload: object, *, turn: int, owner: str,
        session_id: str, turn_id: str,
    ) -> Step:  # fmt: skip
        """Checks in order; the first failure cancels (or expires) and is audited. Never
        deletes (execution is a separate node)."""
        try:
            return self._confirm(pending, payload, turn=turn, owner=owner,
                                 session_id=session_id, turn_id=turn_id)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - AuditError included: fail closed
            logger.error("delete confirm failed: %s", type(exc).__name__)
            return Step("unsafe", UNSAFE_TEXT, error=type(exc).__name__)

    def _close(self, event: str, pending: Mapping[str, Any], reason: str, *, owner: str,
               session_id: str, turn_id: str) -> Step:  # fmt: skip
        outcome = "cancelled" if event == A.DELETE_CANCELLED else "expired"
        held = self._held.get(str(pending.get("pending_action_id") or ""))
        if held is not None:  # a closed action never confirms or executes again
            held["proof"] = held["confirmed_at"] = None
        ev = self.audit.record(
            event, actor_user_id=owner, session_id=session_id, turn_id=turn_id,
            pending_action_id=str(pending.get("pending_action_id") or ""),
            target_ids=list(pending.get("report_ids") or ()) or None,
            count=pending.get("count") if type(pending.get("count")) is int else None,
            outcome=outcome, details={"source": "delete", "kind": KIND, "error_type": reason},
        )  # fmt: skip
        text = CANCELLED_TEXT if outcome == "cancelled" else EXPIRED_TEXT
        return Step(outcome, text, event=event, audit_event_id=ev.event_id if ev else None,
                    count=0, error=reason)  # fmt: skip

    def _confirm(self, pending, payload, *, turn, owner, session_id, turn_id) -> Step:
        close = lambda ev, why: self._close(  # noqa: E731
            ev, pending, why, owner=owner, session_id=session_id, turn_id=turn_id
        )
        pay = payload if isinstance(payload, Mapping) else {}
        pid = pending.get("pending_action_id")
        if not is_confirm(pay.get("reply"), pending.get("count")):
            return close(A.DELETE_CANCELLED, "declined")  # AC-12.3
        if not isinstance(pid, str) or pay.get("pending_action_id") != pid:
            return close(A.DELETE_CANCELLED, "wrong_action")
        if self._terminal(pid) is not None:  # single use: a replay is cancelled
            return close(A.DELETE_CANCELLED, "replayed")
        if pending.get("owner") != owner or pending.get("session_id") != session_id:
            return close(A.DELETE_CANCELLED, "wrong_owner")  # AC-12.4
        expires = pending.get("expires_at")
        if type(expires) is not int or self.clock() >= expires:
            return close(A.DELETE_EXPIRED, "timeout")  # AC-12.6
        if type(turn) is not int or turn != pending.get("preview_turn", -2) + 1:
            return close(A.DELETE_CANCELLED, "wrong_turn")  # next user turn only
        ids = pending.get("report_ids")
        if not isinstance(ids, list) or ids_sha256(ids) != pending.get("ids_sha256"):
            return close(A.DELETE_CANCELLED, "set_changed")  # AC-12.6: bound to the preview
        if not self._token_ok(pending):
            return close(A.DELETE_EXPIRED, "key_changed")  # restart or tampered state
        fields = _fields(pending)
        if not self._proof_ok(pid, self._token(fields), fields, pay):
            return close(A.DELETE_CANCELLED, "proof_mismatch")
        ev = self.audit.record(  # a rerun of this node finds the first row (idempotent)
            A.DELETE_CONFIRMED, actor_user_id=owner, session_id=session_id, turn_id=turn_id,
            pending_action_id=pid, target_ids=ids, count=len(ids), outcome="confirmed",
            details={"source": "delete", "kind": KIND},
        )  # fmt: skip
        if ev is None:
            ev = self.audit.get(pid, A.DELETE_CONFIRMED)
        held = self._hold(pid)
        if held["confirmed_at"] is None:  # a node rerun keeps the first confirm time
            held["confirmed_at"] = float(self.clock())
        return Step("confirmed", "", pending=dict(pending), event=A.DELETE_CONFIRMED,
                    audit_event_id=ev.event_id if ev else None, count=len(ids))  # fmt: skip

    # -- abandon (a Ctrl-C or a stranded execute; never deletes) ---------------------------

    def abandon(
        self, pending: Mapping[str, Any], reason: str, *, owner: str, session_id: str,
        turn_id: str,
    ) -> Step:  # fmt: skip
        """Close a delete paused at the confirm stage or stranded before execute WITHOUT
        running it (MJ-1): :data:`INTERRUPTED` (a Ctrl-C) writes ``delete.cancelled``,
        :data:`STRANDED` (found by the next, unrelated turn) or ``key_changed`` (a resume after
        a restart) writes ``delete.expired``; an action that already has a terminal row is
        reported, not closed again (one terminal row per action). The
        proof and confirm time are dropped first, so nothing can execute afterwards, even if
        the audit write fails. A delete that already committed reports its recorded count.
        Never raises."""
        pid = str(pending.get("pending_action_id") or "")
        held = self._held.get(pid)
        if held is not None:
            held["proof"] = held["confirmed_at"] = None
        try:
            done = self._terminal(pid)
            if done is not None:  # already closed (or committed): report it, write nothing
                return self._reported(done)
            event = A.DELETE_CANCELLED if reason == INTERRUPTED else A.DELETE_EXPIRED
            st = self._close(event, pending, reason, owner=owner, session_id=session_id,
                             turn_id=turn_id)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - AuditError included: nothing deleted
            logger.error("delete abandon failed: %s", type(exc).__name__)
            return Step("unsafe", UNSAFE_TEXT, error=type(exc).__name__)
        return replace(st, text=STRANDED_TEXT) if reason == STRANDED else st

    # -- execute ------------------------------------------------------------------------

    def execute(
        self, pending: Mapping[str, Any], *, owner: str, session_id: str, turn_id: str
    ) -> Step:
        try:
            return self._execute(pending, owner=owner, session_id=session_id, turn_id=turn_id)
        except Exception as exc:  # noqa: BLE001 - AuditError / mismatch: nothing deleted
            logger.error("delete execute failed: %s", type(exc).__name__)
            return Step("unsafe", UNSAFE_TEXT, error=type(exc).__name__)

    def _execute(self, pending, *, owner, session_id, turn_id) -> Step:
        pid = pending.get("pending_action_id")
        ids = pending.get("report_ids")
        conf = self.audit.get(pid, A.DELETE_CONFIRMED) if isinstance(pid, str) else None
        if conf is None or not isinstance(ids, list) or not ids:
            return Step("unsafe", UNSAFE_TEXT, error="not_confirmed")
        # recompute the binding: owner, the confirmed set and its digest must all agree
        if (
            conf.actor_user_id != owner
            or pending.get("owner") != owner
            or sorted(conf.target_ids) != sorted(ids)
            or ids_sha256(ids) != pending.get("ids_sha256")
        ):
            return Step("unsafe", UNSAFE_TEXT, error="binding_mismatch")
        done = self._terminal(pid)
        if done is not None:  # a rerun after the commit, or already closed: report it
            return self._reported(done)
        close = lambda why: self._close(  # noqa: E731
            A.DELETE_EXPIRED, pending, why, owner=owner, session_id=session_id, turn_id=turn_id
        )
        if not self._token_ok(pending):  # a restart (new K_delete) or tampered state
            return close("key_changed")
        held = self._held.get(pid)
        at = held["confirmed_at"] if held else None
        if at is None:  # confirmed elsewhere (another process) or already closed
            return close("confirm_lost")
        if self.clock() > at + EXECUTE_GRACE_S:  # OD-10: a retry only inside the window
            return close("timeout")
        present = [i for i in ids if self.store.get(i, owner) is not None]  # owner re-check
        if len(present) != len(ids):  # OD-14 (owner 2026-10-05): re-ask, never a subset
            gone = [i for i in ids if i not in set(present)]
            return replace(close(SET_CHANGED), text=_set_changed_text(gone, len(present)))
        out = A.audited_delete(
            self.audit, kind=KIND, actor_user_id=owner, session_id=session_id,
            turn_id=turn_id, pending_action_id=pid, target_ids=present,
        )  # fmt: skip
        n = out.event.count or 0
        return Step("executed", _deleted_text(n), event=A.DELETE_EXECUTED,
                    audit_event_id=out.event.event_id, count=n)  # fmt: skip


def _set_changed_text(gone: Sequence[str], left: int) -> str:
    """The reply when the confirmed set changed before execute: name what vanished (ids only,
    no titles: the rows are gone), say nothing was deleted, and ask for a fresh preview."""
    shown = ", ".join(gone[:PREVIEW_SIZE]) + (f" and {len(gone) - PREVIEW_SIZE} more"
                                              if len(gone) > PREVIEW_SIZE else "")  # fmt: skip
    one = len(gone) == 1
    text = (f"{len(gone)} previewed report{'' if one else 's'} no longer exist"
            f"{'s' if one else ''} ({shown}), so nothing was deleted.")
    if left:
        text += (f" {left} other report{'' if left == 1 else 's'} still match"
                 f"{'es' if left == 1 else ''}; ask again to see a fresh preview and confirm.")
    return text


def _deleted_text(n: int) -> str:
    return f"Deleted {n} report{'' if n == 1 else 's'}."


def render_prompt(rows: Sequence[Any], scope: Any, *, missing: int = 0,
                  truncated: bool = False) -> str:  # fmt: skip
    """The confirmation prompt, rendered by code only (no model text): count, the first
    PREVIEW_SIZE lines (id, date, title), "and M more", the backup notice (AC-12.11)."""
    n = len(rows)
    lines = [f"This will delete {n} saved report{'' if n == 1 else 's'}:"]
    for r in rows[:PREVIEW_SIZE]:
        lines.append(f"  {r.report_id}  {str(r.created_at)[:10]}  {_title(r, scope)}")
    if n > PREVIEW_SIZE:
        lines.append(f"  ... and {n - PREVIEW_SIZE} more")
    if missing:
        lines.append(f"{missing} of the ids you named were not found among your saved reports.")
    if truncated:
        lines.append("Only your newest saved reports were searched.")
    lines.append(BACKUP_NOTICE)
    if n > PREVIEW_SIZE:
        lines.append(f"Type {n} to confirm. Any other reply cancels.")
    else:
        lines.append("Type yes to confirm. Any other reply cancels.")
    return "\n".join(lines)


# --- LLM-step seams (service level; the delete tool is not exposed to the model in 22a) ------


def gate_step(calls: Iterable[Any]) -> dict[str, dict[str, Any]] | None:
    """A model step that calls the delete tool together with any other tool is refused as a
    whole: every call gets ``delete_not_alone`` and none runs. None: the step may run."""
    calls = list(calls)
    if len(calls) > 1 and any(getattr(c, "name", None) == DELETE_TOOL for c in calls):
        err = {"ok": False, "error": {"code": "delete_not_alone"}}
        return {str(getattr(c, "id", i)): dict(err) for i, c in enumerate(calls)}
    return None


def check_tool_request(
    *, user_message: str, tools_used: Iterable[str], pending: Mapping[str, Any] | None,
    selector: object,
) -> tuple[DeleteRequest | None, str | None]:  # fmt: skip
    """The checks a delete tool call must pass before a preview (AC-12.12): delete intent in
    the USER's message, no report content viewed earlier in this turn (taint), no delete
    already pending, and a narrow stated selector. Returns (request, None) or (None, code)."""
    if not isinstance(user_message, str) or INTENT_RE.search(_fold(user_message)) is None:
        return None, "no_intent"
    if TAINT_TOOLS & set(tools_used):
        return None, "tainted"
    if pending:
        return None, "delete_pending"
    req = parse_delete_request(selector, command=True)
    if req is None or req.error:
        return None, SELECTOR_EMPTY
    return req, None


def setup_delete(conn: Any, audit: Any, store: Any, *, clock: Callable[[], float] = time.time,
                 key: DeleteKey | None = None) -> DeleteService | None:  # fmt: skip
    """Register the ``saved_report`` kind on the live schema and build the service. Any
    failure returns None: the feature stays off and ``/delete`` unregistered (fail closed)."""
    if conn is None or audit is None or store is None:
        return None
    try:
        A.register_deletable(
            A.DeletableKind(KIND, "saved_report", "report_id", owner_column="owner_user_id"),
            conn=conn,
        )
        return DeleteService(audit, store, clock=clock, key=key)
    except Exception as exc:  # noqa: BLE001
        logger.error("delete feature disabled: %s", type(exc).__name__)
        return None
