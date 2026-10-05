"""The only place a BigQuery job is created (HLD §4.4 step 3-6, §5.3 caps; AC-14.x, AC-15.5).

Every query goes through ``BigQueryRunner``:

1. ``prepare(sql, session)``: a dry run (``dry_run=True``, ``use_query_cache=False``). A missing
   or malformed estimate fails closed. An estimate over the per-query cap is ``COST_CAP``; one
   that would exceed what is left of the session budget is ``SESSION_BUDGET``. Nothing has
   executed yet. On success it returns a ``Prepared`` that this runner has *issued*: the runner
   records it (by identity) together with the exact SQL, the estimate and the session.
2. ``execute(prepared, session, ...)``: accepts only a ``Prepared`` this runner issued, for the
   same session, once. A forged, copied, mutated or replayed ``Prepared`` is refused before any
   client call, and the SQL and estimate that run are the runner's own record, not the
   object's fields. Then it reserves bytes on the session budget atomically (refusing with
   ``SESSION_BUDGET`` if nothing or too little is left), takes one SQL slot from the turn
   budget (``SqlCounter``, satisfied by ``graph.budget.TurnBudget``) and runs the job with
   ``maximum_bytes_billed`` equal to the reservation (always > 0), ``job_timeout_ms``
   bounded, labels set, a client-generated ``job_id``, no job retry, bounded API retries,
   one total deadline across the call, and at most ``row_cap + 1`` rows fetched so truncation
   is detectable. The reservation is settled with the billed bytes (or the estimate when the
   billed bytes are unknown or the job failed), releasing the unused part.
3. ``run(sql, session, ...)`` = ``prepare`` + ``execute``; the normal entry point.

Every ``Exception`` from the client is mapped by ``errors.map_bq_exception``; the raw exception
is dropped here. A ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) cancels the job
best-effort, charges the estimate and is re-raised. ``cancel_inflight()`` is the cancel hook
for Ctrl+C (AC-15.5).

Results are read row by row into plain dicts; ``to_dataframe`` is never called.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from opsfleet_agent.bq.errors import BqFailure, ErrorCode, Stage, failure, map_bq_exception

GB = 10**9  # decimal gigabyte, as BigQuery bills

DEFAULT_PER_QUERY_CAP = 1 * GB
DEFAULT_SESSION_CAP = 10 * GB
DEFAULT_JOB_TIMEOUT_MS = 60_000
DEFAULT_ROW_CAP = 200
DEFAULT_API_TIMEOUT_S = 30.0  # bound on each HTTP call (create job / dry run / cancel)
RESULT_GRACE_S = 15.0  # client-side wait beyond job_timeout_ms before we give up and cancel
JOB_ID_PREFIX = "opsfleet_"


class WarehouseJob(Protocol):
    job_id: str | None
    total_bytes_processed: int | None
    total_bytes_billed: int | None

    def result(self, **kwargs: Any) -> Any: ...

    def cancel(self, **kwargs: Any) -> Any: ...


class WarehouseClient(Protocol):
    """The subset of ``google.cloud.bigquery.Client`` we use; tests pass a fake.

    Rows reach run_sql and the differencing guard only through ``BigQueryRunner``, which MUST set
    ``QueryResult.truncated`` whenever rows are cut by any cap (a cap without the flag would
    reopen R5-H1).
    """

    def query(self, query: str, job_config: Any = ..., **kwargs: Any) -> WarehouseJob: ...

    def get_table(self, table: Any, **kwargs: Any) -> Any: ...

    def cancel_job(self, job_id: str, **kwargs: Any) -> Any: ...


class SqlCounter(Protocol):
    """Turn-level SQL counter. ``graph.budget.TurnBudget`` satisfies this.

    ``consume_sql()`` returns None when a slot was taken, or a refusal object when exhausted.
    """

    def consume_sql(self) -> object | None: ...


def make_bigquery_client(project: str) -> WarehouseClient:
    """Build the real client lazily (keeps unit tests import-light). ``project`` comes from
    ``GOOGLE_CLOUD_PROJECT`` via config; auth is ADC."""
    if not project:
        raise ValueError("a GCP project is required (set GOOGLE_CLOUD_PROJECT)")
    from google.cloud import bigquery

    return bigquery.Client(project=project)


def bounded_retry(timeout_s: float) -> Any:
    """The library's API retry policy with its total time capped at ``timeout_s``.

    The library default retries for up to 600 s; every call we make passes this instead.
    """
    try:
        from google.cloud.bigquery.retry import DEFAULT_RETRY
    except ImportError:  # pragma: no cover - library always present in this project
        return None
    return DEFAULT_RETRY.with_timeout(max(0.0, float(timeout_s)))


def _job_config(**kwargs: Any) -> Any:
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(**kwargs)


def _is_byte_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# --- session budget ---------------------------------------------------------------------------


class SessionByteBudget:
    """Bytes the session may still bill.

    ``reserve`` takes a slice of the budget atomically before a job runs; ``settle`` releases
    the slice and charges what was actually billed. ``remaining`` excludes open reservations,
    so concurrent jobs can never together be allowed to bill more than the cap.
    """

    def __init__(self, cap: int = DEFAULT_SESSION_CAP) -> None:
        if cap <= 0:
            raise ValueError("session cap must be positive")
        self.cap = cap
        self.used = 0
        self.reserved = 0
        self._lock = threading.RLock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.cap - self.used - self.reserved)

    def charge(self, nbytes: int) -> None:
        with self._lock:
            self.used += max(0, int(nbytes))

    def reserve(self, estimate: int, per_query_cap: int) -> int | None:
        """Reserve ``min(per_query_cap, remaining)`` if ``estimate`` fits; else None.

        The reservation is the job's ``maximum_bytes_billed``, so it is never 0 (BigQuery may
        read 0 as "no limit / project default").
        """
        with self._lock:
            remaining = self.cap - self.used - self.reserved
            if remaining <= 0 or estimate < 0 or estimate > remaining:
                return None
            amount = min(per_query_cap, remaining)
            if amount <= 0:
                return None
            self.reserved += amount
            return amount

    def settle(self, reservation: int, billed: int) -> None:
        """Release ``reservation`` and charge ``billed`` (one lock, so no window in between)."""
        with self._lock:
            self.reserved = max(0, self.reserved - reservation)
            self.used += max(0, int(billed))


# --- labels -----------------------------------------------------------------------------------

_LABEL_BAD = re.compile(r"[^a-z0-9_-]")
_MAX_LABELS = 16
RESERVED_LABELS: dict[str, str] = {"app": "opsfleet-agent"}
# values of these keys are identifiers of people/sessions: only a hash goes into job metadata
HASHED_LABEL_KEYS = frozenset({"user", "user_id", "session", "session_id"})


def label_hash(value: object) -> str:
    """sha256 of the raw value, first 16 hex chars (a pseudonym, not a secret)."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def sanitise_labels(labels: Mapping[str, str] | None) -> dict[str, str]:
    """BigQuery label rules: lowercase ``[a-z0-9_-]``, key starts with a letter, <= 63 chars.

    Reserved keys (``RESERVED_LABELS``) cannot be set or overridden by the caller in any case
    and are applied last. Values of ``HASHED_LABEL_KEYS`` are replaced by ``label_hash``.
    """
    out: dict[str, str] = {}
    room = _MAX_LABELS - len(RESERVED_LABELS)
    for k, v in (labels or {}).items():
        key = _LABEL_BAD.sub("_", str(k).lower())[:63]
        if not key or not key[0].isalpha() or key in RESERVED_LABELS:
            continue
        if key not in out and len(out) >= room:
            break
        if key in HASHED_LABEL_KEYS:
            out[key] = label_hash(v)
        else:
            out[key] = _LABEL_BAD.sub("_", str(v).lower())[:63]
    out.update(RESERVED_LABELS)
    return out


# --- results ----------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Prepared:
    """A query that passed the dry run and the caps.

    Only ``BigQueryRunner.prepare`` issues usable ones: ``execute`` looks the object up by
    identity in the issuing runner's registry, so constructing one by hand does nothing.
    The fields are informational; ``execute`` uses the runner's own record.
    """

    sql: str
    bytes_estimated: int


@dataclass(frozen=True)
class _Issued:
    sql: str
    estimate: int
    session: SessionByteBudget


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    bytes_estimated: int
    bytes_billed: int
    job_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class _CancelRequested(Exception):
    """Internal: a cancel arrived between job creation and registering it as in flight."""


def _columns(iterator: Any, rows: list[dict[str, Any]]) -> list[str]:
    schema = getattr(iterator, "schema", None)
    if schema:
        return [getattr(f, "name", str(f)) for f in schema]
    return list(rows[0].keys()) if rows else []


def _read_rows(iterator: Iterable[Any], limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in iterator:  # bounded: at most `limit` rows are kept and we stop there
        if len(rows) >= limit:
            break
        rows.append(dict(row.items()) if hasattr(row, "items") else dict(row))
    return rows


# --- runner -----------------------------------------------------------------------------------


class BigQueryRunner:
    def __init__(
        self,
        client: WarehouseClient,
        project: str,
        *,
        per_query_cap: int = DEFAULT_PER_QUERY_CAP,
        job_timeout_ms: int = DEFAULT_JOB_TIMEOUT_MS,
        row_cap: int = DEFAULT_ROW_CAP,
        api_timeout_s: float = DEFAULT_API_TIMEOUT_S,
        job_config_factory: Callable[..., Any] = _job_config,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not project:
            raise ValueError("a GCP project is required (set GOOGLE_CLOUD_PROJECT)")
        if per_query_cap <= 0 or job_timeout_ms <= 0 or row_cap <= 0 or api_timeout_s <= 0:
            raise ValueError("caps and timeouts must be positive")
        self._client = client
        self.project = project
        self.per_query_cap = per_query_cap
        self.job_timeout_ms = job_timeout_ms
        self.row_cap = row_cap
        self.api_timeout_s = api_timeout_s
        # one deadline per execute: create job + wait for the job + grace
        self.total_timeout_s = api_timeout_s + job_timeout_ms / 1000 + RESULT_GRACE_S
        self._job_config = job_config_factory
        self._clock = clock
        self._retry = bounded_retry(api_timeout_s)
        # RLock: cancel_inflight may run in a signal handler that interrupted this thread
        # while it held the lock; a plain Lock would deadlock there.
        self._lock = threading.RLock()
        self._inflight: WarehouseJob | None = None
        self._cancelled = False
        self._issued: weakref.WeakKeyDictionary[Prepared, _Issued] = weakref.WeakKeyDictionary()

    # -- step 1: dry run + caps --

    def prepare(self, sql: str, session: SessionByteBudget) -> Prepared | BqFailure:
        if session.remaining <= 0:
            return failure(ErrorCode.SESSION_BUDGET, Stage.PRECHECK)
        config = self._job_config(dry_run=True, use_query_cache=False)
        try:
            job = self._client.query(
                sql,
                job_config=config,
                project=self.project,
                retry=self._retry,
                timeout=self.api_timeout_s,
                job_retry=None,
            )
            estimate = job.total_bytes_processed
        except Exception as exc:  # noqa: BLE001 - mapped to a fixed, typed failure
            return map_bq_exception(exc, sql=sql, stage=Stage.DRY_RUN)
        if estimate is None:  # fail closed: no estimate, no execution
            return failure(ErrorCode.COST_CAP, Stage.DRY_RUN)
        if not _is_byte_count(estimate):  # malformed estimate: fixed failure, no echo
            return failure(ErrorCode.BQ_RUNTIME, Stage.DRY_RUN)
        if estimate > self.per_query_cap:
            return failure(ErrorCode.COST_CAP, Stage.DRY_RUN, bytes_estimated=estimate)
        if estimate > session.remaining:
            return failure(ErrorCode.SESSION_BUDGET, Stage.DRY_RUN, bytes_estimated=estimate)
        prepared = Prepared(sql=sql, bytes_estimated=estimate)
        with self._lock:
            self._issued[prepared] = _Issued(sql=sql, estimate=estimate, session=session)
        return prepared

    # -- step 2: execute --

    def execute(
        self,
        prepared: Prepared,
        session: SessionByteBudget,
        sql_counter: SqlCounter | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> QueryResult | BqFailure:
        """Run a ``Prepared`` issued by this runner's ``prepare`` for this ``session``, once.

        Anything else is refused with ``BQ_RUNTIME`` at ``Stage.PRECHECK`` and no client call.
        """
        with self._lock:
            try:
                record = self._issued.pop(prepared, None)
            except TypeError:  # not even hashable: certainly not ours
                record = None
        if record is None or record.session is not session:
            return failure(ErrorCode.BQ_RUNTIME, Stage.PRECHECK)
        sql, est = record.sql, record.estimate
        deadline = self._clock() + self.total_timeout_s

        with self._lock:
            if self._cancelled:
                self._cancelled = False
                return failure(ErrorCode.CANCELLED, Stage.EXECUTE, bytes_estimated=est)
        reservation = session.reserve(est, self.per_query_cap)
        if reservation is None:
            return failure(ErrorCode.SESSION_BUDGET, Stage.PRECHECK, bytes_estimated=est)
        if sql_counter is not None and sql_counter.consume_sql() is not None:
            session.settle(reservation, 0)
            return failure(ErrorCode.BUDGET_EXHAUSTED, Stage.PRECHECK, bytes_estimated=est)

        config = self._job_config(
            maximum_bytes_billed=reservation,
            job_timeout_ms=self.job_timeout_ms,
            labels=sanitise_labels(labels),
            use_query_cache=True,
        )
        job_id = f"{JOB_ID_PREFIX}{uuid.uuid4().hex}"
        job: WarehouseJob | None = None
        try:
            try:
                job = self._client.query(
                    sql,
                    job_config=config,
                    job_id=job_id,
                    project=self.project,
                    retry=self._retry,
                    timeout=min(self.api_timeout_s, self._left(deadline)),
                    job_retry=None,
                )
            except BaseException:
                # the server may have created the job before the call failed: cancel by id
                self._best_effort_cancel_id(job_id)
                raise
            with self._lock:
                self._inflight = job
                cancel_now = self._cancelled  # a cancel that landed before registration
            if cancel_now:
                raise _CancelRequested
            left = self._left(deadline)
            if left <= 0:
                raise TimeoutError
            iterator = job.result(
                max_results=self.row_cap + 1,
                timeout=left,
                retry=bounded_retry(left),
                job_retry=None,
            )
            rows = _read_rows(iterator, self.row_cap + 1)
        except Exception as exc:  # noqa: BLE001 - mapped to a fixed, typed failure
            session.settle(reservation, est)  # the job may have billed; charge the estimate
            if job is not None:
                self._best_effort_cancel(job)
            was_cancelled = self._take_cancelled()
            if was_cancelled or isinstance(exc, _CancelRequested):
                return failure(ErrorCode.CANCELLED, Stage.EXECUTE, bytes_estimated=est)
            mapped = map_bq_exception(exc, sql=sql, stage=Stage.EXECUTE)
            return BqFailure(
                code=mapped.code,
                stage=mapped.stage,
                error_class=mapped.error_class,
                identifier=mapped.identifier,
                bytes_estimated=est,
            )
        except BaseException:  # KeyboardInterrupt, SystemExit: cancel, charge, re-raise
            session.settle(reservation, est)
            if job is not None:
                self._best_effort_cancel(job)
            raise
        finally:
            with self._lock:
                self._inflight = None
        billed = getattr(job, "total_bytes_billed", None)
        billed = int(billed) if _is_byte_count(billed) else est
        session.settle(reservation, billed)
        if self._take_cancelled():  # Ctrl+C landed while the job was finishing: honour it
            return failure(ErrorCode.CANCELLED, Stage.EXECUTE, bytes_estimated=est)
        truncated = len(rows) > self.row_cap
        rows = rows[: self.row_cap]
        return QueryResult(
            columns=_columns(iterator, rows),
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            bytes_estimated=est,
            bytes_billed=billed,
            job_id=getattr(job, "job_id", None) or job_id,
        )

    def run(
        self,
        sql: str,
        session: SessionByteBudget,
        sql_counter: SqlCounter | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> QueryResult | BqFailure:
        prepared = self.prepare(sql, session)
        if isinstance(prepared, BqFailure):
            return prepared
        return self.execute(prepared, session, sql_counter, labels)

    # -- cancel hook (AC-15.5) --

    def cancel_inflight(self) -> bool:
        """Cancel the running job, if any.

        May be called from another thread or from a signal handler: the lock is re-entrant, so
        a handler that interrupts the executing thread inside a locked section cannot deadlock.
        Returns True if a job was registered as in flight. Either way the cancel flag is set:
        a job created but not yet registered is cancelled as soon as it registers, and with no
        job at all the next ``execute`` is refused once.
        """
        with self._lock:
            job = self._inflight
            self._cancelled = True
        if job is None:
            return False
        self._best_effort_cancel(job)
        return True

    def _left(self, deadline: float) -> float:
        return max(0.0, deadline - self._clock())

    def reset_cancel(self) -> None:
        """Drop a pending cancel flag (the CLI calls this after a Ctrl-C cancel), so the
        next ``execute`` of a new turn is not refused by a stale cancel."""
        with self._lock:
            self._cancelled = False

    def _take_cancelled(self) -> bool:
        with self._lock:
            was, self._cancelled = self._cancelled, False
        return was

    def _best_effort_cancel(self, job: WarehouseJob) -> None:
        try:
            job.cancel(retry=self._retry, timeout=self.api_timeout_s)
        except Exception:  # noqa: BLE001 - cancel is best effort; nothing raw is kept
            pass

    def _best_effort_cancel_id(self, job_id: str) -> None:
        try:
            self._client.cancel_job(
                job_id, project=self.project, retry=self._retry, timeout=self.api_timeout_s
            )
        except Exception:  # noqa: BLE001 - cancel is best effort; nothing raw is kept
            pass
