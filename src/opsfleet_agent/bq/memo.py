"""Per-session query memo and the result-cache key (HLD §4.5; AC-14.3, AC-08.11).

Key = ``(sql_hash, scope_key, refresh_date, user_id)``:

* ``sql_hash`` is taken over the SQL *after* the scope rewrite with ``as_of`` pinned per turn,
  so the hash already encodes the scope's filters; ``scope_key`` is still part of every key so
  two scopes can never share an entry even if their rewritten SQL happened to coincide.
* ``refresh_date`` is the dataset's daily refresh *date* (``schema.TableMetadataCache``),
  never a table modified time: ``make_memo_key`` rejects a ``datetime``. A new refresh date
  is a different key, so it misses.
* ``user_id`` (SEC-11): one memo per session already isolates users, but the key carries the
  user too, so a memo that is ever shared or mis-scoped still cannot hand one user's rows to
  another.

Entries are deep-copied on store and on every hit, so a caller that mutates a result it got
from the memo cannot change what a later hit returns. The memo is guarded by a lock.

Ordering contract for ``run_sql`` (iteration 13 relies on it): the memo is looked up **before**
the differencing step, and differencing then runs on the memoised result exactly as it would
on a fresh one (``run_memoised``). A hit therefore skips BigQuery but never skips differencing.
A result is stored only after differencing accepted it, so a refused result is never replayed.
"""

from __future__ import annotations

import copy
import hashlib
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

_WS = re.compile(r"\s+")


def normalise_sql(sql: str) -> str:
    """Whitespace-insensitive form. Case is kept: literals are case-sensitive."""
    return _WS.sub(" ", sql).strip().rstrip(";").strip()


def sql_hash(sql: str) -> str:
    return hashlib.sha256(normalise_sql(sql).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoKey:
    sql_hash: str
    scope_key: str
    refresh_date: date
    user_id: str


def make_memo_key(sql: str, scope_key: str, refresh_date: date, *, user_id: str) -> MemoKey:
    if isinstance(refresh_date, datetime) or not isinstance(refresh_date, date):
        raise TypeError("refresh_date must be a date (the dataset refresh date), not a time")
    if not scope_key:
        raise ValueError("scope_key is required in every cache key")
    if not user_id:
        raise ValueError("user_id is required in every memo key")
    return MemoKey(
        sql_hash=sql_hash(sql), scope_key=scope_key, refresh_date=refresh_date, user_id=user_id
    )


def result_cache_key(sql: str, scope_key: str, refresh_date: date) -> str:
    """Production result-cache key: ``sha256(normalised SQL + scope key + refresh date)``."""
    if isinstance(refresh_date, datetime) or not isinstance(refresh_date, date):
        raise TypeError("refresh_date must be a date (the dataset refresh date), not a time")
    if not scope_key:
        raise ValueError("scope_key is required in every cache key")
    material = "\x1f".join((normalise_sql(sql), scope_key, refresh_date.isoformat()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class _Entry[T]:
    value: T
    turn_id: str


class QueryMemo[T]:
    """Bounded LRU, one per session; ``clear()`` at session end.

    Stores a deep copy and returns a fresh deep copy on every hit; thread-safe.
    """

    def __init__(self, max_entries: int = 64) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._data: OrderedDict[MemoKey, _Entry[T]] = OrderedDict()
        self._lock = threading.RLock()

    def lookup(self, key: MemoKey) -> tuple[T, str] | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            self._data.move_to_end(key)
            return copy.deepcopy(entry.value), entry.turn_id

    def store(self, key: MemoKey, value: T, *, turn_id: str) -> None:
        snapshot = copy.deepcopy(value)
        with self._lock:
            self._data[key] = _Entry(snapshot, turn_id)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


@dataclass(frozen=True)
class MemoOutcome[T, F]:
    result: T | None
    hit: bool
    duplicate_in_turn: bool  # run_sql reports DUPLICATE_QUERY with the earlier result
    failure: F | None = None  # an execute failure or a differencing refusal


def run_memoised[T, F](
    memo: QueryMemo[T],
    key: MemoKey,
    *,
    turn_id: str,
    execute: Callable[[], T | F],
    differencing: Callable[[T], F | None],
    is_failure: Callable[[object], bool],
) -> MemoOutcome[T, F]:
    """Memo lookup, then (on a miss) execute, then differencing on *either* path.

    ``differencing(result)`` returns None to accept or a refusal to block.
    ``is_failure(x)`` tells whether ``execute()`` returned a failure instead of a result.
    """
    found = memo.lookup(key)
    if found is not None:
        value, stored_turn = found
        refusal = differencing(value)  # a hit still goes through differencing
        if refusal is not None:
            return MemoOutcome(result=None, hit=True, duplicate_in_turn=False, failure=refusal)
        return MemoOutcome(
            result=value, hit=True, duplicate_in_turn=stored_turn == turn_id, failure=None
        )
    produced = execute()
    if is_failure(produced):
        return MemoOutcome(result=None, hit=False, duplicate_in_turn=False, failure=produced)  # type: ignore[arg-type]
    result: T = produced  # type: ignore[assignment]
    refusal = differencing(result)
    if refusal is not None:
        return MemoOutcome(result=None, hit=False, duplicate_in_turn=False, failure=refusal)
    memo.store(key, result, turn_id=turn_id)
    return MemoOutcome(result=result, hit=False, duplicate_in_turn=False, failure=None)
