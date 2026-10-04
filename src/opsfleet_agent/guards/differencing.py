"""Cross-query differencing guard, per user and across sessions (iteration 10; FR-70, AC-08.7,
AC-08.15, HLD §5.5, D-40). Reworked after the iteration 10 security review (B-1, H-1..H-4,
M-1, M-3, L-1) and its re-reviews (R2-H1..H3, R2-M1, R2-L1, R2-L2, G-1..G-3; R3-H1).

Differencing inside one statement is refused earlier by the small-cell rule as
``qi_differencing``. This guard covers the cross-query form: two aggregates of one user, from
the same or different sessions within 30 days, whose difference isolates fewer than ``k``
customers. It is pure apart from the :class:`FingerprintStore` (one indexed read at prepare,
one read-and-insert transaction at release): no BigQuery job, no LLM, no logging of SQL or
values.

**Fingerprint** (built from the policy-checked, scoped, small-cell-rewritten statement; the
code CTEs ``__p``/``__oi``/``__o``/``__u`` are read through to their base table, the brand
list is in the scope key). Everything except ``scope_key`` is a keyed digest:

* ``dims``: the cell's dimensions. A ``GROUP BY`` expression becomes a dimension identified by
  the **underlying column** it derives from (lineage through aliases, ordinals, derived
  tables, user CTEs, set operations and the code CTEs) plus the canonical shape of the
  expression over it. Identity-like wrappers (``TRIM``, ``LOWER``, ``UPPER``, a cast to text,
  concatenation with ``''``) are see-through, so ``TRIM(u.state)`` and ``u.state`` are one
  dimension (review H-1/H-2, OD-2). An equality filter ``x = 'v'`` / ``x IN ('v')`` in a
  WHERE or inner-join ON of the cell or of a derived table feeding it through FROM / an inner
  join is an **implicit dimension** fixed to ``v`` (review H-3, OD-3).
  Each dimension also carries the digests of the source columns it derives from.
* ``filters``: the source-column digests of every other predicate (WHERE, ON, HAVING minus
  the small-cell threshold, QUALIFY, everywhere outside the code CTEs) and of any conditional
  measure; ``*`` (``ANY_COLUMN``) when a column cannot be resolved, for an aggregate in a
  filter, an outer ``USING`` and a ``LIMIT``/``OFFSET``.
* ``predicates``: audit only, plus repeat identity: the digest of the whole canonical
  statement and of each predicate / conditional measure, plus a marker digest when every
  measure is ``COUNT(DISTINCT <user key>)`` ("customers-only": row counts are not compared)
  and a marker digest when any SELECT has a user ``HAVING`` / ``QUALIFY`` conjunct other
  than the code's small-cell threshold ("user cut", R3-H1), and a "top-N cut" marker when a
  ``LIMIT`` / ``OFFSET`` / ``FETCH`` sits at or above the cell (on the cell, a wrapper, a CTE
  outside the cell or the statement; R4-H1). A limit strictly inside the cell (row sampling
  in a derived table) is not marked. Both markers are "cuts" in steps 2, 3 and 5. The top-N
  marker is dropped at release when nothing was cut: a single literal ``LIMIT n`` on the
  statement itself, no ``OFFSET``, fewer than ``n`` rows returned, and the client did not
  truncate the result; ``prepare`` checks without it (the row count is unknown) and
  ``release`` decides it before its authoritative check, so the stored fingerprint carries
  the marker exactly when the cut may have dropped a cell. A result truncated by the
  client's row cap is a top-N cut too (R5-H1): ``release`` takes a required ``truncated``
  flag and, when it is set, adds the top-N marker whether or not the statement has a
  ``LIMIT`` and never drops it.
* ``cells``: cell key (sorted ``(dimension, value)`` digests, values normalised: trimmed,
  case-folded, numbers canonical) -> distinct-customer count. The count comes from a
  code-injected ``COUNT(DISTINCT <user key>) AS _cell_customers`` in the same statement
  (removed from the rows before release), or from the population query of an ungrouped cell
  (small-cell variant b). ``None`` when unknown (cell not the top-level SELECT, conditional
  measure, rows cannot be told apart).
* ``rows`` (R2-H1): cell key -> row count, from a code-injected ``COUNT(*) AS _cell_rows``
  (also removed before release). Injected unless the statement is customers-only, so a
  ``COUNT(*)`` / ``SUM`` that changes while every customer count stays equal is still seen.
  ``None`` when not injected. Values are canonicalised (R2-L2): ``'2025-1-1'``,
  ``'2025-01-01 00:00:00'`` and ``date(2025, 1, 1)`` are one value (a date is midnight; an
  aware timestamp is converted to naive UTC); temporal text that cannot be parsed is marked
  undecidable: it never becomes a fixed dimension and never proves a cell empty.

**Rule** (review B-1, OD-1: the "exactly one predicate change" rule is gone). A new aggregate
is compared with **every** stored fingerprint of the same user and scope key from the last 30
days (capped at ``MAX_CANDIDATES``; more fails closed). For each stored one:

1. Identical statement and dimensions: a repeat. **Interim default (R2-M1, owner item):**
   refused only when a cell's customer or row count moved by ``0 < d < k`` (the data changed
   under the same question); identical counts, or a move of ``>= k``, pass.
2. Dimension sets not nested: refused if either side has an unresolved column or one side's
   source columns are a subset of the other's (the same partition in another spelling, e.g.
   ``COALESCE(u.state, '')`` vs ``u.state``); refused if either side has an unresolved filter
   or one side filters a column the other side's dimensions derive from (R2-H3: a filter
   standing in for a dimension, e.g. ``state LIKE 'X'`` vs ``state = 'X'``). If the extra
   dimensions on both sides are only fixed (``x = 'v'``), the populations differ: allowed.
   Otherwise (R2-H2: two grouped partitions of one population) both sides are rolled up to
   their shared dimensions, or to the grand total when none, and compared as in step 5;
   refused instead when either side has a user cut (R3-H1: its sum is unknown).
3. Nested (the coarse side's dimensions are a subset of the fine side's; no shared measure is
   required, review H-4): if the fine side has extra dimensions and the coarse side filters on
   a column those extra dimensions derive from (or either side is unresolved), refused: the
   filter can remove whole fine cells, so their sum hides the difference ("hidden filter").
   Likewise refused when the fine side has extra dimensions and a user cut (R3-H1).
4. Either side's counts unknown: refused (fail closed).
5. Both sides' cells are projected onto the coarse dimensions and summed. A cell present on
   both sides is refused when ``0 < |a - b| < k``, or (unless either side is customers-only)
   when the row counts are known on both sides and differ by ``0 < d < k`` (R2-H1). Equal
   customer counts with the row count unknown on one side are refused (fail closed). A cell
   present on one side only (the other side suppressed it below ``k`` or filtered it out) is
   refused when its count is ``<= 2k - 2`` (review M-1, OD-4: the hidden side may hold up to
   ``k - 1``, so a visible count up to ``2k - 2`` can still leave a difference below ``k``),
   unless the other side provably has no such cell (it fixes the same dimension to a
   different value). Whatever its count, it is refused when the side missing it has a user
   cut (R3-H1: ``HAVING COUNT(*) > 79`` vs the same with ``> 80`` or with an extra filter
   turns a below-``k`` change into a cell's presence); the provably-empty exemption still
   applies.

Refusals use ``SQL_POLICY`` rule ``differencing`` and a templated hint that never contains a
count. Store failures refuse (SEC-12) with a :class:`DifferencingUnavailable` (a
``ScopeRefusal`` subclass, G-2) whose ``cause`` is ``CAUSE_STORE`` (``UNAVAILABLE_HINT``), or
``CAUSE_CAPACITY`` (``CAPACITY_HINT``) when more than ``MAX_CANDIDATES`` distinct
fingerprints are stored; :func:`unavailable_cause` reads it (``None`` for other refusals).
Identical fingerprints are stored once (R2-L1: the row's time and session are refreshed).

**Two checks, one record** (review M-3). :meth:`DifferencingGuard.prepare` refuses what it can
already decide (before any bytes are spent). :meth:`DifferencingGuard.release` re-reads the
candidates and records the new fingerprint **in one transaction**
(:meth:`FingerprintStore.check_and_record`) under a guard lock, so two prepared plans released
in either order are compared with each other.

**Plans are unforgeable** (review L-1): every plan carries a random token registered in the
guard that issued it. ``release`` accepts only that exact object, once; a constructed,
``dataclasses.replace``-d, reused or foreign plan is a ``rewrite_invariant`` refusal.

**Owner decisions** (logged, not self-approved): OD-1 compare against all candidates; OD-2
canonical cell identity keyed by underlying columns; OD-3 equality filters are implicit
dimensions; OD-4 one-sided cells refused up to ``2k - 2``; predicates kept as digests, not
raw literals (the HLD wording says "literals included"; ``test_fingerprint_store_has_no_values``
and AC-08.15's "no values" point the other way). Round 2: R2-M1 interim repeat default (step
1); equal customer counts with an unknown row count refused; the R2-H3 overlap and
unresolved-filter rules refuse some benign pairs (e.g. any non-nested pair after a ``LIMIT``
query); the R2-H2 roll-up applies when **either** side has a grouped extra dimension; the
customers-only exemption is carried by a predicate marker; temporal canonicalisation (date =
midnight, naive = UTC); dedupe keeps one row per identical fingerprint. Round 3: R3-H1
refuses more benign pairs with a user ``HAVING`` / ``QUALIFY`` (any one-sided cell against a
cut side, any roll-up or nested-with-extra-dimensions sum involving one). Round 4: R4-H1
treats a ``LIMIT`` / ``OFFSET`` at or above the cell as a cut, so any cell missing behind a
top-N is refused whatever its count (more false positives for top-N questions; a bound-aware
rule for ``ORDER BY count DESC LIMIT n`` is OD-R4-1), except a full result (fewer rows than
the literal ``LIMIT``, no ``OFFSET``); ``prepare`` is optimistic about that exemption and
``release`` is authoritative. Round 6: R5-H1 marks every result the client's row cap
truncated as a top-N cut, with or without a ``LIMIT`` (more refusals for large results that
miss a cell another query saw).

**Residual gaps** (documented, not closed here):

* M-2: inclusion-exclusion over three or more queries whose pairwise differences are all
  ``>= k`` (e.g. ``base``, ``base+A``, ``base+B``, ``base+A+B``) is not detected.
* FD gap: two non-nested partitions whose columns are functionally dependent (``state`` vs
  ``country`` with an extra filter on one) are not compared.
* Overcount gap: summing distinct-customer counts over an order-level extra dimension
  (``category``, ``year``) overcounts customers present in several fine cells, so a
  difference can be masked.
* L-2: the population of an ungrouped cell is whatever the caller passes (iteration 13
  passes the value it ran); no comparison across scope keys (different brand lists).
* Roll-up masking: cells suppressed below ``k`` are missing from a roll-up sum, so a
  difference hidden in suppressed cells is not seen by the R2-H2 comparison.
* No distinct-order count: a measure over orders (``COUNT(DISTINCT order_id)``) is compared
  through customers and rows only.

**Call sequence for iteration 13 (``run_sql``)**, after ``check_sql`` -> ``apply_scope`` ->
``apply_small_cell``::

    guard = DifferencingGuard(store)              # long-lived: plans are bound to it
    sc = apply_small_cell(scoped, scope)
    if isinstance(sc, PopulationCheck):
        population = <dry-run + run sc.query>     # counted in the SQL budget
        nxt = sc.evaluate(population)             # ScopeRefusal when < k: return it
        plan = guard.prepare(sc, scope, user_id=..., session_id=..., population=population)
    else:
        plan = guard.prepare(sc, scope, user_id=..., session_id=...)
    if isinstance(plan, ScopeRefusal): return the refusal      # pre-execution check
    <dry-run, cap and run plan.query>             # the instrumented statement; memo keys on it
    out = guard.release(plan, rows, truncated=result.truncated)  # before rows reach the model
    if isinstance(out, ScopeRefusal): return the refusal       # post-execution check
    rows = out.rows                               # injected columns removed
    # on any path that will not call release (query error, budget refusal): guard.abandon(plan)

API delta for the wiring (review rework): a plan is single-use and only valid for the guard
instance that issued it, so a memo hit must still call ``prepare`` and then ``release`` with
that fresh plan (the memo must cache rows **with** the injected columns, i.e.
``plan.injected_columns`` = ``_cell_customers`` and/or ``_cell_rows``, or key on
``plan.query.sql``). Round 2 adds: ``DifferencingGuard.abandon(plan)`` (G-1: idempotent,
records nothing, no-op for a foreign or unknown plan), ``DifferencingGuard.open_plan_count()``
(G-3), ``DifferencingUnavailable`` / ``unavailable_cause`` / ``CAUSE_STORE`` /
``CAUSE_CAPACITY`` / ``CAPACITY_HINT`` (G-2), ``ROW_COUNT_COLUMN`` and the plan fields
``row_count_column``, ``customers_only`` and ``injected_columns``; ``Fingerprint.rows`` and
``FingerprintCapError`` in the store. Column metadata (``data["columns"]``) must drop
``plan.injected_columns`` too. ``FingerprintStore.candidates(user_id, scope_key)`` lost its
group-key argument; ``FingerprintStore.check_and_record`` is the release-time write. A
fingerprint table with the pre-review layout makes the store refuse to open (delete the dev
DB).
Startup calls ``store.purge_expired()``; ``/erase`` calls ``store.delete_user(user_id)``.
"""

from __future__ import annotations

import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import sqlglot
from sqlglot import exp

from opsfleet_agent.guards.scope import (
    ProductScope,
    ScopedQuery,
    ScopeRefusal,
    ScopeRule,
    verify_scoped,
)
from opsfleet_agent.guards.scope_ctes import CODE_CTE_ORDER, TABLE_FOR_CODE_CTE
from opsfleet_agent.guards.small_cell import DEFAULT_K, PopulationCheck, SmallCellRewrite
from opsfleet_agent.guards.sql_policy import (
    _Analyzer,
    _branch_selects,
    _contains_aggregate,
    _selected_sources,
    _source_by_alias,
)
from opsfleet_agent.store.fingerprints import (
    ANY_COLUMN,
    MAX_CELLS,
    CellKey,
    Dim,
    Fingerprint,
    FingerprintCapError,
    FingerprintStore,
    StoredFingerprint,
)

__all__ = [
    "CAPACITY_HINT",
    "CAUSE_CAPACITY",
    "CAUSE_STORE",
    "CELL_COUNT_COLUMN",
    "DIFFERENCING",
    "HINT",
    "ROW_COUNT_COLUMN",
    "UNAVAILABLE_HINT",
    "DifferencingGuard",
    "DifferencingPlan",
    "DifferencingUnavailable",
    "Fingerprint",
    "Released",
    "unavailable_cause",
]

DIFFERENCING: Final = "differencing"
CELL_COUNT_COLUMN: Final = "_cell_customers"
#: Code-injected ``COUNT(*)`` per cell (R2-H1), stripped before release like the column above.
ROW_COUNT_COLUMN: Final = "_cell_rows"
HINT: Final = (
    "this question, combined with an earlier one, would single out a few customers; "
    "ask about a broader group or a different breakdown"
)
UNAVAILABLE_HINT: Final = "the privacy check is unavailable right now; try again later"
CAPACITY_HINT: Final = (
    "the privacy check has too many earlier questions to compare right now; try again later"
)
#: :attr:`DifferencingUnavailable.cause` values (G-2).
CAUSE_STORE: Final = "store"
CAUSE_CAPACITY: Final = "capacity"
#: Plans issued but not yet released, per guard (oldest evicted first; evicted = invalid).
MAX_OPEN_PLANS: Final = 1024
_MAX_DEPTH: Final = 32


def _refuse() -> ScopeRefusal:
    return ScopeRefusal(reason_code=DIFFERENCING, hint=HINT)


@dataclass(frozen=True, slots=True)
class DifferencingUnavailable(ScopeRefusal):
    """The check could not run, so the query is refused (fail closed; G-2 structured signal).

    ``reason_code`` is still ``differencing``. ``cause`` is ``CAUSE_STORE`` (store missing or
    failing; ``hint`` is ``UNAVAILABLE_HINT``, as before) or ``CAUSE_CAPACITY`` (more than
    ``MAX_CANDIDATES`` stored fingerprints to compare; ``hint`` is ``CAPACITY_HINT``).
    """

    cause: str = CAUSE_STORE


def unavailable_cause(refusal: object) -> str | None:
    """``cause`` of a :class:`DifferencingUnavailable`, else None (an ordinary refusal)."""
    return refusal.cause if isinstance(refusal, DifferencingUnavailable) else None


def _unavailable(cause: str = CAUSE_STORE) -> DifferencingUnavailable:
    hint = CAPACITY_HINT if cause == CAUSE_CAPACITY else UNAVAILABLE_HINT
    return DifferencingUnavailable(reason_code=DIFFERENCING, hint=hint, cause=cause)


def _store_failure(err: BaseException) -> DifferencingUnavailable:
    return _unavailable(CAUSE_CAPACITY if isinstance(err, FingerprintCapError) else CAUSE_STORE)


def _invariant() -> ScopeRefusal:
    return ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT)


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class DifferencingPlan:
    """Pre-execution outcome: run ``query``, then pass its rows to ``release`` of the guard
    that issued this plan (once). ``applies`` is False for a statement that references no QI
    (nothing to check or record)."""

    query: ScopedQuery
    applies: bool
    user_id: str = ""
    session_id: str = ""
    fingerprint: Fingerprint | None = None
    count_column: str | None = None
    cell_columns: tuple[tuple[str, str, str], ...] = ()
    fixed_cells: tuple[tuple[str, str], ...] = ()
    row_count_column: str | None = None
    customers_only: bool = False
    #: R4-H1: the literal ``LIMIT`` of a statement whose limit marker ``release`` drops when
    #: fewer rows come back (None: no limit, or the marker is permanent)
    limit_rows: int | None = None
    token: str = field(default="", repr=False, compare=False)

    @property
    def allowed(self) -> bool:
        return True

    @property
    def injected_columns(self) -> tuple[str, ...]:
        """Columns the guard added to ``query`` (strip them from any column list; ``release``
        strips them from the rows)."""
        return tuple(c for c in (self.count_column, self.row_count_column) if c is not None)


@dataclass(frozen=True, slots=True)
class Released:
    """Post-execution outcome: rows safe to hand on (injected columns removed)."""

    rows: list[dict[str, Any]]

    @property
    def allowed(self) -> bool:
        return True


# --------------------------------------------------------------------------- rule

_OK: Final = "ok"
_REFUSE: Final = "refuse"
_NEED: Final = "need-counts"
#: Hashed into ``predicates`` of a statement whose measures depend on the customer set only.
_CUSTOMERS_ONLY_MARK: Final = "kind:customers-only"
#: Hashed into ``predicates`` of a statement with a user ``HAVING`` / ``QUALIFY`` conjunct
#: (anything but the code's own small-cell threshold) in any of its SELECTs (R3-H1): such a
#: cut decides which cells exist, so a missing cell or a rolled-up sum proves nothing.
_USER_CUT_MARK: Final = "kind:user-having"
#: Hashed into ``predicates`` of a statement with a ``LIMIT`` / ``OFFSET`` at or above the
#: cell (R4-H1): a top-N cut decides which cells exist exactly like a user ``HAVING``. Kept
#: separate from ``_USER_CUT_MARK`` so ``release`` can drop it when nothing was cut (no
#: ``OFFSET``, a literal ``LIMIT n`` on the statement itself, fewer than ``n`` rows back).
_LIMIT_CUT_MARK: Final = "kind:user-limit"


def _cut(fp: Fingerprint, cuts: frozenset[str]) -> bool:
    """``fp`` carries a cut marker (user ``HAVING`` / ``QUALIFY`` or a top-N ``LIMIT``)."""
    return not cuts.isdisjoint(fp.predicates)


def _cols(fp: Fingerprint, dims: Iterable[Dim] | None = None) -> frozenset[str]:
    out: set[str] = set()
    for d in fp.dims if dims is None else dims:
        out |= d.cols
    return frozenset(out)


def _project(cells: Mapping[CellKey, int], keep: frozenset[str]) -> dict[CellKey, int]:
    out: dict[CellKey, int] = {}
    for key, n in cells.items():
        k = tuple(p for p in key if p[0] in keep)
        out[k] = out.get(k, 0) + n
    return out


def _provably_empty(key: CellKey, fp: Fingerprint) -> bool:
    """``fp`` fixes one of the key's dimensions to another value: it cannot have this cell.
    A value that could not be canonicalised (``?`` prefix) never proves anything (R2-L2)."""
    fixed = {d.dim: d.fixed for d in fp.dims if d.fixed}
    return any(
        dim in fixed and not value.startswith("?") and fixed[dim] != value for dim, value in key
    )


def _compare(
    new: Fingerprint,
    old: Fingerprint,
    keep: frozenset[str],
    k: int,
    pending: bool,
    *,
    repeat: bool = False,
    rows_matter: bool = True,
    cuts: frozenset[str] = frozenset(),
) -> str:
    """Project both sides' cells (and row counts) onto ``keep`` and compare them per cell.
    ``cuts`` are the digests of the cut markers (R3-H1, R4-H1)."""
    if old.cells is None:
        return _REFUSE
    if new.cells is None:
        return _NEED if pending else _REFUSE
    a = _project(new.cells, keep)
    b = _project(old.cells, keep)
    ra = _project(new.rows, keep) if rows_matter and new.rows is not None else None
    rb = _project(old.rows, keep) if rows_matter and old.rows is not None else None
    rows_pending = False
    for key in a.keys() | b.keys():
        if key in a and key in b:
            d = abs(a[key] - b[key])
            if 0 < d < k:
                return _REFUSE
            if not rows_matter:
                continue
            if ra is not None and rb is not None and key in ra and key in rb:
                if 0 < abs(ra[key] - rb[key]) < k:
                    return _REFUSE  # R2-H1: the same customers, a few rows more or less
            elif d == 0 and not repeat:
                if pending and new.rows is None and rb is not None:
                    rows_pending = True  # decide at release, once the rows are counted
                else:
                    return _REFUSE  # row drift cannot be ruled out (fail closed)
            continue
        present, absent = (a[key], old) if key in a else (b[key], new)
        if _provably_empty(key, absent):
            continue
        if _cut(absent, cuts):
            return _REFUSE  # R3-H1 / R4-H1: a HAVING, QUALIFY or LIMIT on that side cut it
        if present <= 2 * k - 2:
            return _REFUSE
    return _NEED if rows_pending else _OK


def _verdict(
    new: Fingerprint,
    old: Fingerprint,
    k: int,
    pending: bool,
    mark: str = "",
    cuts: frozenset[str] = frozenset(),
) -> str:
    """Compare two fingerprints of one user and scope: OK, REFUSE, or NEED (counts of ``new``
    are not known yet; decide at release). ``mark`` is the digest of the customers-only
    marker: row counts are only compared when neither side is customers-only. ``cuts`` are
    the digests of the cut markers (user HAVING / QUALIFY, R3-H1; top-N LIMIT, R4-H1)."""
    rows_matter = not mark or (mark not in new.predicates and mark not in old.predicates)
    dn = frozenset(d.dim for d in new.dims)
    do = frozenset(d.dim for d in old.dims)
    if new.predicates == old.predicates and set(new.dims) == set(old.dims):
        # a repeat (R2-M1 interim default): refused only when a count moved by 0 < d < k
        if old.cells is None:
            return _OK
        if new.cells is None:
            return _NEED if pending else _OK
        return _compare(new, old, dn, k, pending, repeat=True, rows_matter=rows_matter, cuts=cuts)
    if not (dn <= do or do <= dn):
        cn, co = _cols(new), _cols(old)
        if ANY_COLUMN in cn or ANY_COLUMN in co or cn <= co or co <= cn:
            return _REFUSE  # the same partition, spelled differently, or unresolved
        if ANY_COLUMN in new.filters or ANY_COLUMN in old.filters:
            return _REFUSE  # R2-H3: an unresolved filter may stand in for a dimension
        if new.filters & co or old.filters & cn:
            return _REFUSE  # R2-H3: one side filters what the other side breaks down by
        shared = dn & do
        extra = [d for d in (*new.dims, *old.dims) if d.dim not in shared]
        if not any(not d.fixed for d in extra):
            return _OK  # both sides only fix different dimensions: different populations
        if _cut(new, cuts) or _cut(old, cuts):
            return _REFUSE  # R3-H1 / R4-H1: cells were dropped before the roll-up: unknown
        # R2-H2: grouped partitions of one population; their totals over the shared
        # dimensions (or the grand total) must not differ by fewer than k
        return _compare(new, old, shared, k, pending, rows_matter=rows_matter, cuts=cuts)
    coarse, fine = (new, old) if dn <= do else (old, new)
    keep = frozenset(d.dim for d in coarse.dims)
    extra = [d for d in fine.dims if d.dim not in keep]
    if extra:
        extra_cols = _cols(fine, extra)
        if ANY_COLUMN in extra_cols or ANY_COLUMN in coarse.filters or coarse.filters & extra_cols:
            return _REFUSE  # the coarse side filters what the fine side breaks down
        if _cut(fine, cuts):
            return _REFUSE  # R3-H1 / R4-H1: the fine side dropped cells before the sum
    return _compare(new, old, keep, k, pending, rows_matter=rows_matter, cuts=cuts)


# --------------------------------------------------------------------------- guard


class DifferencingGuard:
    """Long-lived, thread-safe. Plans are single-use and bound to this instance."""

    def __init__(self, store: FingerprintStore | None, k: int = DEFAULT_K) -> None:
        self.store = store
        self.k = k
        self._issued: OrderedDict[str, DifferencingPlan] = OrderedDict()
        self._lock = threading.Lock()

    def _issue(self, plan: DifferencingPlan) -> DifferencingPlan:
        token = secrets.token_hex(16)
        plan = replace(plan, token=token)
        with self._lock:
            self._issued[token] = plan
            while len(self._issued) > MAX_OPEN_PLANS:
                self._issued.popitem(last=False)
        return plan

    def _redeem(self, plan: object) -> bool:
        if not isinstance(plan, DifferencingPlan) or not plan.token:
            return False
        with self._lock:
            issued = self._issued.get(plan.token)
            if issued is not plan:
                return False
            del self._issued[plan.token]
        return True

    def abandon(self, plan: object) -> None:
        """Drop a plan that will not be released (the query failed or was refused later).
        Records nothing; idempotent; a foreign, unknown or forged plan is a no-op (G-1)."""
        if not isinstance(plan, DifferencingPlan) or not plan.token:
            return
        with self._lock:
            if self._issued.get(plan.token) is plan:
                del self._issued[plan.token]

    def open_plan_count(self) -> int:
        """Plans issued by this guard and neither released nor abandoned yet (G-3)."""
        with self._lock:
            return len(self._issued)

    def prepare(
        self,
        result: SmallCellRewrite | PopulationCheck,
        scope: ProductScope,
        *,
        user_id: str,
        session_id: str,
        population: int | None = None,
    ) -> DifferencingPlan | ScopeRefusal:
        """Pre-execution check. Returns the statement to run or a refusal. Fails closed."""
        if isinstance(result, PopulationCheck):
            scoped = result.then_run
            if not _is_count(population) or population < result.k:  # type: ignore[operator]
                return _invariant()  # the caller must run and pass the population first
        elif isinstance(result, SmallCellRewrite):
            scoped = result.query  # no QI is detected by ``_extract`` below (applies=False)
            population = None
        else:
            return _invariant()
        if scoped.scope_key != scope.scope_key:
            return ScopeRefusal.for_rule(ScopeRule.SCOPE_INVALID)
        store = self.store
        if store is None:
            return _unavailable()
        try:
            shape = _extract(scoped.sql, result.k, store.digest)
            mark = store.digest(_CUSTOMERS_ONLY_MARK)
            limit_mark = store.digest(_LIMIT_CUT_MARK)
            cuts = frozenset({store.digest(_USER_CUT_MARK), limit_mark})
        except Exception:  # noqa: BLE001 - anything unexpected: fail closed
            return _invariant()
        if shape is None:
            return self._issue(DifferencingPlan(query=scoped, applies=False))
        known: dict[CellKey, int] | None = None
        if population is not None and not shape.conditional and not shape.cell_columns:
            known = {tuple(sorted(shape.fixed_cells)): population}
        inject_customers = shape.injectable and population is None
        inject_rows = (
            not shape.customers_only
            and shape.row_injectable
            and (inject_customers or known is not None)
        )
        fp = Fingerprint(
            scope_key=scoped.scope_key,
            dims=shape.dims,
            filters=shape.filters,
            predicates=shape.predicates,
            cells=known,
        )
        pending = inject_customers or inject_rows
        # R4-H1: a removable limit marker is left out of this pre-check (the row count is not
        # known yet); ``release`` decides it and re-checks in the recording transaction
        probe = fp if shape.limit_rows is None else _without(fp, limit_mark)
        try:
            stored = store.candidates(user_id, scoped.scope_key)
            if any(
                _verdict(probe, s.fingerprint, self.k, pending, mark, cuts) == _REFUSE
                for s in stored
            ):
                return _refuse()
        except Exception as err:  # noqa: BLE001 - incl. FingerprintStoreError: fail closed
            return _store_failure(err)
        query = scoped
        if pending:
            try:
                query = _inject(scoped, scope, shape, customers=inject_customers, rows=inject_rows)
            except Exception:  # noqa: BLE001
                return _invariant()
        return self._issue(
            DifferencingPlan(
                query=query,
                applies=True,
                user_id=user_id,
                session_id=session_id,
                fingerprint=fp,
                count_column=CELL_COUNT_COLUMN if inject_customers else None,
                cell_columns=shape.cell_columns if inject_customers else (),
                fixed_cells=shape.fixed_cells,
                row_count_column=ROW_COUNT_COLUMN if inject_rows else None,
                customers_only=shape.customers_only,
                limit_rows=shape.limit_rows,
            )
        )

    def release(
        self, plan: DifferencingPlan, rows: Iterable[Mapping[str, Any]], *, truncated: bool
    ) -> Released | ScopeRefusal:
        """Post-execution check, before rows reach the model. Re-reads the candidates and
        records the fingerprint in one transaction (fails closed). ``truncated`` is the
        client's row-cap flag (required, no default): a capped result is a top-N cut."""
        if not self._redeem(plan):
            return _invariant()  # forged, copied, reused or from another guard
        try:
            materialised = [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return _invariant()
        if not plan.applies:
            return Released(materialised)
        store = self.store
        if plan.fingerprint is None or store is None:
            return _unavailable()
        fp = plan.fingerprint
        try:
            mark = store.digest(_CUSTOMERS_ONLY_MARK)
            limit_mark = store.digest(_LIMIT_CUT_MARK)
            cuts = frozenset({store.digest(_USER_CUT_MARK), limit_mark})
        except Exception:  # noqa: BLE001
            return _unavailable()
        if type(truncated) is not bool:
            return _invariant()  # fail closed on a caller that does not know
        if truncated:
            # R5-H1: the client's row cap cut the result (an unmarked top-N when the query
            # is ordered): mark it whether or not the query has a LIMIT, and never drop it.
            fp = replace(fp, predicates=tuple(sorted(set(fp.predicates) | {limit_mark})))
        elif plan.limit_rows is not None and len(materialised) < plan.limit_rows:
            # R4-H1: fewer rows than the LIMIT and no OFFSET: nothing was cut. The marker is
            # dropped before the check and the record, so the stored fingerprint omits it too.
            fp = _without(fp, limit_mark)
        if plan.injected_columns:
            try:
                cells, row_counts = _cells(plan, materialised, store.digest)
            except ValueError:
                return _invariant()
            except Exception:  # noqa: BLE001
                return _unavailable()
            if plan.count_column is None:
                cells = dict(fp.cells) if fp.cells is not None else None  # the population
            if row_counts is not None and (cells is None or row_counts.keys() != cells.keys()):
                row_counts = None
            if cells is not None and len(cells) > MAX_CELLS:
                cells = row_counts = None
            fp = replace(fp, cells=cells, rows=row_counts)

        def allow(stored: list[StoredFingerprint]) -> bool:
            return all(
                _verdict(fp, s.fingerprint, self.k, False, mark, cuts) != _REFUSE for s in stored
            )

        try:
            with self._lock:
                recorded = store.check_and_record(plan.user_id, plan.session_id, fp, allow)
        except Exception as err:  # noqa: BLE001 - incl. FingerprintStoreError: fail closed
            return _store_failure(err)
        if recorded is None:
            return _refuse()
        for column in plan.injected_columns:
            for row in materialised:
                row.pop(column, None)
        return Released(materialised)


def _without(fp: Fingerprint, marker: str) -> Fingerprint:
    return replace(fp, predicates=tuple(p for p in fp.predicates if p != marker))


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


_DATE_TEXT: Final = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_TIME_TEXT: Final = re.compile(r"^\d{1,2}:\d{2}")


def _when(value: datetime | date) -> str:
    """Canonical instant: aware -> UTC, naive taken as UTC, a date is its midnight."""
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    elif value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return "t:" + value.isoformat()


def _norm(value: object) -> str:
    """Canonical text of a cell value or literal: trimmed, case-folded, numbers canonical, so
    ``'CA '``, ``'ca'`` and ``30``/``'30'``/``30.0`` match; dates and timestamps canonical
    (R2-L2), so ``'2025-01-01'``, ``'2025-01-01 00:00:00'`` and ``date(2025, 1, 1)`` match.
    Temporal text that cannot be parsed is ``?``-prefixed (undecidable: never fixed, never
    proves a cell empty)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "b:true" if value else "b:false"
    if isinstance(value, int | float | Decimal):
        return _num(str(value)) or "s:" + str(value).casefold()
    if isinstance(value, datetime | date):
        return _when(value)
    if isinstance(value, time):
        return "tm:" + value.isoformat()
    if isinstance(value, str):
        raw = value.strip()
        text = raw.casefold()
        if m := _DATE_TEXT.match(raw):
            # BigQuery accepts one-digit months and days ('2025-1-1'); pad before parsing
            padded = f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}" + raw[m.end() :]
            try:
                return _when(datetime.fromisoformat(padded))
            except ValueError:
                return "?" + text
        if _TIME_TEXT.match(raw):
            try:
                return "tm:" + time.fromisoformat(raw).isoformat()
            except ValueError:
                return "?" + text
        return _num(text) or "s:" + text
    return "s:" + str(value).strip().casefold()


def _num(text: str) -> str | None:
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    if d == 0:
        d = Decimal(0)
    return "n:" + format(d.normalize(), "f")


def _value_digest(value: object, digest: Callable[[str], str]) -> str:
    norm = _norm(value)
    if norm.startswith("?"):
        return "?" + digest("val:" + norm)  # undecidable: kept apart, proves nothing
    return digest("val:" + norm)


def _cells(
    plan: DifferencingPlan, rows: list[dict[str, Any]], digest: Callable[[str], str]
) -> tuple[dict[CellKey, int] | None, dict[CellKey, int] | None]:
    """(cell key -> customers, cell key -> rows) from the injected columns; a side is None
    when its column was not injected, both are None when rows cannot be told apart. A
    missing column or a missing / non-integer count is ValueError."""
    columns = (plan.count_column, plan.row_count_column)
    outs: tuple[dict[CellKey, int] | None, ...] = tuple(None if c is None else {} for c in columns)
    for row in rows:
        lowered = {str(k).lower(): v for k, v in row.items()}
        if len(lowered) != len(row):
            return None, None
        key = dict(plan.fixed_cells)
        for dim, name, fixed in plan.cell_columns:
            if name not in lowered:
                raise ValueError("cell column missing")
            value = _value_digest(lowered[name], digest)
            if fixed and value != fixed:
                return None, None
            key[dim] = value
        ck = tuple(sorted(key.items()))
        for column, out in zip(columns, outs, strict=True):
            if column is None or out is None:
                continue
            count = lowered.get(column.lower())
            if not _is_count(count):
                raise ValueError("cell count missing")
            out[ck] = out.get(ck, 0) + count  # type: ignore[operator]
    return outs[0], outs[1]


# --------------------------------------------------------------------------- lineage

_Lin = tuple  # ("const",) | ("dim", "table.col", tag) | ("unknown", frozenset[str])
_CONST: Final = ("const",)
_UNKNOWN_ALL: Final = ("unknown", frozenset({ANY_COLUMN}))
_OPAQUE = (exp.Subquery, exp.Select, exp.AggFunc, exp.Window, exp.Star, exp.SetOperation)


def _lin_cols(lin: _Lin) -> frozenset[str]:
    if lin[0] == "dim":
        return frozenset({lin[1]})
    if lin[0] == "unknown":
        return lin[1]  # type: ignore[no-any-return]
    return frozenset()


def _empty_string(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and node.is_string and node.this == ""


def _unwrap(node: exp.Expression) -> exp.Expression:
    """Strip identity-like wrappers (H-2): TRIM, LOWER, UPPER, cast to text, ``|| ''``."""
    for _ in range(_MAX_DEPTH):
        while isinstance(node, exp.Paren | exp.Alias):
            node = node.this
        if isinstance(node, exp.Lower | exp.Upper):
            node = node.this
        elif isinstance(node, exp.Trim) and node.args.get("expression") is None:
            node = node.this
        elif isinstance(node, exp.Cast | exp.TryCast) and node.to.is_type(*exp.DataType.TEXT_TYPES):
            node = node.this
        elif isinstance(node, exp.Concat):
            rest = [a for a in node.expressions if not _empty_string(a)]
            if len(rest) != 1:
                return node
            node = rest[0]
        elif isinstance(node, exp.DPipe):
            parts = [node.this, node.expression]
            rest = [a for a in parts if not _empty_string(a)]
            if len(rest) != 1:
                return node
            node = rest[0]
        else:
            return node
    return node


class _Lineage:
    """Resolve expressions of an analysed statement to their underlying table columns."""

    def __init__(self, analyzer: _Analyzer) -> None:
        self.a = analyzer

    def scope_of(self, node: exp.Expression) -> Any:
        cur: exp.Expression | None = node
        while cur is not None:
            scope = self.a.scope_of_expr.get(id(cur))
            if scope is not None:
                return scope
            cur = cur.parent
        raise ValueError("no scope")

    def reduce(self, node: exp.Expression, depth: int = 0) -> _Lin:
        if depth > _MAX_DEPTH:
            return _UNKNOWN_ALL
        node = _unwrap(node)
        if any(True for _ in node.find_all(*_OPAQUE)):
            return _UNKNOWN_ALL
        cols = list(node.find_all(exp.Column))
        if not cols:
            return _CONST
        if len(cols) > 1:
            out: set[str] = set()
            for c in cols:
                out |= _lin_cols(self.column(c, depth + 1))
            return ("unknown", frozenset(out))
        base = self.column(cols[0], depth + 1)
        if node is cols[0] or base[0] == "const":
            return base
        if base[0] != "dim":
            return ("unknown", _lin_cols(base))
        tpl = node.copy()
        for c in list(tpl.find_all(exp.Column)):
            c.replace(exp.Placeholder())
        shape = tpl.sql(dialect="bigquery", normalize=True, comments=False)
        return ("dim", base[1], shape + "|" + base[2])

    def column(self, col: exp.Column, depth: int) -> _Lin:
        if depth > _MAX_DEPTH:
            return _UNKNOWN_ALL
        scope = self.scope_of(col)
        name = col.name.lower()
        if col.table:
            cur = scope
            while cur is not None:
                source = _source_by_alias(cur, col.table)
                if source is not None:
                    return self.source_column(source, name, depth + 1)
                cur = cur.parent
            return _UNKNOWN_ALL
        cur = scope
        while cur is not None:
            hits = [s for s in _selected_sources(cur).values() if self._has(s, name)]
            if len(hits) > 1:
                return _UNKNOWN_ALL
            if hits:
                return self.source_column(hits[0], name, depth + 1)
            select = cur.expression
            if cur is scope and isinstance(select, exp.Select) and self.a._late_clause(col, select):
                named = [p for p in select.expressions if p.alias_or_name.lower() == name]
                if len(named) == 1:
                    return self.reduce(named[0], depth + 1)
                if named:
                    return _UNKNOWN_ALL
            cur = cur.parent
        return _UNKNOWN_ALL

    def _has(self, source: object, name: str) -> bool:
        try:
            return self.a._column_of(source, name) is not None
        except Exception:  # noqa: BLE001
            return True  # unknown source: treat as a hit (then ambiguous or unresolved)

    def source_column(self, source: object, name: str, depth: int) -> _Lin:
        if depth > _MAX_DEPTH:
            return _UNKNOWN_ALL
        if isinstance(source, exp.Table):
            return ("dim", f"{source.name.lower()}.{name}", "")
        expr = getattr(source, "expression", None)
        parent = getattr(expr, "parent", None)
        if isinstance(parent, exp.CTE) and parent.alias in TABLE_FOR_CODE_CTE:
            return ("dim", f"{TABLE_FOR_CODE_CTE[parent.alias]}.{name}", "")
        if isinstance(expr, exp.Select):
            named = [p for p in expr.expressions if p.alias_or_name.lower() == name]
            return self.reduce(named[0], depth + 1) if len(named) == 1 else _UNKNOWN_ALL
        if isinstance(expr, exp.SetOperation):
            branches = list(_branch_selects(expr))
            names = [p.alias_or_name.lower() for p in branches[0].expressions]
            if names.count(name) != 1:
                return _UNKNOWN_ALL
            pos = names.index(name)
            lins = {self.reduce(b.expressions[pos], depth + 1) for b in branches}
            if len(lins) == 1:
                return lins.pop()
            out: set[str] = set()
            for lin in lins:
                out |= _lin_cols(lin)
            return ("unknown", frozenset(out))
        return _UNKNOWN_ALL


# --------------------------------------------------------------------------- extraction


@dataclass(frozen=True, slots=True)
class _Shape:
    """Digested shape of one statement (in memory only)."""

    dims: tuple[Dim, ...]
    filters: frozenset[str]
    predicates: tuple[str, ...]
    injectable: bool
    conditional: bool
    key: str | None  # user key SQL for the injected count
    cell_columns: tuple[tuple[str, str, str], ...]  # (dim, projection name, fixed value)
    fixed_cells: tuple[tuple[str, str], ...]  # (dim, value) of fixed, ungrouped dims
    customers_only: bool = False  # every measure is COUNT(DISTINCT <user key>)
    row_injectable: bool = False  # ``COUNT(*) AS _cell_rows`` can be added to the cell
    limit_rows: int | None = None  # R4-H1: removable top-N limit (see ``_LIMIT_CUT_MARK``)


def _in_code_cte(node: exp.Expression) -> bool:
    cur: exp.Expression | None = node
    while cur is not None:
        if isinstance(cur, exp.CTE) and cur.alias in CODE_CTE_ORDER:
            return True
        cur = cur.parent
    return False


def _aggregating(select: exp.Select) -> bool:
    return (
        select.args.get("group") is not None
        or select.args.get("having") is not None
        or any(_contains_aggregate(p) for p in select.expressions)
    )


def _strip(node: exp.Expression) -> exp.Expression:
    copy = node.copy()
    for col in list(copy.find_all(exp.Column)):
        for arg in ("table", "db", "catalog"):
            col.set(arg, None)
    if isinstance(copy, exp.Paren):
        copy = copy.this
    return copy


def _canon(node: exp.Expression) -> str:
    return _strip(node).sql(dialect="bigquery", normalize=True, comments=False)


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _threshold_of(select: exp.Select, k: int) -> exp.Expression | None:
    """The small-cell ``COUNT(DISTINCT key) >= k`` conjunct (always the rightmost one)."""
    having = select.args.get("having")
    if having is None:
        return None
    cond = having.this
    node = cond.expression if isinstance(cond, exp.And) else cond
    if (
        isinstance(node, exp.GTE)
        and isinstance(node.this, exp.Count)
        and isinstance(node.this.this, exp.Distinct)
        and len(node.this.this.expressions) == 1
        and isinstance(node.this.this.expressions[0], exp.Column)
        and isinstance(node.expression, exp.Literal)
        and node.expression.is_int
        and int(node.expression.this) == k
    ):
        return node
    return None


def _group_exprs(select: exp.Select) -> list[exp.Expression]:
    group = select.args.get("group")
    if group is None:
        return []
    projections = select.expressions
    by_alias = {p.alias.lower(): p.this for p in projections if isinstance(p, exp.Alias)}
    out = []
    for g in group.expressions:
        if isinstance(g, exp.Literal) and g.is_int and 1 <= int(g.this) <= len(projections):
            p = projections[int(g.this) - 1]
            out.append(p.this if isinstance(p, exp.Alias) else p)
        elif isinstance(g, exp.Column) and not g.table and g.name.lower() in by_alias:
            out.append(by_alias[g.name.lower()])
        else:
            out.append(g)
    return out


def _from_of(select: exp.Select) -> exp.Expression | None:
    return select.args.get("from_") or select.args.get("from")


def _positive_selects(cell: exp.Select, lineage: _Lineage) -> set[int]:
    """The cell and the derived selects every one of its rows passes through (FROM or inner
    join, transitively; set operations and outer-joined sides excluded)."""
    out: set[int] = set()
    queue = [cell]
    while queue:
        sel = queue.pop()
        if id(sel) in out or _in_code_cte(sel):
            continue
        out.add(id(sel))
        joins = sel.args.get("joins") or []
        if any((j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
            continue  # no side of a right/full join is positive here
        aliases = []
        frm = _from_of(sel)
        if frm is not None:
            aliases.append(frm.this.alias_or_name)
        aliases += [j.this.alias_or_name for j in joins if not j.side]
        scope = lineage.scope_of(sel)
        for alias in aliases:
            source = _source_by_alias(scope, alias) if alias else None
            expr = getattr(source, "expression", None)
            if isinstance(expr, exp.Select):
                queue.append(expr)
    return out


def _fixed_form(cond: exp.Expression) -> tuple[exp.Expression, str] | None:
    """``x = 'v'``, ``'v' = x`` or ``x IN ('v')``: (x, literal text)."""
    if isinstance(cond, exp.EQ):
        left, right = cond.this, cond.expression
        if isinstance(right, exp.Literal) and not isinstance(left, exp.Literal):
            return left, right.this
        if isinstance(left, exp.Literal) and not isinstance(right, exp.Literal):
            return right, left.this
    if (
        isinstance(cond, exp.In)
        and not cond.args.get("query")
        and not cond.args.get("unnest")
        and len(cond.expressions) == 1
        and isinstance(cond.expressions[0], exp.Literal)
    ):
        return cond.this, cond.expressions[0].this
    return None


_MEASURE_NODES = (
    exp.AggFunc,
    exp.Distinct,
    exp.Star,
    exp.Literal,
    exp.Paren,
    exp.Column,
    exp.Identifier,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Neg,
    exp.Cast,
    exp.DataType,
)


def _extract(sql: str, k: int, digest: Callable[[str], str]) -> _Shape | None:
    """Digested shape of a scoped, small-cell-rewritten statement; None when no QI."""
    root = sqlglot.parse_one(sql, read="bigquery")
    analyzer = _Analyzer(root)
    analyzer.run()
    if not analyzer.references_qi:
        return None
    lineage = _Lineage(analyzer)
    selects = [s for s in root.find_all(exp.Select) if not _in_code_cte(s)]
    cells = [
        s
        for s in selects
        if _aggregating(s) and id(s) in analyzer.infos and not analyzer.infos[id(s)].row_grain
    ]
    if len(cells) != 1:
        raise ValueError("expected exactly one cell")  # small-cell guarantees one
    cell = cells[0]
    threshold = _threshold_of(cell, k)
    if cell.args.get("group") is not None and threshold is None:
        raise ValueError("small-cell threshold missing")

    def col_digest(key: str) -> str:
        return ANY_COLUMN if key == ANY_COLUMN else digest("col:" + key)

    def dim_of(lin: _Lin, node: exp.Expression) -> Dim:
        if lin[0] == "dim":
            return Dim(digest("dim:" + lin[1] + "|" + lin[2]), frozenset({col_digest(lin[1])}))
        cols = _lin_cols(lin) or frozenset({ANY_COLUMN})
        return Dim(digest("dim:u:" + _canon(node)), frozenset(col_digest(c) for c in cols))

    def cols_of(node: exp.Expression) -> set[str]:
        if any(True for _ in node.find_all(exp.AggFunc, exp.Window)):
            return {ANY_COLUMN}
        out: set[str] = set()
        for c in node.find_all(exp.Column):
            out |= {col_digest(x) for x in _lin_cols(lineage.column(c, 0))}
        return out

    positive = _positive_selects(cell, lineage)
    filters: set[str] = set()
    preds: set[str] = set()
    fixed: dict[str, tuple[Dim, str]] = {}
    conflicted: set[str] = set()
    user_cut = False

    def sweep(cond: exp.Expression, kind: str, may_fix: bool) -> None:
        preds.add(digest(f"pred:{kind}:" + _canon(cond)))
        form = _fixed_form(cond) if may_fix else None
        if form is not None:
            lin = lineage.reduce(form[0])
            norm = _norm(form[1])
            if lin[0] == "dim" and not norm.startswith("?"):
                dim = dim_of(lin, form[0])
                value = digest("val:" + norm)
                if dim.dim in fixed and fixed[dim.dim][1] != value:
                    conflicted.add(dim.dim)
                    filters.update(dim.cols)
                else:
                    fixed[dim.dim] = (dim, value)
                return
        filters.update(cols_of(cond))

    for sel in selects:
        pos = id(sel) in positive
        joins = sel.args.get("joins") or []
        no_outer = not any((j.side or "").upper() in ("RIGHT", "FULL") for j in joins)
        where = sel.args.get("where")
        for c in _conjuncts(where.this if where is not None else None):
            sweep(c, "where", pos and no_outer)
        for join in joins:
            side = (join.side or "").lower()
            for c in _conjuncts(join.args.get("on")):
                if (
                    not side
                    and isinstance(c, exp.EQ)
                    and isinstance(c.this, exp.Column)
                    and isinstance(c.expression, exp.Column)
                ):
                    preds.add(digest("pred:on:" + _canon(c)))
                    continue  # inner equi-join key
                sweep(c, f"on-{side or 'inner'}", pos and no_outer and not side)
            if join.args.get("using"):
                names = ",".join(sorted(_canon(u) for u in join.args["using"]))
                preds.add(digest(f"pred:using-{side or 'inner'}:" + names))
                if side:
                    filters.add(ANY_COLUMN)
        having = sel.args.get("having")
        for c in _conjuncts(having.this if having is not None else None):
            if c is not threshold:
                sweep(c, "having", False)
                user_cut = True
        qualify = sel.args.get("qualify")
        for c in _conjuncts(qualify.this if qualify is not None else None):
            sweep(c, "qualify", False)
            user_cut = True
        if sel.args.get("limit") is not None or sel.args.get("offset") is not None:
            filters.add(ANY_COLUMN)
            preds.add(digest("pred:limit:" + _canon(sel)))
    for dim_id in conflicted:
        fixed.pop(dim_id, None)

    conditional = False
    for proj in cell.expressions:
        for agg in proj.find_all(exp.AggFunc):
            if isinstance(agg.parent, exp.Window) or agg.find_ancestor(exp.Window):
                continue
            plain = all(_plain_measure_node(n, lineage) for n in agg.walk())
            if not plain:
                conditional = True
                preds.add(digest("measure:" + _canon(agg)))
                filters.update(cols_of_measure(agg, lineage, col_digest))

    grouped: dict[str, Dim] = {}
    cell_columns: list[tuple[str, str, str]] = []
    projections = cell.expressions
    names = [p.alias_or_name.lower() for p in projections]
    by_id: dict[int, str] = {}
    by_canon: dict[str, str] = {}
    for p, n in zip(projections, names, strict=True):
        inner = p.this if isinstance(p, exp.Alias) else p
        by_id[id(inner)] = n
        by_canon.setdefault(_canon(inner), n)
    mappable = True
    duplicate = False
    for g in _group_exprs(cell):
        lin = lineage.reduce(g)
        if lin[0] == "const":
            continue
        dim = dim_of(lin, g)
        if dim.dim in grouped:
            duplicate = True
            continue
        fixed_value = fixed[dim.dim][1] if dim.dim in fixed else ""
        grouped[dim.dim] = Dim(dim.dim, dim.cols, fixed_value)
        name = by_id.get(id(g)) or by_canon.get(_canon(g))
        if not name:
            mappable = False
            continue
        cell_columns.append((dim.dim, name, fixed_value))
    dims = dict(grouped)
    fixed_cells = []
    for dim_id, (dim, value) in fixed.items():
        if dim_id not in dims:
            dims[dim_id] = Dim(dim.dim, dim.cols, value)
            fixed_cells.append((dim_id, value))

    customers_only = _customers_only(cell, lineage)
    if customers_only:
        preds.add(digest(_CUSTOMERS_ONLY_MARK))
    if user_cut:
        preds.add(digest(_USER_CUT_MARK))
    limit_cut, limit_rows = _top_n(root, cell)
    if limit_cut:
        preds.add(digest(_LIMIT_CUT_MARK))
    preds.add(digest("stmt:" + root.sql(dialect="bigquery", normalize=True, comments=False)))
    base_ok = (
        cell is root
        and not conditional
        and not duplicate
        and mappable
        and CELL_COUNT_COLUMN not in names
        and ROW_COUNT_COLUMN not in names
        and all(names)
        and len(set(names)) == len(names)
    )
    key = threshold.this.this.expressions[0].sql(dialect="bigquery") if threshold else None
    return _Shape(
        dims=tuple(sorted(dims.values(), key=lambda d: d.dim)),
        filters=frozenset(filters),
        predicates=tuple(sorted(preds)),
        injectable=base_ok and threshold is not None,
        conditional=conditional or duplicate,
        key=key,
        cell_columns=tuple(sorted(cell_columns)),
        fixed_cells=tuple(sorted(fixed_cells)),
        customers_only=customers_only,
        row_injectable=base_ok and (threshold is not None or cell.args.get("group") is None),
        limit_rows=limit_rows,
    )


def _top_n(root: exp.Expression, cell: exp.Select) -> tuple[bool, int | None]:
    """R4-H1: (marked, removable limit). Any ``LIMIT`` / ``OFFSET`` / ``FETCH`` that is not
    strictly inside the cell (row sampling below it) may cut cells: marked. Removable (the
    ``LIMIT n`` value) only for a single literal ``LIMIT`` on the statement itself with no
    ``OFFSET``; anything else (a CTE or wrapper limit, a parameter) stays marked."""
    above: list[exp.Expression] = []
    for node in root.find_all(exp.Limit, exp.Offset, exp.Fetch):
        if _in_code_cte(node):
            continue
        inside = node.parent is not cell and any(a is cell for a in _ancestors(node))
        if not inside:
            above.append(node)
    if not above:
        return False, None
    if len(above) != 1:
        return True, None
    node = above[0]
    lit = node.args.get("expression")
    if (
        isinstance(node, exp.Limit)
        and node.parent is root
        and node.args.get("offset") is None
        and isinstance(lit, exp.Literal)
        and not lit.is_string
        and str(lit.this).isdigit()
    ):
        return True, int(str(lit.this))
    return True, None


def _ancestors(node: exp.Expression) -> list[exp.Expression]:
    out: list[exp.Expression] = []
    cur = node.parent
    while cur is not None:
        out.append(cur)
        cur = cur.parent
    return out


_USER_KEYS: Final = frozenset(
    {"users.id", "order_items.user_id", "orders.user_id", "events.user_id"}
)


def _customers_only(cell: exp.Select, lineage: _Lineage) -> bool:
    """Every measure of the cell is ``COUNT(DISTINCT <user key>)`` (no window): its value
    depends on the customer set only, so row counts add nothing (R2-H1)."""
    for proj in cell.expressions:
        if any(True for _ in proj.find_all(exp.Window)):
            return False
        for agg in proj.find_all(exp.AggFunc):
            if not (
                isinstance(agg, exp.Count)
                and isinstance(agg.this, exp.Distinct)
                and len(agg.this.expressions) == 1
                and isinstance(agg.this.expressions[0], exp.Column)
            ):
                return False
            lin = lineage.column(agg.this.expressions[0], 0)
            if not (lin[0] == "dim" and lin[2] == "" and lin[1] in _USER_KEYS):
                return False
    return True


def _plain_measure_node(node: exp.Expression, lineage: _Lineage) -> bool:
    if not isinstance(node, _MEASURE_NODES):
        return False
    if isinstance(node, exp.Column):
        lin = lineage.column(node, 0)
        return lin[0] == "dim" and lin[2] == ""
    return True


def cols_of_measure(
    agg: exp.Expression, lineage: _Lineage, col_digest: Callable[[str], str]
) -> set[str]:
    out: set[str] = set()
    for c in agg.find_all(exp.Column):
        out |= {col_digest(x) for x in _lin_cols(lineage.column(c, 0))}
    return out


def _inject(
    scoped: ScopedQuery, scope: ProductScope, shape: _Shape, *, customers: bool, rows: bool
) -> ScopedQuery:
    """Add ``COUNT(DISTINCT <user key>) AS _cell_customers`` and/or ``COUNT(*) AS _cell_rows``
    to the top-level cell."""
    root = sqlglot.parse_one(scoped.sql, read="bigquery")
    if not isinstance(root, exp.Select) or not (customers or rows):
        raise ValueError("not injectable")
    added: list[exp.Expression] = []
    if customers:
        if shape.key is None or not shape.injectable:
            raise ValueError("not injectable")
        key = sqlglot.parse_one(shape.key, read="bigquery")
        count = exp.Count(this=exp.Distinct(expressions=[key]))
        added.append(exp.alias_(count, CELL_COUNT_COLUMN))
    if rows:
        if not shape.row_injectable:
            raise ValueError("not injectable")
        added.append(exp.alias_(exp.Count(this=exp.Star()), ROW_COUNT_COLUMN))
    root.set("expressions", [*root.expressions, *added])
    sql = root.sql(dialect="bigquery", comments=False)
    verify_scoped(sql, scope)
    return ScopedQuery(sql=sql, parameters=scoped.parameters, scope_key=scoped.scope_key)
