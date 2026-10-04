# Handoff: state of the work (2026-10-04, after 🔴 G3)

Read this first if you are a fresh agent picking up the project.

## Where we are

| Step | State |
|---|---|
| 0 Framework port | Done. `.claude/ai-workflow/` is adapted to this project, reviewed, and public on purpose (it feeds the README "How I worked" section) |
| 1 Requirements | **Requirements rev. 4 approved by the owner (2026-10-04).** Rev. 4 = rev. 3 (client answers: brand scope, author-only reports, hard delete, churn per message, load profile, narrow `--resume`) + rev. 3.1 (light path FR-71, confirm-before-save FR-72, report search FR-73/74, output guard FR-75, scope filter at context assembly FR-76) + the owner decisions: FR-73 M, FR-70 P, FR-14 M; Revise unlimited, each Revise its own budgeted turn (A-36); k = 5 only for customer quasi-identifier breakdowns (A-33); resource use as code-verifiable mechanisms (§6.8); demo brands chosen by us (A-38); frugality with tokens and data as a principle (§6.2a, A-39). Logged in `docs/decisions.md`, "Owner confirmation of requirements rev. 4". **Rev. 4.1 (2026-10-04, owner G2 answers):** no money figures anywhere; R1 is M; FR-48 Golden and FR-74 extended search (FTS5 + semantic) are prototype M. 53 prototype FRs (46 M, 7 P), 21 HLD-only. **Rev. 4.2 (2026-10-04, parity, option C):** prototype and production have the same functions; 74 active FRs: prototype 63 (59 M, 4 P), platform adapters 4, roadmap 7; no application roles |
| Client questions | 6 sent and **answered** on 2026-10-04 (§8.2). Logged in `docs/decisions.md`, "Client answers to requirements questions (2026-10-04)". A-34: the client is told backups keep deleted reports up to 30 days, never restored (owner, Q8); rev. 4.4 shortens this to up to 7 days (PITR only, ADR-014) |
| 2 Design | **Digest approved** (`docs/process/02-design-digest.md`, owner inputs resolved 2026-10-04: LangGraph, self-hosted Langfuse; now a historical record). **Architecture written:** `docs/architecture.md` (HLD rev. 4.4, 13 sections, 8 Mermaid diagrams). ADR-001..014 in `docs/decisions.md`, all **Proposed** |
| 3 Design review | **Done, READY with conditions.** `docs/process/03-design-review.md`: 0 BLOCKER, 4 MAJOR, 10 MINOR, 4 NIT. **All findings resolved** in `architecture.md` and ADR-003/004/007/008 (still Proposed). The "Architect response" section at the end of the review maps each finding to its fix. Mermaid re-validated. Awaiting 🔴 G2 |
| 2b Multi-agent revision | **HLD revision 3** (2026-10-04, owner direction): code supervisor + 5 roles (Quick/Deep analyst, Report writer, Report verifier, Library), model per role, envelope, failure matrix, 3-level bounded retries, `--resume`, per-role evals. New HLD §4.0, ADR-009; ADR-001/003 revised; requirements budgets 10 (Q&A) / 14 (report turn) |
| 3b Design review 2 | **Done.** Appended to `03-design-review.md`: READY with conditions; 0 BLOCKER, 6 MAJOR (R2-M1..M6), 7 MINOR, 2 NIT. **All resolved in HLD revision 4**; see "Architect response (Review 2)" at the end of the review |
| 2c HLD revision 4 | **Done (2026-10-04).** `docs/architecture.md` rev. 4 covers requirements rev. 4 and Review 2 in one pass: brand scope with an explicit `all` flag, author-only reports, hard delete with A-34 backups, FR-69/70, new load (A-8, A-35), narrow resume, light path with a separate `smalltalk` label (8 labels), confirm-before-save with unlimited Revise, `search_reports`, output guard (FR-75), context scope filter (FR-76), delete vault + HMAC proof and confirm/execute split, budget sub-caps, `agent_outcome_total` and alerts. New §1.4, §2.0, §4.5, §6.2a, §9.6; §7.4 is usage only. ADR-003..009 revised, ADR-010..012 added, all **Proposed**. 8 Mermaid blocks validated. Open questions §13.2 Q8–Q11 resolved by the owner (rev. 4.1) |
| 2d HLD revision 4.1 | **Done (2026-10-04).** Owner answers Q8–Q11 and cuts: no money figures; backup disclosure in the delete preview; 20 report messages per user per hour; tiers 5 Golden and 6 extended search (FR-74) committed, tiers 0–6 the minimum, tier 7 P extras; explicit cut list in §1.2. The Q10 / FR-70 wording (session-scoped vs per user across sessions) is reopened by the independent review (R3-M2) and is an open owner decision. Logged in `docs/decisions.md`, "Owner G2 decisions…" |
| 2e HLD revision 4.2 | **Done (2026-10-04).** Parity (option C): the "design-only" status is replaced by prototype / platform adapter / roadmap (§1.2). No application roles: brand list or CEO `all` (data only, reports stay author-only), `access set`, team tooling via IAM-gated jobs (§10.3). Prototype adds FR-10 idle timeout, FR-46/47 feedback and triage (`promote`, `add-eval`), rename, Markdown export, retry report, erasure CLI, history browse, quotas, degraded mode, persona mechanism (FR-51/52 M); pro-class Deep model via `config/models.yaml`. Schedule risk +2.5–3 days, drop order defined. Logged in `docs/decisions.md`, "Owner G2 decisions rev. 4.2" |
| 3c Independent design review | **Done (2026-10-04).** `docs/process/03b-independent-review.md`: READY with conditions (recommendation only); 0 CRITICAL, 11 HIGH, 31 MEDIUM, 36 LOW |
| 2f HLD revision 4.3 | **Done (2026-10-04).** Requirements rev. 4.3 and HLD rev. 4.3 apply Review 3 (the independent review 03b). ADR-001..013 Proposed. Four owner decisions remain open (see Next actions) |
| 2g HLD revision 4.4 | **Done (2026-10-04).** The owner answered the four open decisions (cut line accepted; ADR-013 option A; FR-70 session wording plus a per-user cross-session form, M; embedding `gemini-embedding-001` at 768 dimensions) and asked to close the five §13.3 risks in the design: typed PII by local NER (Presidio + pinned spaCy model), cross-session differencing with value-free fingerprints, a derived never-stored delete token (ADR-007), no deleted-data residue and A-34 at up to 7 days (new ADR-014), and a judge calibration gate (FR-66 calibration part M). Requirements rev. 4.4: 74 active FRs, prototype 64 (61 M, 3 P), platform 4, roadmap 6; new AC-08.14, AC-08.15, AC-12.15, AC-12.16, AC-29.6. ADR-001..014 Proposed. Schedule about +1–1.5 days, deadline unchanged. Logged in `docs/decisions.md`, "Owner decisions at G2 review (2026-10-04)" |
| 4 Plan | **Done, 🔴 G3 approved 2026-10-04.** `docs/process/04-plan.md` rev. 2: 49 iterations (8, 14, 22, 28 split a/b), 71.5 h vs 67 h capacity; drop order 39, 38, 37, 36, 33, 34, 35, 42, 41, 40. Plan review: `docs/process/04b-plan-review.md` (all findings fixed in rev. 2). Owner decisions: `docs/decisions.md`, "Step 4b plan review" and "🔴 G3 approval" |
| 5–6 | Not started |

## Next actions

**Next action: Step 5, iteration 1 of `docs/process/04-plan.md` (skeleton, all dependencies, config, startup check).** Q-3 closed 2026-10-04: limits read in AI Studio (flash 5 RPM / 20 RPD, flash-lite 15 RPM / 500 RPD), the owner stays on the free tier; plan §5 and R4 amended, Quick analyst back on flash-lite per HLD §4.0 (decisions, "Free-tier decision"). Open owner items: the embedding row in AI Studio (not in the pasted list), Q-5 (only if the iteration-2 embedding spike fails), Q-6 (deadline hour). 🔴 G3 approved 2026-10-04. Earlier: 🔴 G2 approved by the owner on 2026-10-04 (HLD and requirements rev. 4.4; ADR-001..014 Accepted). A simplified HLD page for the owner exists outside the repo (link shared with the owner, not recorded here).

1. ~~Architect writes HLD revision 4~~ **Done 2026-10-04** (kept below for reference). (`docs/architecture.md` + ADRs) in one combined pass, against requirements rev. 4 (approved):
   - Requirements rev. 3: brand scope (ADR-008 scope CTEs keyed by `products.brand`, CEO `all` flag), author-only reports (remove sharing), hard delete (remove soft delete and the 30-day restore; backup window per A-34), small-cell k = 5 after scoping and a differencing guard (FR-69, FR-70), new load profile and data growth (A-8, A-35), narrow `--resume` with pending deletes expiring (FR-14, AC-22.6; replace HLD §12 #5 "a restart is a new session").
   - Requirements rev. 3.1 (see the decisions.md entry "Owner review of the HLD diagram"): light path before context assembly (router label for small talk is the architect's call), confirm-before-save report flow (revise ADR-009's "saved by code after the verifier"), report search kept apart from delete targeting, output guard with the role-and-label action allowlist and answer injection scan, scope filter at context assembly with the `scope_snapshot` check on resume.
   - HLD presentation items from the diagram review: a "Speed and tokens" section (prompt-prefix, embedding, persona and table-metadata caches; production SQL result cache keyed by normalised SQL + scope key + data refresh date, invalidated daily), a "Key decisions and alternatives" section surfacing the ADRs, per-module failure handling on the schematic, report and chat-history stores on the overview diagram, and traces described as observability (Golden is for retrieval and offline evals).
   - Requirements rev. 4 (owner decisions): unlimited Revise, each Revise its own report turn with the full budget (drop any revision cap in ADR-009 and the report flow); resource use as code-verifiable mechanisms (§6.8), with no money figures (rev. 4.1); frugality with tokens and data as an explicit design principle (§6.2a: minimal context, no `SELECT *`, 200-row cap, byte caps, light path, caches, session memo of identical queries, per-turn token and byte totals in traces); k = 5 only for customer quasi-identifier breakdowns; demo brand profiles (2–3 brands we choose, A-38). Router `smalltalk` label vs `meta` stays the architect's call.
   - Design Review 2: MAJOR R2-M1..R2-M6, then the MINORs R2-m1..m7 and NITs R2-n1..n2; add "Architect response (Review 2)" to `03-design-review.md`.
2. ~~Republish the HLD diagram~~ **Done** (private page for the owner, outside the repo; link not recorded here).
3. ~~🔴 G2 with the owner~~ **Approved 2026-10-04.** Reviews 1 and 2, the independent Review 3 (03b) and their fixes in HLD rev. 4.3, plus the rev. 4.4 decisions and risk closures. §13.2 Q1–Q11 answered on 2026-10-04 (HLD rev. 4.1; Q10 settled in rev. 4.4). Approved explicitly by the owner.
4. Step 4 plan → 🔴 G3. Step 4 inputs include the README steps for installing the pinned spaCy model on both the uv and pip paths. Step 5 implementation. Step 6 final review → 🔴 G4.

Earlier: the architect resolved Review 1 (M1–M4, the MINORs, n1–n2); n3 waits for the Step 5 README; n4 happened at G2 (approved 2026-10-04).

## Files

| File | Purpose |
|---|---|
| `CLAUDE.md` | Project rules and non-negotiables |
| `.claude/ai-workflow/` | The process: workflow, skills, gates, model tiers |
| `docs/process/01-requirements.md` | Requirements (rev. 2 approved at G1; rev. 4 approved by the owner 2026-10-04; current rev. 4.4, approved at G2 2026-10-04) |
| `docs/process/02-design-digest.md` | Approved design decision digest; historical record, superseded where it conflicts with rev. 4.4 and `decisions.md` |
| `docs/process/03-design-review.md` | Reviews 1 and 2 of the HLD and ADRs (Step 3), with architect responses |
| `docs/process/03b-independent-review.md` | Independent Review 3 of HLD and requirements rev. 4.2 |
| `docs/architecture.md` | The HLD deliverable (production design + prototype scope) |
| `docs/data-model.md` | The 4 tables (ER diagram, row counts, categories). The PII marking is our assumption A-3 |
| `docs/decisions.md` | Gate approvals and ADRs |
| Assignment text | Outside the repo (ask the owner for the file) |

## Environment

- **Toolchain:** Python 3.12 and uv (dev); `requirements.txt` exported with `uv export` for pip users.
- **GCP:** set `GOOGLE_CLOUD_PROJECT` in your environment (or `.env`) to your own project. Auth is ADC.
- **CLI tools:** ensure `bq` and `gcloud` are on PATH.
- **Gemini:** the key is `GEMINI_API_KEY` in `.env`. Model ids per role live in `config/models.yaml` (ADR-009, HLD §3): a flash-class model for the Report writer, a pro-class model for the Deep analyst behind the eval gate, flash-lite for the router, Quick analyst, verifier and Library agent, with a fallback per role. Verify every id against the provider's model list at the start of Step 4 (R3-L31).
- **Data:** order dates run 2019-01-12 .. today; the dataset refreshes continuously.

## Hard rules (from the owner)

- **Secrets:** never print, log or commit `.env` or the API key.
- **Public repo:** `.claude/` is committed. Don't name real people in public docs.
- **Gates:** never self-approve a 🔴 gate. Don't edit `CLAUDE.md` without the owner's OK, even if a subagent suggests it.
- **Client questions:** only client-side definitions and constraints, never our own architecture, storage or cloud choices. No cap on how many.
- **Quality over the 6–12 h estimate.** The deadline is Thursday 2026-10-08.
- **Language:** talk to the owner in Russian. Repo docs are in English.
