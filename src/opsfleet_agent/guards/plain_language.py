"""Plain-language answers (owner decision D-151).

Chat answers are read by business users, not engineers. They must not show table names,
column names, SQL or schema terms. Three code-owned layers enforce this:

* :data:`PLAIN_LANGUAGE_RULE` is a prompt section added by code (never by the persona) to
  every prompt that writes user-facing chat text: the quick and deep analyst, the light
  path and the force answer. :data:`REPORT_PLAIN_LANGUAGE_RULE` is the report-writer variant:
  it covers the report prose and leaves the report structure alone.
* :func:`humanize_identifiers` is a pure, deterministic rewrite applied to model-written
  chat text **after** the output guard has allowed it. It replaces identifiers the model
  still wrote (``sale_price``, ``order_items.created_at``, "the orders table") with business
  words. It never changes a digit, it is idempotent, and it leaves non-SQL code blocks alone.
* :func:`strip_sql` (owner decision D-151a, 2026-10-05, which replaces the old AC-02.2
  exception) is the output-side check: SQL is **never** shown in chat, even when the user asks
  for it. It removes fenced SQL blocks, inline SQL and unfenced ``SELECT ... FROM`` text from
  the final answer and puts :data:`SQL_REMOVED_NOTE` in their place. A request for the SQL
  gets :func:`sql_request_reply`: the agent says it does not show queries and describes the
  data used in business words (:func:`describe_data_used`). Traces and JSONL keep sanitized
  SQL for developers; ``sql_used`` stays stored with a saved report, and the report shows a
  "Data used" section instead.

The identifier set comes from :data:`opsfleet_agent.guards.sql_policy.ALLOWED_TABLES`, so a
schema change cannot leave a column un-humanized without a test noticing (every allowed
column has a phrase, checked at import time).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from types import MappingProxyType
from typing import Final

from opsfleet_agent.guards.sql_policy import ALLOWED_TABLES, DATASET

__all__ = [
    "PLAIN_LANGUAGE_RULE",
    "PLAIN_LANGUAGE_SECTION",
    "REPORT_PLAIN_LANGUAGE_RULE",
    "SCHEMA_TERMS_REWRITTEN",
    "SQL_NOT_SHOWN_TEXT",
    "SQL_REMOVED_NOTE",
    "SQL_STRIPPED",
    "describe_data_used",
    "humanize_identifiers",
    "sql_request_reply",
    "strip_sql",
]

#: Trace rule code (lowercase, like the output guard's codes) recorded when the rewrite
#: changed the answer.
SCHEMA_TERMS_REWRITTEN: Final = "schema_terms_rewritten"
#: Trace rule code recorded when :func:`strip_sql` removed SQL from the answer (D-151a).
SQL_STRIPPED: Final = "sql_stripped"

#: Section title used by every prompt site, so tests can find the section.
PLAIN_LANGUAGE_SECTION: Final = "Answer language"

PLAIN_LANGUAGE_RULE: Final = (
    "Write for a business reader who does not know how the data is stored. In your answer, "
    "never mention table names, column names, field names, SQL, queries, joins or the "
    "database schema. Describe the data in business words instead, for example "
    '"item sale price", "order date", "order status" or "customer sign-up date". '
    "Mention the product scope and the time zone naturally where they matter, for example "
    '"for Calvin Klein products" or "dates are in UTC". '
    "Never show SQL or a query, even if the user asks for it: say that you don't show "
    "database queries and describe the data you used in business words instead."
)

REPORT_PLAIN_LANGUAGE_RULE: Final = (
    "Write every text value for a business reader who does not know how the data is "
    "stored: never mention table names, column names, field names, SQL, queries, joins or "
    'the database schema. Use business words instead, for example "item sale price" or '
    '"order date". Keep every required key; never write SQL or a query.'
)

# --- identifier phrases ---------------------------------------------------------------

_TABLE_WORDS: Final = MappingProxyType(
    {"orders": "orders", "order_items": "order items", "products": "products", "users": "customers"}
)
_RECORD_WORDS: Final = MappingProxyType(
    {
        "orders": "order records",
        "order_items": "order item records",
        "products": "product records",
        "users": "customer records",
    }
)
_COLUMN_WORDS: Final = MappingProxyType(
    {
        "id": "ID",
        "name": "name",
        "brand": "brand",
        "category": "category",
        "department": "department",
        "retail_price": "retail price",
        "cost": "cost",
        "sku": "SKU",
        "distribution_center_id": "distribution center ID",
        "order_id": "order ID",
        "user_id": "customer ID",
        "product_id": "product ID",
        "inventory_item_id": "inventory item ID",
        "status": "status",
        "sale_price": "sale price",
        "created_at": "created date",
        "shipped_at": "shipped date",
        "delivered_at": "delivered date",
        "returned_at": "returned date",
        "first_name": "first name",
        "last_name": "last name",
        "email": "email",
        "age": "age",
        "gender": "gender",
        "state": "state",
        "street_address": "street address",
        "postal_code": "postal code",
        "city": "city",
        "country": "country",
        "latitude": "latitude",
        "longitude": "longitude",
        "traffic_source": "traffic source",
        "user_geom": "customer location",
    }
)
#: Table-specific phrases where the bare column word would be ambiguous.
_QUALIFIED_WORDS: Final = MappingProxyType(
    {
        ("orders", "created_at"): "order date",
        ("orders", "status"): "order status",
        ("orders", "shipped_at"): "order shipped date",
        ("orders", "delivered_at"): "order delivered date",
        ("orders", "returned_at"): "order returned date",
        ("order_items", "created_at"): "item created date",
        ("order_items", "sale_price"): "item sale price",
        ("order_items", "status"): "item status",
        ("order_items", "id"): "order item ID",
        ("products", "id"): "product ID",
        ("products", "name"): "product name",
        ("products", "cost"): "product cost",
        ("users", "id"): "customer ID",
        ("users", "created_at"): "customer sign-up date",
        ("users", "age"): "customer age",
        ("users", "gender"): "customer gender",
        ("users", "country"): "customer country",
        ("users", "state"): "customer state",
        ("users", "city"): "customer city",
    }
)


def _check_phrases() -> None:
    """Import-time guard: every allowlisted table and column has a phrase."""
    if set(_TABLE_WORDS) != set(ALLOWED_TABLES) or set(_RECORD_WORDS) != set(ALLOWED_TABLES):
        raise RuntimeError("plain-language table phrases drift from the policy schema")
    columns = set().union(*ALLOWED_TABLES.values())
    if not columns <= set(_COLUMN_WORDS):
        raise RuntimeError("plain-language column phrases drift from the policy schema")
    for table, column in _QUALIFIED_WORDS:
        if column not in ALLOWED_TABLES.get(table, frozenset()):
            raise RuntimeError("plain-language qualified phrase names an unknown column")
    if any(ch.isdigit() for p in (*_TABLE_WORDS.values(), *_RECORD_WORDS.values(),
                                  *_COLUMN_WORDS.values(), *_QUALIFIED_WORDS.values())
           for ch in p):  # fmt: skip
        raise RuntimeError("plain-language phrases must not contain digits")


_check_phrases()

_ALL_COLUMNS: Final = frozenset(_COLUMN_WORDS)
_TABLES_ALT: Final = "|".join(sorted(map(re.escape, ALLOWED_TABLES), key=len, reverse=True))
_COLUMNS_ALT: Final = "|".join(sorted(map(re.escape, _ALL_COLUMNS), key=len, reverse=True))
#: Bare identifiers rewritten without backticks: only snake_case ones, so ordinary English
#: ("orders", "status", "users") is never touched.
_SNAKE: Final = sorted(
    (n for n in (*ALLOWED_TABLES, *_ALL_COLUMNS, DATASET) if "_" in n), key=len, reverse=True
)

_DATASET_RE: Final = re.compile(
    rf"(?P<the>\bthe\s+)?`?(?:[\w-]+\.)?{re.escape(DATASET)}(?:\.(?P<table>{_TABLES_ALT}))?`?"
    r"(?![\w`]|\.\w)(?:\s+(?:dataset|tables?)\b)?",
    re.IGNORECASE,
)
_TABLE_PHRASE_RE: Final = re.compile(
    rf"(?P<the>\bthe\s+)?(?:parent\s+)?`?(?P<table>{_TABLES_ALT})`?\s+tables?\b"
)
_COLUMN_PHRASE_RE: Final = re.compile(
    rf"(?P<the>\bthe\s+)?`?(?:(?P<table>{_TABLES_ALT})\.)?(?P<col>{_COLUMNS_ALT})`?"
    r"\s+(?:columns?|fields?)\b"
)
_DOTTED_RE: Final = re.compile(
    rf"(?<![\w.@/-])`?(?P<table>{_TABLES_ALT})\.(?P<col>{_COLUMNS_ALT})`?(?![\w`]|\.\w)"
)
_BACKTICK_RE: Final = re.compile(r"`(?P<body>[A-Za-z_][\w.]{0,79})`")
_BARE_RE: Final = re.compile(
    r"(?<![\w.@/`-])(?P<tok>" + "|".join(map(re.escape, _SNAKE)) + r")(?![\w`])"
)
_ALIAS_RE: Final = re.compile(r"[a-z]{1,3}")

_FENCE_RE: Final = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_PARAGRAPH_SPLIT_RE: Final = re.compile(r"(\n[ \t]*\n)")
_SENTENCE_START_RE: Final = re.compile(r"(?:\A|[.!?:]\s+|\n\s*(?:[-*+]\s+|\d+[.)]\s+)?)\Z")


def _column_phrase(table: str | None, column: str) -> str:
    if table is not None and (table, column) in _QUALIFIED_WORDS:
        return _QUALIFIED_WORDS[(table, column)]
    return _COLUMN_WORDS.get(column, column.replace("_", " "))


def _fit(match: re.Match[str], phrase: str) -> str:
    """Capitalise the phrase when it starts a sentence or list item."""
    if phrase and _SENTENCE_START_RE.search(match.string, 0, match.start()):
        return phrase[0].upper() + phrase[1:]
    return phrase


def _sub_dataset(m: re.Match[str]) -> str:
    table = m.group("table")
    if not table:
        return _fit(m, "the store data")
    return _fit(m, ("the " if m.group("the") else "") + _RECORD_WORDS[table.lower()])


def _sub_table_phrase(m: re.Match[str]) -> str:
    phrase = _RECORD_WORDS[m.group("table")]
    return _fit(m, ("the " if m.group("the") else "") + phrase)


def _sub_column_phrase(m: re.Match[str]) -> str:
    phrase = _column_phrase(m.group("table"), m.group("col"))
    return _fit(m, ("the " if m.group("the") else "") + phrase)


def _sub_dotted(m: re.Match[str]) -> str:
    table, col = m.group("table"), m.group("col")
    if col not in ALLOWED_TABLES[table]:
        return m.group(0)
    return _fit(m, _column_phrase(table, col))


def _sub_backtick(m: re.Match[str]) -> str:
    body = m.group("body")
    parts = body.split(".")
    if len(parts) == 1:
        name = parts[0]
        if name in _TABLE_WORDS:
            return _fit(m, _TABLE_WORDS[name])
        if name in _COLUMN_WORDS:
            return _fit(m, _column_phrase(None, name))
        return m.group(0)
    if len(parts) == 2:
        prefix, col = parts
        if prefix in ALLOWED_TABLES and col in ALLOWED_TABLES[prefix]:
            return _fit(m, _column_phrase(prefix, col))
        if _ALIAS_RE.fullmatch(prefix) and col in _COLUMN_WORDS:  # `oi.sale_price`
            return _fit(m, _column_phrase(None, col))
    return m.group(0)


def _sub_bare(m: re.Match[str]) -> str:
    tok = m.group("tok")
    if tok == DATASET:
        return _fit(m, "the store data")
    if tok in _TABLE_WORDS:
        return _fit(m, _TABLE_WORDS[tok])
    return _fit(m, _column_phrase(None, tok))


_PASSES: Final[tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...]] = (
    (_DATASET_RE, _sub_dataset),
    (_TABLE_PHRASE_RE, _sub_table_phrase),
    (_COLUMN_PHRASE_RE, _sub_column_phrase),
    (_DOTTED_RE, _sub_dotted),
    (_BACKTICK_RE, _sub_backtick),
    (_BARE_RE, _sub_bare),
)


def _humanize_prose(text: str) -> str:
    for pattern, repl in _PASSES:
        text = pattern.sub(repl, text)
    return text


def _humanize_unfenced(text: str) -> str:
    """Rewrite paragraph by paragraph (D-151a: SQL paragraphs are no longer exempt)."""
    parts = _PARAGRAPH_SPLIT_RE.split(text)
    return "".join(p if i % 2 else _humanize_prose(p) for i, p in enumerate(parts))


def humanize_identifiers(text: str) -> str:
    """Replace table, column and dataset identifiers in ``text`` with business words.

    Pure and deterministic. Never adds, removes or changes a digit; idempotent
    (``humanize_identifiers(humanize_identifiers(t)) == humanize_identifiers(t)``).
    Ordinary English words ("orders", "users", "status") are only rewritten in an
    identifier form: backticked, ``table.column``, or "the orders table". Fenced code
    blocks are returned unchanged; SQL is removed separately by :func:`strip_sql`.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    if not text:
        return text
    out: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        out.append(_humanize_unfenced(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_humanize_unfenced(text[pos:]))
    return "".join(out)


# --- D-151a: SQL is never shown in chat ----------------------------------------------------

#: Put in place of SQL removed from an answer. No digits, no identifiers.
SQL_REMOVED_NOTE: Final = "(Query details are not shown.)"

SQL_NOT_SHOWN_TEXT: Final = (
    "I don't show database queries in chat. Every figure I give comes from a query that "
    "was checked and run on the store data."
)
_NO_DATA_USED_TEXT: Final = (
    "There is no earlier answer in this session to describe yet. Ask a data question and I "
    "can tell you in plain words which data the answer is based on."
)

MAX_STRIP_CHARS: Final = 100_000  # longer text is replaced as a whole (fail closed)
MAX_DESCRIBE_SQLS: Final = 12
MAX_DESCRIBE_SQL_CHARS: Final = 8000
MAX_DESCRIBE_COLUMNS: Final = 12

_SQL_LANGS: Final = frozenset({"sql", "bigquery", "googlesql", "postgres", "postgresql", "mysql"})
_FENCE_FULL_RE: Final = re.compile(
    r"(?P<fence>`{3,}|~{3,})(?P<lang>[^\n`]*)\n?(?P<body>.*?)(?:(?P=fence)|\Z)", re.DOTALL
)
#: A SQL statement inside a code block (any case).
_SQL_BODY_RE: Final = re.compile(
    r"\bselect\b[\s\S]{0,2000}?\bfrom\b|\bwith\s+\w+\s+as\s*\(|\b(?:insert\s+into|update\s+\S+\s+set|delete\s+from|"
    r"create\s+(?:or\s+replace\s+)?(?:table|view)|drop\s+(?:table|view))\b",
    re.IGNORECASE,
)
_INLINE_CODE_RE: Final = re.compile(r"`([^`\n]{1,2000})`")
#: Unfenced SQL in prose: upper-case keywords only, so English ("select the top brands
#: from ...") is never touched. From the statement start to the end of the paragraph.
_PROSE_SQL_RE: Final = re.compile(
    r"`?\b(?:WITH\s+\w+\s+AS\s*\(|SELECT\b)(?=[\s\S]{0,2000}?\bFROM\b)[\s\S]*?(?=\n[ \t]*\n|\Z)"
)
#: Unfenced SQL in any case, removed only with a code signal in the same paragraph
#: (backtick, dataset name, aggregate call, GROUP/ORDER BY, comparison, snake_case column).
_PROSE_SQL_ANYCASE_RE: Final = re.compile(
    r"`?\bselect\b(?=[^\n]{0,2000}?\bfrom\b)[\s\S]*?(?=\n[ \t]*\n|\Z)", re.IGNORECASE
)
_SQL_SIGNAL_RE: Final = re.compile(
    r"`|thelook_ecommerce|\b(?:sum|count|avg|min|max)\s*\(|\b(?:group|order)\s+by\b"
    r"|\bwhere\s+\w+(?:\.\w+)?\s*(?:[<>=!]=?|\bin\b|\blike\b)|\b\w+\.\w*_\w*\b",
    re.IGNORECASE,
)


def _strip_fence(m: re.Match[str]) -> str:
    lang = m.group("lang").strip().lower().split(" ")[0] if m.group("lang") else ""
    if lang in _SQL_LANGS or _SQL_BODY_RE.search(m.group("body") or ""):
        return SQL_REMOVED_NOTE
    return m.group(0)


def _strip_inline(m: re.Match[str]) -> str:
    return SQL_REMOVED_NOTE if _SQL_BODY_RE.search(m.group(1)) else m.group(0)


def _strip_prose_anycase(m: re.Match[str]) -> str:
    return SQL_REMOVED_NOTE if _SQL_SIGNAL_RE.search(m.group(0)) else m.group(0)


def _strip_unfenced(text: str) -> str:
    # statements first, so a statement holding backticked tables is removed as a whole
    text = _PROSE_SQL_RE.sub(SQL_REMOVED_NOTE, text)
    text = _PROSE_SQL_ANYCASE_RE.sub(_strip_prose_anycase, text)
    return _INLINE_CODE_RE.sub(_strip_inline, text)


def strip_sql(text: str) -> str:
    """Remove SQL from user-facing text (D-151a). Pure, deterministic and idempotent.

    Removes fenced code blocks tagged as SQL or holding a SQL statement, inline code holding
    one, and unfenced text from an upper-case ``SELECT`` (or ``WITH x AS (``) followed by
    ``FROM`` to the end of its paragraph (any case when the paragraph also has a code
    signal such as a backtick, an aggregate call or ``GROUP BY``). Each removal becomes
    :data:`SQL_REMOVED_NOTE`. Non-SQL code blocks and ordinary prose are kept.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    if not text:
        return text
    if len(text) > MAX_STRIP_CHARS:
        return SQL_NOT_SHOWN_TEXT if _SQL_BODY_RE.search(text) else text
    out: list[str] = []
    pos = 0
    for m in _FENCE_FULL_RE.finditer(text):
        out.append(_strip_unfenced(text[pos : m.start()]))
        out.append(_strip_fence(m))
        pos = m.end()
    out.append(_strip_unfenced(text[pos:]))
    stripped = "".join(out)
    # several statements in a row become one note
    note = re.escape(SQL_REMOVED_NOTE)
    return re.sub(rf"{note}(?:\s*{note})+", SQL_REMOVED_NOTE, stripped)


def _sql_refs(sql: str) -> tuple[list[str], list[tuple[str | None, str]]]:
    """Allowlisted tables and (table, column) references of one statement, in order."""
    import sqlglot
    from sqlglot import exp

    try:
        trees = [t for t in sqlglot.parse(sql, read="bigquery") if t is not None]
    except Exception:  # unparsable: describe nothing rather than guess
        return [], []
    tables: list[str] = []
    aliases: dict[str, str] = {}
    cols: list[tuple[str | None, str]] = []
    for tree in trees:
        for t in tree.find_all(exp.Table):
            name = (t.name or "").lower()
            if name in ALLOWED_TABLES:
                if name not in tables:
                    tables.append(name)
                aliases[(t.alias_or_name or name).lower()] = name
        for c in tree.find_all(exp.Column):
            name = (c.name or "").lower()
            if name in _COLUMN_WORDS:
                cols.append(((c.table or "").lower() or None, name))
    resolved: list[tuple[str | None, str]] = []
    for qual, col in cols:
        table = aliases.get(qual) if qual else None
        if table is None:
            owners = [t for t in tables if col in ALLOWED_TABLES[t]]
            table = owners[0] if len(owners) == 1 else None
        resolved.append((table, col))
    return tables, resolved


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _data_phrase(sqls: object) -> str:
    """ "order records and product records, using brand and item sale price" or ""."""
    if isinstance(sqls, str) or not hasattr(sqls, "__iter__"):
        return ""
    tables: list[str] = []
    phrases: list[str] = []
    for i, sql in enumerate(sqls):  # type: ignore[attr-defined]
        if i >= MAX_DESCRIBE_SQLS:
            break
        if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_DESCRIBE_SQL_CHARS:
            continue
        ts, cols = _sql_refs(sql)
        tables += [t for t in ts if t not in tables]
        for table, col in cols:
            if col == "id" or col.endswith("_id"):
                continue
            phrase = _column_phrase(table, col)
            if phrase not in phrases:
                phrases.append(phrase)
    if not tables:
        return ""
    phrase = _join([_RECORD_WORDS[t] for t in tables])
    if phrases:
        phrase += f", using {_join(phrases[:MAX_DESCRIBE_COLUMNS])}"
    return phrase


def describe_data_used(sqls: object) -> str:
    """One plain sentence naming the data behind the given SQL, or "" when none is known.

    Tables and columns become the business phrases of this module; join keys (IDs) are left
    out. No table name, column name, literal or digit from the SQL reaches the text.
    """
    phrase = _data_phrase(sqls)
    return f"Based on {phrase}." if phrase else ""


def sql_request_reply(sqls: object) -> str:
    """The code-owned answer to "show me the SQL" (D-151a): no SQL, only business words."""
    phrase = _data_phrase(sqls)
    if not phrase:
        return f"{SQL_NOT_SHOWN_TEXT} {_NO_DATA_USED_TEXT}"
    return f"{SQL_NOT_SHOWN_TEXT}\n\nThe recent answers in this session are based on {phrase}."
