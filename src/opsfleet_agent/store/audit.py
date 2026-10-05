"""Append-only audit log (FR-28, AC-28.1..28.5, SEC-17; HLD §6.3.3, data model AUDIT_EVENT).

Contract:

* **Ids, codes and counts only (A-26).** A row holds an event type from ``EVENT_TYPES``, a
  reason code from ``RULES``, an outcome from ``OUTCOMES``, ids in strict code-generated
  formats, a count, and a ``details`` object whose keys are in ``DETAIL_FIELDS`` and whose
  values pass that key's validator (enum member, 64-hex hash, non-negative int, ...). Anything
  else raises :class:`AuditError` before SQLite is touched, so free text (user messages, SQL,
  report bodies, e-mail addresses, provider error text) cannot reach the table. Every pattern
  is applied with ``fullmatch`` (a trailing newline is refused):

  - ``session_id``: 32 lowercase hex (``uuid4().hex``, ``session.start_session``);
  - ``turn_id``: 12 or 32 lowercase hex (``uuid4().hex[:12]`` in the graph, or a full hex);
  - ``pending_action_id``: 32..64 lowercase hex (code-generated, HLD §6.3.3);
  - ``target_ids`` (report ids): 32 lowercase hex (``uuid4().hex``);
  - ``actor_user_id``: the profile ``user_id`` pattern from ``session.py``
    (``[A-Za-z0-9][A-Za-z0-9_.-]{0,63}``), which the profile loader already enforces.

  Hex-only ids cannot hold an e-mail address, a name or a formatted phone number. The actor id
  is the one exception: it is an operator-chosen profile name from ``config/profiles.yaml``,
  and the pattern does not judge its content (it refuses ``@``, spaces and ``+``, nothing more).
* **Append-only, enforced in SQLite.** ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers raise.
  A ``BEFORE INSERT`` trigger turns an insert that collides on ``event_id``, ``seq`` or
  ``(pending_action_id, event_type)`` into a no-op (``RAISE(IGNORE)``); this gives the HLD's
  insert-or-ignore semantics and also neutralises ``INSERT OR REPLACE``, which would otherwise
  delete the old row without firing the delete trigger. ``delete.failed`` is exempt from the
  ``(pending_action_id, event_type)`` uniqueness so that every failed attempt is kept.
  :class:`AuditLog` offers no update or delete method, and the agent has no tool that reaches
  it (SEC-17, AC-28.4). :func:`ensure_schema` compares every ``audit_event`` object in
  ``sqlite_master`` (table, indexes, triggers, SQL text included) with the expected schema, so a
  neutered trigger that kept its name is refused.
* **Audit-first deletes.** :func:`audited_delete` runs the DELETE itself for a kind
  registered with :func:`register_deletable` (``DeletableKind``: table, unique TEXT key, required
  owner column unless ``unowned=True``, dependents); unregistered kinds, protected tables and
  free-form table names are refused, and callers never get the connection. The live schema is
  verified inside the transaction (unique key, TEXT affinity, no collation, no DELETE trigger,
  no undeclared cascading FK). It inserts ``delete.executed`` and runs the DELETE in ONE
  ``BEGIN IMMEDIATE`` transaction, audit row first. If the audit write fails (any exception,
  ``KeyboardInterrupt`` included), the transaction rolls back and no DELETE is issued. Before
  any DELETE the targets must match exactly one owned row each; before ``COMMIT`` the main
  DELETE must have removed ``len(target_ids)`` rows, the change counter must show no side
  effects, and the executed row is re-read; otherwise everything rolls back and
  ``delete.failed`` is appended in its own transaction. A replay of an already executed
  ``pending_action_id`` by the same actor for the same kind and targets returns the recorded
  event and deletes nothing; any other replay appends ``delete.failed`` and raises.
* **Recorder.** :func:`recorder` binds an actor (and optionally a session/turn) and returns a
  ``Callable[[dict], AuditEvent | None]`` that accepts ``tools.run_sql``'s audit dict as-is
  and the dicts built by :func:`from_input`, :func:`from_router`, :func:`from_refusal` and
  :func:`from_output` for guard refusals (iterations 6, 7, 9, 10, 11, 12).

Erasure (iteration 35; SEC-18, AC-28.6, FR-59; D-223/D-224; HLD §7 retention/erasure): the
audit trail outlives a user's data (1 year), so on an erasure request the user's audit rows are
pseudonymised, never deleted. :func:`audited_erase` first appends ``erase.attempted`` in its
own transaction (D-230; the erase is aborted if that write fails), then runs ONE
``BEGIN IMMEDIATE`` transaction: it writes ``erase.executed`` first (actor = the maintainer,
``details.target_user`` = a fresh random ``erased-<16 hex>`` pseudonym, no erased content),
deletes the user's rows in every per-user app.db table (FTS rows verified gone and optimized),
drops ``audit_event_no_update``,
rewrites only ``actor_user_id`` and ``details.target_user`` for that user to the pseudonym,
recreates the trigger from the canonical DDL and checks the exact audit schema before COMMIT
(DDL is transactional in SQLite), so no other column can change and a failure leaves the
trigger in place. That rewrite is the ONE permitted mutation of ``audit_event``.

Schema: created idempotently by :func:`ensure_schema`. ``AUDIT_MIGRATION`` and
``AUDIT_MARKER_SQL`` live in the leaf module ``store.audit_schema`` so that ``store.db.MIGRATIONS``
can import them verbatim at integration (never re-typed: the layout check compares SQL text).
TEMP objects on ``audit_event`` (``sqlite_temp_master``) are refused at open and before every
insert, and all statements qualify ``main.``.

Delete and erase outcomes are not forgeable: ``delete.executed``, ``delete.failed``,
``erase.executed`` and ``erase.failed`` are refused by the public
:meth:`AuditLog.build`/:meth:`AuditLog.record` and by :func:`recorder`
(:class:`DeleteEventRefusedError`); only :func:`audited_delete` and :func:`audited_erase` (and
:func:`record_erase_failure`, which needs a recorded ``erase.executed``) write them, through
the private ``_build``/``_append`` path. Otherwise a forged executed row would make the next real
delete for that ``pending_action_id`` a replay that deletes nothing.

Schema scanning (``_verify_kind``) is token-aware: ``COLLATE`` in a table's DDL and ``DELETE``
in a trigger's SQL are matched as whole words (case-insensitive), so names such as
``collated_by`` or ``deleted_at`` are accepted, while a quoted identifier or string literal
that is exactly one of those words is refused (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import astuple, dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from opsfleet_agent.bq.errors import BqErrorClass, ErrorCode, Stage
from opsfleet_agent.guards import differencing as diff_guard
from opsfleet_agent.guards import input as input_guard
from opsfleet_agent.guards import output as output_guard
from opsfleet_agent.guards.pii import ENTITY_TYPES
from opsfleet_agent.guards.scope import ScopeRefusal, ScopeRule
from opsfleet_agent.guards.sql_policy import Rule
from opsfleet_agent.store.audit_schema import (
    AUDIT_MARKER_SQL,
    AUDIT_MIGRATION,
    CREATED_MARKER,
    DELETE_FAILED_EVENT,
)
from opsfleet_agent.store.db import StoreError, write_tx
from opsfleet_agent.store.feedback import (
    DISMISS_REASONS,
    ROOT_CAUSES,
    TRIAGE_GATES,
    TRIAGE_STATES,
)

# --- event types -------------------------------------------------------------------------

DELETE_PREVIEWED: Final = "delete.previewed"
DELETE_CONFIRMED: Final = "delete.confirmed"
DELETE_EXECUTED: Final = "delete.executed"
DELETE_FAILED: Final = "delete.failed"
DELETE_CANCELLED: Final = "delete.cancelled"
DELETE_EXPIRED: Final = "delete.expired"
GUARDRAIL_REFUSED: Final = input_guard.AUDIT_REFUSED
GUARDRAIL_INJECTION: Final = input_guard.AUDIT_INJECTION
GUARDRAIL_PII_BLOCK: Final = input_guard.AUDIT_PII_BLOCK
TOOL_RUN_SQL: Final = "tool.run_sql"
ERASE_EXECUTED: Final = "erase.executed"  # iteration 35: user erasure (SEC-18, D-223)
ERASE_FAILED: Final = "erase.failed"
# D-230: written in its own transaction BEFORE the erase transaction, so a rolled-back erase
# (its erase.executed row gone with it) still leaves a start row that its erase.failed matches.
ERASE_ATTEMPTED: Final = "erase.attempted"

EVENT_TYPES: Final = frozenset(
    {
        DELETE_PREVIEWED,
        DELETE_CONFIRMED,
        DELETE_EXECUTED,
        DELETE_FAILED,
        DELETE_CANCELLED,
        DELETE_EXPIRED,
        GUARDRAIL_REFUSED,
        GUARDRAIL_INJECTION,
        GUARDRAIL_PII_BLOCK,
        TOOL_RUN_SQL,
        ERASE_EXECUTED,
        ERASE_FAILED,
        ERASE_ATTEMPTED,
        "scope.changed",
        "persona.changed",
        "golden.promoted",
        "feedback.triaged",  # iteration 36: triage CLI state changes (D-218)
        "feedback.dismissed",
        "eval.case_added",
        "report.renamed",
        "report.exported",
        "differencing.suspected",
    }
)

# --- reason codes ------------------------------------------------------------------------

# AC-28.3 guardrail categories (stored as details.category; also accepted as a bare rule).
PII_BLOCK: Final = "pii_block"
SCOPE_BLOCK: Final = "scope_block"
INJECTION: Final = "injection"
OFF_TOPIC: Final = "off_topic"
SQL_POLICY: Final = "sql_policy"
BUDGET: Final = "budget"
UNEXPECTED_ACTION: Final = "unexpected_action"
OUTPUT_INJECTION: Final = "output_injection"
INPUT_REJECTED: Final = "input_rejected"  # unreadable / too long / guard error (not in AC-28.3)
OUTPUT_BLOCKED: Final = "output_blocked"  # too long / still encoded / guard error (not in AC-28.3)
CATEGORIES: Final = frozenset(
    {
        PII_BLOCK,
        SCOPE_BLOCK,
        INJECTION,
        OFF_TOPIC,
        SQL_POLICY,
        BUDGET,
        UNEXPECTED_ACTION,
        OUTPUT_INJECTION,
        INPUT_REJECTED,
        OUTPUT_BLOCKED,
    }
)

_INPUT_CATEGORY: Final[Mapping[str, str]] = {
    input_guard.INJECTION: INJECTION,
    input_guard.PROMPT_EXFILTRATION: INJECTION,
    input_guard.ENCODED_PAYLOAD: INJECTION,
    input_guard.PII_REQUEST: PII_BLOCK,
    input_guard.OFF_TOPIC: OFF_TOPIC,
    input_guard.NON_ENGLISH: OFF_TOPIC,
    input_guard.INVALID_INPUT: INPUT_REJECTED,
    input_guard.INPUT_TOO_LONG: INPUT_REJECTED,
    input_guard.INPUT_GUARD_ERROR: INPUT_REJECTED,
}
# Output codes that block the answer (the others only modify it and are not audited).
_OUTPUT_CATEGORY: Final[Mapping[str, str]] = {
    output_guard.UNEXPECTED_ACTION: UNEXPECTED_ACTION,
    output_guard.OUTPUT_INJECTION: OUTPUT_INJECTION,
    output_guard.OUTPUT_TOO_LONG: OUTPUT_BLOCKED,
    output_guard.OUTPUT_ENCODING: OUTPUT_BLOCKED,
    output_guard.OUTPUT_GUARD_ERROR: OUTPUT_BLOCKED,
}
_OUTPUT_EVENT: Final[Mapping[str, str]] = {output_guard.OUTPUT_INJECTION: GUARDRAIL_INJECTION}
_OUTPUT_MODIFIERS: Final = frozenset(
    {
        output_guard.HTML_STRIPPED,
        output_guard.CONTROL_STRIPPED,
        output_guard.PII_REDACTED,
        output_guard.URL_STRIPPED,
        output_guard.IMAGE_STRIPPED,
    }
)
_SCOPE_RULES: Final = frozenset(r.value for r in ScopeRule)
_POLICY_RULES: Final = frozenset(r.value for r in Rule if r is not Rule.OK)

RULES: Final = frozenset(
    _POLICY_RULES
    | _SCOPE_RULES
    | {diff_guard.DIFFERENCING}
    | set(_INPUT_CATEGORY)
    | set(_OUTPUT_CATEGORY)
    | _OUTPUT_MODIFIERS
    | CATEGORIES
)

# run_sql's own codes (tools/run_sql.py) plus the bq package codes.
RUN_SQL_CODES: Final = frozenset(
    {"SQL_POLICY", "SQL_TOO_LONG", "INVALID_ARGS", "GIVE_UP", "DUPLICATE_QUERY", "TOOL_BUSY"}
)
CODES: Final = frozenset(RUN_SQL_CODES | {c.value for c in ErrorCode})
_BUDGET_CODES: Final = frozenset(
    {ErrorCode.SESSION_BUDGET.value, ErrorCode.BUDGET_EXHAUSTED.value, ErrorCode.COST_CAP.value}
)

OUTCOMES: Final = frozenset(
    {"ok", "error", "refused", "failed", "cancelled", "expired", "previewed", "confirmed"}
)
SOURCES: Final = frozenset({"input", "router", "sql", "output", "delete", "erase"})
# iteration 35: the store an erasure step failed on (erase.failed details.store)
ERASE_STORES: Final = frozenset({"app_db", "checkpoints", "traces", "exports", "golden_candidates"})

# --- validators --------------------------------------------------------------------------

MAX_TARGET_IDS: Final = 1000
MAX_COUNT: Final = 10**12
# All patterns are used with fullmatch (no ^/$: "$" would accept a trailing newline).
ACTOR_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # session.py user_id
SESSION_ID_RE: Final = re.compile(r"[0-9a-f]{32}")  # uuid4().hex
TURN_ID_RE: Final = re.compile(r"[0-9a-f]{12}(?:[0-9a-f]{20})?")  # uuid4().hex[:12] or full
PENDING_ACTION_ID_RE: Final = re.compile(r"[0-9a-f]{32,64}")
TARGET_ID_RE: Final = re.compile(r"[0-9a-f]{32}")  # report ids: uuid4().hex
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}")
_PY_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SQL_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
KIND_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")  # registered deletable kinds
VERSION_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,48}")  # persona "<file version>-<hash8>"


class AuditError(StoreError):
    """Invalid audit input, or the audit store is unavailable. Fail closed."""


class DeleteMismatchError(AuditError):
    """The DELETE matched a different number of rows than requested; nothing was deleted."""


class DeleteEventRefusedError(AuditError):
    """``delete.executed``/``delete.failed`` were offered to a public write path (M-A)."""


def _id(pattern: re.Pattern[str]) -> Callable[[str, object], str]:
    def check(name: str, value: object) -> str:
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise AuditError(f"{name} is not a valid id")
        return value

    return check


_actor_id = _id(ACTOR_ID_RE)
_session_id = _id(SESSION_ID_RE)
_turn_id = _id(TURN_ID_RE)
_pending_id = _id(PENDING_ACTION_ID_RE)
_target_id = _id(TARGET_ID_RE)


def _opt_pending(name: str, value: object) -> str | None:
    return None if value is None else _pending_id(name, value)


def _count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        raise AuditError(f"{name} must be a non-negative integer")
    return value


def _member(allowed: frozenset[str]) -> Callable[[str, object], str]:
    def check(name: str, value: object) -> str:
        if not isinstance(value, str) or value not in allowed:
            raise AuditError(f"{name} is not an allowed value")
        return value

    return check


def _hash(name: str, value: object) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise AuditError(f"{name} must be a 64-character hex digest")
    return value


def _py_name(name: str, value: object) -> str:
    if not isinstance(value, str) or _PY_NAME_RE.fullmatch(value) is None:
        raise AuditError(f"{name} must be a class name")
    return value


def _pii_types(name: str, value: object) -> list[str]:
    if not isinstance(value, list | tuple | frozenset | set):
        raise AuditError(f"{name} must be a list")
    check = _member(frozenset(ENTITY_TYPES))
    return sorted({check(name, v) for v in value})


MAX_SCOPE_BRANDS: Final = 200
# C0, DEL, C1, zero-width/bidi marks (U+200B-200F, 202A-202E, 2066-2069), U+2028/2029
_SCOPE_BAD: Final = r"\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069"
_SCOPE_BRAND_RE: Final = re.compile(
    rf"[^{_SCOPE_BAD}\s](?:[^{_SCOPE_BAD}]{{0,98}}[^{_SCOPE_BAD}\s])?"
)
SCOPE_INVALID: Final = "invalid"  # old_scope marker: the stored scope was no longer valid


def _scope(name: str, value: object) -> str | list[str]:
    """``scope.changed`` old/new scope: the literal ``"all"`` or a bounded list of brand names."""
    if value == "all" and isinstance(value, str):
        return "all"
    if name.endswith("old_scope") and value == SCOPE_INVALID and isinstance(value, str):
        return SCOPE_INVALID
    if not isinstance(value, list | tuple) or not 0 < len(value) <= MAX_SCOPE_BRANDS:
        raise AuditError(f"{name} must be 'all' or a non-empty brand list")
    out = []
    for b in value:
        if not isinstance(b, str) or _SCOPE_BRAND_RE.fullmatch(b) is None:
            raise AuditError(f"{name} has a malformed brand")
        out.append(b)
    return out


# persona.changed reason codes: PersonaInvalid codes plus the change workflow's own (iteration 41)
PERSONA_REASONS: Final = frozenset(
    {
        "forbidden_directive", "too_large", "bad_encoding", "control_characters",
        "missing_fields", "unknown_fields", "unknown_heading", "missing_headings",
        "markup_not_allowed", "unreadable", "unchanged", "smoke_failed", "no_history",
        "write_failed",
    }
)  # fmt: skip

DETAIL_FIELDS: Final[Mapping[str, Callable[[str, object], Any]]] = {
    "code": _member(CODES),
    "class": _member(frozenset(c.value for c in BqErrorClass)),
    "stage": _member(frozenset(s.value for s in Stage)),
    "sql_hash": _hash,
    "executed_sql_hash": _hash,
    "bytes_billed": _count,
    "category": _member(CATEGORIES),
    "source": _member(SOURCES),
    "cause": _member(frozenset({diff_guard.CAUSE_STORE, diff_guard.CAUSE_CAPACITY})),
    "pii_types": _pii_types,
    "error_type": _py_name,
    "requested": _count,
    "matched": _count,
    "kind": _id(KIND_NAME_RE),
    "target_user": _id(ACTOR_ID_RE),
    "old_scope": _scope,
    "new_scope": _scope,
    "from_version": _id(VERSION_RE),
    "to_version": _id(VERSION_RE),
    "smoke": _member(frozenset({"pass", "fail", "skipped"})),
    "reason": _member(PERSONA_REASONS),
    # iteration 36: triage CLI (D-217/D-218); enum members only, never free text
    "root_cause": _member(frozenset(ROOT_CAUSES)),
    "dismiss_reason": _member(frozenset(DISMISS_REASONS)),
    "gate": _member(frozenset(TRIAGE_GATES)),
    "triage_state": _member(frozenset(TRIAGE_STATES)),
    "store": _member(ERASE_STORES),  # iteration 35 (D-223)
}


def _details(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AuditError("details must be a mapping")
    out: dict[str, Any] = {}
    for key, v in value.items():
        if key not in DETAIL_FIELDS:
            raise AuditError("details has a field that is not allowed")
        if v is not None:
            out[key] = DETAIL_FIELDS[key](f"details.{key}", v)
    return out


def _target_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise AuditError("target_ids must be a list of ids")
    ids = tuple(_target_id("target_ids", v) for v in value)
    if len(ids) > MAX_TARGET_IDS:
        raise AuditError("too many target_ids")
    return ids


# --- schema ------------------------------------------------------------------------------

# The DDL lives in the leaf module ``store.audit_schema`` so ``store.db`` can fold it verbatim.
if DELETE_FAILED_EVENT != DELETE_FAILED:  # pragma: no cover - import-time consistency check
    raise ImportError("audit_schema.DELETE_FAILED_EVENT drifted from audit.DELETE_FAILED")

_COLUMNS: Final = (
    "seq",
    "event_id",
    "ts",
    "actor_user_id",
    "session_id",
    "turn_id",
    "pending_action_id",
    "event_type",
    "target_ids",
    "count",
    "rule",
    "outcome",
    "details",
)
_COLS: Final = ", ".join(_COLUMNS)
# Tables whose rows do not count as "user data" for the populated-store check.
_BOOKKEEPING_TABLES: Final = frozenset({"meta", "schema_migrations", "audit_event"})


def _schema_objects(conn: sqlite3.Connection) -> set[tuple[str, str, str | None]]:
    """Every main-schema object attached to audit_event: (type, name, sql)."""
    return {
        (t, n, sql)
        for t, n, sql in conn.execute(
            "SELECT type, name, sql FROM main.sqlite_master "
            "WHERE lower(tbl_name) = 'audit_event' OR lower(name) = 'audit_event'"
        )
    }


def _refuse_temp_objects(conn: sqlite3.Connection) -> None:
    """L5: a TEMP trigger on main.audit_event (or a TEMP table/view shadowing the name) is not
    in ``main.sqlite_master`` and would escape the exact-schema check; refuse the store."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_temp_master "
        "WHERE lower(tbl_name) = 'audit_event' OR lower(name) = 'audit_event' LIMIT 1"
    ).fetchone()
    if row is not None:
        raise AuditError("audit store has a temporary object on audit_event")


def _expected_objects() -> set[tuple[str, str, str | None]]:
    mem = sqlite3.connect(":memory:")
    try:
        for stmt in AUDIT_MIGRATION:
            mem.execute(stmt)
        return _schema_objects(mem)
    finally:
        mem.close()


_EXPECTED: Final = _expected_objects()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND lower(name) = lower(?)",
        (name,),
    ).fetchone()
    return row is not None


def _populated_tables(conn: sqlite3.Connection) -> list[str]:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    out = []
    for name in names:
        if name in _BOOKKEEPING_TABLES or _SQL_NAME_RE.fullmatch(name) is None:
            continue
        if conn.execute(f'SELECT EXISTS (SELECT 1 FROM main."{name}")').fetchone()[0]:
            out.append(name)
    return out


def ensure_schema(conn: sqlite3.Connection, *, allow_create_on_populated: bool = False) -> None:
    """Create the audit schema on a fresh store, or verify it exactly (fail closed).

    Needs a store migrated by ``store.db.open_store`` (the ``meta`` table holds the creation
    marker). When ``audit_event`` is absent it is created only if the marker is absent too and
    no other store table holds rows; ``allow_create_on_populated=True`` is the explicit,
    one-off upgrade path for a store that predates the audit log. When it is present, every
    ``audit_event`` object in ``sqlite_master`` must equal the expected schema, SQL included.
    Nothing is recreated silently: a dropped or altered trigger makes this raise.
    """
    with write_tx(conn):
        _refuse_temp_objects(conn)
        if not _has_table(conn, "meta"):
            raise AuditError("audit store needs a migrated app store")
        marked = conn.execute("SELECT 1 FROM main.meta WHERE key = ?", (CREATED_MARKER,)).fetchone()
        if not _has_table(conn, "audit_event"):
            if marked is not None:
                raise AuditError("audit store table is missing")
            if _populated_tables(conn) and not allow_create_on_populated:
                raise AuditError("refusing to create an empty audit log on a populated store")
            for stmt in AUDIT_MIGRATION:
                conn.execute(stmt)
        if _schema_objects(conn) != _EXPECTED:
            raise AuditError("audit store layout does not match the expected schema")
        if marked is None:
            conn.execute(AUDIT_MARKER_SQL)


# --- records -----------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    seq: int | None
    event_id: str
    ts: str
    actor_user_id: str
    session_id: str
    turn_id: str
    pending_action_id: str | None
    event_type: str
    target_ids: tuple[str, ...] = ()
    count: int | None = None
    rule: str | None = None
    outcome: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


def _ts(seconds: float) -> str:
    stamp = datetime.fromtimestamp(seconds, UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def _decode(row: tuple) -> AuditEvent:
    seq, event_id, ts, actor, session, turn, pending, etype, targets, count, rule, out, det = row
    return AuditEvent(
        seq=seq,
        event_id=event_id,
        ts=ts,
        actor_user_id=actor,
        session_id=session,
        turn_id=turn,
        pending_action_id=pending,
        event_type=etype,
        target_ids=tuple(json.loads(targets)) if targets else (),
        count=count,
        rule=rule,
        outcome=out,
        details=json.loads(det) if det else {},
    )


class AuditLog:
    """Append-only audit store on the app DB. Public methods raise only :class:`AuditError`.

    There is intentionally no update or delete method (AC-28.4); the only audit mutation is
    the pseudonymisation inside :func:`audited_erase` (module docstring).

    Threading: use one connection, and so one ``AuditLog``, per thread. The log holds no lock,
    and :func:`audited_delete` relies on owning the connection's transaction from ``BEGIN
    IMMEDIATE`` to ``COMMIT``; another thread issuing statements on the same connection in that
    window would join that transaction. ``store.db.connect`` keeps ``check_same_thread=True``,
    so cross-thread use raises instead of interleaving. Separate connections are serialised by
    SQLite (``BEGIN IMMEDIATE`` plus ``busy_timeout``).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], float] = time.time,
        allow_create_on_populated: bool = False,
    ):
        self.conn = conn
        self.clock = clock
        try:
            ensure_schema(conn, allow_create_on_populated=allow_create_on_populated)
        except AuditError:
            raise
        except Exception as err:  # noqa: BLE001 - sqlite3.Error, closed conn
            raise AuditError("audit store unavailable") from err

    def build(self, event_type: str, **fields: Any) -> AuditEvent:
        """Validate and build an event (not stored). Raises AuditError on anything off-list.

        ``delete.executed``, ``delete.failed``, ``erase.executed`` and ``erase.failed`` raise
        :class:`DeleteEventRefusedError`: only :func:`audited_delete` and :func:`audited_erase`
        write them (M-A, D-223)."""
        if type(event_type) is not str:  # no str subclass with a custom __eq__/__hash__
            raise AuditError("event_type must be a str")
        if event_type in _AUDITED_DELETE_ONLY:
            raise DeleteEventRefusedError(
                f"{event_type} is written only by audited_delete or audited_erase"
            )
        return self._build(event_type, **fields)

    def _build(
        self,
        event_type: str,
        *,
        actor_user_id: str,
        session_id: str,
        turn_id: str,
        pending_action_id: str | None = None,
        target_ids: Iterable[str] | None = None,
        count: int | None = None,
        rule: str | None = None,
        outcome: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        """The validating builder behind :meth:`build`, without the delete-event refusal."""
        try:
            now = float(self.clock())
            ts = _ts(now)
        except Exception as err:  # noqa: BLE001 - bad clock
            raise AuditError("audit clock unavailable") from err
        return AuditEvent(
            seq=None,
            event_id=uuid.uuid4().hex,
            ts=ts,
            actor_user_id=_actor_id("actor_user_id", actor_user_id),
            session_id=_session_id("session_id", session_id),
            turn_id=_turn_id("turn_id", turn_id),
            pending_action_id=_opt_pending("pending_action_id", pending_action_id),
            event_type=_member(EVENT_TYPES)("event_type", event_type),
            target_ids=_target_ids(target_ids),
            count=None if count is None else _count("count", count),
            rule=None if rule is None else _member(RULES)("rule", rule),
            outcome=None if outcome is None else _member(OUTCOMES)("outcome", outcome),
            details=_details(details),
        )

    def record(self, event_type: str, **fields: Any) -> AuditEvent | None:
        """Append one event in its own transaction. None when ignored as a duplicate."""
        return self._append(self.build(event_type, **fields))

    def _append(self, event: AuditEvent) -> AuditEvent | None:
        """Store a built event in its own transaction (``record`` and ``_record_failure``)."""
        try:
            with write_tx(self.conn):
                return self._insert(event)
        except AuditError:
            raise
        except Exception as err:  # noqa: BLE001
            raise AuditError("audit write failed") from err

    def _insert(self, event: AuditEvent) -> AuditEvent | None:
        """INSERT inside the caller's transaction. None when the duplicate trigger ignored it."""
        _refuse_temp_objects(self.conn)  # L5: a TEMP trigger created after open
        cur = self.conn.execute(
            f"INSERT INTO main.audit_event ({_COLS}) "
            "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.ts,
                event.actor_user_id,
                event.session_id,
                event.turn_id,
                event.pending_action_id,
                event.event_type,
                json.dumps(list(event.target_ids)) if event.target_ids else None,
                event.count,
                event.rule,
                event.outcome,
                json.dumps(dict(event.details), sort_keys=True) if event.details else None,
            ),
        )
        if cur.rowcount != 1:
            return None
        return AuditEvent(**{**event.__dict__, "seq": cur.lastrowid})

    def get(self, pending_action_id: str, event_type: str) -> AuditEvent | None:
        try:
            row = self.conn.execute(
                f"SELECT {_COLS} FROM main.audit_event WHERE pending_action_id=? AND event_type=? "
                "ORDER BY seq LIMIT 1",
                (pending_action_id, event_type),
            ).fetchone()
        except Exception as err:  # noqa: BLE001
            raise AuditError("audit read failed") from err
        return _decode(row) if row else None

    def events(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        newest_first: bool = True,
        limit: int = 200,
    ) -> list[AuditEvent]:
        """Events filtered by session and/or actor, ordered by insertion (``seq``)."""
        where, params = [], []
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if user_id is not None:
            where.append("actor_user_id = ?")
            params.append(user_id)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        order = "DESC" if newest_first else "ASC"
        n = max(1, min(int(limit), 10_000))
        try:
            rows = self.conn.execute(
                f"SELECT {_COLS} FROM main.audit_event {clause}ORDER BY seq {order} LIMIT ?",
                (*params, n),
            ).fetchall()
        except Exception as err:  # noqa: BLE001
            raise AuditError("audit read failed") from err
        return [_decode(r) for r in rows]


# --- audit-first delete ------------------------------------------------------------------


class ReplayMismatchError(AuditError):
    """A pending_action_id already executed for another actor, kind or target set."""


@dataclass(frozen=True)
class DeleteOutcome:
    event: AuditEvent
    replayed: bool  # True: already executed earlier; nothing was deleted this time
    deleted: int | None = None  # rows deleted from the target table (None on a replay)


# Written only by audited_delete/_record_failure (M-A) and audited_erase (iteration 35, D-223).
_AUDITED_DELETE_ONLY: Final = frozenset(
    {DELETE_EXECUTED, DELETE_FAILED, ERASE_EXECUTED, ERASE_FAILED, ERASE_ATTEMPTED}
)
MAX_DEPENDENTS: Final = 8
# Whole-word, case-insensitive (L4): "collated_by" / "deleted_at" do not match.
_COLLATE_RE: Final = re.compile(r"\bCOLLATE\b", re.IGNORECASE)
_DELETE_RE: Final = re.compile(r"\bDELETE\b", re.IGNORECASE)
# Never deletable through audited_delete, whatever a kind says.
_PROTECTED_TABLES: Final = frozenset({"audit_event", "meta", "schema_migrations"})
# FK actions that change rows in another table when a parent row is deleted.
_CASCADING_ACTIONS: Final = frozenset({"CASCADE", "SET NULL", "SET DEFAULT"})
# Iteration 37: an FTS5 index dependent must be a regular (not contentless or
# external-content) FTS5 table; its shadow tables count as touched for the trigger check.
_FTS5_RE: Final = re.compile(r"^CREATE\s+VIRTUAL\s+TABLE\b.*\bUSING\s+FTS5\s*\(", re.I | re.S)
_FTS_CONTENT_RE: Final = re.compile(r"\bcontent(_rowid)?\s*=|\bcontentless", re.I)
_FTS_SHADOWS: Final = ("_data", "_idx", "_content", "_docsize", "_config")


def _sql_name(what: str, value: object) -> str:
    if not isinstance(value, str) or _SQL_NAME_RE.fullmatch(value) is None:
        raise AuditError(f"{what} is not a valid table or column name")
    return value


def _deletable_table(what: str, value: object) -> str:
    name = _sql_name(what, value)
    if name.lower() in _PROTECTED_TABLES or name.lower().startswith("sqlite_"):
        raise AuditError(f"{what} is protected")
    return name


def _q(name: str) -> str:
    """Quote an identifier (names read back from sqlite_master may hold any character)."""
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class DeletableKind:
    """A registered kind of user-owned row that :func:`audited_delete` may remove (M2).

    ``DELETE FROM main.table WHERE key_column IN (targets) AND owner_column = actor``, preceded
    by ``DELETE FROM main.dep_table WHERE dep_column IN (targets)`` for each dependent. The
    owner column is required; a kind with no owner must say ``unowned=True`` explicitly.
    Static checks run here; the live schema is verified at registration (with ``conn``) and
    again inside every delete transaction (:func:`_verify_kind`).

    ``fts_dependents`` (iteration 37): regular FTS5 tables holding an index row per target in
    ``(table, column)``. Their rows are deleted in the same transaction, after the exact
    change-counter check (FTS5 shadow-table writes are not row-for-row), then verified gone,
    and the index is ``optimize``d before COMMIT so no token of a deleted row survives.
    """

    name: str
    table: str
    key_column: str
    owner_column: str | None = None
    dependents: tuple[tuple[str, str], ...] = ()
    unowned: bool = False
    fts_dependents: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependents", _static_checks(self))
        object.__setattr__(self, "fts_dependents", _static_fts_checks(self))


def _static_checks(kind: DeletableKind) -> tuple[tuple[str, str], ...]:
    """Names, protected tables, owner rule and dependents; returns the dependents as tuples.
    Runs at construction and again in :func:`_verify_kind` (a kind mutated through
    ``object.__setattr__`` after registration is refused at delete time)."""
    if not isinstance(kind.name, str) or KIND_NAME_RE.fullmatch(kind.name) is None:
        raise AuditError("deletable kind name is not valid")
    table = _deletable_table("deletable table", kind.table)
    key = _sql_name("key column", kind.key_column)
    if not isinstance(kind.unowned, bool):
        raise AuditError("unowned must be a bool")
    if kind.unowned:
        if kind.owner_column is not None:
            raise AuditError("an unowned kind cannot have an owner column")
    else:
        if kind.owner_column is None:
            raise AuditError("owner_column is required (pass unowned=True for an unowned kind)")
        if _sql_name("owner column", kind.owner_column).lower() == key.lower():
            raise AuditError("owner column must differ from the key column")
    try:
        deps = tuple(tuple(d) for d in kind.dependents)
    except TypeError as err:
        raise AuditError("dependents must be (table, column) pairs") from err
    if len(deps) > MAX_DEPENDENTS:
        raise AuditError("too many dependents")
    seen: set[tuple[str, str]] = set()
    for dep in deps:
        if len(dep) != 2:
            raise AuditError("dependents must be (table, column) pairs")
        dep_table = _deletable_table("dependent table", dep[0])
        dep_col = _sql_name("dependent column", dep[1])
        if dep_table.lower() == table.lower():
            raise AuditError("a dependent cannot be the kind's own table")
        pair = (dep_table.lower(), dep_col.lower())
        if pair in seen:
            raise AuditError("duplicate dependent")
        seen.add(pair)
    return deps  # type: ignore[return-value]


def _static_fts_checks(kind: DeletableKind) -> tuple[tuple[str, str], ...]:
    """The FTS index dependents: (table, column) pairs, bounded, unique, never the kind's own
    table or one of its plain dependents."""
    try:
        deps = tuple(tuple(d) for d in kind.fts_dependents)
    except TypeError as err:
        raise AuditError("fts_dependents must be (table, column) pairs") from err
    if len(deps) > MAX_DEPENDENTS:
        raise AuditError("too many fts dependents")
    taken = {str(kind.table).lower(), *(str(t).lower() for t, *_ in kind.dependents)}
    seen: set[str] = set()
    for dep in deps:
        if len(dep) != 2:
            raise AuditError("fts_dependents must be (table, column) pairs")
        fts_table = _deletable_table("fts dependent table", dep[0]).lower()
        _sql_name("fts dependent column", dep[1])
        if fts_table in taken or fts_table in seen:
            raise AuditError("an fts dependent must be a distinct table")
        seen.add(fts_table)
    return deps  # type: ignore[return-value]


def _fts_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Verify a regular FTS5 table in ``main``; return its lower-case column names."""
    row = conn.execute(
        "SELECT sql FROM main.sqlite_master WHERE type = 'table' AND lower(name) = lower(?)",
        (table,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise AuditError("fts dependent table does not exist")
    if _FTS5_RE.search(row[0].strip()) is None:
        raise AuditError("fts dependent is not an FTS5 table")
    if _FTS_CONTENT_RE.search(row[0]):
        raise AuditError("fts dependent must be a regular FTS5 table")
    return {str(r[1]).lower() for r in conn.execute(f"PRAGMA main.table_info({_q(table)})")}


# Empty in production until iterations 22a/23 register reports and the library.
# Each entry keeps the kind and a value fingerprint taken at registration (L1): a frozen
# dataclass can still be changed through object.__setattr__, even into another valid
# definition, so every delete compares the live fields against the registered ones.
_DELETABLE: dict[str, tuple[DeletableKind, tuple]] = {}


def register_deletable(
    kind: DeletableKind, *, conn: sqlite3.Connection | None = None
) -> DeletableKind:
    """Register a deletable kind. Idempotent for an identical definition; a different
    definition under the same name raises. With ``conn`` the live schema is verified now."""
    if type(kind) is not DeletableKind:  # L1: a subclass could skip the static checks
        raise AuditError("register_deletable needs a DeletableKind (not a subclass)")
    fingerprint = astuple(kind)
    existing = _DELETABLE.get(kind.name)
    if existing is not None and existing[1] != fingerprint:
        raise AuditError("deletable kind is already registered with another definition")
    if conn is not None:
        try:
            _verify_kind(conn, kind)
        except AuditError:
            raise
        except Exception as err:  # noqa: BLE001 - sqlite3.Error, closed conn
            raise AuditError("could not verify the deletable kind") from err
    if astuple(kind) != fingerprint:  # changed while being verified
        raise AuditError("registered deletable kind was modified")
    _DELETABLE[kind.name] = (kind, fingerprint)
    return kind


def unregister_deletable(name: str) -> None:
    """Remove a kind (tests; a long-running process has no reason to call this)."""
    _DELETABLE.pop(name, None)


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_DELETABLE))


def _registered(kind: object) -> DeletableKind:
    if type(kind) is not str or kind not in _DELETABLE:
        raise AuditError("delete kind is not registered")
    spec, fingerprint = _DELETABLE[kind]
    if type(spec) is not DeletableKind or spec.name != kind or astuple(spec) != fingerprint:
        raise AuditError("registered deletable kind was modified")  # mutated after registration
    return spec


def _text_affinity(declared: str) -> bool:
    """SQLite type-affinity rules: INT wins, then CHAR/CLOB/TEXT give TEXT affinity."""
    up = declared.upper()
    return "INT" not in up and any(t in up for t in ("CHAR", "CLOB", "TEXT"))


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple[str, int]]:
    """Verify a real, collation-free table in ``main``; return {lower name: (type, pk)}."""
    row = conn.execute(
        "SELECT sql FROM main.sqlite_master WHERE type = 'table' AND lower(name) = lower(?)",
        (table,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise AuditError("delete target table does not exist")
    sql = row[0].upper()
    if sql.startswith("CREATE VIRTUAL"):
        raise AuditError("delete target table is a virtual table")
    if _COLLATE_RE.search(sql):  # fail closed: a NOCASE key or owner would match other rows
        raise AuditError("delete target table declares a collation")
    return {
        r[1].lower(): (r[2] or "", r[5])
        for r in conn.execute(f"PRAGMA main.table_info({_q(table)})")
    }


def _text_column(cols: Mapping[str, tuple[str, int]], column: str) -> None:
    info = cols.get(column.lower())
    if info is None:
        raise AuditError("delete target column does not exist")
    if not _text_affinity(info[0]):
        raise AuditError("delete target column must have TEXT affinity")


def _key_is_unique(
    conn: sqlite3.Connection, table: str, key: str, cols: Mapping[str, tuple[str, int]]
) -> bool:
    """M1: the key is the table's single-column PRIMARY KEY or has a single-column, non-partial
    UNIQUE index, so one target id can match at most one row."""
    if [n for n, (_, pk) in cols.items() if pk > 0] == [key.lower()]:
        return True
    for _seq, index, unique, _origin, partial in conn.execute(
        f"PRAGMA main.index_list({_q(table)})"
    ):
        if not unique or partial:
            continue
        info = conn.execute(f"PRAGMA main.index_info({_q(index)})").fetchall()
        if len(info) == 1 and isinstance(info[0][2], str) and info[0][2].lower() == key.lower():
            return True
    return False


def _verify_kind(conn: sqlite3.Connection, kind: DeletableKind) -> None:
    """Check the live schema for a kind (M1, L2). Runs at registration and inside every delete
    transaction, so a schema change between the two is caught. The static checks are re-run
    too (L1), so a registered kind mutated through ``object.__setattr__`` is refused."""
    if type(kind) is not DeletableKind:
        raise AuditError("deletable kind is not a DeletableKind")
    try:
        dependents = _static_checks(kind)
    except AuditError as err:
        raise AuditError("registered deletable kind was modified") from err
    try:
        fts_dependents = _static_fts_checks(kind)
    except AuditError as err:
        raise AuditError("registered deletable kind was modified") from err
    if dependents != kind.dependents or fts_dependents != kind.fts_dependents:
        raise AuditError("registered deletable kind was modified")
    main_cols = _table_columns(conn, kind.table)
    _text_column(main_cols, kind.key_column)
    if kind.owner_column is not None:
        _text_column(main_cols, kind.owner_column)
    if not _key_is_unique(conn, kind.table, kind.key_column, main_cols):
        raise AuditError("delete key column is not unique")
    for dep_table, dep_col in kind.dependents:
        _text_column(_table_columns(conn, dep_table), dep_col)
    for fts_table, fts_col in kind.fts_dependents:
        if fts_col.lower() not in _fts_columns(conn, fts_table):
            raise AuditError("fts dependent column does not exist")

    touched = {kind.table.lower(), *(t.lower() for t, _ in kind.dependents)}
    for fts_table, _col in kind.fts_dependents:
        touched |= {fts_table.lower(), *(fts_table.lower() + s for s in _FTS_SHADOWS)}
    # L2: no trigger that could run on these deletes (fail closed: any mention of DELETE).
    for schema in ("main.sqlite_master", "sqlite_temp_master"):
        for tbl, sql in conn.execute(f"SELECT tbl_name, sql FROM {schema} WHERE type = 'trigger'"):
            if str(tbl).lower() in touched and _DELETE_RE.search(str(sql)):
                raise AuditError("delete target table has a DELETE trigger")
    # L2: no FK action that would remove or rewrite rows the delete does not count.
    declared = {(t.lower(), c.lower()) for t, c in kind.dependents}
    pks = [n for n, (_, pk) in main_cols.items() if pk > 0]
    tables = [r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type='table'")]
    for child in tables:
        fks: dict[int, list[tuple]] = {}
        for fk in conn.execute(f"PRAGMA main.foreign_key_list({_q(child)})"):
            fks.setdefault(fk[0], []).append(fk)
        for parts in fks.values():
            parent, on_delete = str(parts[0][2]).lower(), str(parts[0][6]).upper()
            if parent not in touched or on_delete not in _CASCADING_ACTIONS:
                continue
            _fk_id, _seq, _parent, src, dst, *_ = parts[0]
            to_key = (dst is None and pks == [kind.key_column.lower()]) or (
                isinstance(dst, str) and dst.lower() == kind.key_column.lower()
            )
            if not (
                parent == kind.table.lower()
                and len(parts) == 1
                and to_key
                and (child.lower(), str(src).lower()) in declared
            ):
                raise AuditError("delete target is referenced by an undeclared cascading FK")


def audited_delete(
    log: AuditLog,
    *,
    kind: str,
    actor_user_id: str,
    session_id: str,
    turn_id: str,
    pending_action_id: str,
    target_ids: Iterable[str],
) -> DeleteOutcome:
    """Write ``delete.executed``, then delete the targets of a registered kind, in ONE
    transaction (HLD §6.3.3). The caller names a kind and never receives the connection.

    * Unregistered kind, invalid input, a live schema that fails :func:`_verify_kind`, or an
      audit-insert failure (any exception, ``KeyboardInterrupt`` included): rollback, nothing
      is deleted; ``AuditError`` (or the ``BaseException``) propagates.
    * Before any DELETE: ``COUNT(*)`` and ``COUNT(DISTINCT key)`` of the rows matching the
      targets (and the actor as owner) must both equal ``len(target_ids)``.
    * Before ``COMMIT``: the main DELETE removed exactly ``len(target_ids)`` rows, the
      connection's change counter moved by exactly the declared deletes (no cascade or trigger
      side effect), and the executed row is re-read by ``event_id``.
    * Any failure after the audit insert: rollback, ``delete.failed`` appended (best effort,
      ``details.requested``/``matched`` on a mismatch) and the exception propagates
      (:class:`DeleteMismatchError` for count checks).
    * Replay (``delete.executed`` already exists for ``pending_action_id``): returns the
      recorded event and deletes nothing when the actor, kind and target set match; otherwise
      ``delete.failed`` (``error_type`` ``replay_mismatch``) is appended and
      :class:`ReplayMismatchError` is raised.
    """
    spec = _registered(kind)
    targets = _target_ids(target_ids)
    if not targets:
        raise AuditError("target_ids is required for a delete")
    if len(set(targets)) != len(targets):
        raise AuditError("target_ids has duplicates")
    event = log._build(  # private path: public build refuses delete.executed (M-A)
        DELETE_EXECUTED,
        actor_user_id=actor_user_id,
        session_id=session_id,
        turn_id=turn_id,
        pending_action_id=_pending_id("pending_action_id", pending_action_id),
        target_ids=targets,
        count=len(targets),
        outcome="ok",
        details={"source": "delete", "kind": spec.name},
    )
    conn = log.conn
    try:
        open_tx = conn.in_transaction
    except Exception as err:  # noqa: BLE001 - closed connection
        raise AuditError("audit store unavailable; delete aborted") from err
    if open_tx:  # we must own the transaction, so any open one below is ours (L3)
        raise AuditError("connection already has an open transaction; delete aborted")

    stage = "begin"  # begin -> insert -> delete -> committing -> committed
    matched: int | None = None
    stored: AuditEvent | None = None
    outcome: DeleteOutcome | None = None
    try:  # BEGIN is inside the guard (L3): an interrupt right after it still rolls back
        conn.execute("BEGIN IMMEDIATE")
        stage = "insert"
        _verify_kind(conn, spec)
        stored = log._insert(event)
        if stored is not None:
            stage = "delete"
            n = len(targets)
            marks = ",".join("?" * n)
            table, key = f"main.{_q(spec.table)}", _q(spec.key_column)
            where = f"{key} IN ({marks})"
            params: list[str] = list(targets)
            if spec.owner_column is not None:
                where += f" AND {_q(spec.owner_column)} = ?"
                params.append(event.actor_user_id)
            total, distinct = conn.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT {key}) FROM {table} WHERE {where}", params
            ).fetchone()
            matched = int(distinct)
            if total != n or distinct != n:
                raise DeleteMismatchError("targets do not match exactly one row each; rolled back")
            before = conn.total_changes
            declared = 0
            for dep_table, dep_col in spec.dependents:
                declared += conn.execute(
                    f"DELETE FROM main.{_q(dep_table)} WHERE {_q(dep_col)} IN ({marks})", targets
                ).rowcount
            deleted = conn.execute(f"DELETE FROM {table} WHERE {where}", params).rowcount
            matched = deleted
            if deleted != n:
                raise DeleteMismatchError("delete matched a different number of rows; rolled back")
            if conn.total_changes - before != declared + deleted:
                raise DeleteMismatchError(
                    "delete changed rows outside the declared kind; rolled back"
                )
            # Iteration 37: FTS index rows go in the same transaction, after the exact counter
            # check (shadow-table writes are not row-for-row), are verified gone, and the index
            # is optimized so the deleted tokens leave no residue in the shadow tables.
            for fts_table, fts_col in spec.fts_dependents:
                fts = f"main.{_q(fts_table)}"
                conn.execute(f"DELETE FROM {fts} WHERE {_q(fts_col)} IN ({marks})", targets)
                left = conn.execute(
                    f"SELECT COUNT(*) FROM {fts} WHERE {_q(fts_col)} IN ({marks})", targets
                ).fetchone()[0]
                if left:
                    raise DeleteMismatchError("full-text index rows remained; rolled back")
                conn.execute(f"INSERT INTO {fts}({_q(fts_table)}) VALUES('optimize')")
            row = conn.execute(
                "SELECT event_type FROM main.audit_event WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if row is None or row[0] != DELETE_EXECUTED or not conn.in_transaction:
                raise AuditError("audit row lost before commit; delete aborted")
            stage = "committing"
            conn.execute("COMMIT")
            stage = "committed"
            outcome = DeleteOutcome(stored, replayed=False, deleted=deleted)
        else:  # already executed (replay) or a racing duplicate
            _rollback(conn)
    except BaseException as err:
        if stage == "committed" or (stage == "committing" and _committed(conn, event)):
            raise  # L2: the delete is durable; no rollback, no delete.failed
        _rollback(conn)
        if stage in ("delete", "committing"):  # COMMIT did not take effect
            _record_failure(log, event, err, matched)
            raise
        if isinstance(err, AuditError) or not isinstance(err, Exception):
            raise  # AuditError, or KeyboardInterrupt/SystemExit (L1)
        if stage == "begin":
            raise AuditError("audit store unavailable; delete aborted") from err
        raise AuditError("audit write failed; delete aborted") from err
    if outcome is not None:
        return outcome
    return DeleteOutcome(_replayed(log, event), replayed=True)


def _committed(conn: sqlite3.Connection, event: AuditEvent) -> bool:
    """After an exception around COMMIT: did it take effect? True only if no transaction is
    open and the delete.executed row is durably visible; any doubt counts as not committed."""
    try:
        if conn.in_transaction:
            return False
        row = conn.execute(
            "SELECT event_type FROM main.audit_event WHERE event_id = ?", (event.event_id,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - closed or broken connection
        return False
    return row is not None and row[0] == event.event_type


def _replayed(log: AuditLog, attempt: AuditEvent) -> AuditEvent:
    prior = log.get(attempt.pending_action_id or "", DELETE_EXECUTED)
    if prior is None:
        raise AuditError("audit write ignored; delete aborted")
    if (
        prior.actor_user_id != attempt.actor_user_id
        or sorted(prior.target_ids) != sorted(attempt.target_ids)
        or prior.details.get("kind") != attempt.details.get("kind")
    ):
        _record_failure(log, attempt, None, None, error_type="replay_mismatch")
        raise ReplayMismatchError(
            "pending_action_id was executed for another actor, kind or target set"
        )
    return prior


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except Exception:  # noqa: BLE001, S110 - nothing more to do; the caller re-raises
        pass


def _record_failure(
    log: AuditLog,
    executed: AuditEvent,
    err: BaseException | None,
    matched: int | None,
    *,
    error_type: str | None = None,
) -> None:
    details: dict[str, Any] = {
        "source": "delete",
        "error_type": error_type or type(err).__name__,
    }
    if "kind" in executed.details:
        details["kind"] = executed.details["kind"]
    if matched is not None:
        details["requested"] = len(executed.target_ids)
        details["matched"] = matched
    try:
        log._append(  # private path: public record refuses delete.failed (M-A)
            log._build(
                DELETE_FAILED,
                actor_user_id=executed.actor_user_id,
                session_id=executed.session_id,
                turn_id=executed.turn_id,
                pending_action_id=executed.pending_action_id,
                target_ids=executed.target_ids,
                count=0,
                outcome="failed",
                details=details,
            )
        )
    except Exception:  # noqa: BLE001, S110 - best effort; the original error propagates
        pass


# --- user erasure (iteration 35; SEC-18, AC-28.6, FR-59; D-223/D-224) --------------------

#: Per-user app.db tables and their user column, in delete order (vectors before reports).
#: A table that was never created is skipped. ``report_fts`` is cleared by report id first.
ERASE_USER_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("report_vector", "owner_user_id"),
    ("saved_report", "owner_user_id"),
    ("user_preferences", "user_id"),
    ("feedback", "user_id"),
    ("user_quota", "user_id"),
    ("aggregate_fingerprint", "user_id"),
)
ERASE_FTS_TABLE: Final = "report_fts"
ERASE_FTS_KEY: Final = "report_id"
#: Bound on every id list an erasure collects; over it the erase is refused, not truncated.
MAX_ERASE_IDS: Final = 50_000
PSEUDONYM_RE: Final = re.compile(r"erased-[0-9a-f]{16}")
_IN_JSON: Final = "IN (SELECT value FROM json_each(?))"


class EraseError(AuditError):
    """An erasure was refused or did not match its plan; nothing was deleted."""


class EraseRolledBackError(EraseError):
    """A step inside the erase transaction failed after ``erase.executed`` was inserted; the
    whole transaction was rolled back and ``erase.failed`` appended (D-230). Not a refusal."""


@dataclass(frozen=True)
class ErasePlan:
    """What an erasure of one user would remove (read-only scan; ids are never printed)."""

    user_id: str
    counts: Mapping[str, int]  # table -> rows to delete; "audit_event" -> rows to pseudonymise
    report_ids: tuple[str, ...]  # owned reports plus reports named by the user's audit rows
    session_ids: tuple[str, ...]
    feedback_ids: tuple[str, ...]

    @property
    def total(self) -> int:
        """Rows to delete (audit rows are pseudonymised, not deleted)."""
        return sum(n for t, n in self.counts.items() if t != "audit_event")

    def digest(self) -> str:
        """Stable hash of the plan, bound into the CLI's confirmation token (D-222)."""
        doc = {
            "counts": dict(sorted(self.counts.items())),
            "reports": sorted(self.report_ids),
            "sessions": sorted(self.session_ids),
            "feedback": sorted(self.feedback_ids),
        }
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class EraseOutcome:
    event: AuditEvent  # the erase.executed row (actor = maintainer, target_user = pseudonym)
    pseudonym: str
    plan: ErasePlan
    deleted: Mapping[str, int]
    audit_rows: int  # audit rows rewritten to the pseudonym


def _bounded(rows: Iterable[tuple], what: str) -> list[str]:
    out = [str(r[0]) for r in rows]
    if len(out) > MAX_ERASE_IDS:
        raise EraseError(f"too many {what} to erase in one run")
    return out


def _ids(conn: sqlite3.Connection, sql: str, params: tuple, what: str) -> list[str]:
    return _bounded(conn.execute(sql + " LIMIT ?", (*params, MAX_ERASE_IDS + 1)), what)


def _erase_scan(conn: sqlite3.Connection, user_id: str) -> ErasePlan:
    """Collect the plan inside the caller's read (or write) transaction."""
    has = {t: _has_table(conn, t) for t, _ in ERASE_USER_TABLES}
    has_fts = _has_table(conn, ERASE_FTS_TABLE)
    reports: set[str] = set()
    sessions: set[str] = set()
    feedback: list[str] = []
    if has["saved_report"]:
        for rid, sid in conn.execute(
            "SELECT report_id, session_id FROM main.saved_report WHERE owner_user_id = ? LIMIT ?",
            (user_id, MAX_ERASE_IDS + 1),
        ):
            reports.add(str(rid))
            sessions.add(str(sid))
    if has["report_vector"]:
        reports.update(
            _ids(conn, "SELECT report_id FROM main.report_vector WHERE owner_user_id = ?",
                 (user_id,), "vectors")
        )  # fmt: skip
    # Reports the user once deleted or exported: their ids name export files left on disk.
    audit_reports = _ids(
        conn,
        "SELECT DISTINCT j.value FROM main.audit_event a, json_each(a.target_ids) j "
        "WHERE a.actor_user_id = ? AND a.target_ids IS NOT NULL "
        "AND (a.event_type LIKE 'delete.%' OR a.event_type LIKE 'report.%')",
        (user_id,),
        "audit targets",
    )
    if audit_reports and has["saved_report"]:
        foreign = set(
            _ids(
                conn,
                f"SELECT report_id FROM main.saved_report WHERE report_id {_IN_JSON} "
                "AND owner_user_id <> ?",
                (json.dumps(audit_reports), user_id),
                "audit targets",
            )
        )
        audit_reports = [r for r in audit_reports if r not in foreign]
    reports.update(audit_reports)
    if has["feedback"]:
        for fid, sid in conn.execute(
            "SELECT feedback_id, session_id FROM main.feedback WHERE user_id = ? LIMIT ?",
            (user_id, MAX_ERASE_IDS + 1),
        ):
            feedback.append(str(fid))
            sessions.add(str(sid))
    if has["aggregate_fingerprint"]:
        sessions.update(
            _ids(conn, "SELECT DISTINCT session_id FROM main.aggregate_fingerprint "
                 "WHERE user_id = ?", (user_id,), "sessions")
        )  # fmt: skip
    sessions.update(
        _ids(conn, "SELECT DISTINCT session_id FROM main.audit_event WHERE actor_user_id = ?",
             (user_id,), "sessions")
    )  # fmt: skip
    for what, ids in (("reports", reports), ("sessions", sessions), ("feedback", feedback)):
        if len(ids) > MAX_ERASE_IDS:
            raise EraseError(f"too many {what} to erase in one run")
    report_ids = tuple(sorted(reports))
    counts: dict[str, int] = {}
    if has_fts:
        counts[ERASE_FTS_TABLE] = conn.execute(
            f"SELECT COUNT(*) FROM main.{ERASE_FTS_TABLE} WHERE {ERASE_FTS_KEY} {_IN_JSON}",
            (json.dumps(list(report_ids)),),
        ).fetchone()[0]
    for table, col in ERASE_USER_TABLES:
        if has[table]:
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM main.{table} WHERE {col} = ?", (user_id,)
            ).fetchone()[0]
    counts["audit_event"] = conn.execute(
        "SELECT COUNT(*) FROM main.audit_event WHERE actor_user_id = ? "
        "OR json_extract(details, '$.target_user') = ?",
        (user_id, user_id),
    ).fetchone()[0]
    return ErasePlan(
        user_id=user_id,
        counts=counts,
        report_ids=report_ids,
        session_ids=tuple(sorted(sessions)),
        feedback_ids=tuple(sorted(feedback)),
    )


def erase_plan(log: AuditLog, user_id: str) -> ErasePlan:
    """Read-only preview of :func:`audited_erase` (one consistent read transaction)."""
    user = _actor_id("user_id", user_id)
    conn = log.conn
    if conn.in_transaction:
        raise EraseError("connection already has an open transaction")
    try:
        conn.execute("BEGIN")
        try:
            return _erase_scan(conn, user)
        finally:
            _rollback(conn)
    except AuditError:
        raise
    except Exception as err:  # noqa: BLE001
        raise AuditError("audit store unavailable; erase preview failed") from err


def _no_update_trigger() -> str:
    (stmt,) = [s for s in AUDIT_MIGRATION if "audit_event_no_update" in s]
    return stmt


def _pseudonymise(conn: sqlite3.Connection, user_id: str, pseudonym: str) -> int:
    """The ONE permitted audit mutation (D-223): inside the caller's transaction, drop the
    no-update trigger, rewrite only ``actor_user_id`` and ``details.target_user`` for this user,
    recreate the trigger from the canonical DDL and verify the exact schema before COMMIT."""
    if _schema_objects(conn) != _EXPECTED:
        raise AuditError("audit store layout does not match the expected schema")
    conn.execute("DROP TRIGGER main.audit_event_no_update")
    n = conn.execute(
        "UPDATE main.audit_event SET actor_user_id = ? WHERE actor_user_id = ?",
        (pseudonym, user_id),
    ).rowcount
    n += conn.execute(
        "UPDATE main.audit_event SET details = json_set(details, '$.target_user', ?) "
        "WHERE json_extract(details, '$.target_user') = ?",
        (pseudonym, user_id),
    ).rowcount
    conn.execute(_no_update_trigger())
    if _schema_objects(conn) != _EXPECTED:
        raise AuditError("audit trigger was not restored; erase aborted")
    left = conn.execute(
        "SELECT COUNT(*) FROM main.audit_event WHERE actor_user_id = ? "
        "OR json_extract(details, '$.target_user') = ?",
        (user_id, user_id),
    ).fetchone()[0]
    if left:
        raise EraseError("audit rows still name the user; rolled back")
    return n


def audited_erase(
    log: AuditLog,
    *,
    actor_user_id: str,
    user_id: str,
    expected_digest: str | None = None,
    checkpoint: bool = True,
) -> EraseOutcome:
    """Erase every app.db row of ``user_id`` in ONE transaction, audit first (SEC-18, D-224).

    Before the transaction, ``erase.attempted`` is appended on its own (D-230): actor, session,
    turn and pseudonym are the ones the ``erase.executed`` / ``erase.failed`` rows reuse, so a
    rolled-back erase still has a durable start row matched by its ``erase.failed``. If that
    write fails, nothing else happens (``AuditError``).

    Order inside ``BEGIN IMMEDIATE``: scan the plan (refused if ``expected_digest`` differs),
    insert ``erase.executed`` (actor = the maintainer, ``details.target_user`` = a fresh random
    pseudonym, ``count`` = rows to delete), delete the FTS rows of the user's reports (verified
    gone, then ``optimize``), delete the per-user tables in :data:`ERASE_USER_TABLES` (each
    count checked against the plan, then verified zero), pseudonymise the user's audit rows,
    re-read the executed row, COMMIT, then ``wal_checkpoint(TRUNCATE)``.

    * Audit insert failure (or anything before it): rollback, nothing deleted, ``AuditError``.
    * Any failure after ``erase.attempted`` (including a refused plan digest): rollback,
      ``erase.failed`` appended best effort, re-raised.
    * A second erase of the same user deletes nothing and still writes ``erase.executed``
      (count 0, a new pseudonym): idempotent and audited.
    """
    actor = _actor_id("actor_user_id", actor_user_id)
    user = _actor_id("user_id", user_id)
    if actor == user:
        raise EraseError("the erasing maintainer cannot be the erased user")
    pseudonym = "erased-" + secrets.token_hex(8)
    conn = log.conn
    try:
        open_tx = conn.in_transaction
    except Exception as err:  # noqa: BLE001 - closed connection
        raise AuditError("audit store unavailable; erase aborted") from err
    if open_tx:
        raise AuditError("connection already has an open transaction; erase aborted")

    try:  # D-230: the attempt is durable before anything can be deleted
        attempted = log._append(  # private path: public record refuses erase.attempted
            log._build(
                ERASE_ATTEMPTED,
                actor_user_id=actor,
                session_id=uuid.uuid4().hex,
                turn_id=uuid.uuid4().hex[:12],
                details={"source": "erase", "target_user": pseudonym},
            )
        )
    except AuditError:
        raise
    except Exception as err:  # noqa: BLE001 - bad clock / validation
        raise AuditError("audit write failed; erase aborted") from err
    if attempted is None:
        raise AuditError("audit write ignored; erase aborted")

    stage = "begin"  # begin -> insert -> delete -> committing -> committed
    event: AuditEvent | None = None
    result: EraseOutcome | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        stage = "insert"
        plan = _erase_scan(conn, user)
        if expected_digest is not None and plan.digest() != expected_digest:
            raise EraseError("the data changed since the preview; preview again")
        event = log._build(  # private path: public build refuses erase.executed (D-223)
            ERASE_EXECUTED,
            actor_user_id=actor,
            session_id=attempted.session_id,
            turn_id=attempted.turn_id,
            count=plan.total,
            outcome="ok",
            details={"source": "erase", "target_user": pseudonym},
        )
        stored = log._insert(event)
        if stored is None:
            raise AuditError("audit write ignored; erase aborted")
        stage = "delete"
        deleted: dict[str, int] = {}
        if ERASE_FTS_TABLE in plan.counts:
            fts, ids = f"main.{ERASE_FTS_TABLE}", json.dumps(list(plan.report_ids))
            conn.execute(f"DELETE FROM {fts} WHERE {ERASE_FTS_KEY} {_IN_JSON}", (ids,))
            left = conn.execute(
                f"SELECT COUNT(*) FROM {fts} WHERE {ERASE_FTS_KEY} {_IN_JSON}", (ids,)
            ).fetchone()[0]
            if left:
                raise EraseError("full-text index rows remained; rolled back")
            conn.execute(f"INSERT INTO {fts}({ERASE_FTS_TABLE}) VALUES('optimize')")
            deleted[ERASE_FTS_TABLE] = plan.counts[ERASE_FTS_TABLE]
        for table, col in ERASE_USER_TABLES:
            if table not in plan.counts:
                continue
            n = conn.execute(f"DELETE FROM main.{table} WHERE {col} = ?", (user,)).rowcount
            left = conn.execute(
                f"SELECT COUNT(*) FROM main.{table} WHERE {col} = ?", (user,)
            ).fetchone()[0]
            if n != plan.counts[table] or left:
                raise EraseError(f"{table} did not match the erase plan; rolled back")
            deleted[table] = n
        audit_rows = _pseudonymise(conn, user, pseudonym)
        row = conn.execute(
            "SELECT event_type FROM main.audit_event WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        if row is None or row[0] != ERASE_EXECUTED or not conn.in_transaction:
            raise AuditError("audit row lost before commit; erase aborted")
        stage = "committing"
        conn.execute("COMMIT")
        stage = "committed"
        result = EraseOutcome(stored, pseudonym, plan, deleted, audit_rows)
    except BaseException as err:
        if stage == "committed" or (
            stage == "committing" and event is not None and _committed(conn, event)
        ):
            raise
        _rollback(conn)
        # D-230: every attempt gets an outcome row (erase.executed rolled back with the deletes)
        _append_erase_failure(log, attempted, store="app_db", error_type=type(err).__name__)
        if stage in ("delete", "committing") and event is not None:
            if isinstance(err, AuditError) or not isinstance(err, Exception):
                raise
            raise EraseRolledBackError("a delete failed; rolled back") from err
        if isinstance(err, AuditError) or not isinstance(err, Exception):
            raise
        if stage == "begin":
            raise AuditError("audit store unavailable; erase aborted") from err
        raise AuditError("audit write failed; erase aborted") from err
    if checkpoint:
        try:  # fold the WAL so no deleted page survives in it (D-224); best effort after COMMIT
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001, S110 - the erase is durable; the next checkpoint folds it
            pass
    return result


def record_erase_failure(
    log: AuditLog, executed: AuditEvent, *, store: str, error_type: str
) -> AuditEvent | None:
    """Append ``erase.failed`` for a step of an erasure (a file store after COMMIT, D-226).

    Only for a durable ``erase.executed`` row (checked by ``event_id``; ``AuditError``
    otherwise), so it cannot be used to forge erasure history. The write itself is best
    effort: None if it fails."""
    try:
        row = log.conn.execute(
            "SELECT event_type FROM main.audit_event WHERE event_id = ?", (executed.event_id,)
        ).fetchone()
    except Exception as err:  # noqa: BLE001
        raise AuditError("audit read failed") from err
    if row is None or row[0] != ERASE_EXECUTED:
        raise AuditError("erase.failed needs a recorded erase.executed")
    return _append_erase_failure(log, executed, store=store, error_type=error_type)


def _append_erase_failure(
    log: AuditLog, executed: AuditEvent, *, store: str, error_type: str
) -> AuditEvent | None:
    try:
        return log._append(  # private path: public record refuses erase.failed (D-223)
            log._build(
                ERASE_FAILED,
                actor_user_id=executed.actor_user_id,
                session_id=executed.session_id,
                turn_id=executed.turn_id,
                count=0,
                outcome="failed",
                details={
                    "source": "erase",
                    "store": store,
                    "error_type": error_type,
                    "target_user": executed.details.get("target_user"),
                },
            )
        )
    except Exception:  # noqa: BLE001 - best effort; the original error propagates
        return None


# --- recorder and guard adapters ---------------------------------------------------------

_RECORD_KEYS: Final = frozenset(
    {"session_id", "turn_id", "pending_action_id", "target_ids", "count", "rule", "outcome"}
)


def recorder(
    log: AuditLog,
    *,
    user_id: str,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> Callable[[dict], AuditEvent | None]:
    """Bind an actor and return ``record(dict)`` for ``run_sql(audit=...)`` and guard events.

    Accepted keys: ``event``/``event_type``, ``session_id``, ``turn_id``,
    ``pending_action_id``, ``target_ids``, ``count`` (``rows`` is an alias), ``rule``,
    ``outcome``, ``details`` (a mapping) and any ``DETAIL_FIELDS`` key at top level (run_sql
    sends ``code``, ``class``, ``stage``, hashes and ``bytes_billed`` that way). ``user_id``
    may be present only if it equals the bound actor. Any other key raises AuditError.
    """
    actor = _actor_id("user_id", user_id)

    def record(entry: dict) -> AuditEvent | None:
        if not isinstance(entry, Mapping):
            raise AuditError("audit entry must be a mapping")
        data = dict(entry)
        for alias in ("user_id", "actor_user_id"):
            if alias in data and data.pop(alias) != actor:
                raise AuditError("audit entry actor does not match the session user")
        event_type = data.pop("event_type", None) or data.pop("event", None)
        data.pop("event", None)
        if "rows" in data:
            rows = data.pop("rows")
            if rows is not None:
                data.setdefault("count", rows)
        details = dict(data.pop("details", None) or {})
        fields: dict[str, Any] = {}
        for key, value in data.items():
            if key in _RECORD_KEYS:
                fields[key] = value
            elif key in DETAIL_FIELDS:
                details[key] = value
            else:
                raise AuditError("audit entry has a field that is not allowed")
        fields["session_id"] = fields.get("session_id") or session_id
        fields["turn_id"] = fields.get("turn_id") or turn_id
        return log.record(event_type, actor_user_id=actor, details=details, **fields)

    return record


def from_input(decision: Any) -> dict | None:
    """Recorder entry for a refused ``guards.input.InputDecision`` (iteration 11)."""
    if getattr(decision, "allowed", True) or decision.rule is None:
        return None
    rule = decision.rule
    details: dict[str, Any] = {"source": "input", "category": _INPUT_CATEGORY.get(rule)}
    if decision.pii_types:
        details["pii_types"] = sorted(decision.pii_types)
    event = decision.audit_event or input_guard.audit_event_for(rule)
    return {"event_type": event, "rule": rule, "outcome": "refused", "details": details}


def from_router(decision: Any) -> dict | None:
    """Recorder entry for a refused ``roles.router.RouterDecision`` (iteration 11)."""
    rule = getattr(decision, "refusal_rule", None)
    if rule is None:
        return None
    return {
        "event_type": decision.audit_event or input_guard.audit_event_for(rule),
        "rule": rule,
        "outcome": "refused",
        "details": {"source": "router", "category": _INPUT_CATEGORY.get(rule)},
    }


def from_refusal(refusal: ScopeRefusal) -> dict:
    """Recorder entry for a SQL-path refusal (iterations 6, 7, 9, 10).

    ``rule`` is the guard's real reason code; ``details.category`` is the AC-28.3 bucket.
    """
    if not isinstance(refusal, ScopeRefusal):
        raise AuditError("not a SQL-path refusal")
    reason = refusal.reason_code
    if reason in _SCOPE_RULES:
        category = SCOPE_BLOCK
    elif refusal.error_code in _BUDGET_CODES:
        category = BUDGET
    else:
        category = SQL_POLICY
    details: dict[str, Any] = {"source": "sql", "category": category}
    if refusal.error_code in CODES:
        details["code"] = refusal.error_code
    cause = diff_guard.unavailable_cause(refusal)
    if cause is not None:
        details["cause"] = cause
    return {
        "event_type": GUARDRAIL_REFUSED,
        "rule": reason if reason in RULES else category,
        "outcome": "refused",
        "details": details,
    }


def from_budget(code: str) -> dict:
    """Recorder entry for a budget refusal (SESSION_BUDGET, BUDGET_EXHAUSTED or COST_CAP)."""
    if code not in _BUDGET_CODES:
        raise AuditError("not a budget code")
    return {
        "event_type": GUARDRAIL_REFUSED,
        "rule": BUDGET,
        "outcome": "refused",
        "details": {"source": "sql", "category": BUDGET, "code": code},
    }


def from_output(verdict: Any) -> list[dict]:
    """Recorder entries for a blocked ``guards.output.OutputVerdict`` (iteration 12).

    One entry per blocking code; modifications (redaction, stripping) are not audited.
    """
    if getattr(verdict, "allowed", True):
        return []
    codes = [e.code for e in verdict.events if e.code in _OUTPUT_CATEGORY]
    if not codes:
        codes = [output_guard.OUTPUT_GUARD_ERROR]
    out = []
    for code in dict.fromkeys(codes):
        out.append(
            {
                "event_type": _OUTPUT_EVENT.get(code, GUARDRAIL_REFUSED),
                "rule": code,
                "outcome": "refused",
                "details": {"source": "output", "category": _OUTPUT_CATEGORY[code]},
            }
        )
    return out
