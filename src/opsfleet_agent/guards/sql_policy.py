"""SQL policy for model-written BigQuery SQL (iteration 6; HLD §5.3 steps 1-7, §5.2 layers 3 and 5).

``check_sql(sql)`` decides whether one model-written statement may go on to the scope
rewrite (§5.3 step 8, iteration 7) and BigQuery. It is a pure function: no I/O, no
network, no logging. It **fails closed**: anything it cannot parse, resolve or classify
is rejected. The result is a :class:`PolicyDecision` whose ``reason_code`` is a stable
:class:`Rule` value and whose ``hint`` is static text. Neither ever contains any part of
the SQL, its identifiers or its literals, so a decision is safe to show to the model, to
log and to count in ``sql_policy_reject_total{rule}``.

Checks, in order (the first failing check decides the reason code):

1. **Size.** Empty input is ``sql_syntax``; more than ``MAX_SQL_CHARS`` (8,000) is
   ``sql_too_long``.
2. **Tokens** (before parsing, so no ``Command`` fallback is ever built). A tokenizer
   error is ``sql_syntax``. A non-ASCII character in any identifier or bare word is
   ``non_ascii_identifier`` (Unicode look-alikes such as a Cyrillic ``е`` in ``еmail``
   cannot dodge a name check). Every identifier or bare word, split on ``.``, must then
   be made of parts matching ``[A-Za-z_][A-Za-z0-9_]*`` (only the project part
   ``bigquery-public-data`` may contain ``-``), otherwise ``identifier_not_allowed``
   (a dedicated ``SQL_POLICY`` rule rather than ``non_ascii_identifier``, whose hint
   would mislead; a path of only letters, digits, ``_``, ``-`` and ``*``, such
   as another project or a wildcard table, is reported as ``source_not_allowed``).
   This applies to tables, CTE names, aliases and columns alike:
   sqlglot keeps ``\\`` sequences in backtick identifiers literally while BigQuery
   decodes them, so a backticked ``\\x75sers`` could otherwise shadow ``users`` or a
   reserved ``__`` scope CTE; whitespace, ``;`` and quotes inside backticks fail the
   same way. A ``;`` anywhere but at the very end is ``multiple_statements`` (a comment
   after the final ``;`` is allowed: nothing but comments can follow it). The statement
   must start with ``SELECT``, ``WITH`` or ``(``, otherwise ``statement_not_allowed``
   (DDL, DML, ``EXPORT DATA``, ``EXECUTE IMMEDIATE``,
   ``CREATE TEMP FUNCTION``, scripting, ``CALL``...). Comments are dropped by the
   tokenizer and cannot hide anything: sqlglot and BigQuery agree that block comments do
   not nest.
3. **Parse** with ``sqlglot.parse(read="bigquery")``. A parse error is ``sql_syntax``;
   more than one statement is ``multiple_statements``. The root must be a ``SELECT`` or a
   set operation of selects (optionally under ``WITH``).
   **Decision: a parenthesised top-level statement ``(SELECT ...)`` is rejected**
   (``statement_not_allowed``, hint "remove the outer parentheses"). sqlglot parses it as
   a bare ``Subquery`` root, which the rewrite and the post-rewrite invariant (§5.3 steps
   8 and 10) do not expect; the model loses nothing by dropping the parentheses.
   Parenthesised branches inside a ``UNION`` are accepted.
4. **Size of the tree.** More than ``MAX_NODES`` nodes or a depth over ``MAX_DEPTH`` is
   ``too_complex`` (bounds every later walk; the analysis recursion is bounded by depth).
5. **Statements anywhere** in the tree (DML, DDL, ``Command``, ``EXPORT``):
   ``statement_not_allowed``.
6. **Sources** (``source_not_allowed``): ``@@`` system variables and ``@`` parameters,
   ``UNNEST``, table-valued functions (``EXTERNAL_QUERY``, ``ML.*``...), wildcard tables,
   ``FOR SYSTEM_TIME AS OF``, ``TABLESAMPLE``, ``INFORMATION_SCHEMA``, and any table that
   is not one of the four allowlisted ones. A qualified name must be exactly
   ``bigquery-public-data.thelook_ecommerce.<t>`` or ``thelook_ecommerce.<t>``; an
   unqualified name must be ``<t>`` or a CTE visible from that scope. Table names are
   compared exactly (BigQuery table names are case-sensitive).
7. **CTEs.** ``WITH RECURSIVE`` is ``recursive_cte``. A CTE named like an allowlisted
   table (any case) or starting with ``__`` is ``cte_shadows_table``.
8. **Node allowlist.** ``SELECT AS STRUCT|VALUE``, ``STRUCT``, ``ARRAY``, ``[...]`` and
   ``TO_JSON[_STRING]`` are ``unresolved_value``. ``*`` other than in ``COUNT(*)``,
   including ``t.*`` and ``* EXCEPT``, is ``select_star``. Any function that is not on the
   allowlist (UDFs, ``SAFE.`` calls, ``ERROR``, ``FORMAT``, ``JSON_*``, hashes, ``SPLIT``,
   ``ARRAY_AGG``, ``APPROX_QUANTILES``/``APPROX_TOP_*``, ``SESSION_USER``...) is
   ``function_denied``. Any other node type that is not allowlisted (``PIVOT``,
   ``ROLLUP``, ``LATERAL``, ``JOIN ... USING``, ``NATURAL JOIN``, offsets...) is
   ``unsupported_syntax``.
9. **Columns**, resolved scope by scope with ``sqlglot.optimizer.scope`` against the
   exposed schema below (column names are case-insensitive). An unknown column is
   ``unknown_column`` (this is also how ``orders.num_of_item`` and ``orders.gender`` fail:
   they are not exposed, HLD §5.3 step 8); a name found in two sources is
   ``ambiguous_column``; a table or CTE alias used as a value (``SELECT u FROM users u``)
   or a struct path (``u.a.b``) is ``unresolved_value``. A PII column referenced anywhere
   is rejected: ``pii_projection`` in a select list, ``pii_reference`` anywhere else
   (``WHERE``, ``JOIN``, ``GROUP BY``, ``ORDER BY``, inside any function). An output
   alias named like a PII column (``SELECT id AS email``) is ``pii_projection`` and a
   table, derived-table or CTE alias named like one is ``pii_reference``, so the
   downstream column-name scrubber is never fooled.
10. **Quasi-identifiers** (QI: the ``users`` columns age, gender, city, state, country,
    traffic_source and created_at, and anything derived from them, tracked through CTEs,
    derived tables, subqueries and set operations):

    * ``function_denied``: ``STRING_AGG`` over an id key (id-packing: it returns
      every id of a group in one cell, QI or not); ``CAST``/``SAFE_CAST`` over a QI;
      ``REGEXP_*`` or ``PARSE_*`` over a QI; a string function that combines a QI with
      a literal outside a ``GROUP BY`` key.
    * ``qi_position``: a QI inside any aggregate other than ``COUNT``, inside
      ``IF``/``CASE`` within any aggregate, or anywhere in a window function; and a
      conditional inside an aggregate that compares an id key with a literal. Inside
      ``COUNT`` a QI is allowed only as ``COUNT(DISTINCT <column>)`` where the column is
      a bare pass-through of a QI column (iteration 9, review B1): any QI expression
      (comparison, ``OR NULL``, ``LIKE``, ``IN``, ``NULLIF``, ``COALESCE``,
      ``SAFE_DIVIDE``, ``GREATEST``, arithmetic, a CTE column computed from a QI...) or a
      QI in ``COUNTIF`` counts an arbitrary sub-population the threshold does not see.
    * ``small_cell_unplaceable``: an id key (``users.id``, any ``user_id``, any
      ``order_id``, ``order_items.id``, ``inventory_item_id``) compared with a literal in
      a statement that references any QI.
    * ``qi_at_id_grain`` (ADR-013 option A): the statement's output is at row or id grain
      (no aggregation, ``SELECT DISTINCT`` without aggregation, or grouped by an id key),
      or an output column still carries an id (``MIN(id)``, ``ANY_VALUE(user_id)``...),
      and the statement references a QI anywhere; also a scalar or ``IN`` subquery that
      returns a QI at row grain.
    * ``qi_position`` (iteration 9, D-29): ``users.created_at`` (a signup timestamp,
      near-unique) is only allowed inside ``DATE_TRUNC``/``TIMESTAMP_TRUNC``/
      ``DATETIME_TRUNC`` to MONTH, QUARTER or YEAR (optionally over ``DATE(...)``), or in
      a ``WHERE`` filter. Raw use anywhere else, ``EXTRACT``, WEEK/DAY/ISOYEAR
      truncation, or a CTE that passes it through untruncated is rejected (fail closed).
      This check runs last, so it never changes an earlier reason code.

    The HAVING injection, the population check and the rest of the small-cell rule run in
    iteration 9 on the rewritten AST (§5.3 step 9).

``regenerate_sql(sql)`` returns the canonical text of an allowed statement: the parsed
AST printed in the BigQuery dialect with ``comments=False``. ``run_sql`` sends only this
(or the scope rewrite's own regenerated SQL), never the model's raw text.

Not here: the brand-scope rewrite and the post-rewrite invariant (iteration 7), dry-run
and ``maximum_bytes_billed`` (the BigQuery client).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.tokens import TokenType

__all__ = [
    "ALLOWED_TABLES",
    "DATASET",
    "MAX_SQL_CHARS",
    "PII_COLUMNS",
    "PROJECT",
    "QI_COLUMNS",
    "PolicyDecision",
    "Rule",
    "check_sql",
    "regenerate_sql",
]

MAX_SQL_CHARS: Final = 8_000
MAX_NODES: Final = 5_000
MAX_DEPTH: Final = 120
PROJECT: Final = "bigquery-public-data"
DATASET: Final = "thelook_ecommerce"

# --------------------------------------------------------------------------- schema

#: Columns the model may reference, per allowlisted table (HLD §5.3 step 8 CTE lists plus
#: the PII columns of ``users``, which exist so that a reference to them is reported as
#: PII rather than as an unknown column). ``orders.num_of_item`` and ``orders.gender`` are
#: deliberately absent.
ALLOWED_TABLES: Final[MappingProxyType[str, frozenset[str]]] = MappingProxyType(
    {
        "products": frozenset(
            {
                "id", "name", "brand", "category", "department", "retail_price", "cost",
                "sku", "distribution_center_id",
            }
        ),
        "order_items": frozenset(
            {
                "id", "order_id", "user_id", "product_id", "inventory_item_id", "status",
                "sale_price", "created_at", "shipped_at", "delivered_at", "returned_at",
            }
        ),
        "orders": frozenset(
            {
                "order_id", "user_id", "status", "created_at", "shipped_at",
                "delivered_at", "returned_at",
            }
        ),
        "users": frozenset(
            {
                "id", "first_name", "last_name", "email", "age", "gender", "state",
                "street_address", "postal_code", "city", "country", "latitude",
                "longitude", "traffic_source", "created_at", "user_geom",
            }
        ),
    }
)

#: PII columns (HLD §5.2 layer 3): never referenced anywhere.
PII_COLUMNS: Final[MappingProxyType[str, frozenset[str]]] = MappingProxyType(
    {
        "users": frozenset(
            {
                "first_name", "last_name", "email", "street_address", "postal_code",
                "latitude", "longitude", "user_geom",
            }
        )
    }
)

#: Customer quasi-identifiers (HLD §5.2 layer 5). Iteration 9 reuses this constant.
QI_COLUMNS: Final[MappingProxyType[str, frozenset[str]]] = MappingProxyType(
    {
        "users": frozenset(
            {"age", "gender", "city", "state", "country", "traffic_source", "created_at"}
        )
    }
)

#: Keys at customer, order or item grain. ``products.id`` is not one: grouping by
#: product is a product-only aggregate.
_ID_KEYS: Final[MappingProxyType[str, frozenset[str]]] = MappingProxyType(
    {
        "users": frozenset({"id"}),
        "orders": frozenset({"order_id", "user_id"}),
        "order_items": frozenset({"id", "order_id", "user_id", "inventory_item_id"}),
    }
)

_QI: Final = "qi"
_IDKEY: Final = "idkey"
_PII: Final = "pii"
#: Raw (untruncated) ``users.created_at`` lineage: a signup timestamp is near-unique.
_RAWTS: Final = "rawts"
#: A value computed from a QI (anything but a bare pass-through of a QI column).
_QIEXPR: Final = "qiexpr"
#: ``DATE_TRUNC``/``TIMESTAMP_TRUNC``/``DATETIME_TRUNC`` units coarse enough for a signup
#: timestamp (MONTH or coarser). WEEK, DAY and ISOYEAR are not allowed (fail closed).
_COARSE_TS_UNITS: Final = frozenset({"MONTH", "QUARTER", "YEAR"})
_EMPTY: Final[frozenset[str]] = frozenset()


# --------------------------------------------------------------------------- result


class Rule(StrEnum):
    """Stable reason codes. Never contain SQL text; safe for logs and metrics."""

    OK = "ok"
    SQL_SYNTAX = "sql_syntax"
    SQL_TOO_LONG = "sql_too_long"
    MULTIPLE_STATEMENTS = "multiple_statements"
    STATEMENT_NOT_ALLOWED = "statement_not_allowed"
    UNSUPPORTED_SYNTAX = "unsupported_syntax"
    TOO_COMPLEX = "too_complex"
    NON_ASCII_IDENTIFIER = "non_ascii_identifier"
    IDENTIFIER_NOT_ALLOWED = "identifier_not_allowed"
    RECURSIVE_CTE = "recursive_cte"
    CTE_SHADOWS_TABLE = "cte_shadows_table"
    SOURCE_NOT_ALLOWED = "source_not_allowed"
    SELECT_STAR = "select_star"
    UNRESOLVED_VALUE = "unresolved_value"
    UNKNOWN_COLUMN = "unknown_column"
    AMBIGUOUS_COLUMN = "ambiguous_column"
    PII_PROJECTION = "pii_projection"
    PII_REFERENCE = "pii_reference"
    FUNCTION_DENIED = "function_denied"
    QI_POSITION = "qi_position"
    QI_AT_ID_GRAIN = "qi_at_id_grain"
    SMALL_CELL_UNPLACEABLE = "small_cell_unplaceable"
    QI_DIFFERENCING = "qi_differencing"


_HINTS: Final[MappingProxyType[Rule, str]] = MappingProxyType(
    {
        Rule.OK: "",
        Rule.SQL_SYNTAX: "the SQL does not parse as BigQuery SQL; fix the syntax",
        Rule.SQL_TOO_LONG: "the SQL is longer than 8000 characters; simplify it",
        Rule.MULTIPLE_STATEMENTS: "send exactly one SELECT statement",
        Rule.STATEMENT_NOT_ALLOWED: (
            "only a single SELECT (optionally with WITH) is allowed; "
            "remove any outer parentheses"
        ),
        Rule.UNSUPPORTED_SYNTAX: (
            "this SQL construct is not supported; use plain SELECT, JOIN ... ON, "
            "WHERE, GROUP BY, HAVING, ORDER BY and LIMIT"
        ),
        Rule.TOO_COMPLEX: "the query is too large or too deeply nested; simplify it",
        Rule.NON_ASCII_IDENTIFIER: "identifiers must use plain ASCII characters",
        Rule.IDENTIFIER_NOT_ALLOWED: (
            "identifiers may contain only letters, digits and underscores, with no "
            "escapes or spaces"
        ),
        Rule.RECURSIVE_CTE: "recursive CTEs are not allowed",
        Rule.CTE_SHADOWS_TABLE: (
            "rename the CTE: it may not reuse a table name or start with two underscores"
        ),
        Rule.SOURCE_NOT_ALLOWED: (
            "query only the tables orders, order_items, products and users of "
            "bigquery-public-data.thelook_ecommerce"
        ),
        Rule.SELECT_STAR: "name the columns you need",
        Rule.UNRESOLVED_VALUE: (
            "select columns, literals or expressions over them; no table aliases, "
            "STRUCT, ARRAY or TO_JSON values"
        ),
        Rule.UNKNOWN_COLUMN: "column not found; check the schema",
        Rule.AMBIGUOUS_COLUMN: "qualify the column with its table alias",
        Rule.PII_PROJECTION: "personal data columns cannot be selected",
        Rule.PII_REFERENCE: "personal data columns cannot be used anywhere in a query",
        Rule.FUNCTION_DENIED: "this function is not allowed here",
        Rule.QI_POSITION: (
            "customer attributes may only be grouped by, used in a filter of an "
            "aggregate, or counted as COUNT(DISTINCT column); do not compare or "
            "transform them inside an aggregate"
        ),
        Rule.QI_AT_ID_GRAIN: (
            "customer attributes cannot be used in a query that returns individual "
            "customers, orders or items; aggregate instead"
        ),
        Rule.SMALL_CELL_UNPLACEABLE: (
            "use a coarser grouping or aggregate over the whole population"
        ),
        Rule.QI_DIFFERENCING: (
            "compute one aggregate per query when grouping or filtering by customer "
            "attributes; do not combine, subtract or union several groupings or totals"
        ),
    }
)

_ERROR_CODES: Final[MappingProxyType[Rule, str]] = MappingProxyType(
    {
        Rule.SQL_SYNTAX: "SQL_SYNTAX",
        Rule.SQL_TOO_LONG: "SQL_TOO_LONG",
        Rule.UNKNOWN_COLUMN: "UNKNOWN_COLUMN",
    }
)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Outcome of :func:`check_sql`. ``reason_code`` and ``hint`` never contain SQL."""

    allowed: bool
    reason_code: Rule
    hint: str

    @property
    def error_code(self) -> str | None:
        """The ``run_sql`` error code (HLD §5.1): ``None`` when allowed."""
        if self.allowed:
            return None
        return _ERROR_CODES.get(self.reason_code, "SQL_POLICY")

    @property
    def rule(self) -> str | None:
        """The ``SQL_POLICY`` sub-rule for the error payload, else ``None``."""
        return self.reason_code.value if self.error_code == "SQL_POLICY" else None


_ALLOW: Final = PolicyDecision(allowed=True, reason_code=Rule.OK, hint="")


class _Reject(Exception):
    """Internal: carries the rule only, never SQL text."""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule.value)
        self.rule = rule


def _deny(rule: Rule) -> PolicyDecision:
    return PolicyDecision(allowed=False, reason_code=rule, hint=_HINTS[rule])


# --------------------------------------------------------------------------- allowlists


def _classes(*names: str) -> tuple[type[exp.Expression], ...]:
    """Resolve sqlglot class names; a name missing in this sqlglot version is dropped
    (fail closed: the construct then falls through to a rejection)."""
    return tuple(c for n in names if isinstance(c := getattr(exp, n, None), type))


_STATEMENTS: Final = _classes(
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command", "Export",
    "TruncateTable", "Use", "Set", "Transaction", "Commit", "Rollback", "Describe",
)

_STRUCTURE: Final = _classes(
    "Select", "Union", "Intersect", "Except", "Subquery", "With", "CTE", "TableAlias",
    "Table", "Identifier", "From", "Join", "Where", "Group", "Having", "Qualify", "Order",
    "Ordered", "Limit", "Offset", "Alias", "Column", "Star", "Literal", "Null", "Boolean",
    "Paren", "Distinct", "Window", "WindowSpec", "Var", "DataType", "DataTypeParam",
    "Interval", "Exists", "In", "Between", "Like", "Is", "Not", "And", "Or", "EQ", "NEQ",
    "GT", "GTE", "LT", "LTE", "Add", "Sub", "Mul", "Div", "Neg", "Case", "If",
)

_COUNTING: Final = _classes("Count", "CountIf")

#: Aggregates whose result is still an id when the argument is one.
_ID_PRESERVING_AGGS: Final = _classes(
    "Min", "Max", "AnyValue", "FirstValue", "LastValue", "NthValue", "Lag", "Lead",
    "GroupConcat",
)

_AGGREGATES: Final = _COUNTING + _ID_PRESERVING_AGGS + _classes(
    "Sum", "Avg", "ApproxDistinct", "Stddev", "StddevSamp", "StddevPop", "Variance",
    "VariancePop", "Median", "PercentileCont", "PercentileDisc", "LogicalAnd",
    "LogicalOr", "Corr",
)

_WINDOW_ONLY: Final = _classes(
    "RowNumber", "Rank", "DenseRank", "PercentRank", "CumeDist", "Ntile",
)

_STRING_FUNCS: Final = _classes(
    "Concat", "DPipe", "Substring", "Replace", "Pad", "Left", "Right", "StartsWith",
    "EndsWith", "Contains", "StrPosition", "Trim", "Lower", "Upper", "Initcap",
)

_CASTS: Final = _classes("Cast", "TryCast")
_REGEX_PARSE: Final = _classes(
    "RegexpLike", "RegexpExtract", "RegexpReplace", "StrToDate", "StrToTime",
)

_SCALARS: Final = _CASTS + _REGEX_PARSE + _STRING_FUNCS + _classes(
    "Round", "Ceil", "Floor", "Abs", "Sqrt", "Pow", "Ln", "Log", "Exp", "Sign", "Mod",
    "IntDiv", "Greatest", "Least", "SafeDivide", "SafeMultiply", "SafeAdd",
    "SafeSubtract", "Length", "Coalesce", "Nullif", "Date", "DateFromParts", "DateTrunc",
    "TimestampTrunc", "DatetimeTrunc", "DateAdd", "DateSub", "DateDiff", "TimestampAdd",
    "TimestampSub", "TimestampDiff", "DatetimeAdd", "DatetimeSub", "DatetimeDiff",
    "Extract", "CurrentDate", "CurrentTimestamp", "CurrentDatetime", "LastDay",
    "TimeToStr", "TsOrDsToDate", "TsOrDsToTimestamp", "TsOrDsToDatetime", "Timestamp",
    "WeekStart",
)

_ALLOWED_NODES: Final = _STRUCTURE + _AGGREGATES + _WINDOW_ONLY + _SCALARS

#: Value constructors the HLD names explicitly (rule ``unresolved_value``).
_VALUE_CONSTRUCTORS: Final = _classes(
    "Struct", "Array", "JSONFormat", "ArrayAgg", "Bracket", "ToArray",
)

_COMPARISONS: Final = _classes("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "NullSafeEQ")
_CONDITIONALS: Final = _classes("If", "Case")
_QUERY_NODES: Final = (exp.Select, exp.SetOperation)


# --------------------------------------------------------------------------- entry point


def check_sql(sql: str) -> PolicyDecision:
    """Check one model-written statement. Pure; fails closed on anything unexpected."""
    try:
        _check(sql)
    except _Reject as rej:
        return _deny(rej.rule)
    except (SqlglotError, RecursionError):
        return _deny(Rule.SQL_SYNTAX)
    except Exception:  # noqa: BLE001 - fail closed on any analyser bug
        return _deny(Rule.UNSUPPORTED_SYNTAX)
    return _ALLOW


def regenerate_sql(sql: str) -> str:
    """Canonical SQL for an allowed statement: the AST printed as BigQuery, no comments.

    Raises :class:`ValueError` (message: the reason code only) if ``check_sql`` denies it.
    """
    decision = check_sql(sql)
    if not decision.allowed:
        raise ValueError(decision.reason_code.value)
    return _parse(sql).sql(dialect="bigquery", comments=False)


def _check(sql: str) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise _Reject(Rule.SQL_SYNTAX)
    if len(sql) > MAX_SQL_CHARS:
        raise _Reject(Rule.SQL_TOO_LONG)
    _check_tokens(sql)
    root = _parse(sql)
    _check_size(root)
    _check_statements(root)
    _check_sources(root)
    _check_ctes(root)
    _check_nodes(root)
    _check_aliases(root)
    _Analyzer(root).run()


# --------------------------------------------------------------------------- steps 2-4

_START_TOKENS: Final = frozenset({TokenType.SELECT, TokenType.WITH, TokenType.L_PAREN})
_NAME_TOKENS: Final = frozenset({TokenType.IDENTIFIER, TokenType.VAR})
_IDENT_PART: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TABLE_PATH_CHARS: Final = re.compile(r"[A-Za-z0-9_*-]+(?:\.[A-Za-z0-9_*-]+)*")


def _identifier_ok(text: str) -> bool:
    """Whitelist an identifier's text: dot-separated plain parts; only the leading
    project part may be ``bigquery-public-data``."""
    parts = text.split(".")
    return all(
        _IDENT_PART.fullmatch(part) is not None or (i == 0 and part == PROJECT)
        for i, part in enumerate(parts)
    )


def _check_tokens(sql: str) -> None:
    try:
        tokens = sqlglot.tokenize(sql, read="bigquery")
    except SqlglotError as err:
        raise _Reject(Rule.SQL_SYNTAX) from err
    if not tokens:
        raise _Reject(Rule.SQL_SYNTAX)
    for tok in tokens:
        if tok.token_type in _NAME_TOKENS and not tok.text.isascii():
            raise _Reject(Rule.NON_ASCII_IDENTIFIER)
    for tok in tokens:
        if tok.token_type in _NAME_TOKENS and not _identifier_ok(tok.text):
            # A plain table-like path (another project, a wildcard) reads better as a
            # source error; anything with escapes, spaces or punctuation does not.
            path_like = _TABLE_PATH_CHARS.fullmatch(tok.text) is not None
            raise _Reject(Rule.SOURCE_NOT_ALLOWED if path_like else Rule.IDENTIFIER_NOT_ALLOWED)
    for i, tok in enumerate(tokens):
        if tok.token_type == TokenType.SEMICOLON and i != len(tokens) - 1:
            raise _Reject(Rule.MULTIPLE_STATEMENTS)
    if tokens[0].token_type not in _START_TOKENS:
        raise _Reject(Rule.STATEMENT_NOT_ALLOWED)


def _parse(sql: str) -> exp.Expression:
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except (SqlglotError, RecursionError) as err:
        raise _Reject(Rule.SQL_SYNTAX) from err
    # A trailing Semicolon node only carries comments after the final `;` (the token
    # check guarantees nothing but comments follows it).
    statements = [s for s in statements if not isinstance(s, exp.Semicolon)]
    statements = [s for s in statements if s is not None] if len(statements) == 1 else statements
    if len(statements) != 1 or statements[0] is None:
        raise _Reject(Rule.MULTIPLE_STATEMENTS if len(statements) > 1 else Rule.SQL_SYNTAX)
    root = statements[0]
    if not isinstance(root, _QUERY_NODES):
        # Includes the parenthesised top level `(SELECT ...)` (a bare Subquery root).
        raise _Reject(Rule.STATEMENT_NOT_ALLOWED)
    return root


def _check_size(root: exp.Expression) -> None:
    count = 0
    stack: list[tuple[exp.Expression, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise _Reject(Rule.TOO_COMPLEX)
        stack.extend((child, depth + 1) for child in node.iter_expressions())


def _iter_nodes(root: exp.Expression) -> Iterator[exp.Expression]:
    """Iterative pre-order walk (no recursion)."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(list(node.iter_expressions())))


# --------------------------------------------------------------------------- steps 5-8


def _check_statements(root: exp.Expression) -> None:
    for node in _iter_nodes(root):
        if isinstance(node, _STATEMENTS):
            raise _Reject(Rule.STATEMENT_NOT_ALLOWED)


_TABLE_ARGS: Final = frozenset({"this", "db", "catalog", "alias"})


def _visible_ctes(table: exp.Table) -> set[str]:
    """CTE names defined in any WITH that encloses ``table`` (exact case)."""
    names: set[str] = set()
    node: exp.Expression | None = table
    while node is not None:
        with_ = node.args.get("with_") if isinstance(node, _QUERY_NODES) else None
        if isinstance(with_, exp.With):
            names.update(cte.alias for cte in with_.expressions)
        node = node.parent
    return names


def _check_sources(root: exp.Expression) -> None:
    for node in _iter_nodes(root):
        if isinstance(node, (exp.Parameter, exp.Placeholder, exp.SessionParameter)):
            raise _Reject(Rule.SOURCE_NOT_ALLOWED)
        if isinstance(node, (exp.Unnest, exp.Lateral, exp.TableSample)):
            raise _Reject(Rule.SOURCE_NOT_ALLOWED)
        if isinstance(node, exp.Table):
            _check_table(node)


def _check_table(table: exp.Table) -> None:
    extra = {k for k, v in table.args.items() if v not in (None, [], False)} - _TABLE_ARGS
    if extra or not isinstance(table.this, exp.Identifier):
        raise _Reject(Rule.SOURCE_NOT_ALLOWED)
    for part in ("db", "catalog"):
        value = table.args.get(part)
        if value is not None and not isinstance(value, exp.Identifier):
            raise _Reject(Rule.SOURCE_NOT_ALLOWED)
    name, db, catalog = table.name, table.db, table.catalog
    if db or catalog:
        if db != DATASET or catalog not in ("", PROJECT) or name not in ALLOWED_TABLES:
            raise _Reject(Rule.SOURCE_NOT_ALLOWED)
        return
    if name not in ALLOWED_TABLES and name not in _visible_ctes(table):
        raise _Reject(Rule.SOURCE_NOT_ALLOWED)


def _check_ctes(root: exp.Expression) -> None:
    reserved = {n.lower() for n in ALLOWED_TABLES}
    for node in _iter_nodes(root):
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise _Reject(Rule.RECURSIVE_CTE)
        if isinstance(node, exp.CTE):
            name = node.alias
            if not name or name.lower() in reserved or name.startswith("__"):
                raise _Reject(Rule.CTE_SHADOWS_TABLE)
            alias = node.args.get("alias")
            if isinstance(alias, exp.TableAlias) and alias.columns:
                raise _Reject(Rule.UNSUPPORTED_SYNTAX)


def _check_nodes(root: exp.Expression) -> None:
    for node in _iter_nodes(root):
        if isinstance(node, exp.Select) and node.args.get("kind"):
            raise _Reject(Rule.UNRESOLVED_VALUE)  # SELECT AS STRUCT / AS VALUE
        if isinstance(node, exp.Star):
            if not isinstance(node.parent, exp.Count) or node.parent.this is not node:
                raise _Reject(Rule.SELECT_STAR)
            continue
        if isinstance(node, _VALUE_CONSTRUCTORS):
            raise _Reject(Rule.UNRESOLVED_VALUE)
        if isinstance(node, exp.Join) and (
            node.args.get("using") or node.args.get("method")
        ):
            raise _Reject(Rule.UNSUPPORTED_SYNTAX)
        if isinstance(node, exp.TableAlias) and node.columns:
            raise _Reject(Rule.UNSUPPORTED_SYNTAX)
        if isinstance(node, exp.Identifier) and not _identifier_ok(node.name):
            raise _Reject(Rule.IDENTIFIER_NOT_ALLOWED)  # defence in depth after tokens
        if type(node) in _ALLOWED_NODE_SET:
            continue
        if isinstance(node, (exp.Func, exp.Dot)):
            raise _Reject(Rule.FUNCTION_DENIED)
        raise _Reject(Rule.UNSUPPORTED_SYNTAX)


_PII_NAMES: Final = frozenset().union(*PII_COLUMNS.values())


def _check_aliases(root: exp.Expression) -> None:
    """No output, table, derived-table or CTE alias may be named like a PII column."""
    for node in _iter_nodes(root):
        if isinstance(node, exp.Alias) and node.alias.lower() in _PII_NAMES:
            raise _Reject(Rule.PII_PROJECTION)
        if isinstance(node, exp.TableAlias) and node.name.lower() in _PII_NAMES:
            raise _Reject(Rule.PII_REFERENCE)


# Exact-type allowlist: a subclass of an allowed class is not implicitly allowed.
_ALLOWED_NODE_SET: Final = frozenset(_ALLOWED_NODES)


# --------------------------------------------------------------------------- steps 9-10


@dataclass(slots=True)
class _ScopeInfo:
    outputs: dict[str, frozenset[str]]
    output_list: list[frozenset[str]]
    row_grain: bool


class _Analyzer:
    """Column resolution, PII and QI rules over the sqlglot scope tree."""

    def __init__(self, root: exp.Expression) -> None:
        self.root = root
        scope = build_scope(root)
        if scope is None:
            raise _Reject(Rule.UNSUPPORTED_SYNTAX)
        self.root_scope: Scope = scope
        self.col_taint: dict[int, frozenset[str]] = {}
        self.infos: dict[int, _ScopeInfo] = {}
        self.in_progress: set[int] = set()
        self.scope_of_expr: dict[int, Scope] = {}
        self.references_qi = False

    # -- driver

    def run(self) -> None:
        scopes = list(self.root_scope.traverse())
        for scope in scopes:
            self.scope_of_expr[id(scope.expression)] = scope
        for scope in scopes:
            self._analyze(scope)
        columns = [n for n in _iter_nodes(self.root) if isinstance(n, exp.Column)]
        if any(id(c) not in self.col_taint for c in columns):
            raise _Reject(Rule.UNSUPPORTED_SYNTAX)  # a column no scope accounted for
        if any(_QI in self.col_taint[id(c)] for c in columns):
            self.references_qi = True
        self._check_functions()
        self._check_positions()
        self._check_id_literals()
        self._check_grain(scopes)
        self._check_timestamps()

    # -- scope analysis

    def _analyze(self, scope: Scope) -> _ScopeInfo:
        key = id(scope.expression)
        if key in self.infos:
            return self.infos[key]
        if key in self.in_progress:
            raise _Reject(Rule.UNSUPPORTED_SYNTAX)
        self.in_progress.add(key)
        for child in (
            *scope.cte_scopes,
            *scope.derived_table_scopes,
            *scope.subquery_scopes,
            *scope.set_operation_scopes,
            *scope.udtf_scopes,
        ):
            self._analyze(child)
        expr = scope.expression
        if isinstance(expr, exp.SetOperation):
            info = self._set_operation_info(scope)
        elif isinstance(expr, exp.Select):
            info = self._select_info(scope)
        else:
            raise _Reject(Rule.UNSUPPORTED_SYNTAX)
        self.in_progress.discard(key)
        self.infos[key] = info
        return info

    def _scope_columns(self, scope: Scope) -> list[exp.Column]:
        return [n for n in scope.walk() if isinstance(n, exp.Column)]

    def _late_clause(self, col: exp.Column, select: exp.Expression) -> bool:
        """True when ``col`` sits in this query's GROUP BY / HAVING / QUALIFY / ORDER BY,
        where a select-list alias may be referenced."""
        late = [select.args.get(k) for k in ("group", "having", "qualify", "order")]
        late_ids = {id(n) for n in late if n is not None}
        node: exp.Expression | None = col
        while node is not None and node is not select:
            if id(node) in late_ids:
                return True
            node = node.parent
        return False

    def _select_info(self, scope: Scope) -> _ScopeInfo:
        select = scope.expression
        columns = self._scope_columns(scope)
        early = [c for c in columns if not self._late_clause(c, select)]
        late = [c for c in columns if self._late_clause(c, select)]
        for col in early:
            self.col_taint[id(col)] = self._resolve(col, scope, aliases=None)
        output_list: list[frozenset[str]] = []
        outputs: dict[str, frozenset[str]] = {}
        for proj in select.expressions:
            taint = self.taint(proj)
            output_list.append(taint)
            name = proj.alias_or_name.lower()
            if name:
                outputs[name] = outputs.get(name, _EMPTY) | taint
        for col in late:
            self.col_taint[id(col)] = self._resolve(col, scope, aliases=outputs)
        return _ScopeInfo(outputs, output_list, self._row_grain(scope, output_list))

    def _set_operation_info(self, scope: Scope) -> _ScopeInfo:
        branches = list(_branch_selects(scope.expression))
        widths = {len(b.expressions) for b in branches}
        if len(widths) != 1:
            raise _Reject(Rule.UNSUPPORTED_SYNTAX)
        infos = [self._analyze(self._scope_for(b)) for b in branches]
        width = widths.pop()
        output_list = [
            frozenset().union(*(i.output_list[pos] for i in infos)) for pos in range(width)
        ]
        names = [p.alias_or_name.lower() for p in branches[0].expressions]
        outputs = {n: t for n, t in zip(names, output_list, strict=True) if n}
        for col in self._scope_columns(scope):  # ORDER BY over the set operation
            self.col_taint[id(col)] = self._resolve(col, scope, aliases=outputs)
        return _ScopeInfo(outputs, output_list, any(i.row_grain for i in infos))

    def _scope_for(self, expr: exp.Expression) -> Scope:
        scope = self.scope_of_expr.get(id(expr))
        if scope is None:
            raise _Reject(Rule.UNSUPPORTED_SYNTAX)
        return scope

    # -- column resolution

    def _resolve(
        self, col: exp.Column, scope: Scope, aliases: dict[str, frozenset[str]] | None
    ) -> frozenset[str]:
        if col.args.get("db") is not None or col.args.get("catalog") is not None:
            raise _Reject(Rule.UNRESOLVED_VALUE)  # struct path or over-qualified name
        if not isinstance(col.this, exp.Identifier):
            raise _Reject(Rule.UNRESOLVED_VALUE)
        name = col.name.lower()
        qualifier = col.table
        taint: frozenset[str] | None = None
        if qualifier:
            current: Scope | None = scope
            while current is not None and taint is None:
                source = _source_by_alias(current, qualifier)
                if source is not None:
                    taint = self._column_of(source, name)
                    if taint is None:
                        raise _Reject(Rule.UNKNOWN_COLUMN)
                current = current.parent
            if taint is None:
                raise _Reject(Rule.UNKNOWN_COLUMN)
        else:
            taint = self._resolve_unqualified(name, scope, aliases)
        if _PII in taint:
            raise _Reject(
                Rule.PII_PROJECTION if _in_select_list(col, scope) else Rule.PII_REFERENCE
            )
        return taint

    def _resolve_unqualified(
        self, name: str, scope: Scope, aliases: dict[str, frozenset[str]] | None
    ) -> frozenset[str]:
        current: Scope | None = scope
        while current is not None:
            selected = _selected_sources(current)
            if name in {alias.lower() for alias in selected}:
                raise _Reject(Rule.UNRESOLVED_VALUE)  # table or CTE alias used as a value
            hits = [
                t for t in (self._column_of(src, name) for src in selected.values())
                if t is not None
            ]
            if len(hits) > 1:
                raise _Reject(Rule.AMBIGUOUS_COLUMN)
            if hits:
                return hits[0]
            if current is scope and aliases is not None and name in aliases:
                return aliases[name]
            current = current.parent
        raise _Reject(Rule.UNKNOWN_COLUMN)

    def _column_of(self, source: object, name: str) -> frozenset[str] | None:
        if isinstance(source, exp.Table):
            table = source.name
            if table not in ALLOWED_TABLES or name not in ALLOWED_TABLES[table]:
                return None
            tags: set[str] = set()
            if name in PII_COLUMNS.get(table, _EMPTY):
                tags.add(_PII)
            if name in QI_COLUMNS.get(table, _EMPTY):
                tags.add(_QI)
            if name in _ID_KEYS.get(table, _EMPTY):
                tags.add(_IDKEY)
            if table == "users" and name == "created_at":
                tags.add(_RAWTS)
            return frozenset(tags)
        if isinstance(source, Scope):
            return self._analyze(source).outputs.get(name)
        raise _Reject(Rule.SOURCE_NOT_ALLOWED)

    # -- taint of an expression

    def taint(self, node: exp.Expression) -> frozenset[str]:
        """QI / id-key lineage of an expression's value."""
        if isinstance(node, exp.Column):
            found = self.col_taint.get(id(node))
            if found is None:
                raise _Reject(Rule.UNSUPPORTED_SYNTAX)
            return found
        if isinstance(node, _QUERY_NODES) and node is not self.root:
            return frozenset().union(*self._analyze(self._scope_for(node)).output_list)
        if isinstance(node, _COUNTING) or isinstance(node, _WINDOW_ONLY):
            return _EMPTY
        children = frozenset().union(*(self.taint(c) for c in node.iter_expressions()))
        if _QI in children and not isinstance(node, (exp.Alias, exp.Paren)):
            children = children | {_QIEXPR}
        if _coarse_truncation(node):
            return children - {_RAWTS}
        if isinstance(node, _AGGREGATES) and not isinstance(node, _ID_PRESERVING_AGGS):
            return children - {_IDKEY}
        return children

    def _raw_taint(self, node: exp.Expression) -> frozenset[str]:
        """Union of every column's lineage inside ``node`` (counting does not hide)."""
        return frozenset().union(
            *(self.col_taint.get(id(n), _EMPTY) for n in _iter_nodes(node)
              if isinstance(n, exp.Column))
        )

    # -- QI rules

    def _check_functions(self) -> None:
        for node in _iter_nodes(self.root):
            if isinstance(node, exp.GroupConcat) and _IDKEY in self._raw_taint(node):
                raise _Reject(Rule.FUNCTION_DENIED)  # id-packing: every id in one cell
            if isinstance(node, _CASTS + _REGEX_PARSE) and _QI in self._raw_taint(node):
                raise _Reject(Rule.FUNCTION_DENIED)
            if (
                isinstance(node, _STRING_FUNCS)
                and _QI in self._raw_taint(node)
                and any(isinstance(n, exp.Literal) for n in _iter_nodes(node))
                and not self._is_group_key(node)
            ):
                raise _Reject(Rule.FUNCTION_DENIED)

    def _is_group_key(self, node: exp.Expression) -> bool:
        select = node.find_ancestor(exp.Select)
        if select is None:
            return False
        group = select.args.get("group")
        if group is None:
            return False
        parent: exp.Expression | None = node
        while parent is not None and parent is not select:
            if parent is group:
                return True
            parent = parent.parent
        projection = _projection_containing(node, select)
        if projection is None:
            return False
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        position = select.expressions.index(projection) + 1
        alias = projection.alias.lower() if isinstance(projection, exp.Alias) else ""
        for key in group.expressions:
            if isinstance(key, exp.Literal) and not key.is_string and key.this == str(position):
                return True
            by_alias = isinstance(key, exp.Column) and not key.table and bool(alias)
            if by_alias and key.name.lower() == alias:
                return True
            if key == inner:
                return True
        return bool(group.args.get("all")) and not any(
            isinstance(n, _AGGREGATES) for n in _iter_nodes(inner)
        )

    def _check_positions(self) -> None:
        for node in _iter_nodes(self.root):
            if isinstance(node, exp.Window) and _QI in self._raw_taint(node):
                raise _Reject(Rule.QI_POSITION)
            if not isinstance(node, _AGGREGATES):
                continue
            if not isinstance(node, _COUNTING) and _QI in self._raw_taint(node):
                raise _Reject(Rule.QI_POSITION)
            if isinstance(node, _COUNTING) and not self._qi_count_allowed(node):
                raise _Reject(Rule.QI_POSITION)
            for inner in _iter_nodes(node):
                if inner is node:
                    continue
                if isinstance(inner, _CONDITIONALS) and _QI in self._raw_taint(inner):
                    raise _Reject(Rule.QI_POSITION)
                if isinstance(inner, (*_CONDITIONALS, exp.CountIf)) and self._has_id_literal(
                    inner
                ):
                    raise _Reject(Rule.QI_POSITION)
            if isinstance(node, exp.CountIf) and self._has_id_literal(node):
                raise _Reject(Rule.QI_POSITION)

    def _qi_count_allowed(self, node: exp.Expression) -> bool:
        """Review B1: a QI inside ``COUNT`` only as ``COUNT(DISTINCT <bare QI column>)``."""
        if _QI not in self._raw_taint(node):
            return True
        if isinstance(node, exp.CountIf):
            return False
        arg = node.this
        if not isinstance(arg, exp.Distinct) or len(arg.expressions) != 1:
            return False
        col = arg.expressions[0]
        if not isinstance(col, exp.Column):
            return False
        return _QIEXPR not in self.col_taint.get(id(col), _EMPTY)

    def _has_id_literal(self, node: exp.Expression) -> bool:
        return any(self._is_id_literal_cmp(n) for n in _iter_nodes(node))

    def _is_id_literal_cmp(self, node: exp.Expression) -> bool:
        if isinstance(node, _COMPARISONS):
            left, right = node.this, node.expression
            return (_IDKEY in self.taint(left) and _is_constant(right)) or (
                _IDKEY in self.taint(right) and _is_constant(left)
            )
        if isinstance(node, exp.In):
            values = node.expressions
            return (
                bool(values)
                and node.args.get("query") is None
                and _IDKEY in self.taint(node.this)
                and all(_is_constant(v) for v in values)
            )
        if isinstance(node, exp.Between):
            return (
                _IDKEY in self.taint(node.this)
                and _is_constant(node.args["low"])
                and _is_constant(node.args["high"])
            )
        return False

    def _check_id_literals(self) -> None:
        if not self.references_qi:
            return
        if any(self._is_id_literal_cmp(n) for n in _iter_nodes(self.root)):
            raise _Reject(Rule.SMALL_CELL_UNPLACEABLE)

    def _check_timestamps(self) -> None:
        """``users.created_at`` (iteration 9, D-29): a raw signup timestamp may only be
        used inside ``DATE_TRUNC``/``TIMESTAMP_TRUNC``/``DATETIME_TRUNC`` to MONTH,
        QUARTER or YEAR (optionally through ``DATE(...)``), or in a ``WHERE`` filter.
        Anything else (grouping or projecting it raw, ``EXTRACT``, WEEK/DAY truncation,
        ``JOIN ... ON``, ``HAVING``, ``ORDER BY``, ``COUNT(created_at)``, a CTE that
        passes it through untruncated) is ``qi_position``. Code CTEs (names starting with
        ``__``, which ``check_sql`` never accepts from the model) are exempt."""
        for node in _iter_nodes(self.root):
            if not isinstance(node, exp.Column):
                continue
            if _RAWTS not in self.col_taint.get(id(node), _EMPTY):
                continue
            if _in_code_cte(node) or _ts_use_allowed(node):
                continue
            raise _Reject(Rule.QI_POSITION)

    def _row_grain(self, scope: Scope, output_list: list[frozenset[str]]) -> bool:
        select = scope.expression
        group = select.args.get("group")
        if group is not None:
            if group.args.get("all"):
                keys = [p for p in select.expressions if not _contains_aggregate(p)]
            else:
                keys = [_group_key_target(k, select) for k in group.expressions]
            return any(_IDKEY in self.taint(k) for k in keys)
        if any(_contains_aggregate(p) for p in select.expressions):
            return False
        if select.args.get("having") is not None:
            return False
        for source in _selected_sources(scope).values():
            if isinstance(source, exp.Table):
                return True
            if isinstance(source, Scope) and self._analyze(source).row_grain:
                return True
        return False

    def _check_grain(self, scopes: list[Scope]) -> None:
        root_info = self._analyze(self.root_scope)
        if self.references_qi and (
            root_info.row_grain or any(_IDKEY in t for t in root_info.output_list)
        ):
            # Row grain, or an id surfaced through MIN/MAX/ANY_VALUE/STRING_AGG.
            raise _Reject(Rule.QI_AT_ID_GRAIN)
        for scope in scopes:
            if not scope.is_subquery:
                continue
            info = self._analyze(scope)
            if info.row_grain and any(_QI in t for t in info.output_list):
                raise _Reject(Rule.QI_AT_ID_GRAIN)


# --------------------------------------------------------------------------- helpers


_TRUNCS: Final = _classes("DateTrunc", "TimestampTrunc", "DatetimeTrunc")
_TS_WRAPPERS: Final = (exp.Paren, *_classes("Date", "TsOrDsToDate"))


def _coarse_truncation(node: exp.Expression) -> bool:
    if not isinstance(node, _TRUNCS):
        return False
    unit = node.args.get("unit")
    if not isinstance(unit, (exp.Literal, exp.Var)):
        return False
    return str(unit.this).upper() in _COARSE_TS_UNITS


def _ts_use_allowed(col: exp.Column) -> bool:
    """A coarse truncation reached through only ``DATE()``/parentheses, via ``this``;
    or a position inside the nearest SELECT's WHERE."""
    child: exp.Expression = col
    node = col.parent
    while node is not None and not isinstance(node, _QUERY_NODES):
        if isinstance(node, _TS_WRAPPERS) and child is node.this:
            child, node = node, node.parent
            continue
        if _coarse_truncation(node) and child is node.this:
            return True
        break
    node = col.parent
    while node is not None and not isinstance(node, _QUERY_NODES):
        if isinstance(node, exp.Where):
            return True
        node = node.parent
    return False


def _in_code_cte(node: exp.Expression) -> bool:
    current: exp.Expression | None = node
    while current is not None:
        if isinstance(current, exp.CTE) and current.alias.startswith("__"):
            return True
        current = current.parent
    return False


def _branch_selects(node: exp.Expression) -> Iterator[exp.Select]:
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.Subquery):
            stack.append(current.this)
        elif isinstance(current, exp.SetOperation):
            stack.extend([current.expression, current.this])
        elif isinstance(current, exp.Select):
            yield current
        else:
            raise _Reject(Rule.UNSUPPORTED_SYNTAX)


def _selected_sources(scope: Scope) -> dict[str, object]:
    """Sources in this scope's FROM / JOIN, by alias (not every visible CTE)."""
    selected = {alias: pair[1] for alias, pair in scope.selected_sources.items()}
    lowered = [a.lower() for a in selected]
    if len(set(lowered)) != len(lowered):
        raise _Reject(Rule.AMBIGUOUS_COLUMN)
    return selected


def _source_by_alias(scope: Scope, qualifier: str) -> object | None:
    wanted = qualifier.lower()
    for alias, source in _selected_sources(scope).items():
        if alias.lower() == wanted:
            return source
    return None


def _in_select_list(col: exp.Column, scope: Scope) -> bool:
    select = scope.expression
    return isinstance(select, exp.Select) and _projection_containing(col, select) is not None


def _projection_containing(node: exp.Expression, select: exp.Select) -> exp.Expression | None:
    projections = {id(p): p for p in select.expressions}
    current: exp.Expression | None = node
    while current is not None and current is not select:
        if id(current) in projections and current.parent is select:
            return current
        current = current.parent
    return None


def _group_key_target(key: exp.Expression, select: exp.Select) -> exp.Expression:
    """Map a GROUP BY ordinal or select-alias reference to the projection it names."""
    if isinstance(key, exp.Literal) and not key.is_string and key.this.isdigit():
        position = int(key.this)
        if 1 <= position <= len(select.expressions):
            return select.expressions[position - 1]
        raise _Reject(Rule.UNSUPPORTED_SYNTAX)
    return key


def _contains_aggregate(node: exp.Expression) -> bool:
    """An aggregate in this query level (not inside a window or a nested query)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (*_QUERY_NODES, exp.Window)) and current is not node:
            continue
        if isinstance(current, _AGGREGATES) and not isinstance(current.parent, exp.Window):
            return True
        stack.extend(current.iter_expressions())
    return False


def _is_constant(node: exp.Expression) -> bool:
    """No column and no query inside: a literal or an expression over literals."""
    return not any(isinstance(n, (exp.Column, *_QUERY_NODES)) for n in _iter_nodes(node))
