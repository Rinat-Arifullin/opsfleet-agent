# OpsFleet Data Agent — instructions for Claude Code

Take-home: an LLM data-analysis chat agent (CLI) over BigQuery `bigquery-public-data.thelook_ecommerce`, using Gemini. The deliverables are a production HLD, a detailed technical description and a working prototype.

## Process
All work follows the lite SDLC framework in [`.claude/ai-workflow/`](.claude/ai-workflow/README.md):
- Entry point: `.claude/ai-workflow/workflows/sdlc-lite.md`
- Roles and skills: `.claude/ai-workflow/agents.md`, `.claude/ai-workflow/skills/`
- Gates and risk areas: `.claude/ai-workflow/config/human-gates.md`. Never self-approve a 🔴 gate.
- Artifacts: `docs/process/` (requirements, design review, plan, reviews), `docs/architecture.md` (the HLD deliverable), `docs/decisions.md` (ADRs)

## Stack & commands
- Python 3.12, uv for development: `uv sync`, `uv add <pkg>`, `uv run ...` (don't `pip install` into the dev env)
- Reviewers may use pip: keep `requirements.txt` in sync via `uv export --no-hashes --format requirements-txt > requirements.txt`, and the README documents both paths
- Lint and test: `uv run ruff check . && uv run pytest -q`
- GCP project comes from the `GOOGLE_CLOUD_PROJECT` env var (never hard-coded; the reviewer runs it in their own project); auth via ADC (`gcloud auth application-default login`)
- Secrets live in `.env` (`GEMINI_API_KEY`). Never print, log or commit it.

## Non-negotiables
- PII, product scope, deletion and the SQL policy are enforced in **code**, not only in prompts
- Unit tests make no network calls; live calls are only in `evals/` and `@pytest.mark.live` tests
- The project must run on the reviewer's machine from the README alone: no hard-coded project IDs, paths or personal accounts
- Every query is dry-run and capped with `maximum_bytes_billed`; every loop and retry is bounded
- A delete writes its audit record first; if the audit write fails, the delete is aborted
