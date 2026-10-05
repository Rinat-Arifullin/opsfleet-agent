# Local Langfuse (optional, development only)

A self-hosted Langfuse on your machine, so you can look at what the agent did in each turn:
routing, the input guard, the quick or deep analyst steps, every LLM call (prompt, output,
tokens, latency, retries and fallbacks), tool calls with sanitized SQL, and the grounding
and output guard verdicts. The agent works the same without it; tracing is off unless the
three `LANGFUSE_*` variables below are set.

The stack is the official Langfuse self-host compose (web, worker, Postgres, ClickHouse,
Redis, MinIO), pinned to Langfuse 4.50.0, with every port bound to `127.0.0.1`.
It needs Docker with Compose v2 and about 4 GB of free RAM.

## Run it

1. Create the settings file and replace every `change-me` value:

   ```bash
   cd infra/langfuse
   cp .env.example .env
   # edit .env: use `openssl rand -hex 32` for the passwords, SALT, NEXTAUTH_SECRET and ENCRYPTION_KEY,
   # and pick your own pk-lf-... / sk-lf-... project keys and admin login
   ```

   `infra/langfuse/.env` is git-ignored. Do not commit it.

2. Start the stack (the first start pulls images and runs migrations, a few minutes):

   ```bash
   docker compose up -d
   docker compose ps          # wait until langfuse-web is "running"
   ```

3. Open <http://localhost:3000> and sign in with `LANGFUSE_INIT_USER_EMAIL` /
   `LANGFUSE_INIT_USER_PASSWORD`. The org and project `opsfleet-data-agent` already exist.

4. Point the agent at it. In the **project root** `.env` (next to `GEMINI_API_KEY`), add:

   ```bash
   LANGFUSE_PUBLIC_KEY=<the LANGFUSE_INIT_PROJECT_PUBLIC_KEY value>
   LANGFUSE_SECRET_KEY=<the LANGFUSE_INIT_PROJECT_SECRET_KEY value>
   LANGFUSE_HOST=http://localhost:3000
   ```

5. Run the CLI as usual (`uv run opsfleet-agent`, or with `OPSFLEET_LLM_PROVIDER=lmstudio`).
   Each user turn appears under **Tracing** as one trace named `turn`, grouped by CLI session
   under **Sessions**. `/trace` in the CLI prints the Langfuse trace id of the last turn.

## Golden evals as a Langfuse dataset

With the server running and `LANGFUSE_*` set (step 4), you can upload the golden cases as a
dataset and run them live against the real agent. Each run appears under **Datasets ->
opsfleet-golden -> Runs**. Every item is linked to its turn trace and gets a `pass` score plus
one `check:<name>` score per eval check.

```bash
# upsert evals/cases/golden as dataset "opsfleet-golden" (item id = case id; safe to repeat)
uv run python evals/langfuse_dataset.py upload

# run the live agent on every item; the run name defaults to <short commit>-<UTC timestamp>
OPSFLEET_LLM_PROVIDER=lmstudio uv run python evals/langfuse_dataset.py run
# a quick check: one item, or chosen cases (--case is repeatable)
OPSFLEET_LLM_PROVIDER=lmstudio uv run python evals/langfuse_dataset.py run --limit 1
OPSFLEET_LLM_PROVIDER=lmstudio uv run python evals/langfuse_dataset.py run --case churn_last_month

# the same live agent through the plain eval runner (results under evals/results/)
OPSFLEET_LLM_PROVIDER=lmstudio uv run python evals/run.py \
  --sut evals.live_sut:live_harness --cases-dir evals/cases/golden
```

`run` prints a summary table and the dataset run URL. It exits with 0 when every case passed,
1 when any case failed, and 2 when Langfuse is not configured or unreachable. Without the
provider override it uses Gemini, which counts against your quota. Live eval state (sessions,
saved reports, quota, JSONL traces) goes to `OPSFLEET_EVAL_DATA_DIR` (default
`<OPSFLEET_DATA_DIR or data>/eval-live`), never to your own CLI store. Each case is capped at
8 turns and at `OPSFLEET_EVAL_CASE_TIMEOUT_S` seconds (default 600). Cases that seed saved
reports, a persona or preferences fail with a clear reason. See `docs/process/iter40b-ods.md`.

## Stop or reset

```bash
docker compose down        # stop, keep data
docker compose down -v     # stop and delete all traces and users
```

## What is sent, and what is not

See `docs/process/iter40-ods.md`. In short: the scrubbed question and answer, LLM prompts and
outputs after PII masking, sanitized SQL and query stats, guard verdict codes and timings.
Never result rows, tool results, secrets, API keys, pending actions or delete proofs.
