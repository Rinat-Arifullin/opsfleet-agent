# Decisions log

Gate approvals and ADRs, newest last. Each ADR records the context, the decision, the rejected alternatives and the consequences.

**Status:** rev. 4.4 (2026-10-04). ADR-001..015 (15 ADRs): ADR-001..014 **Accepted** at 🔴 G2 (owner approval 2026-10-04); ADR-015 (dev-only local LLM provider) **Accepted** by the owner as D-143 (2026-10-05). Rev. 4.3 applied the independent review (`docs/process/03b-independent-review.md`) and recorded four owner decisions as proposals. Rev. 4.4 records the owner's answers to those four (2026-10-04) and closes the five residual risks in the design (ADR-004, 005, 007, 013, 014; section "Owner decisions at G2 review" below). Owner choices on open questions are not ADR approvals; every ADR stays Proposed until 🔴 G2.

## G1: Requirements approved (2026-10-04)

- `docs/process/01-requirements.md` (revision 2) approved by the owner.
- Six questions sent to the client (§8.2): access model, report access, PII, churn, retention, data and load profile. Work continues on the defaults in brackets; any answer that differs is logged here as a change.
- Owner decisions made during the review:
  - answers limited to the user's scope, with an explicit scope label;
  - Saved Reports design, the cloud (GCP), revenue, calendar, residency, SSO, SLA and persona approval are our decisions, not client questions;
  - R1 seed, R4.1 and R8 are planned prototype extras (P);
  - the audit log (M) and feedback capture (P) are in scope;
  - quality over the 6–12 h estimate.

## Step 2 digest: owner inputs (2026-10-04)

- **Framework:** the owner has LangChain/LangGraph experience but asked that the framework be chosen on requirements fit. The orchestrator re-scored ADK vs LangGraph (`02-design-digest.md` §1 rev. 2) and now recommends **LangGraph**. Pending the owner's OK; the full ADR is written by the architect.
- **Langfuse:** adopted for traces (R7), persona prompt management (R8), eval datasets (R6) and feedback scores (R4). Optional at runtime: the prototype works without keys. Traces pass a PII mask hook (risk area 1).
- **2026-10-04 owner OK:** LangGraph approved, the Langfuse plan approved (including the trace PII masking). Prototype Langfuse: **self-hosted in Docker Compose** (v4, headless init), optional at runtime.

---

# Architecture Decision Records (Step 2, 2026-10-04)

*Superseded by rev. 4.2/4.3:* the paragraph below describes the Step 2 state. ADR-001..008 came from the digest; ADR-009..012 were added in HLD rev. 3–4, ADR-013 in rev. 4.3 and ADR-014 in rev. 4.4; Step 3 reviews 1, 2 and 3 are complete. All ADRs stay **Proposed** pending 🔴 G2.

The ADRs below were written by the architect from the approved digest (`docs/process/02-design-digest.md`). They are detailed in `docs/architecture.md`. All are **Proposed**, pending the design review (Step 3) and 🔴 G2. ADR-001 and ADR-002 restate decisions the owner already approved on 2026-10-04.

## ADR-001: Agent framework is LangGraph 1.x (Google ADK rejected)

- **Status:** Accepted at 🔴 G2 (2026-10-04). The owner approved the direction on 2026-10-04.
- **Context:** we need:
  - a deterministic guardrail pipeline around a tool-calling LLM (R2);
  - a durable pause for delete confirmation (R3);
  - a bounded retry and model fallback chain (R5);
  - per-turn tracing (R7);
  - a path to Postgres-backed sessions in production.

  The assignment asks us to state the framework and our experience with it. The author is **experienced with LangChain and LangGraph**. The owner asked that the choice be made on requirements fit, not on experience.
- **Decision:** LangGraph 1.x, with `langchain-google-genai` for Gemini:
  - the explicit graph (`load_context → input_guard → agent ⇄ tools → output_guard → finalize`, plus `confirm_delete` with `interrupt()`);
  - `SqliteSaver` for the prototype, `PostgresSaver` for production;
  - `.with_retry()` and `.with_fallbacks()` on the chat model.

  Guardrails live in our own policy module, called from nodes and tools, never in framework internals.
- **Alternatives:**
  - **Google ADK.** Gemini-native, with callbacks, tool confirmation and `adk eval`. Rejected because its main advantages are neutralised: Gemini works through `langchain-google-genai`, and evals live in Langfuse (ADR-002). Its 2.x API is still moving fast, and fallback across models would need LiteLLM or custom code.
  - **A hand-rolled loop on `google-genai`.** It would mean rebuilding checkpointing, durable interrupts and retry plumbing.
- **Consequences:**
  - Each guardrail is a unit-testable node or function.
  - The confirmation pause survives restarts.
  - We depend on the LangChain ecosystem's release cadence: versions are pinned and upgrades pass the eval gate.
  - Swapping the framework later stays cheap, because enforcement and the delete token are framework-independent (ADR-004, ADR-007).
  - LangGraph re-runs an interrupted node from its start, so code before `interrupt()` must be read-only.
- **Revised:** 2026-10-04 (ADR-009). The single `agent ⇄ tools` loop is replaced by a deterministic code supervisor (the parent graph) dispatching five role subgraphs; `confirm_delete` and `execute_delete` stay in the parent graph. Retries and fallback are owned by one call wrapper (ADR-003), not by `.with_retry()` / `.with_fallbacks()`.

## ADR-002: Observability, prompt management and eval datasets on self-hosted Langfuse

- **Status:** Accepted at 🔴 G2 (2026-10-04). The owner approved it on 2026-10-04, including self-hosted Docker for the prototype.
- **Context:**
  - R7 needs deep per-turn traces;
  - R8 needs non-developers to change the persona without a redeploy;
  - R6 needs eval datasets and experiment comparisons;
  - R4 needs feedback attached to traces.

  Traces are 🔴 risk area 1: they must not become a PII side channel. Traces should stay inside our own perimeter.
- **Decision:**
  - **Prototype:** Langfuse v4 in Docker Compose (`infra/langfuse/docker-compose.yml`), with pinned images, loopback-only ports and headless init through `LANGFUSE_INIT_*`. It is optional and fail-open: without keys, the agent writes local JSONL traces and reads the local persona file.
  - **Production:** self-hosted on GCP. Web and worker run on Cloud Run, ClickHouse on GKE Autopilot (or a VM), with Memorystore Redis, Cloud SQL Postgres and GCS. Cloud Logging and Monitoring carry service metrics and alerts.
  - **PII:** spans are built from post-scrub data only, and `Langfuse(mask_otel_spans=...)` runs the same scrubber at export. JSONL traces use the same function.
  - **Persona:** `get_prompt(label="production", cache_ttl_seconds=60, fallback=...)`.
- **Alternatives:**
  - **Langfuse Cloud:** traces leave our project.
  - **Cloud Trace only:** no LLM-aware views, prompt management or datasets.
  - **LangSmith:** SaaS by default, and self-hosting is an enterprise offering.
  - **A custom persona store and UI:** we would rebuild versioning and rollback.
- **Consequences:**
  - One tool covers R4, R6, R7 and R8.
  - The ops cost is a six-container stack (about 4 GB RAM locally) and a ClickHouse to run in production. Production hosting (owner decision 2026-10-04): web and worker on Cloud Run, Postgres in Cloud SQL, Redis in Memorystore, blobs in GCS, ClickHouse on GKE Autopilot; the all-on-GKE Helm option was rejected.
  - Langfuse outages never block a turn.

## ADR-003: Model routing, retries and fallback

- **Status:** Accepted at 🔴 G2 (2026-10-04).
- **Context:** the assignment prefers recent Gemini models, and the free tier has tight rate limits. R5 requires resilience to provider failures without runaway cost.
- **Decision:**
  - **Primary:** `gemini-3.8-flash` for agent steps, SQL and reports (structured `ReportDraft` output with 1 repair attempt).
  - **flash-lite:** `gemini-3.1-flash-lite` for the router, the light path, history summaries and as the fallback for the agent.
  - **Model IDs** live in `config/models.yaml`. `gemini-3.8-flash` and `gemini-3.1-flash-lite` were checked against the provider's model list on 2026-10-04 (rev. 4.3; see ADR-009 note); they are re-checked at the start of Step 4. A startup check fails fast with a clear message if a configured ID is unknown. Production uses Vertex AI.
  - **One call wrapper owns all retries and the fallback** (`call_llm(role, messages)`, owned by the turn budget). There is no stacking of `with_retry` and `with_fallbacks`:
    - SDK retries are disabled: `max_retries=1` means a single attempt (0 would mean the Google default of 5 retries).
    - `bind_tools` is applied to the primary and the fallback model before wrapping, so the fallback sees the same tools.
    - Each attempt's timeout is min(60 s, remaining turn time). Every attempt is counted against the turn's LLM-call budget.
    - Primary: up to 3 retries per call and 6 per turn, backoff 1 s, 2 s and 4 s plus jitter, on 429, 5xx and timeouts only. A retry starts only if the backoff plus a 10 s minimum attempt fits in the remaining time.
    - Fallback: flash-lite, a single attempt, under the same rule.
    - Then `force_answer` if a call and time remain, otherwise a templated message.
    - Tests: `test_retry_wrapper_bounded` (asserts attempt count and wall time under a fake clock with always-failing models) and `test_sdk_single_attempt`.
  - **Budgets:** a client-side rate limiter whose waits are recorded and count against the turn deadline.
  - **Router:** fails open to the rules only, label `complex`, because enforcement is in code.
  - **Production:** a circuit breaker per (model, region) that opens after 5 failures in 60 s. The chain continues to flash in a secondary US region.
  - **Tracing:** `model_used`, `fallback_used`, `retries` and `limiter_wait_ms` on every generation span.
- **Alternatives:**
  - **A pro-class model everywhere:** higher latency and cost with no measured gain on 4-table SQL. It stays a config option behind the eval gate.
  - **A second vendor as the fallback:** a second contract and privacy review, and a different tool-calling dialect.
  - **Relying on SDK retries:** opaque and unbounded at the turn level.
  - **LangChain `with_retry` plus `with_fallbacks`:** the layers multiply, and neither knows the turn deadline.
- **Consequences:**
  - Answers may come from the weaker model during incidents. This is visible in the trace and tracked by the fallback-rate metric (ticket at > 20%).
  - Vertex availability of both models must be confirmed in Step 4.
  - The worst-case turn time is bounded by the turn deadline, not by the product of retry layers.
- **Revised:** 2026-10-04 after the design review (`docs/process/03-design-review.md`).
- **Revised:** 2026-10-04 (ADR-009). Models are bound per role: flash-lite for the router (formerly the classifier), Quick analyst, report verifier and Library agent; flash for the Deep analyst and report writer (a pro-class model for Deep is a `config/models.yaml` choice in either environment since rev. 4.2, used only if it passes the golden-suite gate on the Deep-tagged cases; per-role suites and gates are roadmap, see ADR-009). Every role keeps the same wrapper, the same fallback rule and one shared `TurnBudget`.
- **Revised (rev. 4):** 2026-10-04 after Design Review 2 and requirements rev. 4. Every provider attempt (retry or fallback) counts against the calling role's sub-cap as well as the turn cap, so retries in one role cannot starve the next (R2-M4).

## ADR-004: Guardrails are enforced in code at the tool boundary; prompts only guide

- **Status:** Accepted at 🔴 G2 (2026-10-04).
- **Context:** R2 forbids PII in output and restricts each user to their own products. The model can be prompt-injected directly or through data, so any control the model can be talked out of is not a control. This covers 🔴 risk areas: PII masking, product scoping, the SQL execution policy and the prompt and guardrail layers.
- **Decision:** a layered pipeline in which each layer's gaps are covered by the next:
  1. input guard: rules, then the flash-lite router (user messages only; ADR-010);
  2. prompt safety core;
  3. the sqlglot SQL policy in `run_sql`: a single SELECT, a 4-table allowlist, an allowlist of FROM source types, CTE names that shadow a table rejected, scope-aware name resolution, and the PII denylist across all clauses and functions;
  4. scope rewrite into PII-free CTEs, re-validated after the rewrite (ADR-008);
  5. the small-cell rule (k = 5, FR-69), applied after brand scoping and only to breakdowns by customer quasi-identifiers (never to product-only groups such as brand, category or month), for quasi-identifiers (age, gender, city, state, country, traffic_source and anything derived from them), in three cases:
     - (a) grouping by a quasi-identifier: code injects `HAVING COUNT(DISTINCT user_id) >= 5` at that level, before any outer ORDER BY or LIMIT, and reports suppressed groups;
     - (b) user or order grain: no quasi-identifier in the projection, ORDER BY or window partition; a quasi-identifier filter is allowed only if a separate capped population check returns at least k users;
     - (c) windows over a quasi-identifier, nested aggregates or correlated quasi-identifiers: rejected (`small_cell_unplaceable`).

     "Top customers by spend" (user_id grain, no quasi-identifier) stays allowed, with an eval case;
  6. dry-run plus `maximum_bytes_billed`, session budget and row cap;
  7. a result scrubber;
  8. an output guard (PII, scope label, grounding), plus the action allowlist per role and router label and the answer injection scan (FR-75), failing closed;
  9. trace masking;
  10. the code-confirmed delete (ADR-007);
  11. the context scope filter (FR-76): history, summaries, Golden examples, reports and preferences are filtered by the current scope in code before the prompt is built.

  Every layer is unit-tested with the earlier layers disabled. The precedence order is safety > report format > persona and preferences.
- **Alternatives:**
  - **Prompt-only guardrails:** bypassable.
  - **BigQuery permissions only:** the public dataset exposes every column, and production views alone would not stop row-level PII leaks through free text.
  - **Regex SQL filtering:** bypassed by aliases, `*` and functions. (`SELECT *` is now rejected outright, `select_star`, under frugality §6.2a.)
  - **A third-party guard service:** it adds latency and a data processor, and does not understand our schema.
- **Consequences:**
  - A fully hijacked model still cannot leak PII columns, widen scope or delete.
  - The SQL policy adds complexity and can over-block. Over-blocks are traced by rule ID and tuned through evals.
  - Residual risk: differencing attacks across many aggregates. The differencing guard (FR-70) refuses an aggregate whose population differs by fewer than k customers from one this user already received, in the session or in any session of the last 30 days (per-user fingerprints with no values, rev. 4.4). Collusion between users is detected by an org-wide probing detector in production, not prevented; differential privacy is a roadmap option. Differencing of id-grain lists is handled by ADR-013 (option A, chosen by the owner 2026-10-04).
  - Typed PII (rev. 4.4): the input and output guards run a local NER detector (Presidio with spaCy `en_core_web_sm`) after the regex scrubber, with a brand allowlist; recall on `adversarial/pii_typed/*` is a release gate (≥ 95%), brand false positives 0.
  - The population check in case (b) costs one extra capped query against the SQL budget.
- **Revised:** 2026-10-04 after the design review (`docs/process/03-design-review.md`).
- **Revised (rev. 4):** 2026-10-04 after Design Review 2 and requirements rev. 4. Layers 1, 5, 8 and 11 updated (router, FR-69 after brand scoping, FR-75, FR-76); FR-70 differencing guard; `SELECT *` rejected (R2-M6).

## ADR-005: Stores are SQLite for the prototype and Cloud SQL Postgres for production; audit first

- **Status:** Accepted at 🔴 G2 (2026-10-04).
- **Context:**
  - The prototype must run from the README alone on a reviewer's machine.
  - R3 needs the audit record written before the delete, with the delete aborted if the audit write fails (`CLAUDE.md` non-negotiable).
  - Production needs HA, PITR, checkpoints, a vector index for the Golden Bucket, and an immutable audit trail.
- **Decision:**
  - **Prototype:**
    - SQLite in WAL mode with `BEGIN IMMEDIATE` and a busy timeout, for reports, audit, preferences and feedback.
    - A separate SQLite file for `SqliteSaver` checkpoints.
    - JSONL traces, and a YAML Golden seed with a local embedding cache.
  - **Production:**
    - Cloud SQL Postgres (HA, PITR) for app data, `PostgresSaver` checkpoints encrypted with `EncryptedSerializer`, and pgvector plus full-text search for Golden retrieval.
    - Audit in an append-only table (INSERT and SELECT only) plus a transactional outbox → Pub/Sub → a delete-protected BigQuery dataset (1 year).
    - Hard delete, no restore (A-15). Backups: PITR 7 days plus a daily export kept 30 days; a restore re-applies the audited deletes before the database is reopened (A-34).
  - **Delete transaction:** `confirm_delete` validates and commits `delete.confirmed`. `execute_delete` then commits `delete.executed` and the DELETE in one transaction, audit row first; any failure rolls back. A unique key on `(pending_action_id, event_type)` makes a replayed node a no-op.
- **Alternatives:**
  - **Firestore:** a weaker fit for the multi-row transactional audit-first rule, and it needs a separate vector store.
  - **AlloyDB:** more than the scale needs.
  - **Vertex AI Vector Search:** justified only past about 1M vectors; it does no lexical search and has its own deploy lifecycle.
  - **Files or JSON for reports in the prototype:** no transactions.
- **Consequences:**
  - The prototype and production share one relational model, so the port is a backend swap behind the `ReportStore` and `AuditLog` interfaces.
  - One production database to operate. Audit RPO is 0 for acknowledged deletes.
  - Deleted reports may persist in backups for up to 30 days; this is stated to the client (HLD §13.2 Q8).
- **Revised (rev. 4):** 2026-10-04 after Design Review 2 and requirements rev. 4. Hard delete and A-34 backups replace soft delete; the confirm/execute split and the unique audit key (R2-M1).

## ADR-006: Production on GCP (Cloud Run, IAP, Vertex AI, BigQuery authorized views, Cloud SQL)

- **Status:** Accepted at 🔴 G2 (2026-10-04). The cloud choice is ours (A-13).
- **Context:** the data is in BigQuery, the models are Gemini, the audience is 50–500 internal users, and SSO goes through the client's IdP with US residency.
- **Decision:**
  - **Compute:** Cloud Run for the agent API, web UI, admin console and Golden ingestion job. *Superseded by rev. 4.2:* there is no admin console and no application roles; access is a brand list or `all`, set with `access set` (FR-08), and team tooling (access, triage, audit viewer, erasure) runs as IAM-gated Cloud Run Jobs.
  - **Edge and identity:** a load balancer with Cloud Armor, and IAP with Identity Platform federated to the client IdP. The app verifies the IAP JWT.
  - **Models:** Vertex AI Gemini.
  - **Warehouse:** BigQuery reached through **per-scope authorized views** (brand-filtered, PII columns excluded) read by impersonated per-scope service accounts, in addition to the code rewrite.
  - **State:** Cloud SQL Postgres.
  - **Security:** Secret Manager and workload identity, with no key files. VPC Service Controls around the data services.
  - **Operations:** Cloud Logging and Monitoring alerts; GitHub Actions with eval gates (Workload Identity Federation, owner decision 2026-10-04); Cloud Run traffic splitting for canaries; Cloud Scheduler and Eventarc for Golden refresh.
  - **Region:** US.
- **Alternatives:**
  - **GKE for everything:** more operations than needed.
  - **Vertex AI Agent Engine:** couples the runtime to ADK-style deployment.
  - **BigQuery row access policies in place of authorized views:** the brand predicate needs a join to `products`, which a row policy on `order_items` cannot express without denormalising.
  - **App-level OAuth:** more security code.
  - **Another cloud:** the data and models are on GCP.
- **Consequences:**
  - Two independent scope layers in production (code and IAM). A bug in one does not leak.
  - Per-scope views and service accounts must be generated when scopes change, which the admin console automates. *Superseded by rev. 4.2:* generation runs from `access set` as an IAM-gated Cloud Run Job; there is no admin console.
  - It is a GCP lock-in, accepted.

## ADR-007: Delete confirmation uses a code-issued token held outside the LLM

- **Status:** Accepted at 🔴 G2 (2026-10-04). Covers 🔴 risk area: destructive operations.
- **Context:** R3 requires a strict confirmation that does not break the UX. The model must not be able to confirm on the user's behalf, whether by hallucination, injection or a "pre-confirmed" request (AC-12.7, AC-12.9). The deleted set must equal the previewed set (AC-12.6).
- **Decision:**
  - The model-facing `delete_reports` tool is **preview only**.
  - `delete_reports` must be the only tool call in its step (`delete_not_alone`), and a thread holds at most one pending delete (`delete_pending`).
  - Code computes the owner-scoped match and derives a token bound to the SHA-256 of the sorted IDs, the owner, the session, the preview turn and an expiry (10 min): `token = HMAC-SHA256(K_delete, pending_action_id ‖ ids_sha256 ‖ owner ‖ session_id ‖ preview_turn ‖ expires_at)` (rev. 4.4). The token is **never stored**: graph state and checkpoints hold only its SHA-256, and the CLI or web API (trusted code that holds `K_delete`) re-derives it and resumes with a proof `HMAC(token, reply ‖ pending_action_id ‖ ids_sha256 ‖ preview_turn ‖ expires_at)`, never the token itself (R2-M2).
  - **Key `K_delete` (rev. 4.4, R3-H8):** in the prototype, 32 random bytes generated in memory at process start (never written to disk, logs or traces). In production, one Secret Manager secret read by every instance, so any instance can re-derive and verify. Rotating the key, or a prototype restart, changes the derived hash, so a pending delete fails verification and expires (fails safe; at most 10 minutes of pending deletes are affected).
  - The `delete.previewed` audit record is insert-or-ignore, keyed by thread and step, so a replayed node does not duplicate it.
  - **Token issuance (rev. 4.3, R3-M31):** `pending_action_id` is generated by code (not taken from the framework's interrupt id) and carried in the interrupt payload. The token is issued and the pending action written in the **preview** node, a checkpointed parent-graph node before `confirm_delete`, with a get-or-create write of the pending action (its fields and `token_sha256`), so a re-run of the preview node keeps the first pending action and therefore the same derived token. `confirm_delete` only calls `interrupt()` and verifies; it writes nothing before `interrupt()` and is safe to re-run. Test: `test_confirm_delete_rerun_keeps_token`.
  - **No store dependency for delete (rev. 4.4):** verification needs only `K_delete` and the checkpointed pending action, so delete confirmation does not depend on Memorystore. Memorystore keeps only the per-user quota counters (FR-58) and the per-user rate limiter, with Cloud SQL as the durable fallback.
  - **Edge case: resume lands on a different instance.** That instance reads the same `K_delete`, re-derives the token from the checkpointed pending action, checks it against `token_sha256` and recomputes the proof (`test_confirm_delete_on_other_instance_verifies`). If the derived hash differs (key rotated or prototype restart) or the action expired, `delete.expired` is audited, nothing is deleted, the user is told the preview expired, and the message is routed to `input_guard` as a new turn (`test_key_rotation_expires_pending_delete`).
  - **Single use:** the pending action is consumed on the first resume, whatever the outcome, so a replayed proof finds nothing to confirm. Test: `test_delete_token_derived_not_stored`.
  - `confirm_delete` calls `interrupt()`, and the CLI shows a code-rendered preview (the count and the first 20 items).
  - The CLI resumes with `Command(resume={reply, pending_action_id, proof})`.
  - `confirm_delete` validates; `execute_delete` deletes (ADR-005). Execution requires an exact English confirm word from a configured list, a proof that verifies in constant time against the re-derived token, a matching ID hash, no expiry, and the reply arriving on the turn right after the preview (`preview_turn + 1`).
  - Any other reply cancels (`delete.cancelled`) and is routed to `input_guard` as a new message with a fresh turn budget.
  - In the prototype a process restart generates a new `K_delete`, so a pending delete always expires on `--resume` (`delete.expired`; nothing is deleted; R2-M3).
  - Audit rows are unique on `(pending_action_id, event_type)`.
  - The pending action, `__interrupt__` and the resume proof are dropped by key from every trace sink (OTel span masking, the Langfuse callback filter and the JSONL writer) through one shared function. Tests: `test_delete_token_never_in_traces`, `test_checkpoint_holds_token_hash_only`.
  - Every step is audited, audit first.
- **Alternatives:**
  - **The model asks and then calls a `confirm=true` tool:** the model holds authority.
  - **`interrupt()` alone:** it ties the guarantee to framework behaviour and does not bind the set.
  - **A typed confirmation phrase including the count:** safer but more friction. It may be used for very large deletes in production.
  - **A random token held in a vault (rev. 4 to 4.3):** process-local in the prototype, Memorystore in production. Rejected in rev. 4.4: it adds a shared-store dependency to a 🔴 path, a Memorystore outage would block every delete, and the vault itself holds plaintext tokens that must be protected and evicted.
- **Consequences:**
  - Strong guarantees with one extra user turn.
  - The token never reaches the model, the traces or the checkpoints in clear text, and it is not stored anywhere.
  - `K_delete` becomes a secret to protect (Secret Manager in production). Remainder: an attacker who steals `K_delete` and already holds a valid session can forge a proof only for that session's own pending delete, within the 10-minute next-turn window.
  - Confirmation words are English only (resolved 2026-10-04).
- **Revised:** 2026-10-04 after the design review (`docs/process/03-design-review.md`).
- **Revised (rev. 4):** 2026-10-04 after Design Review 2 and requirements rev. 4. Vault plus HMAC proof, confirm/execute split, expiry on resume, re-entry at `input_guard`, unique audit key (R2-M1, R2-M2, R2-M3).
- **Revised (rev. 4.3, proposed):** 2026-10-04 after the independent review. Code-generated `pending_action_id` in the interrupt payload, token issued by get-or-create in the preview node, `confirm_delete` interrupt-and-verify only, production vault and per-user counters in Memorystore with Cloud SQL fallback, cross-instance resume edge case (R3-H8, R3-M3, R3-M31).
- **Revised (rev. 4.4, proposed):** 2026-10-04 for residual risk 3. The vault is replaced by a token derived with HMAC from `K_delete` and never stored; the vault becomes a rejected alternative; Memorystore no longer sits on the delete path; key rotation or restart expires pending deletes. Pending the owner at 🔴 G2.

## ADR-008: Scope is enforced by AST rewrite into code-built, PII-free CTEs

- **Status:** Accepted at 🔴 G2 (2026-10-04). Covers 🔴 risk areas: product scoping, PII masking and the SQL execution policy.
- **Context:** each user may analyse only their products (R2, A-1, A-2). `orders` and `users` have no product column. Model-written `WHERE` clauses cannot be trusted (AC-09.2).
- **Decision:**
  - After the policy check, sqlglot replaces every reference to an allowlisted table with a code-generated CTE:
    - `__p`: products filtered by `brand IN UNNEST(@scope_brands)` (exact match, A-1);
    - `__oi`: order items of scoped products, with an explicit column list;
    - `__o`: orders containing scoped items, with an explicit column list that omits `num_of_item` (it counts out-of-scope items) and `gender` (a duplicate quasi-identifier);
    - `__u`: users with scoped items, **projecting only non-PII columns**.
  - Scope values are passed as array query parameters.
  - Before the rewrite: model CTE names that use the reserved `__` prefix or equal one of the 4 table names are rejected (`cte_shadows_table`); FROM sources must be a table, a subquery or an UNNEST of a column (`source_not_allowed`); names are resolved per scope (an unqualified name that matches a visible CTE is that CTE, a qualified name is always the base table).
  - After the rewrite: the final SQL is re-validated, so every base-table reference must sit inside a code-built CTE. Any violation fails closed (`rewrite_invariant`).
  - Tests: `test_cte_shadowing_rejected`, `test_nested_cte_scope_resolution`, `test_source_allowlist`, `test_rewrite_invariant_fail_closed`, `test_orders_num_of_item_not_exposed`.
  - An all-products scope keeps the CTEs (PII-free) without the filter, only when the profile sets the explicit `all` flag (A-17). It is never inferred from an empty brand list; an empty list fails closed.
  - Answers carry a scope label.
- **Alternatives:**
  - **Appending a `WHERE` to the model's SQL:** fragile across subqueries and UNIONs.
  - **Asking the model to filter:** not enforcement.
  - **Prototype views in the reviewer's project:** setup burden, and still needs a rewrite.
- **Consequences:**
  - The model's SQL cannot reach out-of-scope rows or PII columns; `SELECT *` is rejected anyway (`select_star`, frugality), and PII is physically absent from the CTEs as a second layer.
  - `orders.num_of_item` is not exposed, because it would count out-of-scope items. Item counts come from `order_items` within scope.
  - The rewrite adds joins, so it costs a few MB of extra scan on this dataset, inside the caps.
- **Revised:** 2026-10-04 after the design review (`docs/process/03-design-review.md`).
- **2026-10-04 owner answers to HLD §13.2:** the delete confirm words are English only (Q2). CI is GitHub Actions by default, because the assignment delivers via a GitHub repository (Q4). Q1 (k) and Q3 (Langfuse production hosting) are still under discussion; G2 is not yet approved.
- **2026-10-04 owner answers, continued:** k = 5 (Q1). Langfuse production hosting is Cloud Run (web, worker) + Cloud SQL + Memorystore + GCS + ClickHouse on GKE Autopilot (Q3). The whole agent and all project work are **English only**: a non-English message gets an English request to rephrase (FR-17, AC-23.4 and A-14 updated; the `language` preference removed). G2 is still pending the owner's explicit approval.
- **Revised (rev. 4):** 2026-10-04 after Design Review 2 and requirements rev. 4. Scope by brand via `@scope_brands`; explicit `all` flag; `SELECT *` rejected.

## ADR-009: Multi-agent topology is a code supervisor with five specialist roles

- **Status:** Accepted at 🔴 G2 (2026-10-04). The owner chose this topology on 2026-10-04. It revises ADR-001 (graph shape) and ADR-003 (model per role).
- **Context:** one flash agent for every request pays the larger model's latency and token use on simple questions, and a report checked by the model that wrote it is not independently verified. The owner asked for different models for different roles, with defined behaviour when a role fails, bounded retries and per-role quality control.
- **Decision:**
  - The parent LangGraph graph is a **deterministic supervisor in code**: conditional edges over typed state, no LLM call to route.
  - `input_guard` runs the code rules, then the router: one flash-lite structured call on the user message plus the last 2 turns, before the full context loads, returning `{label: simple|complex|report|library|meta|smalltalk|off_topic|injection, is_english, refusal_text}` (8 labels, ADR-010). It never sees tool results or stored reports. Router down → rules only, label `complex`.
  - Five role subgraphs, each with its own model, prompt version and tool list:
    - Quick analyst (flash-lite; `list_tables`, `get_schema`, `run_sql`);
    - Deep analyst (flash; a pro-class model only if it passes the golden-suite gate on the Deep-tagged cases; same tools);
    - Report writer (flash; no tools; scrubbed ledger in, `ReportDraft` out);
    - Report verifier (flash-lite, separate prompt; no tools; `{verdict, issues[]}`, after the code grounding check);
    - Library agent (flash-lite; `save_report(last_answer)`, `list_reports`, `search_reports`, `view_report`, `delete_reports`, `set_preference`).
  - Roles return one envelope `{status: ok|partial|failed|escalate, output, error_class, missing[], used}`; the supervisor routes on `status` and `error_class` only.
  - Quick → Deep escalation at most once per turn (on `escalate`, 2 failed SQL or 4 calls), with the ledger; no role runs twice in a turn.
  - A verified report is shown as a draft with Save / Revise / Cancel; code saves it only after `confirm_save` (idempotency key `sha256(turn_id + draft_hash)`; ADR-011). The delete stays at parent-graph level, split into `confirm_delete` and `execute_delete` (ADR-007).
  - Budgets with per-role sub-caps: Q&A 10 LLM calls (router ≤ 2 + analysis ≤ 7, Quick ≤ 4 + `force_answer` 1); report turn 14 (router ≤ 2 + analysis ≤ 6 + `force_answer` 1 + writer ≤ 3 + verifier ≤ 2); light turn 3; 6 SQL; 120 s / 180 s. `recursion_limit` about 60 with `RemainingSteps`; `GraphRecursionError` is caught. Retries are bounded at call, agent and flow level, all from the same `TurnBudget` (R2-M4).
  - A failure outcome per role (HLD §4.0.5); `output_guard` fails closed.
  - A checkpoint after every node; `--resume <session>` is narrow: it resumes only the interrupted turn. An unconfirmed draft is shown again, never saved; a pending delete expires (`delete.expired`). `execute_delete` checks the audit log for `pending_action_id`, so a resumed turn cannot delete twice (R2-M3).
  - Evals (HLD §6.6; wording corrected in rev. 4.3, R3-L5): the prototype runs **one golden suite with cases tagged by role**, plus a router labelled set of about 50 messages; `adversarial/{pii,scope,injection,offtopic,delete}` and `resilience/*` are gated at 100%. A role's model swap is a config change behind the golden-suite gate on that role's cases. Per-role suites and per-role gates are roadmap.
  - Guardrails stay in shared code that every role passes through (ADR-004, ADR-008).
- **Alternatives:**
  - **Single agent loop (the previous design):** simpler, but the larger model pays for simple questions and there is no independent report check.
  - **Free swarm or peer handoffs:** unbounded hand-off chains, hard to budget and test.
  - **LLM supervisor (`langgraph-supervisor`):** non-deterministic routing, an extra LLM call per hop, and a routing decision that prompt injection can influence.
- **Consequences:**
  - Simple questions are cheaper and faster; reports get an independent check; the writer and verifier cannot act on injected text because they have no tools.
  - More moving parts: five prompts, role-tagged eval cases, more spans. Mitigated by one shared call wrapper, one envelope type and per-role metrics (escalation rate, verifier reject rate, partial rate per role).
  - A misrouted simple/complex question costs one escalation, not a wrong answer; the escalation rate (> 30%) is an alert for router calibration.
  - The report turn cap rises from 10 to 14 LLM calls; FR budgets in the requirements were updated on 2026-10-04.
  - Per-role outcomes are measured by `agent_outcome_total{agent,status,error_class}` with alerts (R2-M5).
- **Revised (rev. 4):** 2026-10-04 after Design Review 2 and requirements rev. 4. 8 router labels, light path, `search_reports`, confirm-before-save, delete split, sub-caps, narrow resume, outcome metric.
- **Note (rev. 4.3, R3-L31, R3-L32):** SDK and model facts the call wrapper assumes.
  - `thinking_level` and `thinking_budget` are mutually exclusive in a request; `config/models.yaml` sets exactly one of them per model.
  - Function (tool) responses are matched to their calls by `call_id`.
  - In `langchain-google-genai`, `max_retries=1` means one attempt. The retry ladder lives in our wrapper, not in the client: primary → one retry on 429, 5xx or timeout → fallback model once → fail. The fallback call counts against the role's sub-cap (R3-M6).
  - Model ids were checked against the provider's model list on 2026-10-04: `gemini-3.8-flash` and `gemini-3.1-flash-lite` are both listed; the flash-lite line has a published shutdown date in 2027 and a named successor, both to be recorded in `config/models.yaml` comments. The pro-class model is available as a preview id only and stays behind the eval gate. The "ID to verify" markers are resolved.

- **2026-10-04 owner decision:** adopt the supervisor + 5 roles topology, with explicit failure behaviour, bounded retries and per-role quality control (ADR-009). HLD revision 3 and requirements budgets (10 Q&A / 14 report) updated; re-review requested before G2.

## ADR-010: Router labels and the light path

- **Status:** Accepted at 🔴 G2 (2026-10-04). Resolves the owner's open point "separate `smalltalk` label or join `meta`" (requirements rev. 4).
- **Context:** FR-71 requires small talk, help and off-topic messages to get a short reply with 0 SQL, 0 embeddings, no Golden retrieval and no full history, while the input rules and output guard still run.
- **Decision:**
  - The router returns one of **8 labels**: `simple`, `complex`, `report`, `library`, `meta`, `smalltalk`, `off_topic`, `injection`.
  - It runs after the input rules and before context assembly, on the user message plus the last 2 turns only. It never reads tool results, result rows or stored reports.
  - `smalltalk` and `meta` (help, capabilities) go to `light_reply`: one flash-lite call, light budget 3, then `output_guard`. `off_topic`, `injection` and non-English messages get a templated, audited refusal with no further LLM call.
  - The label also selects the action allowlist checked by `output_guard` (FR-75).
  - Eval: a router suite with a confusion matrix over all 8 labels, plus `router_label_injection` cases.
- **Alternatives:**
  - **Small talk under `meta`:** one label fewer, but traces and metrics could no longer tell greetings from capability questions, and the help-answer quality and router calibration would be measured on a mixed bag. Rejected.
  - **Rules only for small talk:** brittle across phrasings.
- **Consequences:**
  - Cheap turns stay cheap and do not load history or touch BigQuery.
  - One more label to calibrate; a misroute to the light path costs one rephrase, never a data leak, because the light path has no tools.

## ADR-011: Reports are confirmed before save, revised without a cap, author-only

- **Status:** Accepted at 🔴 G2 (2026-10-04). Covers 🔴 risk area: report persistence.
- **Context:** FR-72 (confirm before save), A-36 (unlimited Revise, owner decision), A-32 and the client answer (only the author sees a report), FR-41 retired (no sharing), FR-39 retired (no soft delete).
- **Decision:**
  - The writer and verifier produce a draft held in `SESSION.pending_draft`. The CLI shows it with **Save / Revise / Cancel**. Nothing is stored until Save.
  - `confirm_save` is code: it accepts only the Save word for the current `pending_draft`, then writes `SAVED_REPORT` with `idempotency_key = sha256(turn_id + draft_hash)`. A repeated Save or a resumed node returns the existing row.
  - **Revise is unlimited:** each Revise is a new report turn with its own full budget (14 LLM calls, 6 SQL, 180 s). A per-user rate limit on `report`-labelled messages (20 per hour, confirmed by the owner at G2 on 2026-10-04, HLD §13.2 Q9) bounds total LLM calls and tokens.
  - On `--resume` an unconfirmed draft is shown again, never saved automatically.
  - Every read, search, list and delete filters by author and current scope in code. A report whose scope has drifted has its title masked (AC-21.5).
  - Delete is a hard delete (ADR-005, ADR-007).
- **Alternatives:**
  - **Auto-save after the verifier (rev. 3):** stores reports the user did not want.
  - **A Revise cap of 2:** the owner chose unlimited; per-turn budgets keep each loop bounded.
  - **Sharing with ACLs:** retired by the client answer.
- **Consequences:**
  - One extra user step per report; no unwanted rows.
  - A store failure on Save leaves the draft in place for retry (HLD §8).

## ADR-012: Speed and tokens: caches that never loosen a guardrail

- **Status:** Accepted at 🔴 G2 (2026-10-04).
- **Context:** frugality (requirements §6.2a, A-39) and the cost-as-mechanisms decision ask for minimal tokens and bytes without weakening scope or PII enforcement.
- **Decision:**
  - **Caches:** the prompt prefix (provider context caching where available), persona per user, table metadata, embeddings of Golden examples, and in production a SQL result cache.
  - **Keys:** every data cache is keyed **after** the scope rewrite: normalised final SQL + scope key + data refresh date. It is invalidated at each daily refresh. A cache hit still passes the result scrubber and the output guard.
  - **No cache loosens a guardrail:** policy, rewrite, small-cell and differencing checks run before any cache lookup.
  - **Frugality in code:** no `SELECT *`, explicit columns, 200-row result cap, `maximum_bytes_billed`, no repeated identical query in a session, light path for small talk, per-turn token and byte usage in traces.
- **Alternatives:**
  - **Cache keyed on the model's SQL:** two users with different scopes could share a hit. Rejected.
  - **No caching:** simpler but higher latency and more LLM calls, tokens and scanned bytes.
- **Consequences:**
  - Lower cost and latency on repeated questions; cache keys are a tested security boundary (`test_cache_key_includes_scope`).

## ADR-013: Differencing of id-grain queries

- **Status:** Accepted at 🔴 G2 (2026-10-04) (rev. 4.3, from R3-H3). The owner chose option A on 2026-10-04. Covers 🔴 risk area: PII.
- **Context:** the SQL policy allows a customer quasi-identifier (QI) filter in a query that returns row-level ids or order ids, as long as the filtered population is at least k=5. Two such lists (for example, one filtered by state and one by state and age band) differ by ids, and every id in the intersection or difference then carries the extra demographics. FR-70, the differencing guard, covers aggregates only and was below the cut line.
- **Options:**
  - **A. No QI at id grain:** a query that returns row-level ids or order ids may have no customer QI predicate and no QI projection. Filters on orders, products and dates stay allowed. "Top customers in a state" becomes an aggregate (count or revenue by state) or an explicit refusal with the reason.
  - **B. Pseudonymous ids:** id-grain results carry a per-session HMAC of `user_id`, so lists cannot be joined across sessions and the user never sees a stable key. Lists within one session can still be differenced.
  - **C. Extend FR-70:** promote FR-70 to M and extend it to id lists: keep a per-session set of (QI predicate set → id-list hash) and refuse a second query whose symmetric difference with an earlier one is below k. Stateful, and the hardest to test completely.
- **Decision (owner's choice, 2026-10-04):** Option A. It removes the attack at the parse step, is stateless and needs no new storage.
  - Enforced in **code** by the SQL policy (ADR-004): after scope rewrite, a statement whose output grain is row-level ids or order ids is rejected with a named reason if any predicate or projection references a customer QI column.
  - Test: `test_policy_rejects_qi_predicate_at_id_grain`.
- **Consequences:**
  - FR-70 is the differencing guard for aggregates, M in the prototype, in its session and per-user cross-session forms (rev. 4.4); id-grain differencing is handled here, not by FR-70.
  - Some legitimate questions ("top customers in California") are answered as aggregates or refused with a reason; the refusal is templated and audited like other policy rejections.
  - HLD §5.2 row 5 and requirements FR-70 are updated to match (by their owners, not in this file).

## ADR-014: Report residue and backups

- **Status:** Accepted at 🔴 G2 (2026-10-04) (rev. 4.4, residual risk 4). Covers 🔴 risk areas: deletion and PII.
- **Context:** a hard delete (ADR-005, ADR-007) removes the report rows, but copies can survive: freed SQLite pages, the WAL file, the FTS5 index, the report embedding, and in production the daily export and Cloud SQL backups (previously up to 30 days). Reports hold aggregates only (no PII by construction), but a user who deletes a report expects it gone, and the notice must state the true window.
- **Decision (proposed):**
  - **Prototype (enforced in code):**
    - The reports database opens with `PRAGMA secure_delete=ON`, so freed pages are zeroed.
    - The report row, its FTS5 rows and its embedding are deleted in the same transaction as the audit-first delete (ADR-005).
    - After a delete or `/erase` commits, code runs `PRAGMA wal_checkpoint(TRUNCATE)` so no copy stays in the WAL file.
    - The prototype takes no backups.
    - Test: `test_hard_delete_leaves_no_residue_in_db_file` (scans the raw database and WAL bytes for a synthetic marker string after delete).
  - **Production (platform adapter):**
    - The daily export contains only the audit tables; report tables, embeddings and conversation checkpoints are excluded.
    - Cloud SQL point-in-time recovery is kept for **7 days**; no longer-lived backups of report tables.
    - Access to backups is break-glass IAM only (time-bound grant, audited).
    - A restore replays the audited `delete.executed` and erase events before the instance serves traffic, so deleted reports do not come back.
  - The delete preview and `/erase` notice state that backup copies may remain for **up to 7 days** (`test_delete_preview_includes_backup_notice`).
- **Alternatives:**
  - **Crypto-shredding (a per-report or per-user key, destroyed on delete):** rejected. Server-side full-text search (FTS5 / Postgres full-text) and pgvector similarity need the plaintext in the database, so the searchable copies could not be encrypted under a destroyable key without dropping search.
  - **No backups of report tables at all:** rejected for production; a 7-day PITR window is the minimum for operational recovery.
  - **Keep 30-day backups:** rejected; the residue window is four times longer for no product need.
- **Consequences:**
  - The prototype database file keeps no deleted report content.
  - Remainder: in production, deleted reports (aggregates only) may survive in PITR backups for up to 7 days, readable only through break-glass access; conversation checkpoints may still quote report text until their own retention or `/erase` removes them.
  - Requirements A-34 and the HLD notice wording change from 30 to 7 days (by their owners).

## ADR-015: Dev-only local LLM provider (LM Studio)

- **Status:** Accepted by the owner as D-143 (2026-10-05). Dev tooling only; it changes no product behaviour, guardrail or gate. Not a 🔴 risk area: PII, scope, deletion and the SQL policy are enforced in code and do not depend on the provider.
- **Context:** the Gemini free tier (5 RPM / 20 RPD on gemini-3.8-flash) runs out during development and manual testing. The owner runs LM Studio locally, which serves an OpenAI-compatible API. The local model may change (the default is now `qwen/qwen3.8-27b`; it was Qwen3-30B-A3B-Instruct-2507 and may change again), and newer Qwen models can emit reasoning text.
- **Decision:**
  - `OPSFLEET_LLM_PROVIDER=gemini|lmstudio` (default `gemini`) and optional `OPSFLEET_LLM_BASE_URL` (default `http://127.0.0.1:1234/v1`; the IPv4 loopback avoids IPv6 `localhost` resolution issues on macOS), read only in `config.py`. Unset means today's behaviour exactly. Any userinfo (`user:pass@`) in the URL is removed before it appears in an error or log message.
  - Model ids are config values in the `local:` section of `config/models.yaml` (`chat_model`, `embedding_model`, `embedding_dim`; defaults `qwen/qwen3.8-27b`, `text-embedding-nomic-embed-text-v1.5`, 768); `embedding_dim` must be an integer in 1..8192 and is required when the section exists. No code depends on a specific model. Under `lmstudio` every role maps to `local.chat_model`, with no fallback, no Gemini thinking params and no free-tier limiter. `GEMINI_API_KEY` is not required.
  - Chat: `ChatOpenAI` with SDK retries off (the ADR-003 ladder still owns retries and the turn deadline). OpenAI SDK errors map onto the existing classes: timeout, 429 and 5xx are transient; a connection error whose cause chain contains `ConnectionRefusedError` or `httpx.ConnectError` (server not running) is non-retryable with the message "LM Studio is not reachable at <url>; start the server and load <model>"; any other connection error (reset, read error) is transient and bounded by the ADR-003 ladder; other statuses are non-retryable. ChatOpenAI runs with `use_responses_api=False` (LM Studio serves Chat Completions). Reasoning is stripped with a bias towards not leaking it (dev-only provider): if the first think tag is `</think>`, or a `</think>` stands alone on its line in text that does not open with `<think>`, everything up to it is a template-opened reasoning prefix and goes; complete `<think>...</think>` blocks go anywhere; any `</think>` still left (nested or unbalanced tags) drops everything through the last one; an unterminated `<think>` goes only at the start. Dropping a prefix logs a warning with its length, never the content. A `<think>` mentioned mid-answer with no closing tag is kept. These blocks and `reasoning_content` are stripped in the adapter, before any parser, JSON extraction or the user sees the text.
  - Startup check: `GET <base_url>/models` replaces the Gemini model list; a missing model fails with the ids LM Studio reports.
  - Embeddings: `/v1/embeddings` with the `search_query: ` / `search_document: ` prefixes; any vector whose length differs from `local.embedding_dim` is rejected. Golden vectors of the local provider live in `<data dir>/lmstudio/`, and cache keys carry the model id, dimension and the query/document prefixes (Gemini has empty prefixes, so its keys are byte-identical to before), so vectors of different models never mix and switching provider does not evict the Gemini cache.
  - Provider logic lives in `graph/providers.py`; call sites change one line each.
- **Alternatives:**
  - **LiteLLM or another gateway:** rejected; a new dependency and a proxy for one dev use case.
  - **Gemma via the Gemini API:** rejected; it uses the same key quota.
  - **Mock LLM for manual testing:** rejected; it does not exercise real tool calling.
- **Consequences:**
  - Development no longer spends the Gemini quota; evals and the deliverable stay on Gemini.
  - Local answer quality, latency and tool-calling reliability differ from Gemini; a pass under `lmstudio` is not evidence for Gemini.
  - The app database (`app.db`), checkpoints and traces are shared between providers; they hold no vectors. Sessions, saved reports and audit rows created under `lmstudio` are visible under `gemini` and the other way round.
  - No fallback: everything local is test-only. There is no automatic switch between providers and no fallback from LM Studio to Gemini or back; the eval judge (`evals/judge.py`) stays on Gemini.

## Client answers to requirements questions (2026-10-04)

The client answered the six questions of requirements §8.2 on 2026-10-04. Requirements revision 3 (`docs/process/01-requirements.md`) incorporates them. **Status: confirmed by the owner on 2026-10-04 as part of requirements revision 4 (🔴 gate passed; see "Owner confirmation of requirements rev. 4").**

**Client answers:**
1. **Access model:** each manager manages 1..N brands; the CEO sees everything.
2. **Report access:** only the author has access to a report.
3. **PII:** the definition is confirmed as written (A-3).
4. **Churn:** there is no common definition; each brand manager may have their own.
5. **Retention:** chats are kept 90 days; deletion is a hard delete with no recovery; audit logs are kept 1 year.
6. **Load:** 100 active managers per day, 10 questions per manager per day, 1 report per manager per day; data refreshed daily with ~1M new rows per day.

**Owner decisions (2026-10-04):**
- **Churn:** no stored per-user definition. The agent uses the default monthly definition, states it, and invites the user to restate their own window and activity event in the message. A definition given in a message is honoured for that session. The R4 preference priority is unchanged.
- **`--resume`:** kept in the prototype as a narrow "resume the interrupted turn" capability. A pending delete always expires on resume (audit `delete.expired`; nothing is deleted). This is the requirements side of Design Review 2 finding R2-M3.
- **Process:** one combined pass. After the owner confirms requirements rev. 3, the architect writes HLD revision 4 covering these changes together with the Design Review 2 MAJOR findings (R2-M1..R2-M6) and the MINOR findings.

**Changed assumptions:** A-1 (scope by brand), A-3 (confirmed), A-4 (churn default plus per-message definitions), A-8 (load), A-15 (hard delete), A-17 (CEO `all` flag), A-20 (retention confirmed, no restore window), A-24 (narrow resume), A-32 (author-only reports). New: A-33 (small cells and differencing in thin brand slices), A-34 (hard delete versus backups: deleted data can stay in backups up to 30 days; a restore re-applies the audited deletes; to be stated to the client), A-35 (the 1M rows/day are warehouse-wide; sized as if all land in the agent's fact tables).

**Changed FRs and ACs:** FR-02 and FR-03 (brand scope, exact match), FR-14 (split: narrow resume in the prototype; history browse and search HLD-only), FR-16 (churn statement), FR-59 (hard delete and backup window). FR-39 (soft delete and restore) and FR-41 (sharing) are **retired**, with their IDs kept. New FR-69 (small-cell rule, k = 5 after brand scoping, prototype M) and FR-70 (differencing guard, prototype P). New AC-08.6, AC-08.7 and AC-22.6; AC-05.2 and the brand examples in US-01..US-26 updated. §6 sizing, cost, backup, retention and delete rows updated; §7 and §9 aligned. FR totals: 70 IDs, 2 retired, 68 active (47 prototype: 39 M, 8 P; 21 HLD-only).

*Superseded by HLD rev. 4 and later:* the impacts below were applied in HLD revision 4.

**Impact on the HLD (for revision 4):** `docs/architecture.md` still describes category/department scope, soft delete with a 30-day restore, report sharing, the old load profile, and "a restart is a new session" (§12 #5). These are to be changed by the architect in HLD revision 4.

## Owner review of the HLD diagram: five requirement changes (2026-10-04)

From the comments on the HLD diagram review, the owner **approved five changes in principle** on 2026-10-04. They are folded into requirements revision 3.1 (`docs/process/01-requirements.md`, "Changes in rev. 3.1"). **Status: confirmed by the owner on 2026-10-04 as requirements revision 4 (🔴 gate passed; see the entry below).**

**Changes (all prototype M unless stated):**
1. **Light path (FR-71):** the router runs first on the message plus the last 2 turns, before the full context loads. Small talk, help/capabilities and off-topic get a short reply with 0 SQL, 0 embeddings, no Golden retrieval and no full history. The input rules and the output guard still run. AC-11.3 to AC-11.6.
2. **Confirm before save (FR-72, FR-28 changed):** a report is shown as a verified draft with Save / Revise / Cancel and saved only on Save. Revise capped at 2 (A-36, proposed). On `--resume` an unconfirmed draft is shown again, never saved automatically; save stays idempotent. AC-06.1 changed, AC-06.4, AC-06.5 new; AC-21.1, AC-21.2, AC-22.5, AC-22.6 and A-5 updated.
3. **Report search (FR-73 M, FR-74 HLD-only):** search over the user's own, in-scope reports by text, tags and date range; a found report can be opened and discussed. Search results never become delete targets; the A-16 matcher stays deterministic (A-37). AC-21.10, AC-21.11.
4. **Output guard (FR-75):** action allowlist per role and router label, fail closed, audited (`unexpected_action`) and alerted; answer scan for injection signs (`output_injection`), including acting on instructions found in result data. AC-10.4 to AC-10.6; AC-16.1 and AC-28.3 updated.
5. **Scope filter at context assembly (FR-76):** history and summary, Golden examples, reports and previews, and preferences are filtered in code by the current scope before the prompt is built. After a scope shrink, `--resume` of an old-scope session starts a new, empty session. AC-09.5, AC-09.6; AC-21.5 changed (a drifted report's title is now masked in lists, search and delete previews, replacing "Listing still shows the title"); A-31 updated.

FR totals after rev. 3.1: 76 IDs, 2 retired, 74 active (52 prototype: 44 M, 8 P; 22 HLD-only).

**Owner to confirm (RESOLVED 2026-10-04, see "Owner confirmation of requirements rev. 4" below):** FR-73 as M (recommended); the Revise cap of 2 (recommended); whether the router gets a separate `smalltalk` label or small talk joins `meta` (the architect's call; requirements only fix the behaviour). Earlier open decisions stay open: FR-70 P or M; k = 5 for non-quasi-identifier groups in thin brands; ~$5,000/month platform cost; demo brands; FR-14 resume kept as M.

**Impact on the HLD (for revision 4):**
- ADR-009 says "Generated reports are saved by code after the verifier". This becomes "saved by code after the user confirms the verified draft"; the ADR and `docs/architecture.md` (report flow, "nothing is saved on failure") need updating by the architect.
- The router label set has no small-talk label; the light path must be drawn before context assembly, with the input rules still ahead of it.
- `output_guard` gains the action allowlist and the answer injection scan; context assembly gains the scope filter and the `scope_snapshot` check on resume.
- New HLD sections: "Speed and tokens" (prompt-prefix, embedding, persona and table-metadata caches; a production SQL result cache keyed by normalised SQL + scope key + data refresh date, invalidated at each daily refresh) and "Key decisions and alternatives" (surfacing the ADRs); failure handling per module on the schematic; the report and chat-history stores on the overview diagram; traces described as observability only, with the Golden Bucket used for retrieval and offline evals.
- (added by rev. 4) Revise is unlimited: each Revise is its own report turn with the full report-turn budget; the HLD report flow and ADR-009 drop any revision cap.
- (added by rev. 4) The resource section describes code-verifiable mechanisms (bytes, tokens, calls); rev. 4.1 removed all money figures, and HLD §7.4 gives usage per turn type only.
- (added by rev. 4) Frugality with tokens and data is stated as an explicit design principle (requirements §6.2a).

## Owner confirmation of requirements rev. 4 (2026-10-04)

**Gate:** 🔴 requirements re-approval (risk areas: scope, deletion, report persistence, guardrail layers). **Approved by the owner on 2026-10-04.** Requirements revision 3 + revision 3.1 + the decisions below are confirmed together as **revision 4** (`docs/process/01-requirements.md`, "Changes in rev. 4").

**Decisions:**
1. Rev. 3 and rev. 3.1 approved together as revision 4.
2. FR-73 (search over the user's own reports) stays **M**.
3. **Revise is unlimited** (A-36, FR-72, AC-06.4). Each Revise is a separate user turn with its own full report-turn budget (14 LLM calls, 6 SQL, 180 s), so every loop stays bounded per turn. Save stays idempotent; nothing is saved without an explicit Save. Replaces the proposed cap of 2.
4. FR-70 (differencing guard) stays **P**.
5. **k = 5 applies only to breakdowns by customer quasi-identifiers**, never to product-only groups (brand, category, month), even in thin brand scopes (A-33, FR-69).
6. **Cost as mechanisms.** The assignment states no budget; its only cost wording is §R5, "without inflating costs". The earlier platform money total is removed. Requirements §6.8 lists code-verifiable mechanisms: dry-run plus `maximum_bytes_billed` per query, per-turn budgets of LLM calls, SQL queries and a deadline, a cheap model for the router, small talk and simple questions, caching, bounded retries, and a $0 free tier for the prototype. Rev. 4.1 removed all money figures; HLD §7.4 gives usage per turn type only.
7. FR-14 narrow `--resume` stays **M**.
8. **Demo brands:** we choose 2–3 brands with enough data for meaningful answers but small result sets (A-38).
9. **Frugality with tokens and data** is a cross-cutting principle (requirements §6.2a, A-39): minimal context, only needed columns (no `SELECT *`), a 200-row result cap, byte caps, the light path for small talk, caching of prompts, metadata and results, no repeated identical queries within a session, and per-turn token and byte usage in traces.

**FR totals:** unchanged: 76 IDs, 2 retired, 74 active (52 prototype: 44 M, 8 P; 22 HLD-only).

**Still open (architect's call, not a requirement):** whether the router gets a separate `smalltalk` label or small talk joins `meta`.

**Impact on the HLD (revision 4):** in addition to the rev. 3 and rev. 3.1 impacts above: unlimited Revise with each Revise its own budgeted turn; the resource section as mechanisms with §7.4 as usage only (rev. 4.1); frugality as an explicit principle; the demo brand profiles.

## HLD revision 4 written (2026-10-04)

The architect wrote HLD revision 4 (`docs/architecture.md`) covering requirements rev. 4 and Design Review 2 (R2-M1..M6, minors). ADR-003..009 revised, ADR-010..012 added; all stay **Proposed**. 🔴 G2 is pending the owner.

## Owner confirmation of two rev. 4 frugality rules (2026-10-04)

The owner kept both rules the analyst added in requirements rev. 4 §6.2a:
- `SELECT *` and `t.*` are rejected by the SQL policy on every table, not only where PII columns would be projected.
- A release fails if the median per-case token or byte usage in evals grows by more than 25% without a logged reason.

## Owner G2 decisions on HLD open questions and prototype scope (2026-10-04)

The owner answered HLD §13.2 Q8–Q11 and the Review 2 feasibility cuts. Requirements and the HLD moved to rev. 4.1. G2 itself is still pending.

| # | Question | Owner answer |
|---|---|---|
| Q8 | Backup window after a hard delete (A-34) | Default: the client is told that deleted reports may stay in backups for up to 30 days and are never restored. The delete preview states this. *Rev. 4.4: shortened to up to 7 days (PITR only; report tables are excluded from the 30-day export), ADR-014* |
| Q9 | Rate limit on `report`-labelled messages | Yes: 20 per user per hour |
| Q10 | FR-70 (differencing guard) state | Per user across sessions, in the prototype and in production. *Rev. 4.3 proposed alignment (R3-M2), pending the owner at G2:* FR-70 is the session differencing guard for aggregates (P, or M under the rev. 4.3 proposed cut line); the cross-session form is roadmap. Id-grain differencing is handled by ADR-013. *Rev. 4.4, owner decision 2026-10-04:* the session wording is accepted and FR-70 also has a per-user persisted cross-session form in the prototype (M) |
| Q11 | Money figures | Removed everywhere. No prices, dollar estimates, spend alerts or budgets in money. Token, byte and call limits stay as the resource controls |
| Cuts | Review 2 feasibility | Golden seed (FR-48) and extended report search (FR-74: FTS5 bm25 ranking plus semantic search with RRF fusion, fallback to FTS5) are committed to the prototype as M. HLD §1.2 lists everything cut |

*Superseded by rev. 4.2 (parity) and by the rev. 4.3 proposed cut line below.*
**Build tiers (HLD §1.2):** tiers 0–4 unchanged; tier 5 Golden seed; tier 6 extended search; tiers 0–6 are the minimum for 2026-10-08; tier 7 holds the P extras (FR-70, R4, R8). If tier 6 runs late, its semantic half is dropped first and ranked FTS5 search ships.

**Cut from the prototype (design-only):** see the "Cut from the prototype" list in HLD §1.2. *Superseded by rev. 4.2:* the design-only status is gone; items are in both environments or on the roadmap.

**Totals (requirements rev. 4.1):** 53 prototype FRs (46 M, 7 P) and 21 HLD-only. *Superseded by the rev. 4.2 totals below.*

## Owner G2 decisions rev. 4.2: parity between prototype and production (2026-10-04)

The owner asked that the prototype and production have the same functions. Requirements and the HLD moved to rev. 4.2. G2 itself is still pending.

| # | Topic | Owner answer |
|---|---|---|
| 1 | Parity | Option C: the prototype and production have the same functions and differ only by platform adapters (identity, stores, audit archive, monitoring, release pipeline). Large functions move to a post-launch roadmap that is in neither environment. The "design-only" status is gone |
| 2 | Roles | No application roles (FR-09). Access is a brand list or the CEO `all` flag, set with `access set` (FR-08). Team tooling (access, triage, audit viewer, erasure) is reached through infrastructure access, not the chat API |
| 3 | CEO `all` flag | Covers data only. Reports stay author-only; `all` does not open other users' reports |
| 4 | Session timeout | FR-10 (30-minute idle timeout, drops a pending delete and an unsaved draft) is in the prototype |
| 5 | Feedback triage | FR-46 and FR-47 are prototype M. Negative feedback and auto-flagged turns are traced to a root-cause class by deterministic rules; `triage promote` adds a reviewed trio to the Golden seed (PII scan, dry-run, human review, eval no-regress), `triage add-eval` turns a case into a regression eval. Prompts and the seed are changed by people, never by the agent itself |
| 6 | Small functions | Moved into the prototype: rename, Markdown export, retry report (FR-37, 38, 40), erasure CLI (FR-59), audit viewer, browsing your own past sessions (FR-14), per-user quotas (FR-58), degraded mode (FR-63), persona mechanism (FR-51, 52 → M) |
| 7 | Models | The Deep analyst model is selectable in `config/models.yaml`, so a pro-class model can be used in either environment |
| 8 | Roadmap | FR-26, 27, 45, 49, 50, 53, 66; PDF, email and Slack export; regenerate; general resume and history search; per-role eval suites |

**Reversals of Review 2 cuts.** Retry report (R2-m3) and the pro-class Deep model (R2-m7) were cut to HLD-only in rev. 4.1. Both are back: retry report is in the prototype, and the pro-class model is a config choice.

**Schedule.** About 2.5–3 extra days of work before Thursday 2026-10-08. Drop order if late: the semantic half of tier 6, then tier 7. Any further drop needs an owner decision and is reported in the README. *Rev. 4.3 proposes a replacement drop order with checkpoints (below), pending the owner.*

**Totals (requirements rev. 4.2):** 76 IDs, 2 retired, 74 active. Prototype 63 (59 M, 4 P: FR-42, 43, 44, 70); platform adapters FR-07, 57, 67, 68; roadmap FR-26, 27, 45, 49, 50, 53, 66.

## Rev. 4.3 proposals from the independent review (2026-10-04; decided by the owner 2026-10-04, see the next section)

The independent review (`docs/process/03b-independent-review.md`) asks for four owner decisions: the cut line (R3-H9), FR-70 and id-grain differencing (R3-H3, ADR-013), the Q10 wording (R3-M2) and the embedding model (R3-L30). They are recorded here as **proposals**. Nothing in this section is approved; the owner decides at G2.

**1. Proposed cut line (R3-H9, R3-M30).** *Decided 2026-10-04: accepted, with the rev. 4.4 risk closures added to Must.* This partly reverses the owner's rev. 4.2 parity decision (option C): functions above the line stay in both environments, but items below it may ship in the prototype as "if time" only. It needs the owner's explicit choice.
- **Must:** tiers 0–4; FR-70 in its session form; FR-46 `/feedback`; a minimal FR-48 Golden seed with top-k retrieval.
- **Drop order if late** (each dropped item keeps its HLD design):
  1. FR-74 semantic half;
  2. FR-74 FTS ranking (FR-73 substring search stays);
  3. FR-47 triage CLI (promote, add-eval);
  4. FR-37, FR-38, FR-40 (rename, Markdown export, retry report);
  5. FR-14 `/history` (narrow `--resume` stays);
  6. FR-59 erasure CLI;
  7. FR-08 `access set` (`config/profiles.yaml` stays);
  8. FR-52 smoke check and rollback ("an invalid file keeps the last valid one" stays);
  9. Langfuse;
  10. FR-42, FR-43, FR-44.
- **Checkpoints:** end of day 1 (Mon 2026-10-05): tiers 0–2 green on the clean path. End of day 2 (Tue 2026-10-06): tiers 3–4, the delete flow and evals green. If a checkpoint slips, the drop order applies automatically; the owner is told, not asked, and the README lists what was dropped.

**2. Q10 and FR-70 alignment (R3-M2).** *Decided 2026-10-04: accepted, plus a per-user cross-session form (M).* FR-70 is the **session** differencing guard for aggregates: P under rev. 4.2, M under the proposed cut line. The cross-session form is roadmap. Id-grain differencing is handled by ADR-013 (option A proposed). The requirements and the HLD are aligned by their owners.

**3. Embedding model (R3-L30).** *Decided 2026-10-04: accepted.* `gemini-embedding-001` (GA) with `output_dimensionality` 768, set in `config/models.yaml`. The model id and dimensionality are stored beside every vector; a change of either triggers a re-embed instead of a silent mismatch.

**4. Model ids (R3-L31) and SDK behaviour (R3-L32):** see the rev. 4.3 note in ADR-009.

## Step 4 plan inputs (rev. 4.3, from the independent review)

Inputs for the Step 4 plan; they are not decisions and change no requirement.
- **`pyproject.toml` (R3-M20):** runtime dependencies; `requires-python = ">=3.12,<3.14"`; a `dev` dependency group (ruff, pytest and test helpers); `[project.scripts] opsfleet-agent = "opsfleet_agent.cli:main"`; ruff configuration; pytest configuration with a registered `live` marker.
- **Deliverables task group (R3-M21):** README (uv and pip paths), `.env.example` with placeholders only, `requirements.txt` exported from the lock, and a run on a clean machine from the README alone.
- **Layout (R3-M22):** `src/opsfleet_agent/{__main__.py, cli.py, config.py, graph/, roles/, tools/, guards/, bq/, store/, obs/}`; `prompts/`; `config/{models.yaml, profiles.yaml}`; `evals/`; `tests/{unit,live}`; `infra/langfuse/`.
- **CI (R3-L26):** `ci.yml` runs ruff, offline pytest and the offline golden eval.
- **No network in unit tests (R3-L28):** an autouse fixture in `tests/unit` blocks sockets.
- **Library notes (R3-L36):**
  - role subgraphs are compiled with `checkpointer=False` so only the parent graph checkpoints;
  - LangGraph `EncryptedSerializer` needs `pycryptodome` and `LANGGRAPH_AES_KEY`;
  - the self-hosted Langfuse bootstrap uses the `LANGFUSE_INIT_*` environment variables;
  - pin `sqlglot>=30,<31`;
  - a day-1 spike on the sqlglot BigQuery dialect (parse, qualify, rewrite) and on `interrupt()` / resume;
  - `requirements.txt` is produced with `uv export --no-hashes --format requirements-txt > requirements.txt` (it keeps the `-e .` line, so pip installs the project too);
  - BigQuery `to_dataframe` warns without optional extras; use `result.to_arrow()` or iterate rows.
- **Hashes (R3-L22):** uv users install from `uv.lock` with hashes; pip users get pinned versions without hashes. The README states this.

**Totals (requirements rev. 4.3 proposal):** unchanged from rev. 4.2 until the owner decides: 76 IDs, 2 retired, 74 active. Prototype 63 (59 M, 4 P: FR-42, 43, 44, 70); platform adapters FR-07, 57, 67, 68; roadmap FR-26, 27, 45, 49, 50, 53, 66. If the owner accepts the proposed cut line, FR-70 becomes M (prototype 63: 60 M, 3 P) and the items in the drop order stay prototype FRs marked "if time". ADRs: 13 (ADR-001..013), all **Proposed**. *Superseded by the rev. 4.4 totals below.*

- **Typed-PII model (rev. 4.4):** the spaCy model `en_core_web_sm` is pinned as a package dependency; the README documents installing it for both the uv and the pip paths, and startup fails with a clear message if it is missing (`test_pii_detector_missing_model_fails_startup`).

## Owner decisions at G2 review (2026-10-04)

The owner answered the four rev. 4.3 questions on 2026-10-04 and asked that the five residual risks of HLD rev. 4.3 §13.3 be closed in the design rather than accepted. Requirements and the HLD moved to rev. 4.4. **🔴 G2 itself is still pending; no ADR is Accepted.**

| # | Question | Owner answer |
|---|---|---|
| 1 | Cut line, drop order, checkpoints (R3-H9, R3-M30) | Accepted as proposed. The rev. 4.4 risk closures join Must and are not droppable |
| 2 | Id-grain differencing (R3-H3) | ADR-013 option A. The ADR stays Proposed until G2 |
| 3 | Q10 / FR-70 wording (R3-M2) | Session wording accepted; FR-70 also gets a per-user persisted cross-session form in the prototype (M) |
| 4 | Embedding model (R3-L30) | `gemini-embedding-001` at 768 dimensions |

**Residual risks closed (HLD §13.3).** Each has a control in code and a test or eval gate:
1. Typed PII: local Presidio with spaCy after the regex scrubber, brand allowlist, same detector in the output guard; `adversarial/pii_typed/*` recall ≥ 95%, brand false positives 0 (ADR-004, HLD §5.4).
2. Differencing: per-user fingerprints (no values) kept 30 days and checked across sessions; production org-wide probing detector (detects, does not prevent); differential privacy is roadmap (ADR-004, ADR-013, HLD §5.5).
3. Delete token: derived as `HMAC-SHA256(K_delete, pending_action_id ‖ ids_sha256 ‖ owner ‖ session_id ‖ preview_turn ‖ expires_at)`, never stored; prototype key in memory, production key in Secret Manager; rotation or restart expires pending deletes; no Memorystore on the delete path (ADR-007).
4. Backup residue: prototype `secure_delete`, same-transaction FTS and embedding deletes, WAL checkpoint, no backups; production report tables excluded from the 30-day export, PITR 7 days, audited break-glass, restore replays deletes; A-34 becomes "up to 7 days"; crypto-shredding rejected (ADR-005, ADR-014).
5. Judge: numbers never judged; `evals/calibration/` with 30 owner-labelled synthetic cases; judge scores count only at ≥ 80% agreement; judge provider configurable in `config/models.yaml` (FR-66 calibration part, prototype M; quarterly recalibration is roadmap).

**Schedule.** About +1–1.5 days; the deadline stays Thursday 2026-10-08. The drop order is unchanged and contains no closure.

**Totals (requirements rev. 4.4):** 76 IDs, 2 retired, 74 active. Prototype 64 (61 M, 3 P: FR-42, 43, 44); platform adapters 4 (FR-07, 57, 67, 68); roadmap 6 (FR-26, 27, 45, 49, 50, 53). FR-66 is split like FR-14: the calibration gate is prototype M, online quality and quarterly recalibration are roadmap. ADRs: 14 (ADR-001..014), all **Proposed**.


## 🔴 G2 approval (2026-10-04)

The owner explicitly approved HLD rev. 4.4 (`docs/architecture.md`) and requirements rev. 4.4 (`docs/process/01-requirements.md`), including the rev. 4.4 risk closures and the remainders listed in HLD §13.3. ADR-001..014 move from Proposed to Accepted. Next: Step 4 (plan), then 🔴 G3.

## Step 4b plan review: owner decisions (2026-10-04, before 🔴 G3)

Source: `docs/process/04b-plan-review.md` (security, traceability and schedule reviews; consolidated verdict NOT READY, one plan revision).

| # | Question | Owner decision |
|---|---|---|
| 1 | Q-1 / SCH-9: where P items go in the drop order | P items first: iteration 39 (FR-42..44) becomes drop item 1, then 38, 37, … The P items stay described in the README with their HLD design |
| 2 | SCH-7: worst-case shippable scope | Accepted. Minimum: tiers 0–1, Q&A through iterations 14/15, reports save/list/view (17–18), delete with audit and no residue (21–23), offline adversarial and resilience suites (27–28), calibration (20/30), seed (31), `/feedback` (32), README and clean-machine run. Beyond the last drop item, cut volume but keep every gate: golden ≈ 15 cases, router ≈ 25 messages, static persona, session caps only, JSONL-only traces |
| 3 | SCH-1 / SCH-3: Monday checkpoint and capacity factor | Kept as in the plan: Monday 22:00 = iterations 1–19 plus the manual demo; factor 1.5x. The reviewers' slip forecast is recorded as a known schedule risk (R1); decisions 1–2 are the mitigation |
| 4 | T-2: free-tier limits and model ids | Checked against the official Gemini docs on 2026-10-04 (see below); numeric limits are confirmed by the owner against the key in AI Studio |

**T-2 findings (official docs, 2026-10-04).**
- The rate-limits page publishes no free-tier numbers; the per-key limits are only shown in the AI Studio rate-limit dashboard. The §5 numbers in the plan stay assumptions until the owner reads them there.
- `gemini-3.8-flash` and `gemini-3.1-flash-lite` are stable and have a free tier.
- The pro-class model exists only as `gemini-3.1-pro-preview` and has **no free tier**. The Deep analyst therefore stays on flash in the prototype (as ADR-009 already says); the pro option stays a config choice for paid keys. No Deep/pro row is needed in the live budget.
- `gemini-embedding-001` is listed as a stable model but is not on the current pricing page, which shows `gemini-embedding-2` as the free embedding model. The iteration-2 spike must confirm that `gemini-embedding-001` works on a free key; if not, the orchestrator proposes `gemini-embedding-2` at 768 dimensions to the owner as an amendment to G2 decision 4 (owner decides).

## 🔴 G3 approval (2026-10-04)

The owner explicitly approved the implementation plan `docs/process/04-plan.md` rev. 2 (after the 04b review). Accepted as proposed: Q-7 (`config/golden_seed.yaml`), Q-8 (drop order kept; if 35 is dropped, the retention gap is documented in the README), Q-9 (R1 slip forecast against the Monday target; delete flow on Wednesday), Q-10 (a/b splits of iterations 8, 14, 22, 28). Still open: Q-3 (free-tier limits read by the owner in AI Studio, before L1), Q-5 (embedding fallback, decided by the owner only if the iteration-2 spike fails), Q-6 (deadline hour; Step 6 ring-fenced from Thu 12:00 meanwhile). Next: Step 5, iteration 1.

## T-2: free-tier limits read by the owner in AI Studio (2026-10-04, after 🔴 G3)

| Model | RPM | TPM | RPD |
|---|---|---|---|
| `gemini-3.8-flash` | 5 | 250K | 20 |
| `gemini-3.1-flash-lite` | 15 | 250K | 500 |
| `gemini-3.5-flash` (separate bucket, not used) | 5 | 250K | 20 |

The embedding model was not in the pasted list (to be confirmed). Plan §5 assumed 250 RPD / 10 RPM for flash and 1,000 RPD for flash-lite, so the live-call budget no longer fits: flash is 12.5x lower. The plan also deviates from HLD §4.0 by putting the Quick analyst on flash (the HLD puts it on flash-lite). Owner decision pending on how to re-fit the budget.

## Free-tier decision (2026-10-04, after 🔴 G3)

The owner chose to **stay on the free tier** (no billing on the Gemini key). Plan `docs/process/04-plan.md` is amended accordingly:
- **Model split per HLD §4.0 restored:** flash (`gemini-3.8-flash`) serves only the Deep analyst and the Report writer. The Quick analyst returns to `gemini-3.1-flash-lite`. Plan rev. 2 had put Quick on flash; that was a deviation from the HLD, not a design change.
- **Daily ceilings at 75% of the limit:** flash 15 of 20, flash-lite 375 of 500. The limiter runs at 80% of RPM (flash 4 RPM, flash-lite 12 RPM).
- **Spreading the Deep and report evals:** the about 12 golden Deep and report cases (about 36 flash calls) are split across L2 (Tue), L6a (Wed) and the Thursday final run. Every flash-lite suite runs in full in the final run. The golden flash count is reported as the union of the three runs, with the date and commit of each run.
- **Gates unchanged.** A lower count is reported, not hidden. A case that ran on the flash-lite fallback after a 429 is reported separately.
- **Wednesday-evening final run dropped:** there is no flash-lite room for it.
- **Risk R4 rewritten:** a reviewer with a free key runs out of flash after about 6 Deep questions. Mitigations:
  - the per-role fallback with a visible notice;
  - the estimator refuses over-budget runs;
  - the README states the limits.
- **Still open:**
  - the embedding model row was not in the pasted list, so its limit stays an assumption until the iteration-2 spike (Q-5);
  - Q-6 (deadline hour).
