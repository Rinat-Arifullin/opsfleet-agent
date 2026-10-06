"""BigQuery error mapping: a fixed table of classes, codes and messages (HLD §4.4, §5.3, R3-H2).

Provider error text can echo cell values (a failed cast quotes the value), so it is used only
*inside* ``map_bq_exception`` to pick a class and is then dropped. A ``BqFailure`` holds the
class, the stable ``run_sql`` error code, a fixed message and hint from ``MESSAGES`` and, for
``UNKNOWN_COLUMN`` only, an identifier that also appears as a bare identifier in the SQL that
was submitted (so it carries nothing the model did not write itself). Nothing here stores,
logs, chains or re-raises the original exception.
"""

from __future__ import annotations

import concurrent.futures
import re
from dataclasses import dataclass
from enum import StrEnum


class BqErrorClass(StrEnum):
    """HLD classes plus UNAVAILABLE (auth, network, 5xx, quota) for the BQ_UNAVAILABLE code."""

    SYNTAX = "SYNTAX"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    TIMEOUT = "TIMEOUT"
    BYTES_CAP = "BYTES_CAP"
    UNAVAILABLE = "UNAVAILABLE"
    OTHER = "OTHER"


class ErrorCode(StrEnum):
    """Stable ``run_sql`` error codes produced by the bq package (HLD §4.4 tool table)."""

    SQL_SYNTAX = "SQL_SYNTAX"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    BQ_RUNTIME = "BQ_RUNTIME"
    TIMEOUT = "TIMEOUT"
    COST_CAP = "COST_CAP"
    SESSION_BUDGET = "SESSION_BUDGET"
    BQ_UNAVAILABLE = "BQ_UNAVAILABLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CANCELLED = "CANCELLED"


class Stage(StrEnum):
    PRECHECK = "precheck"  # our own caps, before any job runs
    DRY_RUN = "dry_run"
    EXECUTE = "execute"


CLASS_TO_CODE: dict[BqErrorClass, ErrorCode] = {
    BqErrorClass.SYNTAX: ErrorCode.SQL_SYNTAX,
    BqErrorClass.UNKNOWN_COLUMN: ErrorCode.UNKNOWN_COLUMN,
    BqErrorClass.TYPE_MISMATCH: ErrorCode.BQ_RUNTIME,
    BqErrorClass.TIMEOUT: ErrorCode.TIMEOUT,
    BqErrorClass.BYTES_CAP: ErrorCode.COST_CAP,
    BqErrorClass.UNAVAILABLE: ErrorCode.BQ_UNAVAILABLE,
    BqErrorClass.OTHER: ErrorCode.BQ_RUNTIME,
}

# (message for the user, hint for the model). Fixed strings only: no SQL, no provider text.
MESSAGES: dict[ErrorCode, tuple[str, str]] = {
    ErrorCode.SQL_SYNTAX: (
        "The query could not be parsed.",
        "Fix the BigQuery Standard SQL syntax and try again.",
    ),
    ErrorCode.UNKNOWN_COLUMN: (
        "The query refers to a column or table that does not exist.",
        "Check the column names against get_schema and try again.",
    ),
    ErrorCode.BQ_RUNTIME: (
        "The query failed while running.",
        "Check types in comparisons, casts and function arguments; simplify the query.",
    ),
    ErrorCode.TIMEOUT: (
        "The query took too long and was stopped.",
        "Narrow the date range or aggregate more before joining.",
    ),
    ErrorCode.COST_CAP: (
        "The query would scan more data than one query may use.",
        "Narrow the question: fewer columns, a shorter date range, or pre-aggregate.",
    ),
    ErrorCode.SESSION_BUDGET: (
        "This session has used its data-scan budget, so no more queries can run.",
        "Answer from the results already gathered, or ask the user to start a new session.",
    ),
    ErrorCode.BQ_UNAVAILABLE: (
        "The data warehouse is unavailable right now.",
        "Do not retry this turn; tell the user the data service is temporarily unavailable.",
    ),
    ErrorCode.BUDGET_EXHAUSTED: (
        "The query limit for this turn has been reached.",
        "Answer from the results already gathered.",
    ),
    ErrorCode.CANCELLED: ("Cancelled.", "The user cancelled the query; stop."),
}


@dataclass(frozen=True)
class BqFailure:
    """Typed, sanitised failure. Every field is a fixed string, an enum, a number or None."""

    code: ErrorCode
    stage: Stage
    error_class: BqErrorClass | None = None
    identifier: str | None = None
    bytes_estimated: int | None = None

    @property
    def message(self) -> str:
        return MESSAGES[self.code][0]

    @property
    def hint(self) -> str:
        return MESSAGES[self.code][1]

    def envelope(self) -> dict[str, object]:
        """What the LLM, trace and audit record may see."""
        env: dict[str, object] = {
            "error": self.code.value,
            "message": self.message,
            "hint": self.hint,
            "stage": self.stage.value,
        }
        if self.error_class is not None:
            env["class"] = self.error_class.value
        if self.identifier is not None:
            env["identifier"] = self.identifier
        return env


def failure(code: ErrorCode, stage: Stage, **kw: object) -> BqFailure:
    return BqFailure(code=code, stage=stage, **kw)  # type: ignore[arg-type]


# --- classification (text is inspected locally, never kept) ---------------------------------

_IDENT = r"`?([A-Za-z_][A-Za-z0-9_]{0,127})`?"
_UNKNOWN_PATTERNS = (
    re.compile(r"Unrecognized name:\s*" + _IDENT),
    re.compile(r"Name\s+" + _IDENT + r"\s+not found inside"),
    re.compile(r"Field name\s+" + _IDENT + r"\s+does not exist"),
    # Table only (D-22): the last segment of project:dataset.table. A missing dataset or
    # project is not matched and stays BQ_RUNTIME.
    re.compile(r"Not found:\s*Table\s+(?:[A-Za-z0-9_\-]+[:.])*" + _IDENT + r"\s+was not found"),
)
_TYPE_PATTERNS = re.compile(
    r"No matching signature|Could not cast|Bad \w+ value|Invalid cast|cannot be compared"
    r"|Cannot coerce|Invalid (?:date|timestamp|datetime|time|numeric)|type mismatch"
    r"|Argument .* has type|Cannot execute IN subquery",
    re.IGNORECASE,
)
_UNAVAILABLE_REASONS = {
    "backendError",
    "internalError",
    "rateLimitExceeded",
    "quotaExceeded",
    "accessDenied",
    "billingNotEnabled",
    "billingTierLimitExceeded",
}
_UNAVAILABLE_TYPES = (
    "ServiceUnavailable",
    "InternalServerError",
    "BadGateway",
    "GatewayTimeout",
    "TooManyRequests",
    "Forbidden",
    "Unauthorized",
    "DefaultCredentialsError",
    "RefreshError",
    "TransportError",
)
_STRING_LITERAL = re.compile(
    r"'''.*?'''|\"\"\".*?\"\"\"|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", re.DOTALL
)
_COMMENT = re.compile(r"--[^\n]*|#[^\n]*|/\*.*?\*/", re.DOTALL)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _reasons(exc: BaseException) -> set[str]:
    out: set[str] = set()
    for item in getattr(exc, "errors", None) or ():
        if isinstance(item, dict) and isinstance(item.get("reason"), str):
            out.add(item["reason"])
    return out


def _text(exc: BaseException) -> str:
    parts = [str(getattr(exc, "message", "") or "")]
    for item in getattr(exc, "errors", None) or ():
        if isinstance(item, dict):
            parts.append(str(item.get("message", "")))
    parts.append(str(exc))
    return "\n".join(parts)


def _sql_identifiers(sql: str) -> set[str]:
    """Bare identifier tokens of the SQL, with string literals and comments removed."""
    stripped = _COMMENT.sub(" ", _STRING_LITERAL.sub(" ", sql))
    stripped = stripped.replace("`", " ")
    return {w.lower() for w in _WORD.findall(stripped)}


def _type_names(exc: BaseException) -> set[str]:
    return {t.__name__ for t in type(exc).__mro__}


def classify(exc: BaseException) -> BqErrorClass:
    names = _type_names(exc)
    reasons = _reasons(exc)
    if (
        isinstance(exc, concurrent.futures.TimeoutError | TimeoutError)
        or "DeadlineExceeded" in names
        or "timeout" in reasons
    ):
        return BqErrorClass.TIMEOUT
    if "bytesBilledLimitExceeded" in reasons:
        return BqErrorClass.BYTES_CAP
    if reasons & _UNAVAILABLE_REASONS or names & set(_UNAVAILABLE_TYPES):
        return BqErrorClass.UNAVAILABLE
    if isinstance(exc, ConnectionError) or (
        isinstance(exc, OSError) and not isinstance(exc, FileNotFoundError)
    ):
        return BqErrorClass.UNAVAILABLE
    text = _text(exc)
    if re.search(r"timed out|exceeded the maximum execution time", text, re.IGNORECASE):
        return BqErrorClass.TIMEOUT
    if re.search(r"bytes billed|maximum bytes billed", text, re.IGNORECASE):
        return BqErrorClass.BYTES_CAP
    if any(p.search(text) for p in _UNKNOWN_PATTERNS):
        return BqErrorClass.UNKNOWN_COLUMN
    if re.search(r"Syntax error", text, re.IGNORECASE):
        return BqErrorClass.SYNTAX
    if _TYPE_PATTERNS.search(text):
        return BqErrorClass.TYPE_MISMATCH
    return BqErrorClass.OTHER


def _identifier(exc: BaseException, sql: str) -> str | None:
    text = _text(exc)
    allowed = _sql_identifiers(sql)
    for pattern in _UNKNOWN_PATTERNS:
        m = pattern.search(text)
        if m and m.group(1).lower() in allowed:
            return m.group(1)
    return None


def map_bq_exception(exc: BaseException, *, sql: str, stage: Stage) -> BqFailure:
    """Map any exception from a BigQuery call to a sanitised ``BqFailure``.

    ``sql`` is used only to check that an extracted identifier is one the model wrote.
    """
    cls = classify(exc)
    ident = _identifier(exc, sql) if cls is BqErrorClass.UNKNOWN_COLUMN else None
    return BqFailure(code=CLASS_TO_CODE[cls], stage=stage, error_class=cls, identifier=ident)
