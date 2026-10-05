# D-151: plain-language chat answers (owner decision)

Scope: OWNER-QUEUE D-151. Not committed; owner review pending.

## Problem

Chat answers showed storage details: table names (`order_items`), column names
(`sale_price`, `orders.created_at`), "the orders table", the dataset name, and talk of SQL and
joins. The readers are business users. These terms add noise, and they expose the schema in
every answer. The product scope and the UTC time zone are useful context, but they were
mentioned either not at all or in schema terms.

## Decision

Chat answers use business language. They name no tables, columns or fields, and they do not
talk about SQL, queries, joins or the schema. They mention the scope and the time zone in plain
words where these matter ("for Calvin Klein products", "dates are in UTC"). In line with the
project's non-negotiables, the rule is enforced in code, not only in a prompt:

1. **Prompt rule (code-owned).** A shared constant, `PLAIN_LANGUAGE_RULE`, is added by code as
   a `## Answer language` section of every prompt that writes user-facing text. The persona
   cannot remove it: persona validation forbids words like "sql" and "rules", and
   `assemble_prompt` always places code sections before the fenced persona.
2. **Deterministic rewrite.** `humanize_identifiers(text)` replaces any identifiers the model
   still wrote with business words, after the output guard has allowed the answer.

**Exception (AC-02.2).** When the user explicitly asks "show me the SQL you used", the agent
still shows it (golden case `show_sql`, which requires SELECT and FROM). The prompt rule allows
SQL only in that case and only in a fenced code block. The rewrite leaves fenced blocks, and
any paragraph containing `SELECT ... FROM`, unchanged. This is a deliberate deviation from "no
code in chat": dropping it would break an accepted requirement.

## Design

`src/opsfleet_agent/guards/plain_language.py` (new, pure, no I/O):

- `PLAIN_LANGUAGE_RULE`, `PLAIN_LANGUAGE_SECTION` ("Answer language"),
  `REPORT_PLAIN_LANGUAGE_RULE` (the report-writer variant: text values only, every key kept,
  the system appends the SQL).
- `SCHEMA_TERMS_REWRITTEN = "schema_terms_rewritten"` is the trace rule code. It is lowercase,
  like the output guard codes.
- `humanize_identifiers(text) -> str`:
  - The identifier set comes from `sql_policy.ALLOWED_TABLES` plus the dataset name. An
    import-time check fails if any allowlisted table or column has no phrase, so a schema
    change cannot slip through quietly.
  - Six ordered passes:
    1. Dataset references (`bigquery-public-data.thelook_ecommerce[.table]`, "... dataset")
       become "the store data" or "order records".
    2. "the X table" becomes "the order records", "the customer records", and so on.
    3. "the `col` column/field" becomes "the sale price".
    4. `table.column`, with or without backticks, uses a table-aware phrase:
       `orders.created_at` becomes "order date", `users.created_at` becomes "customer sign-up
       date", `order_items.sale_price` becomes "item sale price".
    5. A backticked known identifier or `alias.column` (for example `oi.sale_price`) loses its
       backticks and is rewritten. Unknown backticked text is left alone.
    6. Bare snake_case identifiers (`sale_price`, `order_items`, `traffic_source`) are
       rewritten. These passes match case-sensitively and only at word boundaries.
  - Ordinary English is never touched. Bare words like "orders", "users", "status" and
    "brand" are rewritten only in an identifier form (backticked, `table.column`, "the orders
    table").
  - A replacement that starts a sentence or list item is capitalized.
  - **Numbers never change.** No replacement phrase contains a digit (checked at import), and
    no identifier contains one. The rewrite is **idempotent**, because its output has no
    underscores, backticks, dotted identifiers or "X table" phrases left.
  - Code is left as is: fenced blocks (including an unclosed fence), and paragraphs that
    contain uppercase `SELECT` followed by `FROM`.

## Where it is enforced

| Site | Prompt rule | Rewrite |
|---|---|---|
| Quick / deep analyst (`roles/analyst.build_system_prompt`) | yes, after "Analyst rules" | yes, in `graph._finalize` |
| Force answer (`graph._force_text`), including the template fallback | yes | yes, in `graph._finalize` (same branch) |
| Light path, model small-talk reply (`roles/light_path`) | yes | yes, only when `source == "model"` |
| Light path, static capabilities text / templates | n/a (code text) | no |
| Clarification (`graph/context._clarification_for`) | n/a (code-built, not model-written) | no |
| Report writer (`roles/report_writer`) | `REPORT_PLAIN_LANGUAGE_RULE` (prose only) | no: the report artifact, its required sections and the appended SQL stay unchanged |
| Refusal / error / delete / confirm texts | n/a (code text) | no |

**Why after the guard.** The output guard judges the model's own words: PII, URLs, injection,
prompt leak, grounding. The rewrite only swaps known identifiers for fixed code phrases. It
cannot add a digit, URL, HTML or personal data, so the guarded verdict still holds. Rewriting
first would mean the guard checks text the model never wrote. A blocked answer is never
rewritten: it is replaced by the refusal text.

**Trace.** When the rewrite changes the text, `schema_terms_rewritten` is added to the guard
output span's `rule_hits` (and to `ctx.guard_codes` in the graph). The verdict stays `allow`.

The rule text is **not** added to the protected (leak) snippets. A correct answer often echoes
part of it ("dates are in UTC"), and per-sentence leak matching would then block a good answer.

## Tests

`tests/unit/test_plain_language.py` (15 tests, offline):

- The example answer is rewritten into business words: no backticks, snake_case or dotted
  identifiers remain, and `$7,715.29`, `87.5%` and `Q1 2024` are kept.
- Ordinary English ("orders", "users", "status") is unchanged, and the empty string is handled.
- Each identifier form maps to the expected phrase, and capitalization works at sentence start.
- Idempotence.
- Digit sequences are identical before and after.
- SQL the user asked for is kept: a fenced block, a bare `SELECT ... FROM` paragraph, and an
  unclosed fence.
- A non-str input raises `TypeError`.
- The prompt contains the rule for the quick analyst, the deep analyst, the light path (through
  the graph), the force answer (through the graph), and the report writer (the prose variant;
  the report still renders all its sections).
- Graph integration: an allowed analyst answer is rewritten, and the guard span records `allow`
  plus `schema_terms_rewritten`. A plain answer gets no code. A light model reply is rewritten
  and traced.

Offline evals replay recorded model text, so they are not affected. The `show_sql` golden case
keeps its SQL through the exception above.

## Risks

- Prompt-only coverage for prose outside identifiers. The rewrite cannot remove words like
  "joined" or "query"; the prompt rule handles those. A live eval (LLM judge) could measure
  compliance later.
- Grammar. "The users table has" becomes "The customer records has". This is rare, since the
  prompt rule makes identifiers uncommon in the first place.
- A user who writes an identifier on purpose (for example "what does `sale_price` mean?") gets
  the business phrase back. This is acceptable for business readers, and SQL they explicitly
  request is kept.
