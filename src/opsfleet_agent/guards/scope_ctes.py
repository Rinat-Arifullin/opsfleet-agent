"""Code-built, PII-free scope CTEs (iteration 7; HLD §5.3 step 8, ADR-008).

Each allowlisted table is replaced, in the model's statement, by one of four CTEs that code
prepends to the statement:

* ``__p``  -- ``products`` filtered by ``brand IN UNNEST(@scope_brands)``;
* ``__oi`` -- ``order_items`` whose product is in ``__p``;
* ``__o``  -- ``orders`` that contain at least one item in ``__oi``;
* ``__u``  -- ``users`` who bought at least one item in ``__oi``.

Scope reaches ``orders`` and ``users`` through ``order_items`` and ``products`` because those
tables have no product column (A-2). Under the CEO ``all`` flag (A-17) the CTEs are kept, so
the PII-free projection still applies, but **every** scope ``WHERE`` is dropped: users who
never bought are counted (R3-M12, AC-08.12).

Every CTE has an explicit column list (never ``SELECT *``). The list is the policy's exposed
schema minus the PII columns, so PII is not even present in the rewritten statement, and
``orders.num_of_item`` / ``orders.gender`` are absent. An import-time check ties these lists
to :data:`opsfleet_agent.guards.sql_policy.ALLOWED_TABLES` and ``PII_COLUMNS``.

The templates below are code constants. Brand values never appear in SQL text: they are bound
as the BigQuery array query parameter ``@scope_brands``.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType
from typing import Final

import sqlglot
from sqlglot import exp

from opsfleet_agent.guards.sql_policy import ALLOWED_TABLES, DATASET, PII_COLUMNS, PROJECT

__all__ = [
    "CODE_CTE_FOR_TABLE",
    "CODE_CTE_ORDER",
    "SCOPE_PARAM",
    "TABLE_FOR_CODE_CTE",
    "build_cte",
    "cte_body",
    "cte_body_sql",
    "required_ctes",
]

#: Name of the BigQuery array query parameter that carries the brand list.
SCOPE_PARAM: Final = "scope_brands"

#: Allowlisted table -> code CTE name.
CODE_CTE_FOR_TABLE: Final[MappingProxyType[str, str]] = MappingProxyType(
    {"products": "__p", "order_items": "__oi", "orders": "__o", "users": "__u"}
)
TABLE_FOR_CODE_CTE: Final[MappingProxyType[str, str]] = MappingProxyType(
    {v: k for k, v in CODE_CTE_FOR_TABLE.items()}
)

#: Definition order. A CTE may only read CTEs defined before it.
CODE_CTE_ORDER: Final[tuple[str, ...]] = ("__p", "__oi", "__o", "__u")

#: Explicit projections, in HLD order.
_COLUMNS: Final[MappingProxyType[str, tuple[str, ...]]] = MappingProxyType(
    {
        "__p": (
            "id", "name", "brand", "category", "department", "retail_price", "cost", "sku",
            "distribution_center_id",
        ),
        "__oi": (
            "id", "order_id", "user_id", "product_id", "inventory_item_id", "status",
            "sale_price", "created_at", "shipped_at", "delivered_at", "returned_at",
        ),
        "__o": (
            "order_id", "user_id", "status", "created_at", "shipped_at", "delivered_at",
            "returned_at",
        ),
        "__u": (
            "id", "age", "gender", "city", "state", "country", "traffic_source", "created_at",
        ),
    }
)

#: Scope filters (brand scope only). Dropped entirely under ``all``.
_FILTERS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "__p": f"brand IN UNNEST(@{SCOPE_PARAM})",
        "__oi": "product_id IN (SELECT id FROM __p)",
        "__o": "order_id IN (SELECT order_id FROM __oi)",
        "__u": "id IN (SELECT user_id FROM __oi)",
    }
)

#: CTEs each filter reads (brand scope only).
_DEPENDS: Final[MappingProxyType[str, tuple[str, ...]]] = MappingProxyType(
    {"__p": (), "__oi": ("__p",), "__o": ("__oi",), "__u": ("__oi",)}
)


def _check_columns() -> None:
    """Import-time guard: the projections equal the exposed schema minus PII."""
    for cte, table in TABLE_FOR_CODE_CTE.items():
        expected = ALLOWED_TABLES[table] - PII_COLUMNS.get(table, frozenset())
        cols = _COLUMNS[cte]
        if set(cols) != expected or len(cols) != len(set(cols)):
            raise RuntimeError(f"scope CTE {cte} columns drift from the policy schema")
        if PII_COLUMNS.get(table, frozenset()) & set(cols):
            raise RuntimeError(f"scope CTE {cte} projects a PII column")


_check_columns()


def cte_body_sql(name: str, *, scoped: bool) -> str:
    """The code-owned body of one CTE as BigQuery SQL. Raises ``KeyError`` on an unknown name."""
    table = TABLE_FOR_CODE_CTE[name]
    cols = ", ".join(_COLUMNS[name])
    sql = f"SELECT {cols} FROM `{PROJECT}.{DATASET}.{table}`"
    if scoped:
        sql += f" WHERE {_FILTERS[name]}"
    return sql


def cte_body(name: str, *, scoped: bool) -> exp.Select:
    """Fresh AST of one CTE body (callers may mutate it)."""
    body = sqlglot.parse_one(cte_body_sql(name, scoped=scoped), read="bigquery")
    if not isinstance(body, exp.Select):  # pragma: no cover - templates are constants
        raise RuntimeError("scope CTE template is not a SELECT")
    return body


def build_cte(name: str, *, scoped: bool) -> exp.CTE:
    """A ``name AS (<body>)`` node ready to be prepended to a ``WITH``."""
    return exp.CTE(this=cte_body(name, scoped=scoped), alias=exp.TableAlias(
        this=exp.to_identifier(name)
    ))


def required_ctes(used: Iterable[str], *, scoped: bool) -> tuple[str, ...]:
    """The code CTEs to emit for ``used``, plus their filter dependencies, in definition order.

    Bounded: the closure runs over at most four names.
    """
    need = {n for n in used if n in TABLE_FOR_CODE_CTE}
    if scoped:
        for _ in range(len(CODE_CTE_ORDER)):
            grown = need | {d for n in need for d in _DEPENDS[n]}
            if grown == need:
                break
            need = grown
    return tuple(n for n in CODE_CTE_ORDER if n in need)


def dependencies(name: str, *, scoped: bool) -> tuple[str, ...]:
    """Direct dependencies of one code CTE under the given scope kind."""
    return _DEPENDS[name] if scoped else ()
