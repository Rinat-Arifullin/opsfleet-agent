"""Cross-query differencing guard (iteration 10; FR-70, AC-08.7, AC-08.15, HLD §5.5, D-40).

All data is synthetic and nothing is executed: each test plays BigQuery by handing ``release``
the rows the instrumented statement would return (group values plus ``_cell_customers`` and,
for a measure that is not customers-only, ``_cell_rows``).
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import sqlglot

from opsfleet_agent.guards import differencing as differencing_mod
from opsfleet_agent.guards.differencing import (
    CAPACITY_HINT,
    CAUSE_CAPACITY,
    CAUSE_STORE,
    CELL_COUNT_COLUMN,
    DIFFERENCING,
    HINT,
    ROW_COUNT_COLUMN,
    UNAVAILABLE_HINT,
    DifferencingGuard,
    DifferencingPlan,
    DifferencingUnavailable,
    Released,
    unavailable_cause,
)
from opsfleet_agent.guards.scope import ProductScope, ScopedQuery, ScopeRefusal, apply_scope
from opsfleet_agent.guards.small_cell import PopulationCheck, SmallCellRewrite, apply_small_cell
from opsfleet_agent.store import fingerprints as fingerprints_mod
from opsfleet_agent.store.db import checkpoint_truncate, open_store
from opsfleet_agent.store.fingerprints import (
    RETENTION_SECONDS,
    Dim,
    Fingerprint,
    FingerprintStore,
    FingerprintStoreError,
)

ACME = ProductScope.for_brands(["Acme"])
ALL = ProductScope.all()
T0 = 1_900_000_000
DAY = 86_400


def t(name: str) -> str:
    return f"`bigquery-public-data.thelook_ecommerce.{name}`"


U, OI, P = t("users"), t("order_items"), t("products")
BY_STATE = (
    f"SELECT u.state, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
    f"JOIN {OI} AS oi ON oi.user_id = u.id {{where}} GROUP BY u.state"
)
BY_STATE_BRAND = (
    f"SELECT u.state, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
    f"JOIN {OI} AS oi ON oi.user_id = u.id JOIN {P} AS p ON p.id = oi.product_id "
    f"{{where}} GROUP BY u.state"
)
TOTAL = f"SELECT COUNT(DISTINCT u.id) AS n FROM {U} AS u {{where}}"


class Clock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "app.db"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(db_path: Path, clock: Clock) -> FingerprintStore:
    return FingerprintStore(open_store(db_path), clock=clock)


def plan_for(
    guard: DifferencingGuard,
    sql: str,
    *,
    scope: ProductScope = ACME,
    user: str = "user-1",
    session: str = "session-1",
    population: int | None = None,
) -> DifferencingPlan | ScopeRefusal:
    scoped = apply_scope(sql, scope)
    assert isinstance(scoped, ScopedQuery), scoped
    result = apply_small_cell(scoped, scope)
    assert isinstance(result, SmallCellRewrite | PopulationCheck), result
    return guard.prepare(result, scope, user_id=user, session_id=session, population=population)


Cell = int | tuple[int, int]  # distinct customers, or (customers, rows)


def _split(cell: Cell) -> tuple[int, int]:
    return cell if isinstance(cell, tuple) else (cell, cell)


def ask(
    guard: DifferencingGuard,
    sql: str,
    cells: dict[str, Cell] | None = None,
    group_col: str = "state",
    pop_rows: int | None = None,
    *,
    truncated: bool = False,
    **kw: Any,
) -> Released | ScopeRefusal:
    """Prepare, 'execute' (synthetic rows: group value -> distinct customers, and rows when
    the guard injected ``_cell_rows``; rows default to the customer count) and release."""
    plan = plan_for(guard, sql, **kw)
    if isinstance(plan, ScopeRefusal):
        return plan
    if plan.count_column is None:
        rows: list[dict[str, Any]] = [{"n": 1}]
        if plan.row_count_column is not None:
            rows[0][ROW_COUNT_COLUMN] = pop_rows if pop_rows is not None else kw["population"]
    else:
        rows = []
        for value, cell in (cells or {}).items():
            customers, row_count = _split(cell)
            row = {group_col: value, "n": customers, CELL_COUNT_COLUMN: customers}
            if plan.row_count_column is not None:
                row[ROW_COUNT_COLUMN] = row_count
            rows.append(row)
    return guard.release(plan, rows, truncated=truncated)


def release_rows(
    guard: DifferencingGuard,
    sql: str,
    rows: list[dict[str, Any]],
    *,
    truncated: bool = False,
    **kw: Any,
) -> Released | ScopeRefusal:
    """Prepare and release explicit rows (``n`` doubles as the cell count; ``_rows``, default
    ``n``, is the row count when the guard injected ``_cell_rows``)."""
    plan = plan_for(guard, sql, **kw)
    if isinstance(plan, ScopeRefusal):
        return plan
    assert plan.count_column == CELL_COUNT_COLUMN, plan
    out = []
    for r in rows:
        row = {k: v for k, v in r.items() if k != "_rows"}
        row[CELL_COUNT_COLUMN] = r["n"]
        if plan.row_count_column is not None:
            row[ROW_COUNT_COLUMN] = r.get("_rows", r["n"])
        out.append(row)
    return guard.release(plan, out, truncated=truncated)


def rows_in_store(store: FingerprintStore) -> int:
    return store.conn.execute("SELECT COUNT(*) FROM aggregate_fingerprint").fetchone()[0]


def refused(out: object, hint: str = HINT) -> bool:
    return (
        isinstance(out, ScopeRefusal)
        and out.reason_code == DIFFERENCING
        and out.error_code == "SQL_POLICY"
        and out.hint == hint
    )


def where(*preds: str) -> str:
    return ("WHERE " + " AND ".join(preds)) if preds else ""


# --------------------------------------------------------------------------- named tests


def test_differencing_guard_session(store: FingerprintStore) -> None:
    """Same session: an added predicate isolating 0 < d < k customers is refused."""
    guard = DifferencingGuard(store)
    first = ask(guard, BY_STATE.format(where=where("u.age >= 30")), {"SYNTH-A": 120, "SYNTH-B": 80})
    assert isinstance(first, Released)
    assert all(CELL_COUNT_COLUMN not in r for r in first.rows)

    second = ask(
        guard,
        BY_STATE.format(where=where("u.age >= 30", "u.traffic_source = 'Search'")),
        {"SYNTH-A": 117, "SYNTH-B": 80},
    )
    assert refused(second)
    assert not any(ch.isdigit() for ch in second.hint)  # never reveals the count


@pytest.mark.parametrize("failure", ["none", "closed", "dropped_table", "write_fails"])
def test_differencing_guard_across_sessions(db_path: Path, clock: Clock, failure: str) -> None:
    """The pair split across two sessions (two connections) is still refused; any store
    failure refuses too (SEC-12)."""
    s1 = FingerprintStore(open_store(db_path), clock=clock)
    first = ask(
        DifferencingGuard(s1),
        BY_STATE.format(where=where("u.age >= 30")),
        {"SYNTH-A": 120},
        session="session-1",
    )
    assert isinstance(first, Released)

    clock.now += 3 * DAY
    conn = open_store(db_path)
    s2 = FingerprintStore(conn, clock=clock)
    if failure == "closed":
        conn.close()
    elif failure == "dropped_table":
        conn.execute("DROP TABLE aggregate_fingerprint")
    elif failure == "write_fails":
        conn.execute("PRAGMA query_only = ON")

    sql = BY_STATE.format(where=where("u.age >= 30", "u.gender = 'F'"))
    if failure == "none":
        assert refused(ask(DifferencingGuard(s2), sql, {"SYNTH-A": 118}, session="session-2"))
    else:
        # even a large, harmless difference is refused when the store cannot be used
        out = ask(DifferencingGuard(s2), sql, {"SYNTH-A": 60}, session="session-2")
        assert refused(out, UNAVAILABLE_HINT)


def test_fingerprint_retention_30_days(store: FingerprintStore, clock: Clock) -> None:
    guard = DifferencingGuard(store)
    base = BY_STATE.format(where=where("u.age >= 30"))
    near = BY_STATE.format(where=where("u.age >= 30", "u.gender = 'F'"))
    assert isinstance(ask(guard, base, {"SYNTH-A": 120}), Released)

    clock.now = T0 + 29 * DAY
    assert len(store.candidates("user-1", ACME.scope_key)) == 1
    assert refused(ask(guard, near, {"SYNTH-A": 118}))

    clock.now = T0 + RETENTION_SECONDS + 1
    assert store.candidates("user-1", ACME.scope_key) == []
    assert store.purge_expired() == 1
    assert store.conn.execute("SELECT COUNT(*) FROM aggregate_fingerprint").fetchone()[0] == 0
    assert isinstance(ask(guard, near, {"SYNTH-A": 118}), Released)


def test_fingerprint_store_has_no_values(store: FingerprintStore, db_path: Path) -> None:
    guard = DifferencingGuard(store)
    markers = ("SYNTH-STATE-QX", "SYNTH-SRC-QY", "98765.4321", "424242")
    sql = (
        f"SELECT u.state, SUM(oi.sale_price) AS revenue FROM {U} AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id "
        "WHERE u.traffic_source = 'SYNTH-SRC-QY' AND oi.sale_price > 98765.4321 "
        "GROUP BY u.state"
    )
    plan = plan_for(guard, sql)
    assert isinstance(plan, DifferencingPlan) and plan.count_column == CELL_COUNT_COLUMN
    assert plan.row_count_column == ROW_COUNT_COLUMN and not plan.customers_only
    assert plan.injected_columns == (CELL_COUNT_COLUMN, ROW_COUNT_COLUMN)
    out = guard.release(
        plan,
        [
            {
                "state": "SYNTH-STATE-QX",
                "revenue": 424242.5,
                CELL_COUNT_COLUMN: 77,
                ROW_COUNT_COLUMN: 91,
            }
        ],
        truncated=False,
    )
    assert isinstance(out, Released)
    assert out.rows == [{"state": "SYNTH-STATE-QX", "revenue": 424242.5}]
    checkpoint_truncate(store.conn)
    raw = b"".join(
        Path(str(db_path) + sfx).read_bytes()
        for sfx in ("", "-wal")
        if Path(str(db_path) + sfx).exists()
    )
    for marker in (*markers, "sale_price", "traffic_source", "state"):
        assert marker.encode() not in raw, marker
    row = store.conn.execute("SELECT predicates, cells FROM aggregate_fingerprint").fetchone()
    assert "SYNTH" not in row[0] and "SYNTH" not in row[1]


# --------------------------------------------------------------------------- red team


def test_complement_all_brands_but_one_is_refused(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(
        ask(guard, BY_STATE_BRAND.format(where=""), {"SYNTH-A": 300}, scope=ALL), Released
    )
    out = ask(
        guard,
        BY_STATE_BRAND.format(where=where("p.brand != 'SYNTH-BRAND'")),
        {"SYNTH-A": 299},
        scope=ALL,
    )
    assert refused(out)


def test_stepwise_range_narrowing_is_refused(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(
        ask(guard, BY_STATE.format(where=where("u.age >= 30")), {"SYNTH-A": 120}), Released
    )
    assert refused(ask(guard, BY_STATE.format(where=where("u.age >= 31")), {"SYNTH-A": 117}))
    # a step that removes many customers is fine, and the next small step from it is not
    assert isinstance(
        ask(guard, BY_STATE.format(where=where("u.age >= 40")), {"SYNTH-A": 70}), Released
    )
    assert refused(ask(guard, BY_STATE.format(where=where("u.age >= 41")), {"SYNTH-A": 68}))


def test_operator_change_and_or_widening_are_modified_predicates(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    a = BY_STATE.format(where=where("u.state = 'SYNTH-A'"))
    b = BY_STATE.format(where=where("u.state IN ('SYNTH-A', 'SYNTH-B')"))
    assert isinstance(ask(guard, a, {"SYNTH-A": 50}), Released)
    assert refused(ask(guard, b, {"SYNTH-A": 52, "SYNTH-B": 40}))


def test_population_check_pair_is_refused_before_execution(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(
        ask(guard, TOTAL.format(where=where("u.age >= 30")), population=200), Released
    )
    plan = plan_for(
        guard, TOTAL.format(where=where("u.age >= 30", "u.gender = 'M'")), population=197
    )
    assert refused(plan)  # pre-execution: the population is already known


def test_measure_with_literal_counts_as_predicate(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    tmpl = (
        f"SELECT u.state, COUNTIF(oi.sale_price > {{price}}) AS n FROM {U} AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id WHERE u.age >= 30 GROUP BY u.state"
    )
    assert isinstance(ask(guard, tmpl.format(price=30), {"SYNTH-A": 90}), Released)
    # the cell's customer count is the same for both; the change hides inside the aggregate
    assert refused(plan_for(guard, tmpl.format(price=31)))  # refused before execution


def test_other_user_is_not_compared(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(
        ask(guard, BY_STATE.format(where=where("u.age >= 30")), {"SYNTH-A": 120}), Released
    )
    out = ask(guard, BY_STATE.format(where=where("u.age >= 31")), {"SYNTH-A": 118}, user="user-2")
    assert isinstance(out, Released)


# --------------------------------------------------------------------------- positive controls


def test_repeated_question_passes(store: FingerprintStore) -> None:
    """R2-M1 interim default (owner item): a repeat with identical counts passes; a repeat
    whose counts moved by 0 < d < k (the data changed under it) is refused; d >= k passes."""
    guard = DifferencingGuard(store)
    sql = BY_STATE.format(where=where("u.age >= 30"))
    for _ in range(3):
        assert isinstance(ask(guard, sql, {"SYNTH-A": 120, "SYNTH-B": 3 + 5}), Released)
    assert rows_in_store(store) == 1  # R2-L1: identical fingerprints are stored once
    assert refused(ask(guard, sql, {"SYNTH-A": 121, "SYNTH-B": 8}))
    assert isinstance(ask(guard, sql, {"SYNTH-A": 130, "SYNTH-B": 8}), Released)


def test_refinement_with_large_difference_passes(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(
        ask(guard, BY_STATE.format(where=where("u.age >= 30")), {"SYNTH-A": 120}), Released
    )
    refined = BY_STATE.format(where=where("u.age >= 30", "u.gender = 'F'"))
    assert isinstance(ask(guard, refined, {"SYNTH-A": 60}), Released)
    # a difference of exactly k is allowed; a cell in one result only needs a count > 2k - 2
    narrower = BY_STATE.format(where=where("u.age >= 30", "u.gender = 'F'", "u.country = 'X'"))
    assert isinstance(ask(guard, narrower, {"SYNTH-A": 55, "SYNTH-C": 9}), Released)


def test_two_changed_predicates_are_still_compared(store: FingerprintStore) -> None:
    """Review B-1: the old 'exactly one predicate change' rule is gone."""
    guard = DifferencingGuard(store)
    assert isinstance(
        ask(guard, BY_STATE.format(where=where("u.age >= 30")), {"SYNTH-A": 120}), Released
    )
    two = BY_STATE.format(where=where("u.age >= 30", "u.gender = 'F'", "u.country = 'X'"))
    assert refused(ask(guard, two, {"SYNTH-A": 118}))
    # positive control: a different partition (country vs state) whose grand total differs
    # by at least k passes (round 2, R2-H2: a total 120 vs 118 would be a pair)
    other_group = (
        f"SELECT u.country, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id WHERE u.age >= 31 GROUP BY u.country"
    )
    assert isinstance(ask(guard, other_group, {"SYNTH-C": 100}, group_col="country"), Released)


def test_query_without_qi_is_not_checked_or_recorded(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    plan = plan_for(guard, f"SELECT p.category, COUNT(*) AS n FROM {P} AS p GROUP BY 1")
    assert isinstance(plan, DifferencingPlan) and not plan.applies
    assert isinstance(
        guard.release(plan, [{"category": "SYNTH", "n": 3}], truncated=False), Released
    )
    assert store.conn.execute("SELECT COUNT(*) FROM aggregate_fingerprint").fetchone()[0] == 0


# --------------------------------------------------------------------------- fail closed


def test_unknown_counts_refuse_a_near_pair_only(store: FingerprintStore) -> None:
    """A cell inside a CTE cannot be instrumented: counts are unknown, so a near pair is
    refused (conservative) while an unrelated first question passes."""
    tmpl = (
        f"WITH c AS (SELECT u.state, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id {{where}} GROUP BY u.state) "
        "SELECT state, n FROM c"
    )
    guard = DifferencingGuard(store)
    plan = plan_for(guard, tmpl.format(where=where("u.age >= 30")))
    assert isinstance(plan, DifferencingPlan) and plan.count_column is None
    assert isinstance(
        guard.release(plan, [{"state": "SYNTH-A", "n": 120}], truncated=False), Released
    )
    assert refused(plan_for(guard, tmpl.format(where=where("u.age >= 31"))))


def test_missing_or_bad_count_is_refused(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    for bad in (None, "12", -1, True, 1.5):
        plan = plan_for(guard, BY_STATE.format(where=where("u.age >= 30")))
        assert isinstance(plan, DifferencingPlan)
        out = guard.release(
            plan, [{"state": "SYNTH-A", "n": 1, CELL_COUNT_COLUMN: bad}], truncated=False
        )
        assert isinstance(out, ScopeRefusal) and out.reason_code == "rewrite_invariant"
    assert store.conn.execute("SELECT COUNT(*) FROM aggregate_fingerprint").fetchone()[0] == 0


def test_reserved_alias_is_not_instrumented(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    sql = (
        f"SELECT u.state, COUNT(DISTINCT u.id) AS {CELL_COUNT_COLUMN} FROM {U} AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id GROUP BY u.state"
    )
    plan = plan_for(guard, sql)
    assert isinstance(plan, DifferencingPlan) and plan.count_column is None
    out = guard.release(plan, [{"state": "SYNTH-A", CELL_COUNT_COLUMN: 9}], truncated=False)
    assert isinstance(out, Released) and out.rows[0][CELL_COUNT_COLUMN] == 9


def test_no_store_and_bad_inputs_fail_closed(store: FingerprintStore) -> None:
    scoped = apply_scope(BY_STATE.format(where=""), ACME)
    assert isinstance(scoped, ScopedQuery)
    result = apply_small_cell(scoped, ACME)
    assert refused(
        DifferencingGuard(None).prepare(result, ACME, user_id="u", session_id="s"),
        UNAVAILABLE_HINT,
    )
    other = ProductScope.for_brands(["Other"])
    out = DifferencingGuard(store).prepare(result, other, user_id="u", session_id="s")
    assert isinstance(out, ScopeRefusal) and out.reason_code == "scope_invalid"
    out = DifferencingGuard(store).prepare(result, ACME, user_id="", session_id="s")
    assert refused(out, UNAVAILABLE_HINT)
    pop = apply_small_cell(apply_scope(TOTAL.format(where=where("u.age >= 30")), ACME), ACME)  # type: ignore[arg-type]
    assert isinstance(pop, PopulationCheck)
    for bad in (None, 2, "50"):
        out = DifferencingGuard(store).prepare(
            pop, ACME, user_id="u", session_id="s", population=bad
        )  # type: ignore[arg-type]
        assert isinstance(out, ScopeRefusal) and out.reason_code == "rewrite_invariant"


def test_store_rejects_bad_input_and_closed_connection(db_path: Path) -> None:
    conn = open_store(db_path)
    store = FingerprintStore(conn)
    with pytest.raises(FingerprintStoreError):
        store.candidates("", "all")
    conn.close()
    for call in (
        lambda: store.candidates("u", "all"),
        lambda: store.purge_expired(),
        lambda: store.delete_user("u"),
    ):
        with pytest.raises(FingerprintStoreError):
            call()
    with pytest.raises(FingerprintStoreError):
        FingerprintStore(conn)
    bare = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(FingerprintStoreError):
        FingerprintStore(bare)  # no meta table: not the app DB


def test_delete_user_removes_only_that_user(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    ask(guard, BY_STATE.format(where=where("u.age >= 30")), {"SYNTH-A": 120})
    ask(guard, BY_STATE.format(where=where("u.age >= 30")), {"SYNTH-A": 120}, user="user-2")
    assert store.delete_user("user-1") == 1
    users = store.conn.execute("SELECT DISTINCT user_id FROM aggregate_fingerprint").fetchall()
    assert users == [("user-2",)]


# --------------------------------------------------------------------------- review PoCs (iter 10)

BASE = BY_STATE.format(where=where("u.age >= 30"))
CITY = "u.city = 'SYNTH-CITY'"
TARGET = BY_STATE.format(where=where("u.age >= 30", CITY))
BY_STATE_COUNTRY = (
    f"SELECT u.state, u.country, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
    f"JOIN {OI} AS oi ON oi.user_id = u.id {{where}} GROUP BY u.state, u.country"
)


def _variant(sql: str, projection: str, group: str = "1") -> str:
    return sql.replace("SELECT u.state,", f"SELECT {projection},").replace(
        "GROUP BY u.state", f"GROUP BY {group}"
    )


@pytest.mark.parametrize(
    "pad",
    [
        "1 = 1",
        "TRUE",
        "u.age IS NOT NULL",
        "u.id IS NOT NULL",
        "oi.id IS NOT NULL",
        "u.age >= 30",
        "u.age >= 29",
        "u.age < 1000",
    ],
)
def test_poc1_padding_conjunct_does_not_hide_the_pair(store: FingerprintStore, pad: str) -> None:
    """Review B-1: a no-op conjunct used to make the pair 'two changes'."""
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-A": 120}), Released)
    padded = BY_STATE.format(where=where("u.age >= 30", CITY, pad))
    assert refused(ask(guard, padded, {"SYNTH-A": 119}))


def test_poc1_between_split_and_stepwise_split(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    between = BY_STATE.format(where=where("u.age BETWEEN 30 AND 40"))
    assert isinstance(ask(guard, between, {"SYNTH-A": 120}), Released)
    split = BY_STATE.format(where=where("u.age >= 30", "u.age <= 40", CITY))
    assert refused(ask(guard, split, {"SYNTH-A": 119}))

    guard = DifferencingGuard(store)
    store.delete_user("user-1")
    assert isinstance(ask(guard, BASE, {"SYNTH-A": 120}), Released)
    step = BY_STATE.format(where=where("u.age >= 31", "u.age <= 200"))
    assert refused(ask(guard, step, {"SYNTH-A": 119}))


def test_poc1_removal_side_is_refused(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    narrow = BY_STATE.format(where=where("u.age >= 30", CITY, "u.age IS NOT NULL"))
    assert isinstance(ask(guard, narrow, {"SYNTH-A": 8}), Released)
    assert refused(ask(guard, BASE, {"SYNTH-A": 9}))


def test_poc1_complement_with_padding_is_refused(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(
        ask(guard, BY_STATE_BRAND.format(where=""), {"SYNTH-A": 300}, scope=ALL), Released
    )
    padded = BY_STATE_BRAND.format(where=where("p.brand != 'SYNTH-BRAND'", "p.id IS NOT NULL"))
    assert refused(ask(guard, padded, {"SYNTH-A": 299}, scope=ALL))


def test_poc2a_filtered_total_vs_grouped_breakdown(store: FingerprintStore) -> None:
    """Review H-3: ``state = 'X'`` is an implicit dimension, so a total filtered to one state
    is compared with a breakdown by state."""
    guard = DifferencingGuard(store)
    total = TOTAL.format(where=where("u.age >= 30", "u.state = 'SYNTH-X'"))
    assert isinstance(ask(guard, total, population=120), Released)
    assert refused(ask(guard, TARGET, {"SYNTH-X": 119}))


def test_poc2b_extra_dependent_group_key(store: FingerprintStore) -> None:
    """Review H-4 / H-1: a finer breakdown is summed back to the coarse cells."""
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    fine = BY_STATE_COUNTRY.format(where=where("u.age >= 30", CITY))
    rows = [{"state": "SYNTH-X", "country": "SYNTH-C", "n": 119}]
    assert refused(release_rows(guard, fine, rows))


@pytest.mark.parametrize(
    ("projection", "group", "column"),
    [
        ("p.category", "u.state, p.category", "category"),
        ("EXTRACT(YEAR FROM oi.created_at) AS y", "u.state, y", "y"),
    ],
)
def test_poc2c_extra_non_qi_group_key(
    store: FingerprintStore, projection: str, group: str, column: str
) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    fine = (
        f"SELECT u.state, {projection}, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id JOIN {P} AS p ON p.id = oi.product_id "
        f"WHERE u.age >= 30 AND {CITY} GROUP BY {group}"
    )
    rows = [{"state": "SYNTH-X", column: "SYNTH-V", "n": 119}]
    assert refused(release_rows(guard, fine, rows))


SPELLINGS = {
    "ordinal": ("u.state", "1", "state"),
    "alias": ("u.state AS s", "s", "s"),
    "trim": ("TRIM(u.state) AS state", "1", "state"),
    "concat": ("CONCAT(u.state, '') AS state", "1", "state"),
    "lower": ("LOWER(u.state) AS state", "1", "state"),
    "coalesce": ("COALESCE(u.state, '') AS state", "1", "state"),
}


@pytest.mark.parametrize("name", sorted(SPELLINGS))
def test_poc2d_e_group_key_spellings_are_one_partition(store: FingerprintStore, name: str) -> None:
    """Review H-1/H-2: another spelling of the same group key is still compared."""
    projection, group, column = SPELLINGS[name]
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    target = _variant(TARGET, projection, group)
    assert refused(ask(guard, target, {"SYNTH-X": 119}, group_col=column))


@pytest.mark.parametrize("name", ["ordinal", "alias", "trim", "concat"])
def test_identity_spellings_with_equal_counts_pass(store: FingerprintStore, name: str) -> None:
    projection, group, column = SPELLINGS[name]
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    same = _variant(BASE, projection, group)
    assert isinstance(ask(guard, same, {"synth-x ": 120}, group_col=column), Released)


def test_poc3a_extra_literal_projection(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    tagged = TARGET.replace("SELECT u.state,", "SELECT u.state, 'v' AS tag,")
    rows = [{"state": "SYNTH-X", "tag": "v", "n": 119}]
    assert refused(release_rows(guard, tagged, rows))


def test_poc3b_projection_order_swapped(store: FingerprintStore) -> None:
    tmpl = (
        f"SELECT {{cols}}, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id {{where}} GROUP BY u.state, u.gender"
    )
    guard = DifferencingGuard(store)
    first = tmpl.format(cols="u.state, u.gender", where=where("u.age >= 30"))
    row = {"state": "SYNTH-X", "gender": "SYNTH-G"}
    assert isinstance(release_rows(guard, first, [{**row, "n": 50}]), Released)
    second = tmpl.format(cols="u.gender, u.state", where=where("u.age >= 30", CITY))
    assert refused(release_rows(guard, second, [{**row, "n": 49}]))


@pytest.mark.parametrize(
    "measure", ["COUNT(DISTINCT oi.user_id)", "COUNT(*)", "SUM(oi.sale_price)"]
)
def test_poc3c_other_measure_is_still_compared(store: FingerprintStore, measure: str) -> None:
    """Review H-4: no shared measure is required, the cell customer counts are compared."""
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    other = TARGET.replace("COUNT(DISTINCT u.id) AS n", f"{measure} AS n")
    assert refused(ask(guard, other, {"SYNTH-X": 119}))


def test_poc3d_measure_literal_padding(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    padded = TARGET.replace("COUNT(DISTINCT u.id) AS n", "COUNT(DISTINCT u.id) AS n, MAX(1) AS z")
    assert refused(release_rows(guard, padded, [{"state": "SYNTH-X", "z": 1, "n": 119}]))


def test_poc3e_one_sided_small_cell_is_refused(store: FingerprintStore) -> None:
    """Review M-1 / OD-4: X=7 vanishes from the refined result (suppressed below k)."""
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 7, "SYNTH-Y": 200}), Released)
    assert refused(ask(guard, TARGET, {"SYNTH-Y": 150}))
    # control: the suppressed side may hold up to k - 1, so a count of 2k - 1 is safe
    guard = DifferencingGuard(store)
    store.delete_user("user-1")
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 9, "SYNTH-Y": 200}), Released)
    assert isinstance(ask(guard, TARGET, {"SYNTH-Y": 150}), Released)


def test_poc3f_two_prepares_then_two_releases(store: FingerprintStore) -> None:
    """Review M-3: the second release re-reads the candidates in its own transaction."""
    guard = DifferencingGuard(store)
    first = plan_for(guard, BASE)
    second = plan_for(guard, TARGET)
    assert isinstance(first, DifferencingPlan) and isinstance(second, DifferencingPlan)
    rows = [{"state": "SYNTH-X", "n": 120, CELL_COUNT_COLUMN: 120}]
    assert isinstance(guard.release(first, rows, truncated=False), Released)
    rows = [{"state": "SYNTH-X", "n": 119, CELL_COUNT_COLUMN: 119}]
    assert refused(guard.release(second, rows, truncated=False))
    assert rows_in_store(store) == 1


def test_poc3f_reverse_release_order(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    first = plan_for(guard, BASE)
    second = plan_for(guard, TARGET)
    assert isinstance(first, DifferencingPlan) and isinstance(second, DifferencingPlan)
    rows = [{"state": "SYNTH-X", "n": 119, CELL_COUNT_COLUMN: 119}]
    assert isinstance(guard.release(second, rows, truncated=False), Released)
    rows = [{"state": "SYNTH-X", "n": 120, CELL_COUNT_COLUMN: 120}]
    assert refused(guard.release(first, rows, truncated=False))


def test_poc3g_forged_copied_reused_or_foreign_plans_are_refused(
    store: FingerprintStore,
) -> None:
    """Review L-1: only the exact plan object issued by this guard is accepted, once."""
    guard = DifferencingGuard(store)
    plan = plan_for(guard, TARGET)
    assert isinstance(plan, DifferencingPlan)
    rows = [{"state": "SYNTH-X", "n": 1, CELL_COUNT_COLUMN: 1}]
    forged = DifferencingPlan(query=plan.query, applies=False)
    copied = dataclasses.replace(plan, applies=False)
    other = DifferencingGuard(store)
    for bad in (forged, copied, object()):
        out = guard.release(bad, rows, truncated=False)  # type: ignore[arg-type]
        assert isinstance(out, ScopeRefusal) and out.reason_code == "rewrite_invariant"
    out = other.release(plan, rows, truncated=False)
    assert isinstance(out, ScopeRefusal) and out.reason_code == "rewrite_invariant"
    assert isinstance(
        guard.release(plan, [{**rows[0], "n": 120, CELL_COUNT_COLUMN: 120}], truncated=False),
        Released,
    )
    out = guard.release(plan, rows, truncated=False)  # reuse
    assert isinstance(out, ScopeRefusal) and out.reason_code == "rewrite_invariant"
    assert rows_in_store(store) == 1


def test_poc3h_inclusion_exclusion_is_a_documented_gap(store: FingerprintStore) -> None:
    """Review M-2 (not closed, owner item): four queries, every pairwise difference >= k,
    derive a cell of 3. This test pins the current behaviour so a change is noticed."""
    guard = DifferencingGuard(store)
    a, b = "u.gender = 'SYNTH-G'", "u.traffic_source = 'SYNTH-S'"
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 1000}), Released)
    assert isinstance(
        ask(guard, BY_STATE.format(where=where("u.age >= 30", a)), {"SYNTH-X": 600}), Released
    )
    assert isinstance(
        ask(guard, BY_STATE.format(where=where("u.age >= 30", b)), {"SYNTH-X": 403}), Released
    )
    out = ask(guard, BY_STATE.format(where=where("u.age >= 30", a, b)), {"SYNTH-X": 6})
    assert isinstance(out, Released)


# --------------------------------------------------------------------------- rule details


def test_hidden_filter_on_a_finer_dimension_is_refused(store: FingerprintStore) -> None:
    """The coarse query filters the column the fine query breaks down by: the filter can drop
    whole fine cells, so the sums cannot be compared (refused before execution)."""
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    fine = (
        f"SELECT u.state, u.age, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id GROUP BY u.state, u.age"
    )
    assert refused(plan_for(guard, fine))


def test_limit_makes_the_filter_unresolved(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    limited = BASE + " LIMIT 10"
    assert isinstance(ask(guard, limited, {"SYNTH-X": 120}), Released)
    fine = (
        f"SELECT u.state, u.gender, COUNT(DISTINCT u.id) AS n FROM {U} AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id WHERE u.age >= 30 GROUP BY u.state, u.gender"
    )
    assert refused(plan_for(guard, fine))


def test_non_nested_partitions_pass(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    by_city = BY_STATE.format(where=where("u.age >= 30", CITY))
    by_gender = BY_STATE.format(where=where("u.age >= 30", "u.gender = 'F'"))
    assert isinstance(ask(guard, by_city, {"SYNTH-X": 120}), Released)
    assert isinstance(ask(guard, by_gender, {"SYNTH-X": 119}), Released)


def test_provably_empty_cell_is_not_one_sided(store: FingerprintStore) -> None:
    """A query fixed to state X cannot contain state Y, so Y=6 elsewhere is not a pair."""
    guard = DifferencingGuard(store)
    only_x = BY_STATE.format(where=where("u.age >= 30", "u.state = 'SYNTH-X'"))
    assert isinstance(ask(guard, only_x, {"SYNTH-X": 120}), Released)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120, "SYNTH-Y": 6}), Released)


def test_conditional_measure_blocks_comparable_queries(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    cond = BASE.replace("COUNT(DISTINCT u.id) AS n", "COUNTIF(oi.sale_price > 30) AS n")
    plan = plan_for(guard, cond)
    assert isinstance(plan, DifferencingPlan) and plan.count_column is None
    assert isinstance(
        guard.release(plan, [{"state": "SYNTH-X", "n": 3}], truncated=False), Released
    )
    # counts unknown: a comparable later question is refused (fail closed), a repeat is not
    assert refused(plan_for(guard, TARGET))
    assert isinstance(ask(guard, cond), Released)


def test_check_and_record_writes_nothing_when_refused(store: FingerprintStore) -> None:
    fp = Fingerprint(scope_key="all", dims=(Dim("d", frozenset({"c"})),), filters=frozenset())
    assert store.check_and_record("u", "s", fp, lambda stored: False) is None
    assert rows_in_store(store) == 0
    assert store.check_and_record("u", "s", fp, lambda stored: stored == []) is not None
    assert rows_in_store(store) == 1
    with pytest.raises(FingerprintStoreError):
        store.check_and_record("u", "s", fp, lambda stored: 1 / 0)  # type: ignore[arg-type,return-value]
    assert rows_in_store(store) == 1


def test_old_fingerprint_layout_fails_closed(db_path: Path) -> None:
    conn = open_store(db_path)
    conn.execute(
        "CREATE TABLE aggregate_fingerprint (fingerprint_id TEXT PRIMARY KEY, user_id TEXT, "
        "session_id TEXT, scope_key TEXT, group_key TEXT, predicates TEXT, cells TEXT, "
        "created_at INTEGER)"
    )
    with pytest.raises(FingerprintStoreError):
        FingerprintStore(conn)


# --------------------------------------------------------------------------- round 2 (R2-*, G-*)

J = f"FROM {U} AS u JOIN {OI} AS oi ON oi.user_id = u.id"
BY_AGE_ROWS = f"SELECT u.age, COUNT(*) AS n {J} {{where}} GROUP BY u.age"
SUM_BY_STATE = f"SELECT u.state, SUM(oi.sale_price) AS n {J} {{where}} GROUP BY u.state"
DAY_CUT = "oi.created_at < '2025-01-01'"


def _by(col: str, *preds: str) -> str:
    return f"SELECT u.{col}, COUNT(DISTINCT u.id) AS n {J} {where(*preds)} GROUP BY u.{col}"


def test_r2_h1_row_drift_with_equal_customer_counts_is_refused(store: FingerprintStore) -> None:
    """N1: COUNT(*) by age loses one order item while every customer count is unchanged."""
    guard = DifferencingGuard(store)
    plan = plan_for(guard, BY_AGE_ROWS.format(where=""))
    assert isinstance(plan, DifferencingPlan) and plan.row_count_column == ROW_COUNT_COLUMN
    guard.abandon(plan)
    q1, q2 = BY_AGE_ROWS.format(where=""), BY_AGE_ROWS.format(where=where(DAY_CUT))
    assert isinstance(ask(guard, q1, {"30": (50, 200), "31": (40, 150)}, "age"), Released)
    assert refused(ask(guard, q2, {"30": (50, 199), "31": (40, 150)}, "age"))
    # positive control: the same customers, at least k rows apart, passes
    assert isinstance(ask(guard, q2, {"30": (50, 190), "31": (40, 150)}, "age"), Released)


def test_r2_h1_sum_measure_row_drift_is_refused(store: FingerprintStore) -> None:
    """N1b: SUM by state, minus one day of order items, equal customer counts."""
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, SUM_BY_STATE.format(where=""), {"SYNTH-CA": (100, 300)}), Released)
    out = ask(guard, SUM_BY_STATE.format(where=where(DAY_CUT)), {"SYNTH-CA": (100, 299)})
    assert refused(out)


def test_r2_h1_injected_row_column_is_stripped(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    out = ask(guard, BY_AGE_ROWS.format(where=""), {"30": (50, 200)}, "age")
    assert isinstance(out, Released)
    assert all(ROW_COUNT_COLUMN not in r and CELL_COUNT_COLUMN not in r for r in out.rows)


def test_r2_h1_customers_only_measure_injects_no_row_count(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    plan = plan_for(guard, BASE)
    assert isinstance(plan, DifferencingPlan)
    assert plan.customers_only and plan.row_count_column is None
    assert plan.injected_columns == (CELL_COUNT_COLUMN,)
    assert ROW_COUNT_COLUMN not in plan.query.sql
    guard.abandon(plan)


def test_r2_h2_non_nested_partitions_of_one_population_are_rolled_up(
    store: FingerprintStore,
) -> None:
    """N2: gender vs traffic_source, both fixed to one state; the totals differ by 1."""
    guard = DifferencingGuard(store)
    ca = "u.state = 'SYNTH-CA'"
    q1 = _by("gender", ca)
    q2 = _by("traffic_source", ca, "u.age != 37")
    assert isinstance(ask(guard, q1, {"SYNTH-F": 60, "SYNTH-M": 40}, "gender"), Released)
    out = ask(guard, q2, {"SYNTH-S": 50, "SYNTH-E": 49}, "traffic_source")
    assert refused(out)


def test_r2_h2_non_nested_partitions_without_shared_dim(store: FingerprintStore) -> None:
    """N2b: no shared dimension, so the grand totals are compared (100 vs 99)."""
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, _by("gender"), {"SYNTH-F": 60, "SYNTH-M": 40}, "gender"), Released)
    out = ask(
        guard,
        _by("traffic_source", "u.age != 37"),
        {"SYNTH-S": 50, "SYNTH-E": 49},
        "traffic_source",
    )
    assert refused(out)


def test_r2_h2_positive_control_totals_far_apart_pass(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, _by("gender"), {"SYNTH-F": 60, "SYNTH-M": 40}, "gender"), Released)
    out = ask(
        guard,
        _by("traffic_source", "u.age >= 30"),
        {"SYNTH-S": 35, "SYNTH-E": 25},
        "traffic_source",
    )
    assert isinstance(out, Released)


def test_r2_h3_filter_standing_in_for_a_dimension_is_refused(store: FingerprintStore) -> None:
    """N3: state fixed on one side, LIKE on the state column (plus a country) on the other."""
    guard = DifferencingGuard(store)
    a = f"SELECT COUNT(DISTINCT u.id) AS n {J} WHERE u.state = 'SYNTH-CA'"
    b = (
        f"SELECT COUNT(DISTINCT u.id) AS n {J} WHERE u.state LIKE 'SYNTH-CA' "
        "AND u.country = 'SYNTH-US' AND u.age != 37"
    )
    assert isinstance(ask(guard, a, population=100), Released)
    assert refused(plan_for(guard, b, population=99))
    assert isinstance(plan_for(guard, b, population=99, user="user-2"), DifferencingPlan)


def test_r2_h3_grouped_variant_is_refused(store: FingerprintStore) -> None:
    """N3b: GROUP BY state vs GROUP BY country filtered on the state column."""
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, _by("state"), {"SYNTH-CA": 100}), Released)
    q2 = _by("country", "u.state LIKE 'SYNTH-CA'", "u.age != 37")
    assert refused(plan_for(guard, q2))
    assert isinstance(plan_for(guard, q2, user="user-2"), DifferencingPlan)


def test_r2_h3_unresolved_filter_on_either_side_is_refused(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, _by("gender") + " LIMIT 10", {"SYNTH-F": 60}, "gender"), Released)
    assert refused(plan_for(guard, _by("traffic_source")))
    assert isinstance(plan_for(guard, _by("traffic_source"), user="user-2"), DifferencingPlan)


def test_r2_l1_identical_fingerprints_are_stored_once(
    store: FingerprintStore, clock: Clock
) -> None:
    guard = DifferencingGuard(store)
    for step in range(3):
        clock.now = T0 + step
        assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    assert rows_in_store(store) == 1
    (latest,) = store.conn.execute("SELECT created_at FROM aggregate_fingerprint").fetchone()
    assert latest == T0 + 2
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 130}), Released)
    assert rows_in_store(store) == 2


def test_r2_l1_capacity_is_a_distinct_unavailable_cause(
    store: FingerprintStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 120}), Released)
    assert isinstance(ask(guard, BASE, {"SYNTH-X": 200}), Released)
    monkeypatch.setattr(fingerprints_mod, "MAX_CANDIDATES", 1)
    out = plan_for(guard, TARGET)
    assert refused(out, CAPACITY_HINT)
    assert isinstance(out, DifferencingUnavailable) and unavailable_cause(out) == CAUSE_CAPACITY


def test_g2_store_failure_is_unavailable_with_store_cause(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    store.conn.close()
    out = plan_for(guard, BASE)
    assert refused(out, UNAVAILABLE_HINT)
    assert isinstance(out, DifferencingUnavailable) and unavailable_cause(out) == CAUSE_STORE
    out = DifferencingGuard(None).prepare(
        apply_small_cell(apply_scope(BASE, ACME), ACME),  # type: ignore[arg-type]
        ACME,
        user_id="u",
        session_id="s",
    )
    assert unavailable_cause(out) == CAUSE_STORE
    # a plain differencing refusal is not an unavailable signal
    assert unavailable_cause(ScopeRefusal(DIFFERENCING, HINT, "SQL_POLICY")) is None


def test_g1_abandon_and_g3_open_plan_count(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    other = DifferencingGuard(store)
    assert guard.open_plan_count() == 0
    plan = plan_for(guard, BASE)
    foreign = plan_for(other, BASE)
    assert isinstance(plan, DifferencingPlan) and isinstance(foreign, DifferencingPlan)
    assert guard.open_plan_count() == 1
    guard.abandon(foreign)  # foreign: no-op
    guard.abandon(dataclasses.replace(plan))  # a copy: no-op
    guard.abandon(object())  # unknown: no-op
    assert guard.open_plan_count() == 1 and other.open_plan_count() == 1
    guard.abandon(plan)
    guard.abandon(plan)  # idempotent
    assert guard.open_plan_count() == 0 and rows_in_store(store) == 0
    out = guard.release(
        plan, [{"state": "SYNTH-X", "n": 120, CELL_COUNT_COLUMN: 120}], truncated=False
    )
    assert isinstance(out, ScopeRefusal) and out.reason_code == "rewrite_invariant"
    assert rows_in_store(store) == 0


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("2025-01-01", "2025-01-01 00:00:00"),
        ("2025-01-01", date(2025, 1, 1)),
        (
            datetime(2025, 1, 1, 4, tzinfo=timezone(timedelta(hours=2))),
            datetime(2025, 1, 1, 2, tzinfo=UTC),
        ),
        ("2025-1-1", "2025-01-01T00:00:00"),
    ],
)
def test_r2_l2_temporal_values_are_canonical(a: object, b: object) -> None:
    assert differencing_mod._norm(a) == differencing_mod._norm(b)
    assert not differencing_mod._norm(a).startswith("?")


def test_r2_l2_undecidable_temporal_value_proves_nothing() -> None:
    norm = differencing_mod._norm("2025-13-45")
    assert norm.startswith("?")
    digest = differencing_mod._value_digest("2025-13-45", lambda s: "d:" + s)
    assert digest.startswith("?")
    fp = Fingerprint(
        scope_key="all", dims=(Dim("day", frozenset({"c"}), fixed="d:other"),), filters=frozenset()
    )
    assert not differencing_mod._provably_empty((("day", digest),), fp)
    assert differencing_mod._provably_empty((("day", "d:x"),), fp)


def test_fingerprint_rows_round_trip_and_legacy_cells(store: FingerprintStore) -> None:
    key = (("d", "v"),)
    fp = Fingerprint(
        scope_key="all",
        dims=(Dim("d", frozenset({"c"})),),
        filters=frozenset(),
        cells={key: 9},
        rows={key: 14},
    )
    assert store.record("u", "s", fp)
    (stored,) = store.candidates("u", "all")
    assert stored.fingerprint.cells == {key: 9} and stored.fingerprint.rows == {key: 14}
    store.conn.execute("UPDATE aggregate_fingerprint SET cells = ?", ('[[[["d", "v"]], 9]]',))
    (stored,) = store.candidates("u", "all")
    assert stored.fingerprint.cells == {key: 9} and stored.fingerprint.rows is None
    with pytest.raises(FingerprintStoreError):
        store.record("u", "s", dataclasses.replace(fp, rows={(("d", "w"),): 1}))


# --------------------------------------------------------------------------- round 3 (R3-H1)

HAVING_STATE = (
    f"SELECT u.state, COUNT(DISTINCT u.id) AS n {J} {{where}} GROUP BY u.state "
    "HAVING COUNT(DISTINCT u.id) >= 100"
)


def _having_age(measure: str, cut: str, *preds: str) -> str:
    return f"SELECT u.age, {measure} AS n {J} {where(*preds)} GROUP BY u.age HAVING {cut}"


def test_r3_h1_having_customer_probe_is_refused(store: FingerprintStore) -> None:
    """P1/P2: HAVING >= 100 hides CA once one 37-year-old is filtered out (100 -> 99)."""
    guard = DifferencingGuard(store)
    p1 = ask(guard, HAVING_STATE.format(where=""), {"SYNTH-CA": 100, "SYNTH-TX": 200})
    assert isinstance(p1, Released)
    assert refused(ask(guard, HAVING_STATE.format(where=where("u.age != 37")), {"SYNTH-TX": 200}))


@pytest.mark.parametrize("measure", ["SUM(oi.sale_price)", "COUNT(DISTINCT u.id)"])
@pytest.mark.parametrize(
    ("cut", "preds"),
    [("COUNT(*) > 80", ()), ("COUNT(*) > 79", (DAY_CUT,))],
    ids=["threshold-moved", "extra-filter"],
)
def test_r3_h1_having_row_probe_is_refused(
    store: FingerprintStore, measure: str, cut: str, preds: tuple[str, ...]
) -> None:
    """H1 vs H2/H3: a HAVING on COUNT(*) turns a one-row change into cell 30's absence, for a
    SUM and for a customers-only measure (whose row counts are never compared)."""
    guard = DifferencingGuard(store)
    h1 = _having_age(measure, "COUNT(*) > 79")
    assert isinstance(ask(guard, h1, {"30": (50, 80), "31": (40, 90)}, "age"), Released)
    assert refused(ask(guard, _having_age(measure, cut, *preds), {"31": (40, 90)}, "age"))


def test_r3_h1_having_and_qualify_are_user_cuts(store: FingerprintStore) -> None:
    """The marker is set by a user HAVING or QUALIFY, never by the small-cell threshold.
    (The policy refuses a QUALIFY on the cell today; it is added to a rewritten statement.)"""
    mark = store.digest(differencing_mod._USER_CUT_MARK)

    def predicates(sql: str, qualify: bool = False) -> tuple[str, ...]:
        rewritten = apply_small_cell(apply_scope(sql, ACME), ACME)
        assert isinstance(rewritten, SmallCellRewrite)
        text = rewritten.query.sql
        if qualify:
            tree = sqlglot.parse_one(text, dialect="bigquery")
            tree.set("qualify", sqlglot.exp.Qualify(this=sqlglot.parse_one("RANK() OVER () > 1")))
            text = tree.sql(dialect="bigquery")
        shape = differencing_mod._extract(text, 5, store.digest)
        assert shape is not None
        return shape.predicates

    assert mark not in predicates(BASE)
    assert mark in predicates(HAVING_STATE.format(where=""))
    assert mark in predicates(BASE, qualify=True)


def test_r3_h1_reverse_order_absent_side_stored_first(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    h2 = _having_age("SUM(oi.sale_price)", "COUNT(*) > 80")
    assert isinstance(ask(guard, h2, {"31": (40, 90)}, "age"), Released)
    h1 = _having_age("SUM(oi.sale_price)", "COUNT(*) > 79")
    assert refused(ask(guard, h1, {"30": (50, 80), "31": (40, 90)}, "age"))


def test_r3_h1_having_side_is_kept_out_of_roll_ups(store: FingerprintStore) -> None:
    """Non-nested partitions whose totals are far apart pass without HAVING (R2-H2 control),
    but a HAVING side's total is unknown, and so is a fine HAVING side's nested sum."""
    guard = DifferencingGuard(store)
    gender = _by("gender") + " HAVING COUNT(DISTINCT u.id) >= 10"
    assert isinstance(ask(guard, gender, {"SYNTH-F": 60, "SYNTH-M": 40}, "gender"), Released)
    assert refused(plan_for(guard, _by("traffic_source", "u.age >= 30")))
    # nested: the coarse question first, then a finer one with a HAVING (another user)
    coarse = _by("gender")
    assert isinstance(ask(guard, coarse, {"SYNTH-F": 60}, "gender", user="user-2"), Released)
    fine = (
        f"SELECT u.gender, u.age, COUNT(DISTINCT u.id) AS n {J} GROUP BY u.gender, u.age "
        "HAVING COUNT(*) > 79"
    )
    assert refused(plan_for(guard, fine, user="user-2"))
    no_having = fine.split(" HAVING")[0]
    plan = plan_for(guard, no_having, user="user-2")
    assert isinstance(plan, DifferencingPlan)  # control: decided at release, not refused


def test_r3_h1_controls(store: FingerprintStore) -> None:
    """A HAVING query with no comparable predecessor is released; a cell the HAVING side
    provably cannot hold (it fixes another state) is still exempt; cells on both sides are
    compared as before."""
    guard = DifferencingGuard(store)
    only_x = HAVING_STATE.format(where=where("u.state = 'SYNTH-X'"))
    assert isinstance(ask(guard, only_x, {"SYNTH-X": 120}), Released)
    by_state = BY_STATE.format(where="")
    assert isinstance(ask(guard, by_state, {"SYNTH-X": 120, "SYNTH-Y": 6}), Released)
    q1, q2 = HAVING_STATE.format(where=""), HAVING_STATE.format(where=where("u.age >= 30"))
    cells1, cells2 = {"SYNTH-CA": 150, "SYNTH-TX": 200}, {"SYNTH-CA": 120, "SYNTH-TX": 170}
    assert isinstance(ask(guard, q1, cells1, user="user-2"), Released)
    assert isinstance(ask(guard, q2, cells2, user="user-2"), Released)
    assert isinstance(ask(guard, q2, {"SYNTH-CA": 147, "SYNTH-TX": 200}, user="user-3"), Released)
    assert refused(ask(guard, q1, cells1, user="user-3"))  # both present: 150 vs 147


# --------------------------------------------------------------------------- round 4 (R4-H1)

TOP = f"SELECT u.state, COUNT(DISTINCT u.id) AS n {J} {{where}} GROUP BY u.state"
TOP_1 = TOP + " ORDER BY n DESC, u.state LIMIT 1"
NOT_37 = where("u.age != 37")
CA_TX = {"SYNTH-CA": 100, "SYNTH-TX": 100}


def _limit_marked(store: FingerprintStore, sql: str) -> tuple[bool, int | None]:
    shape = differencing_mod._extract(_rewritten(sql), differencing_mod.DEFAULT_K, store.digest)
    assert shape is not None
    return store.digest(differencing_mod._LIMIT_CUT_MARK) in shape.predicates, shape.limit_rows


def _rewritten(sql: str) -> str:
    rewritten = apply_small_cell(apply_scope(sql, ACME), ACME)
    assert isinstance(rewritten, SmallCellRewrite), rewritten
    return rewritten.query.sql


def _stored_marked(store: FingerprintStore, user: str = "user-1") -> list[bool]:
    mark = store.digest(differencing_mod._LIMIT_CUT_MARK)
    return [mark in s.fingerprint.predicates for s in store.candidates(user, ACME.scope_key)]


def test_r4_h1_top_n_probe_is_refused(store: FingerprintStore) -> None:
    """poc6: the full breakdown, then a filtered top-1 whose missing CA proves a CA customer
    is 37 (ties go to CA); and the same with two top-1 queries."""
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, TOP.format(where=""), CA_TX), Released)
    assert refused(ask(guard, TOP_1.format(where=NOT_37), {"SYNTH-TX": 100}))
    assert isinstance(ask(guard, TOP_1.format(where=""), {"SYNTH-CA": 100}, user="u2"), Released)
    assert refused(ask(guard, TOP_1.format(where=NOT_37), {"SYNTH-TX": 100}, user="u2"))


def test_r4_h1_top_n_probe_stored_first_is_refused(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    assert isinstance(ask(guard, TOP_1.format(where=NOT_37), {"SYNTH-TX": 100}), Released)
    assert _stored_marked(store) == [True]  # one row back from LIMIT 1: the marker stays
    assert refused(ask(guard, TOP.format(where=""), CA_TX))


def test_r4_h1_full_result_under_limit_is_compared_normally(store: FingerprintStore) -> None:
    """A safety ``LIMIT 100`` that returned fewer rows cut nothing: its marker is dropped
    before the release check and is not stored, so a cell it lacks is judged by ``2k - 2``."""
    guard = DifferencingGuard(store)
    safety = TOP + " LIMIT 100"
    assert _limit_marked(store, safety.format(where="")) == (True, 100)
    assert isinstance(ask(guard, TOP.format(where=""), CA_TX), Released)
    assert isinstance(ask(guard, safety.format(where=NOT_37), {"SYNTH-TX": 100}), Released)
    assert _stored_marked(store) == [False, False]
    # the limited side first: stored without the marker, then compared normally
    assert isinstance(
        ask(guard, safety.format(where=NOT_37), {"SYNTH-TX": 100}, user="u2"), Released
    )
    assert _stored_marked(store, "u2") == [False]
    assert isinstance(ask(guard, TOP.format(where=""), CA_TX, user="u2"), Released)
    # control: exactly LIMIT rows back may have cut a cell, so the marker stays
    two = TOP + " ORDER BY n DESC LIMIT 2"
    two_rows = {"SYNTH-TX": 100, "SYNTH-NY": 100}
    assert isinstance(ask(guard, TOP.format(where=""), CA_TX, user="u3"), Released)
    assert refused(ask(guard, two.format(where=NOT_37), two_rows, user="u3"))
    assert isinstance(ask(guard, two.format(where=NOT_37), two_rows, user="u4"), Released)
    assert _stored_marked(store, "u4") == [True]
    # cells on both sides are still compared under a dropped marker
    assert isinstance(ask(guard, safety.format(where=""), {"SYNTH-X": 120}, user="u5"), Released)
    assert refused(ask(guard, safety.format(where=NOT_37), {"SYNTH-X": 119}, user="u5"))


def test_r4_h1_offset_is_always_marked(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    paged = TOP + " ORDER BY n DESC LIMIT 100 OFFSET 0"
    assert _limit_marked(store, paged.format(where="")) == (True, None)
    assert isinstance(ask(guard, paged.format(where=NOT_37), {"SYNTH-TX": 100}), Released)
    assert _stored_marked(store) == [True]  # one row < 100, but OFFSET: never dropped
    assert refused(ask(guard, TOP.format(where=""), CA_TX))
    offset_1 = TOP + " ORDER BY n DESC, u.state LIMIT 1 OFFSET 1"
    assert isinstance(ask(guard, TOP.format(where=""), CA_TX, user="u2"), Released)
    assert refused(ask(guard, offset_1.format(where=NOT_37), {"SYNTH-CA": 99}, user="u2"))


def test_r4_h1_where_the_limit_sits(store: FingerprintStore) -> None:
    """Row sampling inside the cell is not marked; a limit above the cell is, and only a
    literal limit on the statement itself is removable."""
    sample = (
        "SELECT u.state, COUNT(DISTINCT u.id) AS n "
        f"FROM (SELECT id, state FROM {U} LIMIT 1000) AS u "
        f"JOIN {OI} AS oi ON oi.user_id = u.id GROUP BY u.state"
    )
    assert _limit_marked(store, sample) == (False, None)
    assert _limit_marked(store, TOP.format(where="")) == (False, None)
    assert _limit_marked(store, TOP_1.format(where="")) == (True, 1)
    text = _rewritten(TOP.format(where=""))
    wrapped = f"SELECT * FROM ({text}) LIMIT 3"
    cte = f"WITH c AS ({text}), d AS (SELECT * FROM c ORDER BY n DESC LIMIT 1) SELECT * FROM d"
    digest = store.digest
    mark = digest(differencing_mod._LIMIT_CUT_MARK)
    k = differencing_mod.DEFAULT_K
    for sql, removable in ((wrapped, 3), (cte, None)):
        shape = differencing_mod._extract(sql, k, digest)
        assert shape is not None
        assert mark in shape.predicates
        assert shape.limit_rows == removable


# R5-H1: the client's row cap (200) is an unmarked top-N cut unless ``release`` is told.
CAPPED = {f"SYNTH-S{i:03d}": 300 for i in range(200)}  # the 200 rows the client kept
FULL_201 = {**CAPPED, "SYNTH-CA": 100}  # the full breakdown; CA sorts last under n DESC
BY_N = TOP + " ORDER BY n DESC"


def test_r5_h1_truncated_result_is_marked(store: FingerprintStore) -> None:
    """poc7 C1 (``LIMIT 1000``, 200 rows back) and C2 (no ``LIMIT``): the client cut CA, so a
    capped probe without CA is refused, and the marker is stored either way round."""
    guard = DifferencingGuard(store)
    c1 = BY_N + " LIMIT 1000"
    for user, sql in (("c1", c1), ("c2", BY_N)):
        assert isinstance(ask(guard, TOP.format(where=""), FULL_201, user=user), Released)
        assert refused(ask(guard, sql.format(where=NOT_37), CAPPED, user=user, truncated=True))
        # the capped side first: stored with the marker, then the full breakdown is refused
        first = user + "-first"
        assert isinstance(
            ask(guard, sql.format(where=NOT_37), CAPPED, user=first, truncated=True), Released
        )
        assert _stored_marked(store, first) == [True]
        assert refused(ask(guard, TOP.format(where=""), FULL_201, user=first))


def test_r5_h1_untruncated_result_keeps_round4_rules(store: FingerprintStore) -> None:
    """Control: the same rows untruncated are a full result under ``LIMIT 1000`` (the marker
    is dropped, CA's 100 is judged by ``2k - 2``) and no marker without a ``LIMIT``."""
    guard = DifferencingGuard(store)
    c1 = BY_N + " LIMIT 1000"
    for user, sql in (("c1", c1), ("c2", BY_N)):
        assert isinstance(ask(guard, TOP.format(where=""), FULL_201, user=user), Released)
        assert isinstance(ask(guard, sql.format(where=NOT_37), CAPPED, user=user), Released)
        assert _stored_marked(store, user) == [False, False]


def test_r5_h1_truncated_must_be_a_bool(store: FingerprintStore) -> None:
    guard = DifferencingGuard(store)
    for bad in (None, 1, "yes"):
        plan = plan_for(guard, TOP.format(where=""), user=f"u-{bad}")
        assert isinstance(plan, DifferencingPlan)
        out = guard.release(plan, [], truncated=bad)  # type: ignore[arg-type]
        assert isinstance(out, ScopeRefusal) and out.reason_code == "rewrite_invariant"
    plan = plan_for(guard, TOP.format(where=""))
    with pytest.raises(TypeError):
        guard.release(plan, [])  # type: ignore[call-arg]  # no default: callers must say
