"""Iteration 5: BigQuery runner, error mapping, memo, metadata cache. Fakes only, no network."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest
from google.api_core import exceptions as gexc

from opsfleet_agent.bq.client import (
    GB,
    JOB_ID_PREFIX,
    BigQueryRunner,
    Prepared,
    QueryResult,
    SessionByteBudget,
    label_hash,
    sanitise_labels,
)
from opsfleet_agent.bq.errors import (
    BqErrorClass,
    BqFailure,
    ErrorCode,
    Stage,
    map_bq_exception,
)
from opsfleet_agent.bq.memo import (
    QueryMemo,
    make_memo_key,
    result_cache_key,
    run_memoised,
    sql_hash,
)
from opsfleet_agent.bq.schema import (
    ALLOWED_TABLES,
    NEGATIVE_TTL_S,
    REFRESH_MAX_STALE_S,
    SchemaUnavailable,
    TableMetadataCache,
    TableNotAllowed,
)
from opsfleet_agent.graph.budget import TurnBudget, TurnKind

PROJECT = "test-project"
SQL = "SELECT status, COUNT(*) AS n FROM `bigquery-public-data.thelook_ecommerce.orders` GROUP BY 1"
SENTINEL = "SENTINEL_VALUE_7f3a"  # synthetic; stands in for a cell value echoed by BigQuery


# --- fakes ------------------------------------------------------------------------------------


class FakeRow(dict):
    def items(self):  # noqa: D401 - mimic bigquery.Row.items()
        return super().items()


class FakeIterator:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.schema = [type("F", (), {"name": k})() for k in (rows[0] if rows else {})]

    def __iter__(self):
        return iter(FakeRow(r) for r in self._rows)

    def to_dataframe(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("to_dataframe must not be used")


@dataclass
class FakeJob:
    total_bytes_processed: int | None = None
    total_bytes_billed: int | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    result_exc: BaseException | None = None
    job_id: str | None = "job_1"
    cancelled: int = 0
    result_kwargs: dict[str, Any] = field(default_factory=dict)
    on_result: Any = None

    def result(self, *, max_results=None, timeout=None, **kwargs):
        self.result_kwargs = {"max_results": max_results, "timeout": timeout, **kwargs}
        if self.on_result:
            self.on_result()
        if self.result_exc:
            raise self.result_exc
        return FakeIterator(self.rows[: max_results or None])

    def cancel(self, **kwargs):
        self.cancelled += 1
        return True


class FakeClient:
    def __init__(
        self,
        *,
        estimate: int | None = 1000,
        billed: int | None = 1000,
        rows: list[dict[str, Any]] | None = None,
        dry_exc: BaseException | None = None,
        exec_exc: BaseException | None = None,
        result_exc: BaseException | None = None,
    ) -> None:
        self.estimate = estimate
        self.billed = billed
        self.rows = rows if rows is not None else [{"status": "Complete", "n": 3}]
        self.dry_exc = dry_exc
        self.exec_exc = exec_exc
        self.result_exc = result_exc
        self.calls: list[dict[str, Any]] = []
        self.jobs: list[FakeJob] = []
        self.cancelled_ids: list[str] = []

    @property
    def executed(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if not c["job_config"].dry_run]

    def query(self, query, job_config=None, **kwargs):
        self.calls.append({"sql": query, "job_config": job_config, **kwargs})
        if job_config.dry_run:
            if self.dry_exc:
                raise self.dry_exc
            return FakeJob(total_bytes_processed=self.estimate)
        if self.exec_exc:
            raise self.exec_exc
        job = FakeJob(
            total_bytes_processed=self.estimate,
            total_bytes_billed=self.billed,
            rows=self.rows,
            result_exc=self.result_exc,
            job_id=kwargs.get("job_id"),
        )
        self.jobs.append(job)
        return job

    def cancel_job(self, job_id, **kwargs):
        self.cancelled_ids.append(job_id)

    def get_table(self, table, **kwargs):  # pragma: no cover - not used by the runner
        raise AssertionError


def runner(client: FakeClient, **kw) -> BigQueryRunner:
    return BigQueryRunner(client, PROJECT, **kw)


# --- caps -------------------------------------------------------------------------------------


def test_cost_cap_rejects_before_execution():
    client = FakeClient(estimate=1 * GB + 1)
    out = runner(client).run(SQL, SessionByteBudget())
    assert isinstance(out, BqFailure)
    assert out.code is ErrorCode.COST_CAP
    assert out.bytes_estimated == GB + 1
    assert len(client.calls) == 1 and client.calls[0]["job_config"].dry_run
    assert client.executed == []


def test_dry_run_config_and_missing_estimate_fails_closed():
    client = FakeClient(estimate=None)
    out = runner(client).run(SQL, SessionByteBudget())
    assert isinstance(out, BqFailure) and out.code is ErrorCode.COST_CAP
    cfg = client.calls[0]["job_config"]
    assert cfg.dry_run is True and cfg.use_query_cache is False
    assert client.calls[0]["project"] == PROJECT
    assert client.calls[0]["job_retry"] is None and client.calls[0]["timeout"] == 30.0
    assert client.executed == []


def test_session_budget():
    session = SessionByteBudget(cap=10 * GB)
    client = FakeClient(estimate=900_000_000, billed=900_000_000)
    r = runner(client)
    for _ in range(11):
        r.run(SQL, session)
    # 11 x 0.9 GB = 9.9 GB fits; the 12th would exceed 10 GB and is refused after the dry run
    assert session.used == 11 * 900_000_000
    out = r.run(SQL, session)
    assert isinstance(out, BqFailure) and out.code is ErrorCode.SESSION_BUDGET
    assert len(client.executed) == 11
    # the per-job cap shrinks to what is left of the session
    assert client.executed[-1]["job_config"].maximum_bytes_billed == GB
    small = FakeClient(estimate=1, billed=1)
    r2 = runner(small)
    r2.run(SQL, session)
    assert small.executed[-1]["job_config"].maximum_bytes_billed == session.remaining + 1


def test_session_budget_exhausted_skips_even_the_dry_run():
    session = SessionByteBudget(cap=100)
    session.charge(100)
    client = FakeClient()
    out = runner(client).run(SQL, session)
    assert isinstance(out, BqFailure) and out.code is ErrorCode.SESSION_BUDGET
    assert client.calls == []


def test_job_config_sets_max_bytes_billed():
    client = FakeClient()
    out = runner(client).run(SQL, SessionByteBudget(), labels={"Session": "S-1", "turn": "7"})
    assert isinstance(out, QueryResult)
    call = client.executed[0]
    cfg = call["job_config"]
    assert cfg.maximum_bytes_billed == GB
    assert int(cfg.job_timeout_ms) == 60_000  # the library stores it as a string
    assert cfg.labels == {"app": "opsfleet-agent", "session": label_hash("S-1"), "turn": "7"}
    assert cfg.dry_run is not True
    assert call["job_retry"] is None and call["project"] == PROJECT
    assert call["job_id"].startswith(JOB_ID_PREFIX) and out.job_id == call["job_id"]
    assert 0 < call["timeout"] <= 30.0
    assert call["retry"] is not None and call["retry"].timeout <= 30.0
    kw = client.jobs[0].result_kwargs
    # one total deadline: create (30 s) + job (60 s) + grace (15 s)
    assert kw["max_results"] == 201 and 0 < kw["timeout"] <= 105.0
    assert kw["job_retry"] is None
    assert kw["retry"] is not None and kw["retry"].timeout <= kw["timeout"]


def test_result_wait_uses_what_is_left_of_one_deadline():
    t = [0.0]
    client = FakeClient()
    original = client.query

    def slow_query(q, job_config=None, **kw):
        if not job_config.dry_run:
            t[0] += 50.0  # job creation took 50 s of the 105 s total
        return original(q, job_config=job_config, **kw)

    client.query = slow_query  # type: ignore[method-assign]
    out = runner(client, clock=lambda: t[0]).run(SQL, SessionByteBudget())
    assert isinstance(out, QueryResult)
    kw = client.jobs[0].result_kwargs
    assert kw["timeout"] == pytest.approx(55.0) and kw["retry"].timeout == pytest.approx(55.0)


def test_forged_prepared_is_refused_and_never_queried():
    client = FakeClient()
    r = runner(client)
    session = SessionByteBudget()
    forged = Prepared(sql=SQL, bytes_estimated=-5)
    out = r.execute(forged, session)
    assert isinstance(out, BqFailure) and out.code is ErrorCode.BQ_RUNTIME
    assert out.stage is Stage.PRECHECK
    assert client.calls == [] and session.used == 0 and session.reserved == 0
    # an equal-looking copy of an issued Prepared is not the issued object
    issued = r.prepare(SQL, session)
    assert isinstance(issued, Prepared)
    copy_ = Prepared(sql=issued.sql, bytes_estimated=issued.bytes_estimated)
    assert isinstance(r.execute(copy_, session), BqFailure)
    # another runner or another session cannot use it
    assert isinstance(runner(client).execute(issued, session), BqFailure)
    assert isinstance(r.execute(issued, SessionByteBudget()), BqFailure)
    assert client.executed == []
    # mutating the issued object does not change what runs: the runner's record is used
    issued2 = r.prepare(SQL, session)
    assert isinstance(issued2, Prepared)
    object.__setattr__(issued2, "sql", "SELECT 'evil'")
    object.__setattr__(issued2, "bytes_estimated", 0)
    out = r.execute(issued2, session)
    assert isinstance(out, QueryResult) and out.bytes_estimated == 1000
    assert [c["sql"] for c in client.executed] == [SQL]
    # single use: a replay is refused
    assert isinstance(r.execute(issued2, session), BqFailure)
    assert len(client.executed) == 1
    # not even a Prepared
    assert isinstance(r.execute("SELECT 1", session), BqFailure)  # type: ignore[arg-type]


def test_turn_sql_counter_is_consumed_and_refuses():
    client = FakeClient()
    budget = TurnBudget(TurnKind.QA)
    r = runner(client)
    session = SessionByteBudget()
    results = [r.run(SQL, session, sql_counter=budget) for _ in range(7)]
    assert all(isinstance(x, QueryResult) for x in results[:6])
    assert isinstance(results[6], BqFailure) and results[6].code is ErrorCode.BUDGET_EXHAUSTED
    assert len(client.executed) == 6


def test_row_cap_truncates_and_reports():
    rows = [{"id": i} for i in range(250)]
    client = FakeClient(rows=rows)
    out = runner(client).run(SQL, SessionByteBudget())
    assert isinstance(out, QueryResult)
    assert out.row_count == 200 and len(out.rows) == 200 and out.truncated is True
    assert out.columns == ["id"]
    assert (
        out.bytes_estimated == 1000
        and out.bytes_billed == 1000
        and out.job_id == client.executed[0]["job_id"]
    )


def test_unknown_billed_charges_estimate():
    client = FakeClient(estimate=5000, billed=None)
    session = SessionByteBudget()
    out = runner(client).run(SQL, session)
    assert isinstance(out, QueryResult) and out.bytes_billed == 5000
    assert session.used == 5000


def test_sanitise_labels():
    labels = sanitise_labels({"Bad Key!": "Va lue", "9x": "dropped", "k": "x" * 100})
    assert labels["bad_key_"] == "va_lue"
    assert "9x" not in labels and len(labels["k"]) == 63


def test_reserved_labels_cannot_be_overridden_and_ids_are_hashed():
    labels = sanitise_labels(
        {"APP": "evil", "app": "evil", "user_id": "U-123", "session_id": "S-9"}
        | {f"k{i}": "v" for i in range(30)}
    )
    assert labels["app"] == "opsfleet-agent"
    assert labels["user_id"] == label_hash("U-123") and len(labels["user_id"]) == 16
    assert labels["session_id"] == label_hash("S-9")
    assert "u-123" not in json.dumps(labels).lower()
    assert len(labels) <= 16


# --- interrupts, budget reservation, cancel race, malformed estimates (T1 review 2) -----------


def test_keyboard_interrupt_in_result_cancels_charges_and_reraises():
    client = FakeClient(estimate=7000, result_exc=KeyboardInterrupt())
    session = SessionByteBudget()
    r = runner(client)
    with pytest.raises(KeyboardInterrupt):
        r.run(SQL, session)
    assert client.jobs[0].cancelled == 1
    assert session.used == 7000 and session.reserved == 0
    assert r.cancel_inflight() is False  # nothing left registered as in flight


def test_query_call_failure_cancels_by_job_id_and_charges_estimate():
    client = FakeClient(estimate=3000, exec_exc=gexc.ServiceUnavailable(SENTINEL))
    session = SessionByteBudget()
    out = runner(client).run(SQL, session)
    assert isinstance(out, BqFailure) and out.code is ErrorCode.BQ_UNAVAILABLE
    _assert_clean(out)
    sent_id = client.executed[0]["job_id"]
    assert sent_id.startswith(JOB_ID_PREFIX) and client.cancelled_ids == [sent_id]
    assert session.used == 3000 and session.reserved == 0


def test_keyboard_interrupt_in_query_call_cancels_by_job_id_and_reraises():
    client = FakeClient(estimate=3000, exec_exc=KeyboardInterrupt())
    session = SessionByteBudget()
    with pytest.raises(KeyboardInterrupt):
        runner(client).run(SQL, session)
    assert client.cancelled_ids == [client.executed[0]["job_id"]]
    assert session.used == 3000 and session.reserved == 0


def test_session_spent_between_prepare_and_execute_never_sends_zero_cap():
    client = FakeClient(estimate=1000)
    session = SessionByteBudget(cap=1000)
    r = runner(client)
    p = r.prepare(SQL, session)
    assert isinstance(p, Prepared)
    session.charge(1000)  # another query used the rest of the budget meanwhile
    out = r.execute(p, session)
    assert isinstance(out, BqFailure) and out.code is ErrorCode.SESSION_BUDGET
    assert client.executed == [] and session.reserved == 0


def test_budget_reservation_never_sends_zero_and_settles():
    b = SessionByteBudget(cap=100)
    assert b.reserve(0, 50) == 50  # an estimate of 0 still gets a positive cap
    assert b.reserved == 50 and b.remaining == 50
    assert b.reserve(60, 1000) is None  # estimate above what is left
    assert b.reserve(-1, 1000) is None
    second = b.reserve(10, 1000)
    assert second == 50 and b.remaining == 0
    assert b.reserve(0, 1000) is None  # nothing left: never a 0 cap
    b.settle(50, 5)
    b.settle(second, 20)
    assert b.used == 25 and b.reserved == 0 and b.remaining == 75


class WorstCaseClient(FakeClient):
    """Every job bills its full maximum_bytes_billed; thread-safe; results overlap in time."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._mu = threading.Lock()

    def query(self, query, job_config=None, **kwargs):
        with self._mu:
            job = super().query(query, job_config=job_config, **kwargs)
        if not job_config.dry_run:
            job.total_bytes_billed = job_config.maximum_bytes_billed
            job.on_result = lambda: time.sleep(0.01)
        return job


def test_concurrent_runs_never_exceed_session_cap():
    cap = 10_000
    client = WorstCaseClient(estimate=1000)
    session = SessionByteBudget(cap=cap)
    r = runner(client, per_query_cap=4000)
    outs: list[Any] = []

    def worker():
        for _ in range(5):
            outs.append(r.run(SQL, session))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    caps = [c["job_config"].maximum_bytes_billed for c in client.executed]
    assert caps and all(c > 0 for c in caps)
    assert sum(caps) <= cap  # worst case: every job bills its whole cap
    assert session.used <= cap and session.reserved == 0
    assert any(isinstance(o, BqFailure) and o.code is ErrorCode.SESSION_BUDGET for o in outs)


def test_cancel_between_query_and_registration_is_not_lost():
    client = FakeClient()
    r = runner(client)
    original = client.query

    def query(q, job_config=None, **kw):
        job = original(q, job_config=job_config, **kw)
        if not job_config.dry_run:
            assert r.cancel_inflight() is False  # job not registered yet: the flag is set
        return job

    client.query = query  # type: ignore[method-assign]
    out = r.run(SQL, SessionByteBudget())
    assert isinstance(out, BqFailure) and out.code is ErrorCode.CANCELLED
    assert client.jobs[0].result_kwargs == {}  # result() never called
    assert client.jobs[0].cancelled == 1
    client.query = original  # type: ignore[method-assign]
    assert isinstance(r.run(SQL, SessionByteBudget()), QueryResult)  # flag consumed


@pytest.mark.parametrize("estimate", ["12", -5, True, 1.5])
def test_malformed_estimate_fails_closed(estimate):
    client = FakeClient(estimate=estimate)  # type: ignore[arg-type]
    session = SessionByteBudget()
    out = runner(client).run(SQL, session)
    assert isinstance(out, BqFailure) and out.code is ErrorCode.BQ_RUNTIME
    assert out.stage is Stage.DRY_RUN and out.envelope()["message"] == out.message
    assert client.executed == [] and session.used == 0 and session.reserved == 0


def test_cancel_inflight_is_reentrant_under_the_runner_lock():
    r = runner(FakeClient())
    done = threading.Event()

    def call():
        with r._lock:  # e.g. a signal handler firing while the runner holds its lock
            r.cancel_inflight()
        done.set()

    t = threading.Thread(target=call)
    t.start()
    t.join(2)
    assert done.is_set()


# --- error mapping ----------------------------------------------------------------------------


def _bad_request(message: str, reason: str = "invalidQuery") -> gexc.BadRequest:
    return gexc.BadRequest(message, errors=[{"reason": reason, "message": message}])


def _assert_clean(failure: BqFailure) -> None:
    blob = json.dumps(failure.envelope()) + repr(failure)
    assert SENTINEL not in blob
    assert "thelook_ecommerce" not in blob  # no SQL either
    assert "Bad int64 value" not in blob and "Syntax error" not in blob


def test_bq_error_is_mapped_not_forwarded():
    exc = _bad_request(f"Bad int64 value: {SENTINEL}")
    client = FakeClient(result_exc=exc)
    out = runner(client).run(SQL, SessionByteBudget())
    assert isinstance(out, BqFailure)
    assert out.code is ErrorCode.BQ_RUNTIME
    assert out.error_class is BqErrorClass.TYPE_MISMATCH
    assert out.stage is Stage.EXECUTE
    assert out.identifier is None
    _assert_clean(out)
    assert client.jobs[0].cancelled == 1  # best-effort cancel after a failure

    dry = runner(FakeClient(dry_exc=_bad_request(f"Syntax error: Unexpected '{SENTINEL}'"))).run(
        SQL, SessionByteBudget()
    )
    assert isinstance(dry, BqFailure) and dry.code is ErrorCode.SQL_SYNTAX
    assert dry.stage is Stage.DRY_RUN
    _assert_clean(dry)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (_bad_request("Syntax error: Expected end of input"), ErrorCode.SQL_SYNTAX),
        (_bad_request("Unrecognized name: statuz; Did you mean status?"), ErrorCode.UNKNOWN_COLUMN),
        (_bad_request("No matching signature for operator = "), ErrorCode.BQ_RUNTIME),
        (_bad_request("something odd"), ErrorCode.BQ_RUNTIME),
        (
            _bad_request("Query exceeded limit for bytes billed", "bytesBilledLimitExceeded"),
            ErrorCode.COST_CAP,
        ),
        (gexc.ServiceUnavailable("down"), ErrorCode.BQ_UNAVAILABLE),
        (gexc.InternalServerError("boom"), ErrorCode.BQ_UNAVAILABLE),
        (gexc.TooManyRequests("slow"), ErrorCode.BQ_UNAVAILABLE),
        (gexc.Forbidden("no", errors=[{"reason": "accessDenied"}]), ErrorCode.BQ_UNAVAILABLE),
        (ConnectionError("reset"), ErrorCode.BQ_UNAVAILABLE),
        (gexc.DeadlineExceeded("slow"), ErrorCode.TIMEOUT),
        (TimeoutError(), ErrorCode.TIMEOUT),
        (_bad_request("Query exceeded the maximum execution time", "timeout"), ErrorCode.TIMEOUT),
    ],
)
def test_error_classes(exc, code):
    sql = "SELECT statuz FROM t"
    assert map_bq_exception(exc, sql=sql, stage=Stage.EXECUTE).code is code


def test_unknown_column_identifier_only_if_in_sql_outside_literals():
    exc = _bad_request("Unrecognized name: statuz at [1:8]")
    ok = map_bq_exception(exc, sql="SELECT statuz FROM t", stage=Stage.DRY_RUN)
    assert ok.identifier == "statuz" and ok.envelope()["identifier"] == "statuz"
    # identifier present only inside a literal or comment -> withheld
    exc2 = _bad_request(f"Unrecognized name: {SENTINEL}")
    for sql in (f"SELECT '{SENTINEL}' FROM t", f"SELECT 1 -- {SENTINEL}\nFROM t"):
        out = map_bq_exception(exc2, sql=sql, stage=Stage.DRY_RUN)
        assert out.code is ErrorCode.UNKNOWN_COLUMN and out.identifier is None
        _assert_clean(out)


def test_failure_does_not_keep_exception():
    out = map_bq_exception(_bad_request(SENTINEL), sql=SQL, stage=Stage.EXECUTE)
    assert set(vars(out)) == {"code", "stage", "error_class", "identifier", "bytes_estimated"}
    assert not any(isinstance(v, BaseException) for v in vars(out).values())


# --- cancel hook (AC-15.5) --------------------------------------------------------------------


def test_cancel_hook_cancels_inflight_job():
    client = FakeClient(result_exc=gexc.BadRequest("Job cancelled by user"))
    r = runner(client)

    def press_ctrl_c():
        assert r.cancel_inflight() is True

    original = client.query

    def query(q, job_config=None, **kw):
        job = original(q, job_config=job_config, **kw)
        if not job_config.dry_run:
            job.on_result = press_ctrl_c
        return job

    client.query = query  # type: ignore[method-assign]
    out = r.run(SQL, SessionByteBudget())
    assert isinstance(out, BqFailure) and out.code is ErrorCode.CANCELLED
    assert client.jobs[0].cancelled >= 1
    # a cancel that lands while a successful job finishes is still honoured
    client.result_exc = None
    out = r.run(SQL, SessionByteBudget())
    assert isinstance(out, BqFailure) and out.code is ErrorCode.CANCELLED
    # the flag is consumed: the next query runs normally
    client.query = original  # type: ignore[method-assign]
    assert isinstance(r.run(SQL, SessionByteBudget()), QueryResult)


def test_cancel_with_nothing_inflight_refuses_next_execute_once():
    client = FakeClient()
    r = runner(client)
    assert r.cancel_inflight() is False
    out = r.run(SQL, SessionByteBudget())
    assert isinstance(out, BqFailure) and out.code is ErrorCode.CANCELLED
    assert client.executed == []
    assert isinstance(r.run(SQL, SessionByteBudget()), QueryResult)


def test_runner_requires_project():
    with pytest.raises(ValueError):
        BigQueryRunner(FakeClient(), "")


# --- memo -------------------------------------------------------------------------------------

D1 = date(2026, 10, 3)
D2 = date(2026, 10, 4)
U1 = "user-a"  # synthetic user ids
U2 = "user-b"


def _memo_run(memo, key, turn, execute, diff_log, refuse=False):
    def differencing(result):
        diff_log.append(result)
        return "REFUSED" if refuse else None

    return run_memoised(
        memo,
        key,
        turn_id=turn,
        execute=execute,
        differencing=differencing,
        is_failure=lambda x: isinstance(x, BqFailure),
    )


def test_repeated_query_reuses_result():
    client = FakeClient()
    r = runner(client)
    session = SessionByteBudget()
    memo: QueryMemo[QueryResult] = QueryMemo()
    key = make_memo_key(SQL, "scope:all", D1, user_id=U1)
    log: list = []
    first = _memo_run(memo, key, "t1", lambda: r.run(SQL, session), log)
    same_turn = _memo_run(memo, key, "t1", lambda: r.run(SQL, session), log)
    next_turn = _memo_run(memo, key, "t2", lambda: r.run(SQL, session), log)
    assert first.hit is False and first.result is not None
    assert same_turn.hit and same_turn.duplicate_in_turn and same_turn.result == first.result
    assert next_turn.hit and not next_turn.duplicate_in_turn and next_turn.result == first.result
    assert same_turn.result is not first.result  # a copy, not the cached object
    assert len(client.executed) == 1 and len(client.calls) == 2  # one dry run, one job
    # whitespace-only differences hash the same
    assert make_memo_key(SQL.replace(" ", "\n  ") + ";", "scope:all", D1, user_id=U1) == key


def test_memo_hit_still_runs_differencing():
    memo: QueryMemo[str] = QueryMemo()
    key = make_memo_key(SQL, "scope:all", D1, user_id=U1)
    log: list = []
    _memo_run(memo, key, "t1", lambda: "rows", log)
    out = _memo_run(memo, key, "t2", lambda: pytest.fail("must not execute"), log, refuse=True)
    assert out.hit is True and out.result is None and out.failure == "REFUSED"
    assert log == ["rows", "rows"]  # differencing ran on the miss and on the hit


def test_refused_result_is_not_memoised():
    memo: QueryMemo[str] = QueryMemo()
    key = make_memo_key(SQL, "scope:all", D1, user_id=U1)
    out = _memo_run(memo, key, "t1", lambda: "rows", [], refuse=True)
    assert out.failure == "REFUSED" and len(memo) == 0
    fail = BqFailure(ErrorCode.TIMEOUT, Stage.EXECUTE)
    out2 = _memo_run(memo, key, "t1", lambda: fail, [])
    assert out2.failure is fail and len(memo) == 0


def test_memo_misses_after_refresh_date_change():
    memo: QueryMemo[str] = QueryMemo()
    calls: list[int] = []

    def execute():
        calls.append(1)
        return f"rows{len(calls)}"

    _memo_run(memo, make_memo_key(SQL, "scope:all", D1, user_id=U1), "t1", execute, [])
    out = _memo_run(memo, make_memo_key(SQL, "scope:all", D2, user_id=U1), "t2", execute, [])
    assert out.hit is False and out.result == "rows2" and len(calls) == 2
    with pytest.raises(TypeError):
        make_memo_key(SQL, "scope:all", datetime(2026, 10, 4, 3, 0, tzinfo=UTC), user_id=U1)  # type: ignore[arg-type]


def test_cache_key_includes_scope():
    a = make_memo_key(SQL, "scope:region=EU", D1, user_id=U1)
    b = make_memo_key(SQL, "scope:region=US", D1, user_id=U1)
    assert a != b and a.sql_hash == b.sql_hash
    memo: QueryMemo[str] = QueryMemo()
    _memo_run(memo, a, "t1", lambda: "eu", [])
    out = _memo_run(memo, b, "t1", lambda: "us", [])
    assert out.hit is False and out.result == "us"
    with pytest.raises(ValueError):
        make_memo_key(SQL, "", D1, user_id=U1)


def test_result_cache_key_includes_scope():
    k1 = result_cache_key(SQL, "scope:region=EU", D1)
    k2 = result_cache_key(SQL, "scope:region=US", D1)
    k3 = result_cache_key(SQL, "scope:region=EU", D2)
    assert len({k1, k2, k3}) == 3
    assert k1 == result_cache_key(SQL + "  ;", "scope:region=EU", D1)
    assert len(k1) == 64 and k1 != sql_hash(SQL)


def test_memo_is_bounded_and_clearable():
    memo: QueryMemo[int] = QueryMemo(max_entries=2)
    keys = [make_memo_key(f"SELECT {i}", "s", D1, user_id=U1) for i in range(3)]
    for i, k in enumerate(keys):
        memo.store(k, i, turn_id="t")
    assert len(memo) == 2 and memo.lookup(keys[0]) is None
    memo.clear()
    assert len(memo) == 0


def test_memo_key_includes_user_and_hits_are_copies():
    assert make_memo_key(SQL, "s", D1, user_id=U1) != make_memo_key(SQL, "s", D1, user_id=U2)
    with pytest.raises(ValueError):
        make_memo_key(SQL, "s", D1, user_id="")
    memo: QueryMemo[dict[str, Any]] = QueryMemo()
    key = make_memo_key(SQL, "s", D1, user_id=U1)
    stored = {"rows": [{"n": 1}]}
    memo.store(key, stored, turn_id="t1")
    stored["rows"].append({"n": 666})  # mutating the original after store does not leak in
    hit = memo.lookup(key)
    assert hit is not None and hit[0] == {"rows": [{"n": 1}]}
    hit[0]["rows"].clear()  # mutating a hit does not change the cache
    again = memo.lookup(key)
    assert again is not None and again[0] == {"rows": [{"n": 1}]}
    assert memo.lookup(make_memo_key(SQL, "s", D1, user_id=U2)) is None


# --- table metadata ---------------------------------------------------------------------------


@dataclass
class FakeField:
    name: str
    field_type: str = "STRING"
    mode: str = "NULLABLE"
    description: str | None = None


@dataclass
class FakeTable:
    schema: list[FakeField]
    num_rows: int
    modified: datetime


class FakeMetaClient:
    def __init__(self, modified: datetime) -> None:
        self.modified = modified
        self.requested: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self.fail: BaseException | None = None

    def query(self, *a, **k):  # pragma: no cover
        raise AssertionError("metadata must not run a query job")

    def get_table(self, table, **kwargs):
        self.requested.append(table)
        self.kwargs.append(kwargs)
        if self.fail:
            raise self.fail
        return FakeTable([FakeField("id", "INTEGER", "REQUIRED")], 10, self.modified)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_schema_cache_allowed_tables_and_refresh_date():
    client = FakeMetaClient(datetime(2026, 10, 3, 23, 30, tzinfo=UTC))
    clock = Clock()
    cache = TableMetadataCache(client, clock=clock)
    info = cache.get("orders")
    assert info.columns[0].name == "id" and info.columns[0].type == "INTEGER"
    assert client.requested == ["bigquery-public-data.thelook_ecommerce.orders"]
    cache.get("orders")
    assert len(client.requested) == 1  # cached
    with pytest.raises(TableNotAllowed):
        cache.get("events")
    assert cache.refresh_date() == date(2026, 10, 3)
    assert len(client.requested) == 1 + len(ALLOWED_TABLES)
    assert cache.refresh_date() == date(2026, 10, 3)
    assert len(client.requested) == 1 + len(ALLOWED_TABLES)  # within the refresh TTL
    # a new daily refresh is seen after the refresh TTL, and the memo key changes
    client.modified = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)
    clock.t += 3601
    assert cache.refresh_date() == date(2026, 10, 4)


def test_schema_fetch_failure_keeps_last_value_and_hides_text():
    client = FakeMetaClient(datetime(2026, 10, 3, 1, 0, tzinfo=UTC))
    clock = Clock()
    cache = TableMetadataCache(client, clock=clock)
    first = cache.refresh_date()
    client.fail = gexc.ServiceUnavailable(SENTINEL)
    clock.t += 3601
    assert cache.refresh_date() == first
    fresh = TableMetadataCache(client, clock=clock)
    with pytest.raises(SchemaUnavailable) as ei:
        fresh.get("users")
    assert SENTINEL not in str(ei.value) and ei.value.__cause__ is None
    assert ei.value.__context__ is None  # the provider exception is not even implicitly chained
    assert ei.value.error_class is BqErrorClass.UNAVAILABLE


def test_table_not_allowed_message_is_fixed():
    cache = TableMetadataCache(FakeMetaClient(datetime(2026, 10, 3, tzinfo=UTC)))
    with pytest.raises(TableNotAllowed) as ei:
        cache.get(f"events_{SENTINEL}")
    assert str(ei.value) == "table not allowed" and SENTINEL not in repr(ei.value)


def test_get_table_has_bounded_retry_and_one_deadline():
    client = FakeMetaClient(datetime(2026, 10, 3, tzinfo=UTC))
    clock = Clock()
    cache = TableMetadataCache(client, clock=clock, api_timeout_s=20.0)
    cache.refresh_date()
    assert len(client.kwargs) == len(ALLOWED_TABLES)
    for kw in client.kwargs:
        assert 0 < kw["timeout"] <= 20.0
        assert kw["retry"] is not None and kw["retry"].timeout <= kw["timeout"]


def test_refresh_shares_one_deadline_across_tables():
    client = FakeMetaClient(datetime(2026, 10, 3, tzinfo=UTC))
    clock = Clock()
    original = client.get_table

    def slow(table, **kw):
        clock.t += 15.0  # each call eats 15 s of the 20 s budget
        return original(table, **kw)

    client.get_table = slow  # type: ignore[method-assign]
    cache = TableMetadataCache(client, clock=clock, api_timeout_s=20.0)
    with pytest.raises(SchemaUnavailable) as ei:
        cache.refresh_date()
    assert ei.value.error_class is BqErrorClass.TIMEOUT
    assert len(client.requested) == 2  # the third table had no time left: not called
    assert client.kwargs[1]["timeout"] == pytest.approx(5.0)


def test_negative_ttl_avoids_refetch_then_retries():
    client = FakeMetaClient(datetime(2026, 10, 3, tzinfo=UTC))
    client.fail = gexc.ServiceUnavailable("down")
    clock = Clock()
    cache = TableMetadataCache(client, clock=clock)
    with pytest.raises(SchemaUnavailable):
        cache.get("orders")
    clock.t += NEGATIVE_TTL_S - 1
    with pytest.raises(SchemaUnavailable):
        cache.get("orders")
    assert len(client.requested) == 1  # no new API call within the negative TTL
    client.fail = None
    clock.t += 2
    assert cache.get("orders").name == "orders"
    assert len(client.requested) == 2


def test_stale_refresh_date_older_than_max_age_fails_closed():
    client = FakeMetaClient(datetime(2026, 10, 3, tzinfo=UTC))
    clock = Clock()
    cache = TableMetadataCache(client, clock=clock)
    first = cache.refresh_date()
    client.fail = gexc.ServiceUnavailable("down")
    clock.t += REFRESH_MAX_STALE_S - 10
    assert cache.refresh_date() == first  # stale but within the max age
    clock.t += NEGATIVE_TTL_S + 20  # now older than the max age
    with pytest.raises(SchemaUnavailable) as ei:
        cache.refresh_date()
    assert ei.value.error_class is BqErrorClass.UNAVAILABLE
