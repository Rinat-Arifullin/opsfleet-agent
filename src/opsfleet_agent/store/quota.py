"""Per-user usage quotas kept in the local store (FR-63, AC-22.8; iteration 24).

Counters are per user and per fixed UTC window (hour, day). The check runs in code BEFORE any
LLM or BigQuery call (see `graph/degraded.py`). Limits come from `Settings.quota_*`
(`quota:` in config/models.yaml, D-138); the constants below are the code defaults.

Schema: migration 4 in `store/db.py` (DDL in `store/quota_schema.py`, D-139); `ensure_schema`
re-applies it idempotently as a fallback for a store opened without `open_store`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from opsfleet_agent.store.db import write_tx
from opsfleet_agent.store.quota_schema import QUOTA_MIGRATION

DEFAULT_LLM_PER_HOUR: Final = 300
DEFAULT_LLM_PER_DAY: Final = 2000
DEFAULT_BQ_BYTES_PER_DAY: Final = 100 * 10**9  # 100 GB

@dataclass(frozen=True)
class QuotaLimits:
    llm_per_hour: int = DEFAULT_LLM_PER_HOUR
    llm_per_day: int = DEFAULT_LLM_PER_DAY
    bq_bytes_per_day: int = DEFAULT_BQ_BYTES_PER_DAY


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    reason: str = ""  # "" | "llm_hour" | "llm_day" | "bq_bytes_day"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class QuotaStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        limits: QuotaLimits | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._conn = conn
        self.limits = limits or QuotaLimits()
        self._clock = clock
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with write_tx(self._conn) as c:
            for stmt in QUOTA_MIGRATION:
                c.execute(stmt)

    def _windows(self) -> dict[str, str]:
        now = self._clock().astimezone(UTC)
        return {
            "llm_hour": now.strftime("%Y-%m-%dT%H"),
            "llm_day": now.strftime("%Y-%m-%d"),
            "bq_bytes_day": now.strftime("%Y-%m-%d"),
        }

    def _used(self, user_id: str, kind: str) -> int:
        row = self._conn.execute(
            "SELECT used FROM user_quota WHERE user_id=? AND kind=? AND win=?",
            (user_id, kind, self._windows()[kind]),
        ).fetchone()
        return int(row[0]) if row else 0

    def usage(self, user_id: str) -> dict[str, int]:
        return {k: self._used(user_id, k) for k in self._windows()}

    def check(self, user_id: str, *, extra_calls: int = 1) -> QuotaDecision:
        """Would `extra_calls` more LLM calls (and any BigQuery work) fit? Pure read."""
        lim = self.limits
        if self._used(user_id, "llm_hour") + extra_calls > lim.llm_per_hour:
            return QuotaDecision(False, "llm_hour")
        if self._used(user_id, "llm_day") + extra_calls > lim.llm_per_day:
            return QuotaDecision(False, "llm_day")
        if self._used(user_id, "bq_bytes_day") >= lim.bq_bytes_per_day:
            return QuotaDecision(False, "bq_bytes_day")
        return QuotaDecision(True)

    def remaining_bytes(self, user_id: str) -> int:
        return max(0, self.limits.bq_bytes_per_day - self._used(user_id, "bq_bytes_day"))

    def _add(self, user_id: str, kind: str, amount: int) -> None:
        if amount <= 0:
            return
        with write_tx(self._conn) as c:
            c.execute(
                "INSERT INTO user_quota (user_id, kind, win, used) VALUES (?,?,?,?) "
                "ON CONFLICT (user_id, kind, win) DO UPDATE SET used = used + excluded.used",
                (user_id, kind, self._windows()[kind], amount),
            )

    def record_calls(self, user_id: str, n: int = 1) -> None:
        self._add(user_id, "llm_hour", n)
        self._add(user_id, "llm_day", n)

    def record_bytes(self, user_id: str, n: int) -> None:
        self._add(user_id, "bq_bytes_day", n)
