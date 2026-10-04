"""Brand-scope rewrite and post-rewrite invariant (iteration 7; HLD §5.3 steps 8 and 10).

Pipeline (:func:`apply_scope`):

1. validate the session scope (an empty brand list without the explicit ``all`` flag is
   refused: fail closed, never "no filter");
2. :func:`opsfleet_agent.guards.sql_policy.check_sql` (steps 1-7);
3. :func:`scope_rewrite` on the AST: every reference to an allowlisted table, in any of the
   three name forms (``bigquery-public-data.thelook_ecommerce.X`` with any backtick quoting,
   ``thelook_ecommerce.X``, bare ``X``) and in any CTE, subquery, join or set operation, is
   replaced by the code CTE ``__p`` / ``__oi`` / ``__o`` / ``__u`` (keeping its alias, or
   aliased to the short table name), and the needed code CTEs are prepended to the root
   ``WITH``;
4. the SQL is regenerated from the AST (BigQuery dialect, comments dropped), never patched
   as text;
5. :func:`verify_scoped` re-parses the output and proves the invariant: base tables appear
   only inside code CTE bodies that match their template exactly, and every other table
   reference resolves to a visible CTE. Otherwise the query is refused with ``SQL_POLICY``
   rule ``rewrite_invariant`` and logged at ERROR (no SQL in the log).

Brand values never enter the SQL text: they are bound as the array query parameter
``@scope_brands`` (:attr:`ScopedQuery.parameters`). The caller (iteration 13) must pass
:meth:`ScopedQuery.bigquery_parameters` to both the dry-run and the real job.

Nothing here does I/O except the ERROR log line; every walk is iterative and bounded by
the policy's node cap.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from opsfleet_agent.guards.scope_ctes import (
    CODE_CTE_FOR_TABLE,
    CODE_CTE_ORDER,
    SCOPE_PARAM,
    build_cte,
    cte_body,
    dependencies,
    required_ctes,
)
from opsfleet_agent.guards.sql_policy import (
    ALLOWED_TABLES,
    DATASET,
    MAX_NODES,
    PROJECT,
    PolicyDecision,
    Rule,
    check_sql,
)

__all__ = [
    "MAX_BRAND_CHARS",
    "ProductScope",
    "ScopeError",
    "ScopeInvariantError",
    "ScopeParameter",
    "ScopeRefusal",
    "ScopeRule",
    "ScopedQuery",
    "apply_scope",
    "scope_rewrite",
    "verify_scoped",
]

_log = logging.getLogger(__name__)

MAX_BRAND_CHARS: Final = 100
_CTRL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
_QUERY_NODES: Final = (exp.Select, exp.SetOperation)
_TABLE_ARGS: Final = frozenset({"this", "db", "catalog", "alias"})
_BASE_NAMES_CI: Final = frozenset(n.lower() for n in ALLOWED_TABLES)
_PARAM_NODES: Final = (exp.Parameter, exp.Placeholder, exp.SessionParameter, exp.Unnest)


# --------------------------------------------------------------------------- scope


class ScopeError(ValueError):
    """An invalid scope. The message is static text, never a brand value."""


@dataclass(frozen=True, slots=True)
class ProductScope:
    """The session's product scope: 1..N brands, or the explicit CEO ``all`` flag (A-17).

    ``all`` is never inferred from an empty list: ``ProductScope(brands=())`` is invalid.
    """

    brands: tuple[str, ...] = ()
    all_products: bool = False

    def __post_init__(self) -> None:
        _validate_scope(self.brands, self.all_products)

    @classmethod
    def for_brands(cls, brands: Any) -> ProductScope:
        if isinstance(brands, str) or brands is None:
            raise ScopeError("brands must be a sequence of brand names")
        return cls(brands=tuple(brands), all_products=False)

    @classmethod
    def all(cls) -> ProductScope:
        return cls(brands=(), all_products=True)

    @classmethod
    def from_profile(cls, profile: Any) -> ProductScope:
        """Build from ``session.Profile`` (duck-typed: ``brands``, ``all_products``).

        Fails closed: a profile with ``all_products: true`` **and** a non-empty brand list is
        ambiguous and raises :class:`ScopeError` (never silently widened to all products).
        ``from_profile(None)`` also raises (no brands). A session with no profile does not
        call this: callers pass ``scope=None`` to :func:`apply_scope`, which refuses with
        ``empty_scope``.
        """
        if profile is None:
            raise ScopeError("no profile: refusing to build a scope")
        all_flag = getattr(profile, "all_products", False)
        if all_flag is True:
            if getattr(profile, "brands", ()):
                raise ScopeError("the all-products scope takes no brand list")
            return cls.all()
        if all_flag is not False:
            raise ScopeError("all_products must be a bool")
        return cls.for_brands(getattr(profile, "brands", ()))

    @property
    def scoped(self) -> bool:
        """True when the brand ``WHERE`` filters apply (i.e. not the ``all`` scope)."""
        return not self.all_products

    @property
    def scope_key(self) -> str:
        """Stable cache-key component (``"all"`` or a hash of the sorted brand set)."""
        if self.all_products:
            return "all"
        material = "\x1f".join(sorted(self.brands)).encode("utf-8")
        return "brands:" + hashlib.sha256(material).hexdigest()[:32]


def _validate_scope(brands: Any, all_products: Any) -> None:
    if not isinstance(all_products, bool):
        raise ScopeError("all_products must be a bool")
    if not isinstance(brands, tuple):
        raise ScopeError("brands must be a tuple")
    if all_products:
        if brands:
            raise ScopeError("the all-products scope takes no brand list")
        return
    if not brands:
        raise ScopeError("empty brand list: refusing to run unscoped")
    for brand in brands:
        if not isinstance(brand, str) or not brand.strip():
            raise ScopeError("each brand must be a non-empty string")
        if len(brand) > MAX_BRAND_CHARS or _CTRL_RE.search(brand):
            raise ScopeError("brand name too long or has control characters")
    if len(set(brands)) != len(brands):
        raise ScopeError("duplicate brand in scope")


# --------------------------------------------------------------------------- results


class ScopeRule(StrEnum):
    """``SQL_POLICY`` sub-rules added by this module."""

    EMPTY_SCOPE = "empty_scope"
    SCOPE_INVALID = "scope_invalid"
    REWRITE_INVARIANT = "rewrite_invariant"


_SCOPE_HINTS: Final = {
    ScopeRule.EMPTY_SCOPE: "no product scope is configured for this session; nothing can run",
    ScopeRule.SCOPE_INVALID: "the session's product scope is invalid; nothing can run",
    ScopeRule.REWRITE_INVARIANT: "the query could not be scoped safely; it was not run",
}


@dataclass(frozen=True, slots=True)
class ScopeParameter:
    """One BigQuery query parameter (``ARRAY<STRING>``). Values are never put in SQL text."""

    name: str
    values: tuple[str, ...]

    def to_bigquery(self) -> Any:
        """A ``google.cloud.bigquery.ArrayQueryParameter`` (imported lazily)."""
        from google.cloud import bigquery

        return bigquery.ArrayQueryParameter(self.name, "STRING", list(self.values))


@dataclass(frozen=True, slots=True)
class ScopedQuery:
    """A scoped statement ready for dry-run and execution."""

    sql: str
    parameters: tuple[ScopeParameter, ...]
    scope_key: str

    def bigquery_parameters(self) -> list[Any]:
        """``query_parameters`` for ``QueryJobConfig`` (dry-run **and** real run)."""
        return [p.to_bigquery() for p in self.parameters]


@dataclass(frozen=True, slots=True)
class ScopeRefusal:
    """A refused query. Mirrors :class:`PolicyDecision`; never contains SQL or brands."""

    reason_code: str
    hint: str
    error_code: str = "SQL_POLICY"

    @property
    def allowed(self) -> bool:
        return False

    @property
    def rule(self) -> str | None:
        return self.reason_code if self.error_code == "SQL_POLICY" else None

    @classmethod
    def from_policy(cls, decision: PolicyDecision) -> ScopeRefusal:
        return cls(
            reason_code=decision.reason_code.value,
            hint=decision.hint,
            error_code=decision.error_code or "SQL_POLICY",
        )

    @classmethod
    def for_rule(cls, rule: ScopeRule) -> ScopeRefusal:
        return cls(reason_code=rule.value, hint=_SCOPE_HINTS[rule])


class ScopeInvariantError(Exception):
    """The post-rewrite invariant failed. The message is a static code, never SQL."""


# --------------------------------------------------------------------------- walking


def _iter_nodes(root: exp.Expression) -> Iterator[exp.Expression]:
    """Iterative pre-order walk, capped (fail closed beyond the policy's node budget)."""
    stack = [root]
    seen = 0
    cap = MAX_NODES * 4  # the rewrite adds at most four small CTEs
    while stack:
        seen += 1
        if seen > cap:
            raise ScopeInvariantError("too_many_nodes")
        node = stack.pop()
        yield node
        stack.extend(reversed(list(node.iter_expressions())))


def _is_base_reference(table: exp.Table) -> bool:
    """True for a reference to an allowlisted base table in any of the three name forms."""
    if table.name not in ALLOWED_TABLES:
        return False
    db, catalog = table.db, table.catalog
    if not db and not catalog:
        return True
    return db == DATASET and catalog in ("", PROJECT)


def _visible_ctes(table: exp.Table) -> set[str]:
    """CTE names visible from ``table`` (exact case). A CTE body sees only earlier CTEs of
    its own ``WITH`` (no recursion); a query body sees all of them; outer ``WITH``\\s stay
    visible to nested queries."""
    names: set[str] = set()
    prev: exp.Expression = table
    via_cte: exp.CTE | None = None
    node = table.parent
    while node is not None:
        with_ = node.args.get("with_")
        if isinstance(with_, exp.With):
            ctes = list(with_.expressions)
            if prev is with_ and via_cte is not None and via_cte in ctes:
                ctes = ctes[: ctes.index(via_cte)]
            names.update(c.alias for c in ctes)
        if isinstance(node, exp.CTE):
            via_cte = node
        elif not isinstance(node, exp.With):
            via_cte = None
        prev = node
        node = node.parent
    return names


# --------------------------------------------------------------------------- rewrite


def scope_rewrite(statement: exp.Expression, scope: ProductScope) -> exp.Expression:
    """Return a scoped copy of ``statement`` (the input is not mutated).

    Every base-table reference becomes the matching code CTE; the needed code CTEs are
    prepended to the root ``WITH``. Does **not** run ``check_sql`` or the invariant: use
    :func:`apply_scope`.
    """
    if not isinstance(scope, ProductScope):
        raise ScopeError("scope must be a ProductScope")
    if not isinstance(statement, _QUERY_NODES):
        raise ScopeError("statement root must be a SELECT or a set operation")
    root = statement.copy()
    targets = [n for n in _iter_nodes(root) if isinstance(n, exp.Table) and _is_base_reference(n)]
    used: set[str] = set()
    for table in targets:
        code = CODE_CTE_FOR_TABLE[table.name]
        used.add(code)
        alias = table.args.get("alias")
        if alias is None:
            alias = exp.TableAlias(this=exp.to_identifier(table.name))
        else:
            alias = alias.copy()
        table.replace(exp.Table(this=exp.to_identifier(code), alias=alias))
    names = required_ctes(used, scoped=scope.scoped)
    if names:
        code_ctes = [build_cte(n, scoped=scope.scoped) for n in names]
        with_ = root.args.get("with_")
        if isinstance(with_, exp.With):
            with_.set("expressions", code_ctes + list(with_.expressions))
        else:
            root.set("with_", exp.With(expressions=code_ctes))
    return root


# --------------------------------------------------------------------------- invariant


def verify_scoped(sql: str, scope: ProductScope) -> None:
    """Prove the step 10 invariant on generated SQL; raise :class:`ScopeInvariantError`."""
    try:
        parsed = sqlglot.parse(sql, read="bigquery")
    except (SqlglotError, RecursionError) as err:
        raise ScopeInvariantError("unparseable") from err
    if len(parsed) != 1 or not isinstance(parsed[0], _QUERY_NODES):
        raise ScopeInvariantError("bad_root")
    root = parsed[0]

    skip = {id(c) for c in _check_code_ctes(root, scope)}

    stack: list[exp.Expression] = [root]
    seen = 0
    while stack:
        seen += 1
        if seen > MAX_NODES * 4:
            raise ScopeInvariantError("too_many_nodes")
        node = stack.pop()
        if id(node) in skip:
            continue  # template-checked code CTE
        if isinstance(node, exp.CTE):
            alias = node.alias or ""
            if alias.lower().startswith("__") or alias.lower() in _BASE_NAMES_CI:
                raise ScopeInvariantError("reserved_cte_name")
        if isinstance(node, _PARAM_NODES):
            raise ScopeInvariantError("parameter_outside_code_cte")
        if isinstance(node, exp.TableAlias) and (node.name or "").startswith("__"):
            raise ScopeInvariantError("reserved_alias")
        if isinstance(node, exp.Table):
            _check_reference(node)
        stack.extend(node.iter_expressions())


def _check_code_ctes(root: exp.Expression, scope: ProductScope) -> list[exp.CTE]:
    """Validate the code-CTE prefix of the root ``WITH``; return those CTE nodes."""
    with_ = root.args.get("with_")
    ctes = list(with_.expressions) if isinstance(with_, exp.With) else []
    if isinstance(with_, exp.With) and with_.args.get("recursive"):
        raise ScopeInvariantError("recursive_with")
    prefix: list[exp.CTE] = []
    for cte in ctes:
        if not (cte.alias or "").startswith("__"):
            break
        prefix.append(cte)
    names = [c.alias for c in prefix]
    if any(n not in CODE_CTE_ORDER for n in names):
        raise ScopeInvariantError("unknown_code_cte")
    if names != [n for n in CODE_CTE_ORDER if n in names]:
        raise ScopeInvariantError("code_cte_order")
    if tuple(names) != required_ctes(names, scoped=scope.scoped):
        raise ScopeInvariantError("code_cte_dependency_missing")
    for cte in prefix:
        alias = cte.args.get("alias")
        if isinstance(alias, exp.TableAlias) and alias.columns:
            raise ScopeInvariantError("code_cte_template")
        expected = cte_body(cte.alias, scoped=scope.scoped).sql(dialect="bigquery")
        if cte.this.sql(dialect="bigquery") != expected:
            raise ScopeInvariantError("code_cte_template")
        for dep in dependencies(cte.alias, scoped=scope.scoped):
            if dep not in names[: names.index(cte.alias)]:
                raise ScopeInvariantError("code_cte_dependency_missing")
    return prefix


def _check_reference(table: exp.Table) -> None:
    """A table outside the code CTE bodies must be a bare, visible CTE name."""
    extra = {k for k, v in table.args.items() if v not in (None, [], False)} - _TABLE_ARGS
    if extra or not isinstance(table.this, exp.Identifier):
        raise ScopeInvariantError("unexpected_source")
    if table.db or table.catalog:
        raise ScopeInvariantError("base_table_outside_code_cte")
    if table.name.lower() in _BASE_NAMES_CI:
        raise ScopeInvariantError("base_table_outside_code_cte")
    if table.name not in _visible_ctes(table):
        raise ScopeInvariantError("unresolved_reference")


# --------------------------------------------------------------------------- pipeline


def _code_ctes_in(sql: str) -> bool:
    root = sqlglot.parse_one(sql, read="bigquery")
    with_ = root.args.get("with_")
    return isinstance(with_, exp.With) and any(
        c.alias in CODE_CTE_ORDER for c in with_.expressions
    )


_RESERVED_ALIAS_HINT: Final = (
    "rename the table alias: aliases may not start with two underscores"
)


def _has_reserved_alias(root: exp.Expression) -> bool:
    """A model-written table/subquery alias starting with ``__`` (the code-CTE namespace).

    CTE names with that prefix are already refused by ``check_sql`` (``cte_shadows_table``);
    this covers ``FROM products AS __p`` and derived-table aliases the same way.
    """
    return any(
        isinstance(n, exp.TableAlias) and (n.name or "").startswith("__")
        for n in _iter_nodes(root)
    )


def apply_scope(sql: str, scope: ProductScope | None) -> ScopedQuery | ScopeRefusal:
    """Policy-check, scope-rewrite and verify one model-written statement. Fails closed.

    This is the entry point callers use (never :func:`scope_rewrite` directly). A ``None``
    scope (no profile selected) returns a :class:`ScopeRefusal` with rule ``empty_scope``
    rather than raising; build the scope with :meth:`ProductScope.from_profile`, which
    raises :class:`ScopeError` on ``None`` or an ambiguous profile.
    """
    if scope is None:
        return ScopeRefusal.for_rule(ScopeRule.EMPTY_SCOPE)
    if not isinstance(scope, ProductScope):
        return ScopeRefusal.for_rule(ScopeRule.SCOPE_INVALID)
    try:
        _validate_scope(scope.brands, scope.all_products)  # defends against object.__setattr__
    except ScopeError:
        empty = not scope.all_products and not scope.brands
        return ScopeRefusal.for_rule(ScopeRule.EMPTY_SCOPE if empty else ScopeRule.SCOPE_INVALID)

    decision = check_sql(sql)
    if not decision.allowed:
        return ScopeRefusal.from_policy(decision)

    try:
        root = sqlglot.parse_one(sql, read="bigquery")
        if _has_reserved_alias(root):
            return ScopeRefusal(
                reason_code=Rule.IDENTIFIER_NOT_ALLOWED.value, hint=_RESERVED_ALIAS_HINT
            )
        scoped_sql = scope_rewrite(root, scope).sql(dialect="bigquery", comments=False)
        verify_scoped(scoped_sql, scope)
    except ScopeInvariantError as err:
        _log.error("scope rewrite invariant failed: %s", err.args[0] if err.args else "?")
        return ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT)
    except Exception as err:  # noqa: BLE001 - fail closed on any rewriter bug
        _log.error("scope rewrite failed: %s", type(err).__name__)
        return ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT)

    params: tuple[ScopeParameter, ...] = ()
    if scope.scoped and _code_ctes_in(scoped_sql):
        # Brand scope: any code CTE implies __p, the only reader of @scope_brands.
        params = (ScopeParameter(SCOPE_PARAM, scope.brands),)
    return ScopedQuery(sql=scoped_sql, parameters=params, scope_key=scope.scope_key)
