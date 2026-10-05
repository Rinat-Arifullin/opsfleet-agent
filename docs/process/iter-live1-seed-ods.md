# Live-1: session seeding and per-run isolation for the live eval SUT (ODs)

Owner request 2026-10-05: the live Langfuse run failed with `check:run=False` on
`discuss_saved_report`, `persona_tone_change`, `report_save_confirm`, `report_search` and
`roadmap_actions_unsupported`. The live SUT accepted only `session.profile` and refused every
other seed (OD-4 in `docs/process/iter40b-ods.md`). This change seeds the case `session:`
fields through the real APIs, isolates every case under its own user id, and lets a case that
still cannot run live say so (`live: {skip: <reason>}`, reported n/a).

Files:
- new `evals/live_seed.py`: namespacing (`run_user_id`), the `seeded(...)` context manager,
  saved-report seeding through `ReportStore.save`, the persona presets (`formal`, `casual`)
  applied through `commands.persona.apply_persona`, `setup_turns` validation
- `evals/live_sut.py`: runs each case as the namespaced profile inside `seeded(...)`, plays
  setup turns before the case turns (own trace file, not scored), keeps the base profile id as
  the Langfuse user, counts setup turns toward the 8-turn cap
- `evals/run.py`: new case key `live: {skip: <reason>}` (`Case.live_skip`); a live run reports
  such a case as `na` without calling the SUT, an offline run ignores it; the request estimate
  leaves it out
- `evals/langfuse_dataset.py`: `live_skip` carried in the item metadata and back
- golden cases: `live.skip` on `report_search`, `discuss_saved_report`, `report_save_confirm`;
  `roadmap_actions_unsupported` seeds its report with `owner: self` and runs under every profile
- new tests `tests/unit/test_live_seed.py`; `tests/unit/test_live_sut.py` updated (the refused
  seeds are now `preferences` and unknown keys)
- docs: `infra/langfuse/README.md`

No hot files, no new dependencies, no config fields, no graph changes.

## Case schema

```yaml
session:
  profile: analyst_a
  saved_reports:                 # at most 20; keys: id, title, owner, created, tags, sections
  - {id: R-DEMO-0001, title: Synthetic Q1 summary, owner: self, tags: [returns]}
  persona: formal                # a preset name: formal | casual
  setup_turns: ["warm-up turn"]  # at most 4, played before the case turns, not scored
live:
  skip: why this case cannot run against the live SUT   # reported n/a live; offline runs it
```

## Five failing cases

| Case | Now | Why |
|---|---|---|
| `persona_tone_change` | runs live | the `formal` preset is applied and audited for the case |
| `roadmap_actions_unsupported` | runs live, every profile | the seeded report belongs to the running profile (`owner: self`) |
| `report_search` | `live: skip` | OD-4, OD-5 |
| `discuss_saved_report` | `live: skip` | OD-2, OD-5 |
| `report_save_confirm` | `live: skip` | OD-9 |

## Decisions
1. **OD-1 One user id per case.** Every case runs as `<profile>.ev<tag>` (a fresh 8-hex tag,
   the base id cut so the result stays a valid 64-char profile id). Saved reports, pending
   drafts, quota, history and health are keyed by that id, so nothing carries over between
   cases or runs, and the real `analyst_a` rows are never touched. The cost: a quota or
   pending-draft behaviour that spans cases cannot be tested live; such a case needs
   `setup_turns`. The runtime is still one per base profile; Langfuse traces keep the base id,
   so the dashboard groups by the shipped profiles.
2. **OD-2 Fixture ids live in the title.** `ReportStore` assigns uuid-hex ids, so
   `R-DEMO-0001` cannot be the live id. The seeded title is `"<fixture id> <title>"`, which
   keeps the fixture id visible to the library and the model; a case that addresses the report
   by its id cannot pass live.
3. **OD-3 Seeds skip the content guard, not the store checks.** The report guard is a
   pass-through for seeds (the text is fixed synthetic content from `live_seed.py` and the
   case title), but `ReportStore.save` still runs the owner check, the required-section check
   and the secret scrub. Bodies are synthetic placeholders with every required section.
4. **OD-4 `created` is not backdated.** The store stamps `created_at`; there is no API to set
   it and no raw SQL is used. The `created` key is accepted and ignored, so a date-range case
   cannot run live.
5. **OD-5 The library is not a graph route.** List, search and open are slash commands
   (`/reports`, `/search`, `/open`) handled by the CLI, not by `run_turn`, so a case whose
   input is "find my reports ..." or "open report ..." gets a graph answer that never reads
   the library. `report_search` and `discuss_saved_report` are live-skipped until the graph
   routes library requests or the SUT learns to play slash commands.
6. **OD-6 The persona smoke check is structural.** The production smoke subset is a live eval
   run of its own; the seed passes a check that the parsed persona has text. The preset still
   goes through `parse_persona` (header, sections, forbidden words), the audit-first write and
   the version check after load.
7. **OD-7 The persona swap is per case on a shared runtime.** The preset is written under
   `<eval data dir>/seed/<tag>/`, and the graph's `services.persona` source is replaced for the
   case and restored in `finally`. Cases run one at a time on the SUT's owner thread, so no
   other case sees the swap; a case that times out retires its runtime, so a swap can not leak
   into a later case.
8. **OD-8 `preferences` stays unsupported.** Iteration 39 has no persisted preference store, so
   there is nothing real to seed through. The seed raises a clear `CaseError`, as do unknown
   keys.
9. **OD-9 `report_save_confirm` checks `R-`.** Live report ids are uuid hex, so the `R-`
   expectation can only pass on the recorded fake. The case is live-skipped rather than edited,
   because the save and draft flow is being reworked in parallel; when that lands, replace
   `R-` with a check that holds live (for example `saved`) and drop the skip. Whether the
   earlier `run=False` came from the expectation or from the save flow itself is not known.
10. **OD-10 Profile opt-outs.** `roadmap_actions_unsupported` now runs under all profiles
    (its fake answer names no brand, and the seed belongs to whichever profile runs).
    `report_search` and `discuss_saved_report` keep `profiles: [analyst_a]`: they are
    live-skipped, `report_search` asserts on another owner's report, and the
    `discuss_saved_report` fake names an `analyst_a` brand. Lifting them belongs with OD-5.
11. **OD-11 Seed directories are not cleaned up.** Each persona seed leaves a few small files
    under `<eval data dir>/seed/<tag>/`, and namespaced reports stay in the eval store. Both
    live only in the eval data dir, never in the user's store; delete the dir to reset.
12. **OD-12 Eval session ids are not uuid hex.** The live SUT's session id is
    `<ev-case id>-<6 hex>`, readable in Langfuse, but the audit store accepts uuid-hex session
    ids only. The persona seed audits with a fresh uuid-hex session id for that reason. A live
    graph path that audits with the session id (a delete, for example) would fail closed; that
    predates this change and no case in the five depends on it. Switching the live session id
    to uuid hex would fix it at the cost of readable Langfuse sessions.
