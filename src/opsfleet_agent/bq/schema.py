"""Table metadata and the dataset refresh date, for the 4 allowed tables only (HLD §4.5, §5.3).

Metadata comes from ``client.get_table`` (a metadata API call, no query job, no bytes billed).
The schema is cached for a day; the refresh date is re-read more often (default hourly) so a
daily dataset refresh is noticed within the hour and the memo misses. The refresh date is the
latest UTC *date* of the tables' ``modified`` timestamps: the memo key gets the date, never
the timestamp.

Failure handling:

* every ``get_table`` call passes a bounded API retry and a timeout taken from one deadline per
  public call (``api_timeout_s`` in total, across the four tables of a refresh);
* a failure is cached for ``NEGATIVE_TTL_S``: within that window no new fetch is made;
* if a fetch fails and a previous value exists, the previous value is kept, but a refresh date
  older than ``REFRESH_MAX_STALE_S`` is not served: the call fails closed;
* otherwise ``SchemaUnavailable`` (error class only, no provider text, no chained exception).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from opsfleet_agent.bq.client import WarehouseClient, bounded_retry
from opsfleet_agent.bq.errors import BqErrorClass, classify

DATASET = "bigquery-public-data.thelook_ecommerce"
ALLOWED_TABLES: tuple[str, ...] = ("orders", "order_items", "products", "users")

DAY_S = 24 * 3600.0
HOUR_S = 3600.0
# After a failed fetch, do not call the API again for this long (avoids hammering a dead backend
# on every turn); callers get the stale value if one is allowed, else SchemaUnavailable.
NEGATIVE_TTL_S = 60.0
# A refresh date last confirmed longer ago than this is not served from cache on failure: the
# memo key would silently pin results to an old dataset version, so we fail closed instead.
REFRESH_MAX_STALE_S = DAY_S


class SchemaUnavailable(Exception):
    """Metadata could not be read. Carries only the error class, never provider text."""

    def __init__(self, error_class: BqErrorClass) -> None:
        super().__init__(f"table metadata unavailable ({error_class.value})")
        self.error_class = error_class


class TableNotAllowed(ValueError):
    """The requested table is not in ``ALLOWED_TABLES``. The message never echoes the name."""

    def __init__(self) -> None:
        super().__init__("table not allowed")


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    mode: str
    description: str | None = None


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: tuple[ColumnInfo, ...]
    num_rows: int | None
    modified: datetime | None


def _columns(schema: Any) -> tuple[ColumnInfo, ...]:
    out = []
    for f in schema or ():
        out.append(
            ColumnInfo(
                name=f.name,
                type=getattr(f, "field_type", None) or getattr(f, "type", "UNKNOWN"),
                mode=getattr(f, "mode", None) or "NULLABLE",
                description=getattr(f, "description", None),
            )
        )
    return tuple(out)


_REFRESH = "\x00refresh"  # negative-cache slot for refresh_date (not a table name)


class TableMetadataCache:
    def __init__(
        self,
        client: WarehouseClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        schema_ttl_s: float = DAY_S,
        refresh_ttl_s: float = HOUR_S,
        api_timeout_s: float = 30.0,
        negative_ttl_s: float = NEGATIVE_TTL_S,
        refresh_max_stale_s: float = REFRESH_MAX_STALE_S,
    ) -> None:
        self._client = client
        self._clock = clock
        self.schema_ttl_s = schema_ttl_s
        self.refresh_ttl_s = refresh_ttl_s
        self.api_timeout_s = api_timeout_s
        self.negative_ttl_s = negative_ttl_s
        self.refresh_max_stale_s = refresh_max_stale_s
        self._tables: dict[str, tuple[TableInfo, float]] = {}
        self._refresh: tuple[date, float] | None = None
        self._failed: dict[str, tuple[BqErrorClass, float]] = {}

    def _fetch(self, name: str, deadline: float) -> TableInfo:
        left = deadline - self._clock()
        if left <= 0:
            raise SchemaUnavailable(BqErrorClass.TIMEOUT)
        error_class: BqErrorClass | None = None
        try:
            t = self._client.get_table(f"{DATASET}.{name}", retry=bounded_retry(left), timeout=left)
        except Exception as exc:  # noqa: BLE001 - raw text is dropped
            error_class = classify(exc)
        if error_class is not None:  # raised outside the except: no __context__ either
            raise SchemaUnavailable(error_class)
        return TableInfo(
            name=name,
            columns=_columns(getattr(t, "schema", None)),
            num_rows=getattr(t, "num_rows", None),
            modified=getattr(t, "modified", None),
        )

    def _recent_failure(self, slot: str, now: float) -> BqErrorClass | None:
        failed = self._failed.get(slot)
        if failed and now - failed[1] < self.negative_ttl_s:
            return failed[0]
        return None

    def get(self, name: str, *, force: bool = False) -> TableInfo:
        if name not in ALLOWED_TABLES:
            raise TableNotAllowed()
        now = self._clock()
        cached = self._tables.get(name)
        if cached and not force and now - cached[1] < self.schema_ttl_s:
            return cached[0]
        recent = self._recent_failure(name, now)
        if recent is not None:
            if cached:
                return cached[0]
            raise SchemaUnavailable(recent)
        try:
            info = self._fetch(name, now + self.api_timeout_s)
        except SchemaUnavailable as exc:
            self._failed[name] = (exc.error_class, now)
            if cached:
                return cached[0]
            raise
        self._failed.pop(name, None)
        self._tables[name] = (info, now)
        return info

    def all_tables(self) -> list[TableInfo]:
        return [self.get(n) for n in ALLOWED_TABLES]

    def _stale_refresh(self, now: float, error_class: BqErrorClass) -> date:
        if self._refresh and now - self._refresh[1] < self.refresh_max_stale_s:
            return self._refresh[0]
        raise SchemaUnavailable(error_class)

    def refresh_date(self) -> date:
        """Latest UTC date any allowed table was modified (the dataset's refresh date)."""
        now = self._clock()
        if self._refresh and now - self._refresh[1] < self.refresh_ttl_s:
            return self._refresh[0]
        recent = self._recent_failure(_REFRESH, now)
        if recent is not None:
            return self._stale_refresh(now, recent)
        deadline = now + self.api_timeout_s  # one deadline across all four tables
        try:
            infos = [self._fetch(n, deadline) for n in ALLOWED_TABLES]
        except SchemaUnavailable as exc:
            self._failed[_REFRESH] = (exc.error_class, now)
            return self._stale_refresh(now, exc.error_class)
        stamps = [i.modified for i in infos if i.modified is not None]
        if not stamps:
            self._failed[_REFRESH] = (BqErrorClass.OTHER, now)
            return self._stale_refresh(now, BqErrorClass.OTHER)
        self._failed.pop(_REFRESH, None)
        for info in infos:
            self._tables[info.name] = (info, now)
        latest = max(s if s.tzinfo else s.replace(tzinfo=UTC) for s in stamps)
        value = latest.astimezone(UTC).date()
        self._refresh = (value, now)
        return value
