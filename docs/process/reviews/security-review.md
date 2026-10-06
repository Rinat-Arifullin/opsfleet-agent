## Security Review: iteration 39b (HEAD 812b29d) — SQL path, delete, audit, secrets, observability

Date: 2026-10-06 · Reviewer: T1 (two subagents: Part A SQL path with ~190 pure-function probes, Part B high-stakes paths; every anchor sed-verified, no network, `.env` never read) · **Verdict: APPROVED**

> Checklist (security-reviewer.md): LLM01 prompt injection · LLM02 PII disclosure · Authorization / product scope · LLM06/LLM08 excessive agency · SQL execution · LLM10 unbounded consumption · Secrets & supply chain. Ids `SEC-An` (SQL path), `SEC-Bn` (high-stakes). Full probe logs and the 53 + 43 verified-blocked rows are in the subagent reports kept in the session scratchpad; the rows below are the summary.

### CRITICAL
None.

### HIGH
None.

### MEDIUM
None.

### LOW
- **SEC-A1 (docs)** — `docs/technical.md:199` cites `_pipeline` at `run_sql.py:701` (actual `:819`); §4 lists aggregate-only before policy while code runs `apply_scope :824` then `aggregate_only_plan :831-834`. `tests/unit/test_run_sql_order.py::test_order_matches_hld_5_1` pins the code order. Same as RC-4.
- **SEC-A2 (fail-closed false positive)** — `SELECT COUNT(*) FROM users` and `COUNT(DISTINCT u.id) FROM users u` are refused as `small_cell_unplaceable`: QI columns inside the `__u` code-CTE body set `references_qi` (`sql_policy.py:835-836`, `scope_ctes.py:112-134`), and the planner (`small_cell.py:286-287` → `:269-270`) has nowhere to place the threshold. Derive `references_qi` from columns outside code-CTE bodies (`_in_code_cte` already exists in `differencing.py`). No test.
- **SEC-B1** — `LANGGRAPH_AES_KEY` is used as raw AES-EAX key material with only a 16/24/32-length check (`graph/graph.py:300-304`); a typed passphrase is accepted and `checkpoints.db` could be brute-forced offline. Require hex/base64 key material or derive the key with a KDF; document `openssl rand -hex 16` in the README.

### INFO
- **SEC-A3** — `verify_scoped` (`scope.py:340,405-407`) is a structural invariant, not a tamper check; harmless because `run_sql.py:869` re-verifies the exact code-produced string. Say so in the docstring and technical.md §4 step 8.
- **SEC-A4** — pseudonymous per-customer rows are allowed outside `aggregate_only` turns (accepted D-157; refused on aggregate-only via `sql_policy.py:314 CUSTOMER_GRAIN`).
- **SEC-B2** — LangGraph stores `thread_id` and the `metadata` JSON in plaintext next to the encrypted blobs (`sqlite/__init__.py:418-436`); harmless today, add a regression test on the metadata keys so a future `configurable` user id does not persist unencrypted.
- **SEC-B3** — `access_set` / `apply_persona` / `rollback_persona` are audit-first but have no maintainer check and no entrypoint yet (`commands/access.py:197`, `pyproject.toml:35-36`); when wired, gate at the entrypoint like `erase.py:325-328` / `triage.py:690-693`.
- **SEC-B4** — on-disk configuration (persona file, `config/maintainers.yaml`, erase confirm key) is trusted without an audit trail; a direct file edit bypasses the audit row.
- **SEC-B5** — what Langfuse receives when enabled is documented in the Part B report: tool results omitted (`langfuse_sink.py:180-184`), NER mask + scrub + 4000-char cap + SDK `mask=` (`:104-136,145-155,384-401`).

### Verified blocked (summary)
| Area | Evidence |
|---|---|
| PII columns / taint lineage, function allowlist, `SELECT *`, parse failure fail-closed | `sql_policy.py:716-755, 641-667, 764-766, 1160-1175`; code CTEs project `ALLOWED_TABLES − PII_COLUMNS` with an import-time check `scope_ctes.py:97-109` |
| Scope bypass (`OR 1=1`, LEFT JOIN, UNION) | table rewrite `scope.py:303-334`, predicate chain `scope_ctes.py:83-90`, `required_ctes :137-149`, byte-equal templates `scope.py:340-416` |
| Only this runner's `Prepared` executes | `bq/client.py:332-369`; cost caps `:47-52` |
| Small cell: id grain, HAVING, `WHERE id=`, subquery/window/QUALIFY/DISTINCT/ROLLUP, differencing | `small_cell.py:246-277, 391-405, 451-503`; `differencing.py:388-470, 532-681`; `store/fingerprints.py:364-389`; duplicate statement `run_sql.py:845-848`; `_scrub_rows :528-585`; `_merge_small_bands :595-716` |
| Delete token: in-memory key, replay, owner/session, 600 s, declined, TOCTOU `ids_sha256` | `delete/token.py:62-78`, `delete/flow.py:75, 111, 190-241, 293, 384, 513-533, 597-620`; tool not exposed to the analyst, `gate_step` refuses mixed steps `:448-475, 672-679` |
| Audit-first delete, append-only triggers, per-user `/audit` | `store/audit.py:1118-1189, 532-533, 678`; `store/audit_schema.py:48-53`; `commands/audit.py:70-75` |
| Erase / triage maintainer gates, HMAC confirm 300 s, symlinks skipped, wrong AES key refused, PII in `--question` | `commands/erase.py:64, 119-131, 155, 164-176, 191-256, 325-333, 371-372`; `commands/triage.py:237-250, 501-510, 522-621, 690-693` |
| Export traversal/overwrite/symlink, rename control chars, owner-bound UPDATE | `commands/report_actions.py:102, 130-166, 217-236, 313-371`; `store/reports.py:283-289` |
| Secrets never logged: `register_secret`/`scrub_text`, `drop_sensitive`, `sanitize_sql`, 0700/0600, `RedactingFilter` | `cli.py:169-171`; `obs/tracer.py:150-234, 309-337` |
| Guards fail closed on exception; output guard caps, unexpected actions, injection/images/URLs | `guards/input.py:524-533`; `guards/output.py:179, 398-490` |
| DB hygiene | `store/db.py:49-91` (0600, `secure_delete`, WAL TRUNCATE); checkpoints AES-EAX, random nonce, `decrypt_and_verify` (`graph.py:285-319`, `encrypted.py:63-78`) |

### Verdict
**APPROVED.** 0 CRITICAL, 0 HIGH, 0 MEDIUM, 3 LOW, 6 INFO. Nothing blocks the prototype; SEC-B1 and SEC-B2 are recommended before the HLD uses "production" wording for the checkpoint store.
