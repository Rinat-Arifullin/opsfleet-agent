# ruff: noqa: E501  (the quirk list in the module docstring is kept as long prose lines)
"""Spike (iteration 2): what sqlglot 30.x really does with BigQuery SQL.

Every test asserts behaviour observed on the installed sqlglot (pinned ``>=30,<31``). A sqlglot
upgrade that changes any of these facts must fail here before it silently weakens a guard.

VERDICT: the scope rewrite (iteration 7) IS expressible. A table reference can be replaced by a
filtered subquery, or by a CTE defined at the top of the statement. No escalation needed.

DIALECT QUIRKS THAT ITERATIONS 6, 7 AND 9 MUST HANDLE
-----------------------------------------------------
Parsing and statement type (iteration 6)
 1. ``parse(sql, dialect="bigquery")`` returns a LIST. Several statements mean len > 1. Empty or
    comment-only input gives ``[None]``. ``"a;;"`` and ``"a; ; b"`` contain ``None`` entries.
    Reject: len != 1, any ``None``, anything that is not ``exp.Select`` / ``exp.Union`` (a parenthesised
    query parses as ``exp.Subquery``; unwrap it or reject it, by decision).
 2. Non-SELECT statements parse to their own classes (Insert, Merge, Drop, Create, Delete, Export,
    LoadData, Declare, Set, Alter, Grant, TruncateTable). ``EXECUTE IMMEDIATE``, ``CALL`` and
    ``BEGIN ... END`` fall back to ``exp.Command`` and log a WARNING through the "sqlglot" logger.
    A ``CREATE TEMP FUNCTION f ...; SELECT f(1)`` is 2 statements (Create + Select).
 3. A syntax error raises ``sqlglot.errors.ParseError``: reject with a reason code, never execute.
 4. Comments never change the AST (they hang off nodes as ``.comments``). A table name inside a
    comment is not a Table node. Generate SQL for execution from the AST with ``comments=False``.
 5. Keywords are case-insensitive. Identifier case is preserved: ``Orders`` stays ``Orders``.
    BigQuery table names are case-sensitive, so allowlist comparison must be exact, not lower().
 6. Unicode look-alikes (Cyrillic o) survive as different identifiers: exact comparison rejects them.

Table identity (iterations 6 and 7)
 7. ``bigquery-public-data.thelook_ecommerce.orders`` parses to the same
    ``catalog='bigquery-public-data', db='thelook_ecommerce', name='orders'`` whether it is unquoted
    (the hyphenated project works unquoted), fully backticked, or backticked per part. Generation
    PRESERVES the input quoting form (it does not normalise), so never compare SQL text: compare
    the (catalog, db, name) parts, and build code-side table nodes yourself.
 8. A 2-part or 1-part name has an EMPTY catalog and/or db. A 4-part name ``a.b.c.d`` is NOT cleanly
    represented (``this`` becomes a Dot, ``name`` is ``d``): compare ``catalog``, ``db`` and ``name``
    together AND reject names whose ``this`` is not a plain ``exp.Identifier``.
 9. ``p.INFORMATION_SCHEMA.TABLES`` gives ``name='INFORMATION_SCHEMA.TABLES'``; wildcard tables give
    ``name='events_*'``. Both fail an exact allowlist; keep them in the red-team set anyway.
10. A user CTE named like an allowlisted table (``WITH orders AS ...; FROM orders``) yields a Table
    with EMPTY db/catalog and the same ``name``. ``Scope.cte_sources`` / ``scope.sources`` tell which
    is which. Never match allowlisted tables by ``name`` alone.
11. ``FOR SYSTEM_TIME AS OF`` lives in ``Table.args['version']``. ``ML.PREDICT(MODEL m, TABLE t)``
    is a Table with ``db='ML'`` whose ``this`` is ``exp.Predict``. ``EXTERNAL_QUERY(...)`` is a Table
    whose ``this`` is ``exp.Anonymous``. ``@@x`` and ``@p`` are ``exp.Parameter``. Detect these by
    node type, not by text search.
12. ``UNNEST(...)`` is NOT an ``exp.Table``: it appears in ``scope.udtfs`` (``exp.Unnest``) and in
    ``scope.sources``. A comma join ``FROM users u, UNNEST(u.arr)`` is a join with an Unnest.
13. ``SELECT AS STRUCT`` sets ``Select.args['kind'] == 'STRUCT'``. A bare table alias used as a value
    (``SELECT o FROM orders o``, ``TO_JSON_STRING(u)``) is an ``exp.Column`` with an empty table and
    ``name == <alias>``: the whole row leaks. Resolve such a column against scope.sources.
14. ``SELECT *`` is ``exp.Star`` and is only expanded by ``qualify`` when the schema is given.

Scope rewrite (iteration 7)
15. ``Table.replace(exp.Subquery(this=..., alias=TableAlias(...)))`` works, and the alias is kept
    (``FROM orders o`` becomes ``FROM (SELECT ...) AS o``). This also covers tables inside user CTEs,
    ``EXISTS`` subqueries and every branch of a UNION. The replacement subquery itself contains the
    raw table: the rewrite must not recurse into code-built nodes (collect the Tables first).
16. The alternative, a top-level CTE, must be PREPENDED to ``With.expressions``. ``Select.with_(...)``
    APPENDS, and BigQuery only lets a CTE see earlier CTEs, so an appended ``__o`` that an earlier user
    CTE references is invalid SQL. ``with_`` on a UNION puts the WITH in front of the whole union.
    A user CTE called ``__o`` collides with the code-built one: reject any user CTE whose name starts
    with ``__``.
17. Nested CTEs (``WITH a AS (WITH b AS ... SELECT ... FROM b) SELECT ... FROM a``) parse. Each level
    has its own ``With``; ``traverse_scope`` yields them innermost first (CTE b, CTE a, ROOT).
    ``find_all(exp.Table)`` reaches all of them, so a rewrite over every Table covers nested CTEs.
18. ``WITH RECURSIVE`` sets ``With.args['recursive']``: reject.
19. ``QUALIFY`` is ``Select.args['qualify']``; a window function in it is ``exp.Window``.

qualify() (iterations 6, 7, 9)
20. ``qualify(expr, dialect="bigquery", schema=...)`` with a nested schema
    ``{project: {dataset: {table: {col: type}}}}`` expands ``SELECT *``, qualifies every column
    with its table alias, adds aliases and QUOTES identifiers with backticks. It mutates in place
    (pass a copy). ``validate_qualify_columns=True`` (the default) raises ``OptimizeError`` for an
    unknown column; treat that as a rejection.

Column lineage (iteration 9)
21. ``sqlglot.lineage.lineage(column, sql, dialect="bigquery", schema=schema)`` returns a Node tree
    whose leaves have ``source`` = the base ``exp.Table`` and ``name`` = ``alias.col``. It goes through
    CTEs, derived tables, scalar subqueries and UNION branches (a UNION node has children named
    "0", "1", ...). Without ``schema`` a ``SELECT *`` CTE ends in a ``*`` leaf: always pass the schema.
    ``COUNT(*)`` has no column leaves. ``DATE_TRUNC(DATE(created_at), MONTH)`` keeps ``created_at``
    as a leaf, so ``users.created_at`` is detected under the expression. Leaves for a column that is
    selected through a ``__u`` scope subquery still resolve to the raw table.
    It handles ONE output column per call; iterate over the root select's output names.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

D = "bigquery"
T = "`bigquery-public-data.thelook_ecommerce.{}`"
ORDERS = T.format("orders")
USERS = T.format("users")
SCHEMA = {
    "bigquery-public-data": {
        "thelook_ecommerce": {
            "users": {
                "id": "INT64",
                "age": "INT64",
                "city": "STRING",
                "email": "STRING",
                "created_at": "TIMESTAMP",
            },
            "orders": {"order_id": "INT64", "user_id": "INT64", "status": "STRING"},
            "order_items": {"user_id": "INT64", "sale_price": "FLOAT64"},
        }
    }
}


def _tables(sql: str) -> list[tuple[str, str, str]]:
    return [(t.catalog, t.db, t.name) for t in parse_one(sql, dialect=D).find_all(exp.Table)]


@pytest.mark.parametrize(
    "ref",
    [
        "`bigquery-public-data.thelook_ecommerce.orders`",
        "`bigquery-public-data`.thelook_ecommerce.orders",
        "`bigquery-public-data`.`thelook_ecommerce`.`orders`",
        "bigquery-public-data.thelook_ecommerce.orders",
    ],
)
def test_hyphenated_project_and_quoting_forms_parse_identically(ref):
    sql = f"SELECT * FROM {ref}"
    assert _tables(sql) == [("bigquery-public-data", "thelook_ecommerce", "orders")]
    # the text round-trips in the form it was written: do not compare SQL strings
    assert parse_one(sql, dialect=D).sql(D) == sql


def test_partial_names_have_empty_catalog_and_db():
    assert _tables("SELECT * FROM thelook_ecommerce.orders") == [
        ("", "thelook_ecommerce", "orders")
    ]
    assert _tables("SELECT * FROM orders") == [("", "", "orders")]


def test_identifier_case_preserved_and_lookalike_distinct():
    assert (
        _tables("sElEcT * fRoM `bigquery-public-data.thelook_ecommerce.Orders`")[0][2] == "Orders"
    )
    assert (
        _tables("SELECT * FROM `bigquery-public-data.thelook_ecommerce.оrders`")[0][2] != "orders"
    )


def test_four_part_and_special_names_are_not_clean_allowlist_matches():
    tb = parse_one("SELECT * FROM `a.b.c.d`", dialect=D).find(exp.Table)
    assert not isinstance(tb.this, exp.Identifier)  # Dot: reject non-Identifier names
    info = parse_one("SELECT * FROM `p`.INFORMATION_SCHEMA.TABLES", dialect=D).find(exp.Table)
    assert info.name == "INFORMATION_SCHEMA.TABLES"
    wild = parse_one("SELECT * FROM `bigquery-public-data.thelook_ecommerce.events_*`", dialect=D)
    assert wild.find(exp.Table).name == "events_*"


def test_statement_count_and_empty_input():
    assert len(parse("SELECT 1; SELECT 2", dialect=D)) == 2
    assert len(parse("SELECT 1;", dialect=D)) == 1
    assert parse("", dialect=D) == [None]
    assert parse("-- only a comment", dialect=D) == [None]
    assert parse("SELECT 1;;", dialect=D)[1] is None
    assert [
        type(s).__name__
        for s in parse("CREATE TEMP FUNCTION f(x INT64) AS (x+1); SELECT f(1)", dialect=D)
    ] == ["Create", "Select"]


@pytest.mark.parametrize(
    ("sql", "cls"),
    [
        ("SELECT 1", exp.Select),
        ("SELECT 1 UNION ALL SELECT 2", exp.Union),
        ("WITH a AS (SELECT 1) SELECT * FROM a", exp.Select),
        ("(SELECT 1)", exp.Subquery),
        ("INSERT INTO a.b VALUES (1)", exp.Insert),
        ("DELETE FROM a.b WHERE x = 1", exp.Delete),
        ("DROP TABLE a.b", exp.Drop),
        ("CREATE TABLE a.b AS SELECT 1", exp.Create),
        ("EXPORT DATA OPTIONS(uri='x') AS SELECT 1", exp.Export),
        ("DECLARE x INT64", exp.Declare),
        ("TRUNCATE TABLE a.b", exp.TruncateTable),
    ],
)
def test_statement_type_detection(sql, cls):
    assert type(parse_one(sql, dialect=D)) is cls


@pytest.mark.parametrize("sql", ["EXECUTE IMMEDIATE 'select 1'", "CALL proc()"])
def test_unparsed_statements_fall_back_to_command(sql):
    assert type(parse_one(sql, dialect=D)) is exp.Command


@pytest.mark.parametrize("sql", ["SELECT 1 FROM", "SELECT ("])
def test_syntax_error_raises_parse_error(sql):
    with pytest.raises(ParseError):
        parse(sql, dialect=D)


def test_comments_do_not_create_tables_and_can_be_dropped():
    sql = f"SELECT 1 FROM /* users */ {ORDERS} -- users\n"
    assert _tables(sql) == [("bigquery-public-data", "thelook_ecommerce", "orders")]
    assert "/*" not in parse_one(sql, dialect=D).sql(D, comments=False)


def test_node_types_for_forbidden_constructs():
    ver = parse_one("SELECT * FROM t FOR SYSTEM_TIME AS OF TIMESTAMP '2020-01-01'", dialect=D)
    assert ver.find(exp.Table).args.get("version") is not None
    ml = parse_one("SELECT * FROM ML.PREDICT(MODEL m, TABLE t)", dialect=D).find(exp.Table)
    assert ml.db == "ML" and isinstance(ml.this, exp.Predict)
    ext = parse_one("SELECT * FROM EXTERNAL_QUERY('c', 'select 1')", dialect=D).find(exp.Table)
    assert isinstance(ext.this, exp.Anonymous)
    assert list(parse_one("SELECT @@x, @p", dialect=D).find_all(exp.Parameter))
    assert parse_one("SELECT AS STRUCT 1 a", dialect=D).args.get("kind") == "STRUCT"
    assert (
        parse_one(
            "WITH RECURSIVE r AS (SELECT 1 n UNION ALL SELECT n+1 FROM r WHERE n<3) "
            "SELECT * FROM r",
            dialect=D,
        )
        .args["with_"]
        .args.get("recursive")
    )


def test_table_alias_used_as_value_is_a_column_named_like_the_alias():
    sel = parse_one(f"SELECT o, TO_JSON_STRING(o) FROM {ORDERS} o", dialect=D)
    col = sel.expressions[0]
    assert isinstance(col, exp.Column) and col.table == "" and col.name == "o"
    assert isinstance(sel.expressions[1], exp.JSONFormat)


def test_unnest_is_a_udtf_not_a_table():
    sql = f"SELECT u.id, t FROM {USERS} u, UNNEST(['a', 'b']) AS t"
    root = traverse_scope(parse_one(sql, dialect=D))[-1]
    assert [x.name for x in root.tables] == ["users"]
    assert [type(x) for x in root.udtfs] == [exp.Unnest]
    assert "t" in root.sources


def test_qualify_clause_in_ast():
    sel = parse_one(
        f"SELECT u.id FROM {USERS} u QUALIFY ROW_NUMBER() OVER (PARTITION BY u.id ORDER BY u.age) = 1",
        dialect=D,
    )
    assert sel.args["qualify"] is not None
    assert list(sel.args["qualify"].find_all(exp.Window))


def test_scope_rewrite_table_to_filtered_subquery_keeps_alias_everywhere():
    sql = (
        f"WITH x AS (SELECT * FROM {ORDERS} o2) SELECT o.status FROM {ORDERS} o JOIN x "
        f"ON x.order_id = o.order_id WHERE EXISTS (SELECT 1 FROM {ORDERS} o3)"
    )
    tree = parse_one(sql, dialect=D)
    scoped = parse_one(
        f"SELECT order_id, user_id, status FROM {ORDERS} WHERE user_id > 0", dialect=D
    )
    for tb in [t for t in tree.find_all(exp.Table) if t.name == "orders"]:  # collect first
        alias = tb.alias or tb.name
        tb.replace(
            exp.Subquery(this=scoped.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias)))
        )
    out = tree.sql(D)
    assert ") AS o JOIN x" in out and ") AS o2" in out and ") AS o3" in out
    # the only raw-table references left are inside the code-built subqueries
    assert out.count("WHERE user_id > 0") == 3
    raw = [t for t in parse_one(out, dialect=D).find_all(exp.Table) if t.db]
    assert len(raw) == 3
    for t in raw:
        sub = t.find_ancestor(exp.Subquery)
        assert sub is not None and sub.this.args["where"] is not None


def test_scope_rewrite_cte_form_must_be_prepended_not_appended():
    sql = f"WITH x AS (SELECT * FROM {ORDERS} o2) SELECT * FROM x"
    cte_body = f"SELECT order_id FROM {ORDERS} WHERE user_id > 0"

    appended = parse_one(sql, dialect=D)
    for tb in [t for t in appended.find_all(exp.Table) if t.name == "orders"]:
        tb.replace(exp.Table(this=exp.to_identifier("__o"), alias=tb.args.get("alias")))
    appended.with_("__o", as_=cte_body, dialect=D, copy=False)
    order = [c.alias for c in appended.args["with_"].expressions]
    assert order == ["x", "__o"]  # x uses __o before it is defined: invalid in BigQuery

    prepended = parse_one(sql, dialect=D)
    for tb in [t for t in prepended.find_all(exp.Table) if t.name == "orders"]:
        tb.replace(exp.Table(this=exp.to_identifier("__o"), alias=tb.args.get("alias")))
    prepended.args["with_"].expressions.insert(
        0,
        exp.CTE(
            this=parse_one(cte_body, dialect=D), alias=exp.TableAlias(this=exp.to_identifier("__o"))
        ),
    )
    assert [c.alias for c in prepended.args["with_"].expressions] == ["__o", "x"]
    assert prepended.sql(D).startswith("WITH __o AS (")


def test_with_on_union_wraps_the_whole_union():
    u = parse_one(f"SELECT id FROM {USERS} UNION ALL SELECT id FROM {USERS}", dialect=D)
    u.with_("__u", as_="SELECT 1", copy=False)
    assert u.sql(D).startswith("WITH __u AS (SELECT 1) SELECT id FROM")
    assert len(list(u.find_all(exp.Select))) == 3  # two branches plus the CTE body


def test_nested_cte_scopes_and_table_discovery():
    sql = f"WITH a AS (WITH b AS (SELECT * FROM {USERS}) SELECT * FROM b) SELECT * FROM a"
    scopes = traverse_scope(parse_one(sql, dialect=D))
    assert [s.scope_type.name for s in scopes] == ["CTE", "CTE", "ROOT"]  # innermost first
    assert [t.name for t in scopes[0].tables] == ["users"]
    assert [t.name for t in scopes[1].tables] == ["b"]  # a reference to the CTE, not a base table
    assert [t.name for t in parse_one(sql, dialect=D).find_all(exp.Table) if t.db] == ["users"]


def test_user_cte_shadowing_an_allowlisted_name_is_distinguishable():
    sql = f"WITH orders AS (SELECT 1 a) SELECT * FROM orders, {ORDERS} o2"
    root = traverse_scope(parse_one(sql, dialect=D))[-1]
    shadow = [t for t in root.tables if t.name == "orders" and not t.db]
    real = [t for t in root.tables if t.name == "orders" and t.db]
    assert len(shadow) == 1 and len(real) == 1
    assert "orders" in root.cte_sources
    assert isinstance(root.sources["o2"], exp.Table)


def test_qualify_expands_star_quotes_and_validates_columns():
    q = qualify(parse_one(f"SELECT * FROM {USERS}", dialect=D), dialect=D, schema=SCHEMA)
    assert q.sql(D) == (
        "SELECT `users`.`id` AS `id`, `users`.`age` AS `age`, `users`.`city` AS `city`, "
        "`users`.`email` AS `email`, `users`.`created_at` AS `created_at` "
        f"FROM {USERS} AS `users`"
    )
    q = qualify(parse_one(f"SELECT u.id FROM {USERS} u", dialect=D), dialect=D, schema=SCHEMA)
    assert q.sql(D) == f"SELECT `u`.`id` AS `id` FROM {USERS} AS `u`"
    with pytest.raises(sqlglot.errors.OptimizeError):
        qualify(parse_one(f"SELECT nosuch FROM {USERS}", dialect=D), dialect=D, schema=SCHEMA)


def _leaves(node) -> set[tuple[str, str]]:
    out = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n.source, exp.Table) and n.name != "*":
            out.add((n.source.name, n.name.split(".")[-1]))
        stack.extend(n.downstream)
    return out


def test_lineage_through_nested_ctes_and_joins():
    sql = (
        f"WITH s AS (SELECT user_id, SUM(sale_price) tot FROM {T.format('order_items')} GROUP BY 1), "
        f"g AS (SELECT u.city AS c, s.tot FROM {USERS} u JOIN s ON s.user_id = u.id) "
        "SELECT c, SUM(tot) AS total FROM g GROUP BY c"
    )
    assert _leaves(lineage("c", sql, dialect=D, schema=SCHEMA)) == {("users", "city")}
    assert _leaves(lineage("total", sql, dialect=D, schema=SCHEMA)) == {
        ("order_items", "sale_price")
    }


def test_lineage_star_cte_needs_schema():
    sql = f"WITH u2 AS (SELECT * FROM {USERS}) SELECT city, COUNT(*) AS n FROM u2 GROUP BY city"
    assert _leaves(lineage("city", sql, dialect=D, schema=SCHEMA)) == {("users", "city")}
    assert _leaves(lineage("n", sql, dialect=D, schema=SCHEMA)) == set()  # COUNT(*): no column
    blind = lineage("city", sql, dialect=D)  # no schema: the star is not expanded
    assert "*" in {d.name for d in _walk(blind)}


def _walk(node):
    yield node
    for c in node.downstream:
        yield from _walk(c)


def test_lineage_sees_qi_under_date_trunc_scalar_subquery_and_union():
    month = f"SELECT DATE_TRUNC(DATE(created_at), MONTH) AS m FROM {USERS}"
    assert _leaves(lineage("m", month, dialect=D, schema=SCHEMA)) == {("users", "created_at")}
    scalar = f"SELECT (SELECT MAX(age) FROM {USERS}) AS a"
    assert _leaves(lineage("a", scalar, dialect=D, schema=SCHEMA)) == {("users", "age")}
    union = f"SELECT city AS c FROM {USERS} UNION ALL SELECT CAST(age AS STRING) FROM {USERS}"
    assert _leaves(lineage("c", union, dialect=D, schema=SCHEMA)) == {
        ("users", "city"),
        ("users", "age"),
    }
