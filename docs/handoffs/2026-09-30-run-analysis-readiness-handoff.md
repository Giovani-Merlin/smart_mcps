# Run-analysis program — readiness handoff (2026-09-30)


> **Update 2026-10-05 (local session) — steps 1–5 below are DONE.** The four local files are committed in
> infinity-skills, the tool is reinstalled, `[recipes]` is configured, and the grill settled on the full
> requirements scope. The draft plan named below was **replaced** by
> `infinity-skills/docs/plans/2026-10-05-001-feat-run-analysis-program-plan.md` (R1–R21, nine runs / four repos,
> 17 units, 11 groups; plan-check, group and the verifier clean). Export now happens inside the plan's `run`
> unit, not as a driver preflight, so the bundle-export loop in step 4 is obsolete. Remaining: add `.cache`
> to `[workspace] data_dirs`, `/orchestrator-deepen` the new plan, then `/orchestrator-run` it. Run 2 (the
> grouping KPI loop) still waits for Run 1's `data/analysis/latest/read-overlap.json`.

Written from a remote container session that had both repositories but not
the operator's machine. It answers one question — *can the run-analysis
program be planned and run now?* — and leaves everything the local session
needs in committed form.

## Verdict

| question                                                        | answer                                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Can `/orchestrator-plan` run against this build?                | **Yes.** The merged r20260927-100604 build parses v2 task maps with `run` and `research` units, and `plan-check` + `group --no-spec` + `group --advise` all pass on the draft plan below. The one thing a remote session cannot do is the grill (one question at a time with the operator).                                                        |
| Can the analysis run be planned without the local docs?         | **Mostly.** R29 of `docs/brainstorms/2026-09-27-research-evaluate-optimize-requirements.md` specifies Run 1 precisely enough for a draft. The infinity-skills requirements doc, `CONTEXT.md` and the ground-truth notes exist only on the operator's machine (not on `master`), so the draft carries no local R-IDs and four open grill questions. |
| Can `/orchestrator-run` execute the plan from a remote session? | **No.** The five run bundles, the transcripts under `~/.claude/projects`, and infinity-skills' `data/corpus.db` are local and gitignored. The run is a local-session job; the plan is the part that travels.                                                                                                                                       |
| Is the 2026-09-03 framework-adapter plan still pending?         | **No — done.** Run r20260904-141438 executed it and its commits are on infinity-skills `master` (`run_bundle.py` reads `schema_version` 2, `server/routers/runs.py` serves the Orchestrator tab and `/runs/signals`). Drop that item from the notes.                                                                                               |
| Is the grouper optimisation still open?                         | Waves 1–2 of the 2026-08-28 grouper brainstorm shipped (r20260828-220035, r20260829-162627). What remains is R20's evaluation harness, which R30 (`grouping-loop-run`) now defines as Run 2: an `evaluate` + `optimize` loop in this repo. Not planned yet; see "Run 2" below.                                                                     |

## What this session produced

- **Draft plan** (infinity-skills, branch `plan/run-analysis-program`):
  `docs/plans/2026-09-30-001-feat-run-analysis-program-plan.md`. Nine units,
  six groups: a slice of four extraction modules (`code`), an ingest `run`
  unit over five bundles, an extraction `run` unit committing three tables
  under `docs/analysis/`, two `research` units, and a `code` unit that
  receives the Spec Refinement. Origin is R29 of this repo's requirements
  doc; the front matter says where to re-point it.
- **This handoff.**

Container-only state that did not travel (redo locally where relevant):
`npm install -g @colbymchenry/codegraph` + `codegraph init/index` in both
repos (needed by `group`), an infinity-skills `.orchestrator/config.toml`
with the two blocks below, and an empty `data/corpus.db` placeholder that let
`plan-check` resolve the data paths.

## What was verified against the code (not assumed)

- `smart-mcps-orchestrate` on `main` (`58ccac2`) exposes `group --no-spec | --advise | --price`, `plan-check`, `split`, `export --out --project`, `calibrate`; registered recipes are `code, run, evaluate, optimize, research`.
- `group` shells out to the `codegraph` CLI (`orchestrator/grouping/graphing.py:191`) and refuses a `run` output outside `[workspace] data_dirs`; `plan-check` lints every `Run:` line's programs against the worker allowlist and every path it reads (`orchestrator/grouping/verification_lint.py`). A file named after `--out` is only accepted when an earlier `mkdir -p <dir>` in the same command creates its directory — the draft plan follows that pattern.
- A `run` command executes through `sh -c` (`orchestrator/execution/run_child.py:146`), so an env prefix such as `INFSKILLS_DISTILL_MAX_WORKERS=8` works.
- infinity-skills: `ingest-run` chains distill → enrich → summarize → graph with per-stage skip flags (`cli.py:39`); `run_full_pipeline` scopes distillation to the run's own sessions and runs the other stages corpus-wide (`extract/run_bundle.py:847`); `ingest-run` is additive and idempotent; `.gitignore` covers `data/`, `.cache/`, `.orchestrator/` and `uv.lock`; the LLM provider defaults to `claude-code` since 2026-09-27.
- The golden bundle `tests/fixtures/run_bundle_v2/` carries a `tool_refused` denial, a `changes_required` verdict, a `group_resolve` escalation, a rewrite and a transcript-less reviewer — a real, LLM-free oracle for every extraction unit's tests.

## Local next steps, in order

1. **Fetch the draft and commit the four local files** in infinity-skills:
   the two `docs/research/2026-09-27-*.md`, `CONTEXT.md`, and
   `docs/brainstorms/2026-09-27-run-analysis-program-requirements.md`.
   `git fetch origin plan/run-analysis-program` and merge or cherry-pick the
   plan commit onto `master`.

2. **Reinstall the tool** (the stale-install guard refuses the pre-#14 copy):
   `uv tool install --reinstall ~/wksp/smart_mcps`, then
   `smart-mcps-orchestrate run --help` must exit 0.

3. **Config** — infinity-skills `.orchestrator/config.toml` needs:

   ```toml
   [recipes]
   enabled = ["run", "research"]

   [workspace]
   data_dirs = ["data"]
   ```

   and `[docs] formats = ["facts", "changelog", "html"]` if the run should commit a report.

4. **Export the five bundles** into the infinity-skills data layer (the draft plan makes this a driver preflight, not a `run` command — open question 2 in the plan):

   ```sh
   cd ~/wksp/infinity-skills
   for r in r20260829-162627 r20260923-163956 r20260924-134934 r20260916-113121 r20260927-100604; do
     smart-mcps-orchestrate export "$r" --repo ~/wksp/smart_mcps --out "data/bundles/$r" --project smart_mcps
   done
   grep -c '"transcript_missing": true' data/bundles/*/ingest.json
   ```

   Swap in `r20260828-220035` for any run that exports mostly missing transcripts.

5. **Reconcile the draft with the requirements doc** from an infinity-skills session (the remote grill was started on 2026-10-05 and stopped by Giovani at its first question — which R-IDs to carry — so the local grill starts there):
   `/orchestrator-plan docs/brainstorms/2026-09-27-run-analysis-program-requirements.md`, pointing the session at the draft as its starting text so the grill carries the local R-IDs onto the existing units and settles the plan's four open questions. Then `smart-mcps-orchestrate plan-check` and `group --no-spec` again.

6. `/orchestrator-deepen docs/plans/2026-09-30-001-feat-run-analysis-program-plan.md`, then `/orchestrator-run` on it. The R29 driver checklist is already written as `Run (driver):` items on U2, U6, U7, U8 and U9.

7. Delete the local `smart_mcps/.orchestrator/notes-r29-analysis-planning.md` once steps 1–5 are done (the notes said it is superseded).

## Run 2 — the grouping KPI loop (R30), not yet planned

All of its inputs but one live in this repo: the stored plans under
`docs/plans/`, the task graphs `group --advise` rebuilds, and the grouper
modules (`orchestrator/grouping/`). The one external input is the per-group
read sets from real coder transcripts, which Run 1's U4 produces as
`data/analysis/readset_overlap.json` (per run: group read sets and pairwise
overlap). The natural sequencing is therefore Run 1 first, then a Run 2 plan
whose harness unit reads that JSON (copied into this repo's data dir) and
whose `optimize` unit's `files:` are the grouper modules, 20 evaluations,
per R30. That plan can be drafted in a remote session; only its run needs
the local machine.

## Not established here

- The ground-truth notes' "Run Child behaviour, worktree traps, export and injection rules" — unread; if they contradict the plan's Decisions (export as preflight; four ingests with skips then one full chain; eight distill workers), the plan changes, not the notes.
- Wall-clock caps (120/120/120/180/300 minutes) are estimates until `uv run infskills summarize --estimate-cost` runs on the first local bundle.
