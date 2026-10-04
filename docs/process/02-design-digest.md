# 02: Design decision digest (Step 2, 🟡 soft gate)

> **Historical record.** This digest records the owner decisions of 2026-10-04 (Step 2). Where it conflicts with `01-requirements.md` rev. 4.3, `docs/architecture.md` (HLD) rev. 4.3 or `docs/decisions.md`, those documents win; this file is kept for the process record only.

- **Status:** PRESENTED to the owner on 2026-10-04. Owner inputs received the same day (§6): the framework is chosen on requirements fit, not on the owner's experience; Langfuse is in. §1 is revised to LangGraph and §7 (Langfuse) is added. The owner approved §1 (LangGraph) and §7 (Langfuse, including the trace-masking approach) on 2026-10-04 and chose self-hosted Langfuse in Docker for the prototype.
- **Input:** `docs/process/01-requirements.md` (approved at G1), `docs/data-model.md`
- **Next:** the architect (T1, `xhigh`) writes `docs/architecture.md` from this digest, following `.claude/ai-workflow/skills/architect.md`. ADRs go to `docs/decisions.md`.

## 1. Agent framework (revision 2: LangGraph)

The owner asked for the choice to follow the requirements, not their experience. Re-scored against the requirements (APIs checked on 2026-10-04: ADK 2.6, LangGraph 1.x, Langfuse docs):

| Requirement driver | LangGraph | Google ADK |
|---|---|---|
| R2/R3 guardrail pipeline as deterministic steps | Explicit graph: `input_guard → agent ⇄ tools → output_guard` nodes, each unit-testable | Callbacks (`before_model`, `before_tool`, `after_tool`, `after_model`) map well too |
| R3 confirmation flow | `interrupt()` + checkpointer: durable pause/resume, survives a process restart | `FunctionTool(require_confirmation=True)` / `tool_context.request_confirmation()` |
| R5 model fallback and retries | `with_retry()` / `with_fallbacks()` on the chat model, built in | Hand-written, or via LiteLLM |
| Sessions and state | `SqliteSaver` → `PostgresSaver` (Cloud SQL) | `DatabaseSessionService` / `VertexAiSessionService` |
| R6 evals | Langfuse datasets/experiments (we adopt Langfuse anyway, §7) | `adk eval` built in |
| R7 tracing | Langfuse `CallbackHandler`, first-class | Langfuse via OpenInference OTel instrumentor |
| Extensibility (new tools, data sources, models) | Provider-agnostic; Gemini via `langchain-google-genai` | Gemini-native; other models via LiteLLM |
| API stability | 1.x, stable | 2.x, still moving fast |
| GCP deploy | Cloud Run container | Cloud Run or Vertex AI Agent Engine |

**Recommendation: LangGraph.** ADK's two real advantages (Gemini-native, built-in eval) are neutralised: Gemini works well through `langchain-google-genai`, and evals go to Langfuse. LangGraph wins on the high-stakes parts: an explicit, testable guardrail graph, a durable confirmation interrupt, built-in model fallback, and a stable API. The owner's experience with LangChain/LangGraph is stated in the HLD as the assignment requires, but it is not the reason for the choice.

Guardrails still live in our own code (a policy module called from graph nodes and tools), never in framework internals. The delete confirmation keeps a code-issued token on top of `interrupt()`, so swapping the framework later stays cheap.

## 2. Model routing

- **Primary:** `gemini-3.8-flash` for the agent loop, SQL generation and reports.
- **`gemini-3.1-flash-lite`** for the cheap pre-LLM topic/injection classifier, and as the fallback model.
- **On failure:** 429/5xx/timeout → 3 retries, exponential backoff with jitter → the fallback model → a friendly message. The trace records which model answered.
- **Production:** the same models via Vertex AI. Model IDs live in config.

## 3. Guardrail placement

1. **Pre-LLM:** deterministic rules plus the flash-lite classifier (off-topic, injection).
2. **In tool (`run_sql`):**
   - sqlglot parse; a single SELECT only; table/column allowlist;
   - PII column denylist, including inference channels (WHERE/LIKE/JOIN oracle, `STRING_AGG`/`ARRAY_AGG`, GROUP BY on PII, small-cell aggregates);
   - **scope injected by code:** `order_items`/`products` references are rewritten into CTEs filtered by the role's categories, and the LLM never writes the scope predicate;
   - BigQuery dry-run plus `maximum_bytes_billed`, and a row cap.
3. **Post-tool:** PII pattern scrub on results.
4. **Post-LLM:** output PII scan, and the scope label added to the answer.
5. **Delete:** two-phase with a code-issued confirmation token. The audit record is written first, and the delete aborts if that write fails (`CLAUDE.md` non-negotiable).

Production adds defence in depth: BigQuery row-level access policies or authorized views per role.

## 4. Stores

| Data | Prototype | Production |
|---|---|---|
| Reports, sessions, preferences, feedback, audit | SQLite; audit + delete in one transaction | Cloud SQL Postgres; audit also streamed to a delete-protected BigQuery dataset |
| Golden Bucket | YAML seed of 10–15 trios; `gemini-embedding` vectors cached locally | GCS raw store, pgvector or Vertex AI Vector Search, an ingestion pipeline with analyst review |
| Persona | Langfuse prompt (label `production`, short cache TTL); a local file is the fallback when Langfuse is not configured | Langfuse prompt management (self-hosted): versions, labels, rollback, editor role |
| Traces | Local JSONL always; Langfuse when keys are set (§7) | Langfuse self-hosted on GCP + Cloud Logging/Monitoring |

## 5. Production cloud (GCP; our decision, A-13)

- **Runtime:** Cloud Run (agent API), IAP with OIDC for SSO, Vertex AI Gemini.
- **Data:** BigQuery with RLS, Cloud SQL with pgvector, Secret Manager.
- **Operations:**
  - Cloud Monitoring alerts;
  - GitHub Actions CI with an eval gate (changed from Cloud Build on 2026-10-04), plus a canary for prompt/model changes;
  - Cloud Scheduler + Cloud Run Job to refresh the Golden Bucket.

## 6. Owner inputs (resolved 2026-10-04)

1. **Framework experience:** experience with LangChain and LangGraph. The owner asked that the choice follow the requirements, not experience → §1 revision 2.
2. **Langfuse:** yes, set it up → §7.

## 7. Langfuse

One tool covers four requirements:

| Requirement | Langfuse feature |
|---|---|
| R7 Observability | Traces per turn (LLM calls, tool calls, SQL, guardrail verdicts), sessions, users, cost/latency dashboards |
| R8 Persona without redeploy | Prompt management: the persona/tone prompt is edited in the Langfuse UI by a non-developer; the agent fetches the `production` label with a short cache TTL; versions and rollback come built in |
| R6 Quality | Datasets + experiments for the golden/red-team evals; LLM-as-judge scores |
| R4 Learning | User feedback as scores on traces; low-scored traces feed the analyst review queue |

- **Prototype:** self-hosted Langfuse v4 in Docker Compose (owner's choice), at `infra/langfuse/docker-compose.yml` with pinned image tags and loopback-only ports. It runs six containers: web, worker, Postgres, ClickHouse, Redis and MinIO. They need roughly 4 GB RAM and 2–3 CPU. Headless init through the `LANGFUSE_INIT_*` env vars (org, project, API keys, admin user) gives a ready project on the first `docker compose up` with no clicks in the UI. The values come from `.env`, and `.env.example` documents them. A helper command generates the local-only keys. **Optional for the reviewer:** with no keys, the agent runs on local JSONL traces and the local persona file. Nothing breaks.
- **Production:** self-hosted Langfuse on GCP (Cloud Run + Cloud SQL Postgres + ClickHouse + GCS), so traces stay in our project.
- **PII (🔴 risk area 1, "what gets written to traces"):** traces carry only post-scrub data; in addition a `mask_otel_spans` export hook runs the same PII scrubber on every exported span. Langfuse must never become a PII side channel.
- **Failure mode:** Langfuse down → tracing is async and fail-open (the turn continues, JSONL still written); prompt fetch fails → last cached version, then the local file.
