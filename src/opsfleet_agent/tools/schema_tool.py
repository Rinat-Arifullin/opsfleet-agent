"""``list_tables`` and ``get_schema``: read-only metadata tools (iteration 13; HLD §4.4).

Both read :class:`TableMetadataCache` only (``tables.get``, no query, no bytes). PII columns
(``sql_policy.PII_COLUMNS``) are omitted from ``get_schema``: the model never learns they
exist, so it cannot ask for them by name. Column descriptions come from provider metadata,
which is untrusted text: they are PII-scrubbed, stripped of control characters and capped.
Errors carry fixed text and, for metadata failures, the error class only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, Final

from opsfleet_agent.bq.errors import ErrorCode
from opsfleet_agent.bq.schema import (
    ALLOWED_TABLES,
    SchemaUnavailable,
    TableInfo,
    TableMetadataCache,
    TableNotAllowed,
)
from opsfleet_agent.guards import pii_regex
from opsfleet_agent.guards.sql_policy import PII_COLUMNS, QI_COLUMNS

__all__ = [
    "BQ_TYPES",
    "ID_KEYS",
    "TABLE_DESCRIPTIONS",
    "column_kind",
    "get_schema",
    "list_tables",
    "schema_section",
]

INVALID_ARGS: Final = "INVALID_ARGS"
MAX_DESCRIPTION_CHARS: Final = 200

#: Fixed table descriptions (ours, not the provider's).
TABLE_DESCRIPTIONS: Final[Mapping[str, str]] = {
    "orders": "One row per order: status, timestamps, number of items, customer key.",
    "order_items": "One row per item sold: order, product, sale price, status, timestamps.",
    "products": "Product catalogue: name, brand, category, department, cost, retail price.",
    "users": "Customers (non-identifying attributes only): age, gender, location, signup.",
}

_NUMERIC: Final = frozenset({"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"})
#: Column types ``get_schema`` passes on; a column of any other type is dropped (L-7).
BQ_TYPES: Final = frozenset(
    _NUMERIC
    | {
        "BOOL",
        "BOOLEAN",
        "STRING",
        "BYTES",
        "DATE",
        "DATETIME",
        "TIME",
        "TIMESTAMP",
        "GEOGRAPHY",
        "JSON",
        "INTERVAL",
        "RANGE",
        "RECORD",
        "STRUCT",
    }
)
#: A column name ``get_schema`` passes on; anything else is dropped, never echoed (L-7).
_COLUMN_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
#: Customer keys per table. A copy of ``sql_policy._ID_KEYS`` (a reviewed guard's private
#: constant); a unit test asserts they stay equal.
ID_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "users": frozenset({"id"}),
    "orders": frozenset({"order_id", "user_id"}),
    "order_items": frozenset({"id", "order_id", "user_id", "inventory_item_id"}),
}
#: ``products.id`` is not a customer key (sql_policy) but it is still a key for the model.
_KEYS: Final[Mapping[str, frozenset[str]]] = {**ID_KEYS, "products": frozenset({"id"})}
_CONTROL = re.compile(r"\s+")


def column_kind(table: str, column: str, bq_type: str) -> str:
    """``key`` | ``quasi_identifier`` | ``metric`` | ``dimension``."""
    if column in _KEYS.get(table, frozenset()) or column.endswith("_id"):
        return "key"
    if column in QI_COLUMNS.get(table, frozenset()):
        return "quasi_identifier"
    if bq_type.upper() in _NUMERIC:
        return "metric"
    return "dimension"


def _clean(text: str | None, scrub: Callable[[str], str]) -> str:
    if not text:
        return ""
    text = "".join(ch if unicodedata.category(ch)[0] not in "CZ" else " " for ch in text)
    text = _CONTROL.sub(" ", text).strip()
    return scrub(text)[:MAX_DESCRIPTION_CHARS]


def _default_scrub(text: str) -> str:
    return pii_regex.scrub(text).text


def _error(code: str, message: str, hint: str, **extra: Any) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message, "retryable": False, "hint": hint}
    err.update(extra)
    return {"ok": False, "error": err}


def _invalid_args() -> dict[str, Any]:
    return _error(
        INVALID_ARGS,
        "The tool call arguments are invalid.",
        "Call get_schema with one field, table, one of: " + ", ".join(ALLOWED_TABLES) + ".",
    )


def _unavailable(exc: SchemaUnavailable) -> dict[str, Any]:
    return _error(
        ErrorCode.BQ_UNAVAILABLE.value,
        "Table metadata is unavailable right now.",
        "Try again later.",
        **{"class": exc.error_class.value},
    )


def list_tables(cache: TableMetadataCache, args: object = None) -> dict[str, Any]:
    """``list_tables {}`` -> ``[{table, description, approx_rows}]``."""
    if args not in (None, {}) and not (isinstance(args, Mapping) and not args):
        return _error(INVALID_ARGS, "list_tables takes no arguments.", "Call it with {}.")
    out = []
    for name in ALLOWED_TABLES:
        try:
            info: TableInfo | None = cache.get(name)
        except SchemaUnavailable:
            info = None  # the table list itself is fixed; the row count is a nice-to-have
        out.append(
            {
                "table": name,
                "description": TABLE_DESCRIPTIONS[name],
                "approx_rows": info.num_rows if info is not None else None,
            }
        )
    return {"ok": True, "data": out}


def get_schema(
    args: object,
    cache: TableMetadataCache,
    *,
    scrub: Callable[[str], str] = _default_scrub,
) -> dict[str, Any]:
    """``get_schema {table}`` -> ``[{column, type, description, kind}]`` without PII columns."""
    if not isinstance(args, Mapping) or set(args) != {"table"}:
        return _invalid_args()
    table = args["table"]
    if type(table) is not str or table not in ALLOWED_TABLES:
        return _invalid_args()  # never echo the name
    try:
        info = cache.get(table)
    except TableNotAllowed:
        return _invalid_args()
    except SchemaUnavailable as exc:
        return _unavailable(exc)
    hidden = PII_COLUMNS.get(table, frozenset())
    columns = []
    for c in info.columns:
        name, bq_type = c.name, c.type
        if type(name) is not str or type(bq_type) is not str:
            continue
        if not _COLUMN_NAME.fullmatch(name) or bq_type.upper() not in BQ_TYPES:
            continue  # provider metadata we do not recognise: dropped, never echoed (L-7)
        if name.lower() in hidden:  # BigQuery column names are case-insensitive
            continue
        columns.append(
            {
                "column": name,
                "type": bq_type.upper(),
                "description": _clean(c.description, scrub),
                "kind": column_kind(table, name, bq_type),
            }
        )
    return {"ok": True, "data": {"table": table, "columns": columns}}


def schema_section(cache: TableMetadataCache) -> str:
    """The tables and columns as one prompt block, so the analyst can skip list_tables and
    get_schema (two model rounds of its budget). Built from the same tool outputs, so PII
    columns stay hidden; provider descriptions (untrusted text) are left out.
    A table whose metadata is unavailable is named with a hint to call ``get_schema``."""
    lines = []
    for t in list_tables(cache)["data"]:
        name, rows = t["table"], t["approx_rows"]
        size = f", about {rows} rows" if isinstance(rows, int) else ""
        lines.append(f"- {name} ({t['description']}{size})")
        res = get_schema({"table": name}, cache)
        if not res["ok"]:
            lines.append("  columns: unavailable now; call get_schema for this table")
            continue
        cols = ", ".join(f"{c['column']} {c['type']} [{c['kind']}]" for c in res["data"]["columns"])
        lines.append(f"  columns: {cols}")
    return "\n".join(lines)
