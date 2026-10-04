"""Small-cell suppression on the scoped AST (iteration 9; HLD §5.3 step 9, ADR-004 layer 5).

:func:`apply_small_cell` runs after :func:`opsfleet_agent.guards.scope.apply_scope` on its
:class:`ScopedQuery`. It is pure: no BigQuery, no network, no logging of SQL. It re-parses the
scoped SQL, re-runs the policy's column resolution and QI rules on it (``_Analyzer``), and
returns one of:

* :class:`SmallCellRewrite` (variant a). Every **cell** (see below) that has a ``GROUP BY``
  gets ``HAVING COUNT(DISTINCT <user key>) >= k`` (``k`` a literal integer, ANDed with any
  existing ``HAVING``), so it is applied before the outer ``ORDER BY`` / ``LIMIT``. No
  companion query is produced (re-review N2): any per-query "were groups suppressed?" bit
  is a one-person oracle, because the model's ``WHERE`` can isolate one customer by QI
  and add a value threshold, so each query would leak one bit of that customer's value.
  The answer layer may only add a **generic, static** note that groups with fewer than
  ``k`` customers are never shown; it must never say whether this particular answer hid
  any group. A query that references no QI is returned unchanged: product-only groups
  and "top customers by spend".
* :class:`PopulationCheck` (variant b). One cell **without** ``GROUP BY`` (a single row over
  a QI-filtered population): ``query`` is ``SELECT COUNT(DISTINCT <user key>) AS population``
  over the cell's own ``FROM``/``JOIN``/``WHERE``, under the same ``WITH`` and parameters.
  The caller runs it first and passes the count to :meth:`PopulationCheck.evaluate`, which
  returns ``then_run`` (the unchanged scoped query) only when the count is at least ``k``.
* :class:`ScopeRefusal` (variant c), error code ``SQL_POLICY``, rule ``qi_position``,
  ``qi_at_id_grain``, ``qi_differencing`` or ``small_cell_unplaceable`` (hint: "use a
  coarser grouping or aggregate over the whole population"); or ``rewrite_invariant`` when
  generated SQL fails :func:`verify_scoped` or anything unexpected happens (fail closed).

``qi_differencing`` (review M1, pending owner O1): a statement that references a QI may
contain at most one **aggregating level** (a ``SELECT`` outside the code CTEs with ``GROUP
BY``, an aggregate or ``HAVING``, not grouped by an id key), counted over all CTEs, derived
tables, subqueries and set-operation branches. Two of them (joined CTEs subtracted, ``UNION``
of a grouping with a total, ``EXCEPT``/``INTERSECT`` of two QI groupings, a total minus a
QI-filtered total, a QI cell next to a product-only total) let the model rebuild a
suppressed cell by differencing, so the statement is refused. An id-grain inner aggregate
feeding one outer cell is not counted.

Definitions (all fail closed):

* A **users instance** is one reference to the code CTE ``__u``, identified by its alias
  path from the query level that reads it (``t.u`` when CTE ``t`` reads ``__u AS u``), so a
  self-join of a CTE gives two instances.
* A query level **touches QI** when one of its own columns carries the QI taint or it reads
  (FROM/JOIN, through any depth of CTEs and derived tables) a level that does.
* A **cell** is an aggregating level (``GROUP BY``, an aggregate, or ``HAVING``) that
  touches QI and is not grouped by an id key. A non-aggregating or id-grain level is an
  intermediate; its rows flow into the levels that read it.
* The **user key** of a cell is a column of one of its FROM/JOIN sources that is a bare
  pass-through of ``id`` of the cell's single users instance. More than one users instance
  feeding a cell, or no such column, is ``small_cell_unplaceable``.

``small_cell_unplaceable`` also covers: a scalar/``IN``/``EXISTS`` subquery (correlated or
not) that touches QI; an aggregate over a cell (nested aggregation); a window in a level
that touches QI or reads a cell (``SUM(COUNT(*)) OVER ()`` would reveal suppressed totals);
``SELECT DISTINCT`` or ``ROLLUP``/``CUBE``/``GROUPING SETS`` on a cell; a set operation
other than the root that touches QI; a cell without ``GROUP BY`` next to any other cell or
with a ``HAVING``; a level that reads a cell and aggregates or re-joins a users instance.
A QI inside ``COUNTIF`` or as anything but ``COUNT(DISTINCT <bare column>)`` is refused
earlier by the policy as ``qi_position`` (review B1).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from opsfleet_agent.guards.scope import (
    ProductScope,
    ScopedQuery,
    ScopeInvariantError,
    ScopeRefusal,
    ScopeRule,
    verify_scoped,
)
from opsfleet_agent.guards.scope_ctes import CODE_CTE_FOR_TABLE, CODE_CTE_ORDER
from opsfleet_agent.guards.sql_policy import (
    _IDKEY,
    _QI,
    QI_COLUMNS,
    Rule,
    _Analyzer,
    _branch_selects,
    _contains_aggregate,
    _deny,
    _Reject,
)

__all__ = [
    "DEFAULT_K",
    "MAX_K",
    "QI_COLUMNS",
    "PopulationCheck",
    "SmallCellResult",
    "SmallCellRewrite",
    "apply_small_cell",
    "population_ok",
]

#: Minimum distinct users per released cell (HLD ``policy.small_cell_k``).
DEFAULT_K: Final = 5
MAX_K: Final = 1_000
_USERS_CTE: Final = CODE_CTE_FOR_TABLE["users"]
_POPULATION: Final = "population"

Path = tuple[str, ...]


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class SmallCellRewrite:
    """Variant (a): run ``query``. There is no data-dependent companion (re-review N2):
    the answer layer may only add a generic, static note that groups with fewer than
    ``k`` customers are hidden, never one that depends on whether any group was hidden."""

    query: ScopedQuery
    k: int

    @property
    def allowed(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class PopulationCheck:
    """Variant (b): run ``query`` (one row ``population``) first, then :meth:`evaluate`."""

    query: ScopedQuery
    then_run: ScopedQuery
    k: int

    @property
    def allowed(self) -> bool:
        return True

    def evaluate(self, population: object) -> ScopedQuery | ScopeRefusal:
        """``then_run`` when ``population >= k``; otherwise a ``small_cell_unplaceable``
        refusal (a missing or non-integer count also refuses)."""
        if population_ok(population, self.k):
            return self.then_run
        return _refuse(Rule.SMALL_CELL_UNPLACEABLE)


SmallCellResult = SmallCellRewrite | PopulationCheck | ScopeRefusal


def population_ok(population: object, k: int = DEFAULT_K) -> bool:
    """True only for an integer (not bool) count of at least ``k``."""
    return (
        isinstance(population, int)
        and not isinstance(population, bool)
        and _valid_k(k)
        and population >= k
    )


def _valid_k(k: object) -> bool:
    return isinstance(k, int) and not isinstance(k, bool) and 2 <= k <= MAX_K


def _refuse(rule: Rule) -> ScopeRefusal:
    return ScopeRefusal.from_policy(_deny(rule))


class _Unplaceable(Exception):
    def __init__(self, rule: Rule = Rule.SMALL_CELL_UNPLACEABLE) -> None:
        super().__init__(rule.value)
        self.rule = rule


# --------------------------------------------------------------------------- entry point


def apply_small_cell(
    scoped: ScopedQuery, scope: ProductScope, k: int = DEFAULT_K
) -> SmallCellResult:
    """Apply the small-cell rule to a scoped query. Pure; fails closed."""
    if not isinstance(scoped, ScopedQuery) or not isinstance(scope, ProductScope):
        return ScopeRefusal.for_rule(ScopeRule.SCOPE_INVALID)
    if scoped.scope_key != scope.scope_key:
        return ScopeRefusal.for_rule(ScopeRule.SCOPE_INVALID)
    if not _valid_k(k):
        return ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT)
    try:
        verify_scoped(scoped.sql, scope)
        root = sqlglot.parse_one(scoped.sql, read="bigquery")
        analyzer = _Analyzer(root)
        analyzer.run()
        if not analyzer.references_qi:
            return SmallCellRewrite(query=scoped, k=k)
        return _Planner(root, analyzer, scoped, scope, k).plan()
    except _Reject as rej:
        return _refuse(rej.rule)
    except _Unplaceable as err:
        return _refuse(err.rule)
    except Exception:  # noqa: BLE001 - incl. ScopeInvariantError: fail closed
        return ScopeRefusal.for_rule(ScopeRule.REWRITE_INVARIANT)


# --------------------------------------------------------------------------- planner


@dataclass(slots=True)
class _Cell:
    scope: Scope
    key: exp.Column


class _Planner:
    def __init__(
        self,
        root: exp.Expression,
        analyzer: _Analyzer,
        scoped: ScopedQuery,
        scope: ProductScope,
        k: int,
    ) -> None:
        self.root = root
        self.an = analyzer
        self.scoped = scoped
        self.scope = scope
        self.k = k
        self.scopes = [s for s in analyzer.root_scope.traverse() if not _in_code_cte(s)]
        self._instances: dict[int, frozenset[Path]] = {}
        self._reads_cell: dict[int, bool] = {}
        self._cells: dict[int, bool] = {}
        self._busy: set[int] = set()

    # -- classification

    def plan(self) -> SmallCellResult:
        for scope in self.scopes:
            expr = scope.expression
            if isinstance(expr, exp.SetOperation):
                if scope is not self.an.root_scope and self._instances_of(scope):
                    raise _Unplaceable()
                continue
            if not isinstance(expr, exp.Select):
                raise _Unplaceable()
            touches = self._touches(scope)
            if scope.is_subquery and touches:
                raise _Unplaceable()  # scalar / IN / EXISTS subquery, correlated or not
            if touches and any(isinstance(n, exp.Window) for n in scope.walk()):
                raise _Unplaceable()
            reads_cell = self._reads_cell_of(scope)
            if reads_cell and (_aggregating(expr) or self._instances_of(scope)):
                raise _Unplaceable()  # aggregate over a cell, or a cell re-joined with users
        aggregating = [
            s
            for s in self.scopes
            if _aggregating(s.expression) and not self.an.infos[id(s.expression)].row_grain
        ]
        if len(aggregating) > 1:
            raise _Unplaceable(Rule.QI_DIFFERENCING)  # review M1: differencing of aggregates
        top = [self.an.scope_of_expr[id(s)] for s in _branch_selects(self.root)]
        for scope in top:
            if self._touches(scope) and not self._is_cell(scope) and not self._reads_cell_of(scope):
                raise _Unplaceable(Rule.QI_AT_ID_GRAIN)  # row or id grain at the top
        cells = [
            _Cell(s, self._user_key(s))
            for s in self.scopes
            if isinstance(s.expression, exp.Select) and self._is_cell(s)
        ]
        if not cells:
            raise _Unplaceable()
        for cell in cells:
            group = cell.scope.expression.args.get("group")
            if cell.scope.expression.args.get("distinct") is not None:
                raise _Unplaceable()
            if group is not None and any(
                group.args.get(a) for a in ("rollup", "cube", "grouping_sets", "totals")
            ):
                raise _Unplaceable()
        ungrouped = [c for c in cells if c.scope.expression.args.get("group") is None]
        if ungrouped:
            if len(cells) != 1 or cells[0].scope.expression.args.get("having") is not None:
                raise _Unplaceable()
            return self._population_check(cells[0])
        return self._rewrite(cells)

    def _touches(self, scope: Scope) -> bool:
        return self._own_qi(scope) or bool(self._instances_of(scope))

    def _own_qi(self, scope: Scope) -> bool:
        return any(_QI in self.an.col_taint.get(id(c), frozenset()) for c in _columns(scope))

    def _is_cell(self, scope: Scope) -> bool:
        key = id(scope.expression)
        if key not in self._cells:
            expr = scope.expression
            self._cells[key] = (
                isinstance(expr, exp.Select)
                and not scope.is_subquery
                and _aggregating(expr)
                and not self.an.infos[key].row_grain
                and self._touches(scope)
            )
        return self._cells[key]

    def _sources(self, scope: Scope) -> list[tuple[str, Scope]]:
        out: list[tuple[str, Scope]] = []
        for alias, (_node, source) in sorted(scope.selected_sources.items()):
            if not isinstance(source, Scope):
                raise _Unplaceable()  # a base table outside the code CTEs
            out.append((alias.lower(), source))
        return out

    def _guard(self, scope: Scope) -> int:
        key = id(scope.expression)
        if key in self._busy or len(self._busy) > len(self.scopes):
            raise _Unplaceable()
        return key

    def _instances_of(self, scope: Scope) -> frozenset[Path]:
        """Users-instance paths whose QI values reach this level's rows."""
        key = self._guard(scope)
        if key in self._instances:
            return self._instances[key]
        self._busy.add(key)
        found: set[Path] = set()
        if isinstance(scope.expression, exp.SetOperation):
            for branch in _branch_selects(scope.expression):
                found |= self._instances_of(self.an.scope_of_expr[id(branch)])
        else:
            sources = self._sources(scope)
            for col in _columns(scope):
                if _QI not in self.an.col_taint.get(id(col), frozenset()):
                    continue
                hit = self._source_of(col, scope, sources)
                if hit is None:
                    if scope.is_subquery or self._is_late_alias(col, scope):
                        continue  # correlated (refused above) or an own output alias
                    raise _Unplaceable()
                alias, source = hit
                if _is_users_cte(source):
                    found.add((alias,))
            for alias, source in sources:
                if _is_code_cte(source) or self._is_cell(source):
                    continue
                found |= {(alias, *p) for p in self._instances_of(source)}
        self._busy.discard(key)
        result = frozenset(found)
        self._instances[key] = result
        return result

    def _reads_cell_of(self, scope: Scope) -> bool:
        key = self._guard(scope)
        if key in self._reads_cell:
            return self._reads_cell[key]
        self._busy.add(key)
        if isinstance(scope.expression, exp.SetOperation):
            branches = [self.an.scope_of_expr[id(b)] for b in _branch_selects(scope.expression)]
            result = any(self._is_cell(b) or self._reads_cell_of(b) for b in branches)
        else:
            result = any(
                not _is_code_cte(src) and (self._is_cell(src) or self._reads_cell_of(src))
                for _alias, src in self._sources(scope)
            )
        self._busy.discard(key)
        self._reads_cell[key] = result
        return result

    def _is_late_alias(self, col: exp.Column, scope: Scope) -> bool:
        select = scope.expression
        names = {p.alias_or_name.lower() for p in select.expressions}
        return not col.table and col.name.lower() in names and self.an._late_clause(col, select)

    def _source_of(
        self, col: exp.Column, scope: Scope, sources: list[tuple[str, Scope]]
    ) -> tuple[str, Scope] | None:
        name = col.name.lower()
        if col.table:
            wanted = col.table.lower()
            hits = [(a, s) for a, s in sources if a == wanted]
        else:
            hits = [(a, s) for a, s in sources if name in self._outputs(s)]
        if len(hits) > 1:
            raise _Unplaceable()
        return hits[0] if hits else None

    def _outputs(self, source: Scope) -> dict[str, frozenset[str]]:
        return self.an.infos[id(source.expression)].outputs

    # -- user key

    def _user_key(self, cell: Scope) -> exp.Column:
        instances = self._instances_of(cell)
        if len(instances) != 1:
            raise _Unplaceable()
        (target,) = instances
        for alias, source in self._sources(cell):
            if _is_users_cte(source):
                if (alias,) == target:
                    return exp.column("id", table=alias)
                continue
            if _is_code_cte(source):
                continue
            for name in self._id_projections(source, target[1:] if target[0] == alias else None):
                return exp.column(name, table=alias)
        raise _Unplaceable()

    def _id_projections(self, source: Scope, rest: Path | None) -> Iterable[str]:
        """Output names of ``source`` that pass through ``id`` of instance ``rest``."""
        if rest is None or not isinstance(source.expression, exp.Select) or self._is_cell(source):
            return []
        select = source.expression
        names = [p.alias_or_name.lower() for p in select.expressions]
        found = []
        for proj, name in zip(select.expressions, names, strict=True):
            if not name or names.count(name) != 1:
                continue
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(inner, exp.Column) and self._id_origin(inner, source) == rest:
                found.append(name)
        return found

    def _id_origin(self, col: exp.Column, scope: Scope) -> Path | None:
        if _IDKEY not in self.an.col_taint.get(id(col), frozenset()):
            return None
        hit = self._source_of(col, scope, self._sources(scope))
        if hit is None:
            return None
        alias, source = hit
        if _is_users_cte(source):
            return (alias,) if col.name.lower() == "id" else None
        if (
            _is_code_cte(source)
            or not isinstance(source.expression, exp.Select)
            or self._is_cell(source)
        ):
            return None
        self._guard(source)
        for proj in source.expression.expressions:
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if proj.alias_or_name.lower() == col.name.lower() and isinstance(inner, exp.Column):
                self._busy.add(id(scope.expression))
                try:
                    origin = self._id_origin(inner, source)
                finally:
                    self._busy.discard(id(scope.expression))
                return None if origin is None else (alias, *origin)
        return None

    # -- SQL generation

    def _threshold(self, key: exp.Column, op: type[exp.Binary]) -> exp.Expression:
        count = exp.Count(this=exp.Distinct(expressions=[key.copy()]))
        return op(this=count, expression=exp.Literal.number(self.k))

    def _with_having(self, select: exp.Select, cond: exp.Expression) -> None:
        old = select.args.get("having")
        if old is not None:
            cond = exp.and_(exp.Paren(this=old.this.copy()), cond)
        select.set("having", exp.Having(this=cond))

    def _bare(self, select: exp.Select) -> exp.Select:
        copy = select.copy()
        for arg in ("with_", "order", "limit", "offset"):
            copy.set(arg, None)
        return copy

    def _wrap(self, body: exp.Select) -> ScopedQuery:
        with_ = self.root.args.get("with_")
        if isinstance(with_, exp.With):
            body.set("with_", with_.copy())
        sql = body.sql(dialect="bigquery", comments=False)
        try:
            verify_scoped(sql, self.scope)
        except ScopeInvariantError as err:
            raise _Unplaceable() from err
        return ScopedQuery(
            sql=sql, parameters=self.scoped.parameters, scope_key=self.scoped.scope_key
        )

    def _rewrite(self, cells: list[_Cell]) -> SmallCellRewrite:
        for cell in cells:
            self._with_having(cell.scope.expression, self._threshold(cell.key, exp.GTE))
        main_sql = self.root.sql(dialect="bigquery", comments=False)
        try:
            verify_scoped(main_sql, self.scope)
        except ScopeInvariantError as err:
            raise _Unplaceable() from err
        main = ScopedQuery(main_sql, self.scoped.parameters, self.scoped.scope_key)
        return SmallCellRewrite(query=main, k=self.k)

    def _population_check(self, cell: _Cell) -> PopulationCheck:
        body = self._bare(cell.scope.expression)
        body.set("distinct", None)
        body.set("having", None)
        body.set("qualify", None)
        count = exp.Count(this=exp.Distinct(expressions=[cell.key.copy()]))
        body.set("expressions", [exp.alias_(count, _POPULATION)])
        return PopulationCheck(query=self._wrap(body), then_run=self.scoped, k=self.k)


# --------------------------------------------------------------------------- helpers


def _in_code_cte(scope: Scope) -> bool:
    node: exp.Expression | None = scope.expression
    while node is not None:
        if isinstance(node, exp.CTE) and node.alias in CODE_CTE_ORDER:
            return True
        node = node.parent
    return False


def _is_users_cte(source: Scope) -> bool:
    parent = source.expression.parent
    return isinstance(parent, exp.CTE) and parent.alias == _USERS_CTE


def _is_code_cte(source: Scope) -> bool:
    parent = source.expression.parent
    return isinstance(parent, exp.CTE) and parent.alias in CODE_CTE_ORDER


def _aggregating(select: exp.Expression) -> bool:
    return isinstance(select, exp.Select) and (
        select.args.get("group") is not None
        or select.args.get("having") is not None
        or any(_contains_aggregate(p) for p in select.expressions)
    )


def _columns(scope: Scope) -> list[exp.Column]:
    return [n for n in scope.walk() if isinstance(n, exp.Column)]
