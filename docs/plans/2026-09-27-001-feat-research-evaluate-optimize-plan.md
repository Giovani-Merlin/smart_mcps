---
title: Research, Evaluate and Optimize — three Unit Recipes and the KPI loop
type: feat
date: 2026-09-27
origin: docs/brainstorms/2026-09-27-research-evaluate-optimize-requirements.md
---

# Research, Evaluate and Optimize — three Unit Recipes and the KPI loop

## Objective

Register three more Unit Recipes on the v1 registry and make the registry,
not the executors, the place a recipe's prompt, contract, tools and merge
policy live. After this plan:

- a `research` unit runs as a worker session with Perplexity and the web
  tools, commits a Findings Artifact under `docs/research/` whose every
  finding carries a source, and may refine the spec of the one downstream
  unit it feeds (R1–R9, R5 via the surprise board);
- an `evaluate` unit is a `run` unit plus a KPI Contract: the measurements
  JSON names the objective, guards and harness paths, the harness is
  hash-checked before every scoring run, and a judge is a harness script
  (R10–R14);
- an `optimize` unit is a coder-and-reviewer group whose every round is one
  candidate, one evaluation and one orchestrator decision against the
  Champion, with an Attempt Ledger in every prompt, a cheat reviewer on every
  `promising` candidate, bounded dials that escalate, a zero-hit COMPLETED,
  and a kept Champion merged through the code ladder (R15–R23);
- the registry carries a per-recipe worker prompt, extra allowed tools,
  session role and a declared-commit-globs merge policy of which the code
  ladder is the unrestricted case (R24, R25, R27); export and report stay
  additive (R26); every new recipe ships with a driver checklist (R28).

**Not covered, by design:** R29 `analysis-run` and R30 `grouping-loop-run` are
the two live runs *after* this plan (infinity-skills, then smart_mcps); their
harness unit and pipeline units are planned then. `synthesize` stays deferred.

## What we already know (resolved context)

**The registry (plan U1 of the run-recipe plan).** `UnitRecipe`
(`orchestrator/recipes/registry.py:27`) is a frozen dataclass with `name`,
`args_model`, `price: PriceFn`, `contract: type[BaseModel]`,
`reviewer_prompt: str | None`, `handoff_prompt: str | None`,
`merge: Literal["code_ladder", "run_commit_paths"]`, `executor: str`
(`module:attr`, resolved by `dispatch._resolve_factory`) and `custom`.
`_entries()` imports `CODE_RECIPE` and `RUN_RECIPE` lazily; `registered_names()`
and `get_recipe()` are the only readers. `RecipePrice(tokens, wall_clock_s, defaulted)`. `RecipesConfig.enabled` (`config.py:735`) validates against
`registered_names()` at load, so a new entry needs no config change to be
listable, and `pipeline._check_recipe_gate` (`pipeline.py:409`) refuses an
unlisted recipe naming the task. `tests/test_executor_dispatch.py` resolves
every registered recipe's executor, so a new entry with a bad `executor`
string fails there.

**The code loop.** `review.py:139` composes `_GroupExecution(GenerationLoop, ReviewerRound, SessionRecords, MergeLadder, EscalationHandlers, SurpriseHandling)`; `ReviewDeps` (`review.py:72`) is the seam every executor
gets (`runner`, `store`, `merge_group`, `rewrite_spec`, `board`, `artifacts`,
`groups_by_id`, `triage`, `recipes_config`, …). `GenerationLoop._run_generation_body`
(`generation.py:231–475`) is one method: first prompt via
`render_coder_prompt` + `_apply_briefing/_env_notice/_operator_note/_artifact_inputs`,
launch through `_launch_call()` (`start_worker` with `base_context`), then a
`while True` of `nudge_until_report(..., CoderReport, ...)`, `needs_input`,
`permission_denied`, the verification gate (`unmet_required_verification`),
`_review_round`, approve → `_merge()`, `changes_required` → `_breaker_reason`
→ `_retire` + `_prepare_handoff` or a warm `resume` with
`render_revision_prompt`. `CoderReport` is hard-coded in five places there;
`render_coder_prompt` loads `prompts/coder.md`; `render_handoff_prompt` loads
`prompts/handoff.md`; `ReviewerRound._review_round` (`reviewer.py:44`) loads
`prompts/reviewer.md` and returns `(None, None)` at `SELF_VERIFY`.
`BreakerConfig.max_rounds_per_generation` defaults to 3 and
`max_generations` to 3 (`config.py:378–379`); `ExecutionConfig.max_rewrites`
is 2 (`config.py:405`).

**Merge.** `MergeLadder._merge` (`merge_ladder.py:121`) reads
`changed_paths(workspace, base_ref)` before `deps.merge_group`, handles
`MergeConflict` and `PreflightFailure` (untracked ladder, flake rerun,
escalation, rewrite) and ends in `_register_code_artifact(commit, paths)`
which writes an `ArtifactEntry(schema="CoderReport", summary=_last_report.summary)`. Nothing in the code ladder restricts which paths may change. The run
recipe's restriction is `_RunExecution._porcelain_offenders`
(`run_executor.py:610`): `git status --porcelain` lines not matching
`commit_paths` globs fail the unit.

**Run Children.** `_RunExecution` (`run_executor.py:117`) owns per-attempt
dirs under `<group_dir>/run/attempt-<k>/` (`<n>.state.json`, `.out`, `.err`,
`.exit`, `.result.json`, `settled.json`), `_run_command` (`:430`) launches
through `run_child.launch(cmd, cwd, exit_path, out_path, err_path, preexec_fn, env)`
with `_confinement_preexec` (worker profile + data dirs + `allow_write` +
attempt dir) and `_child_env` (cache overlay, venv scrub), `_await_exit`
polls with `_relabel_heartbeat`, and re-adopts via `RunChildState` +
`is_adoptable` (ADR 0011). `_read_measurements` (`:598`) keeps top-level
scalar keys of the declared JSON; `_commit_and_merge` (`:623`) skips the
merge when zero commits ahead of `integration_branch(run_id)`;
`_register_artifact` (`:702`) writes the `RunRecord` entry;
`_triage_and_fail` (`:768`) runs the one-shot `deps.triage` and raises
`GroupFailure`. `RunArgs` (`recipes/run.py:31`) is `commands[{cmd, wall_clock_min, cwd}]`, `outputs`, `measurements`, `commit_paths`,
`allow_write` (with the `~/.claude` parse-time guard). `price_run` sums
`wall_clock_min` and charges `TRIAGE_TOKEN_ALLOWANCE = 20_000` tokens.

**Sessions and tools.** `SessionRunner` (`sessions.py:387`) is built once per
run in `cli.py:1755` with `allowed_tools=session.allowed_tools` and
`model=session.model`; `_call` (`sessions.py:669`) adds
`--allowedTools` from `self.allowed_tools + worktree_path_rules(...)` and
`--disallowedTools` from `effective_disallowed_tools()` on *every* call
(`start_worker`, `start_fork`, `resume`), and takes a per-call `model`
override already. `DEFAULT_ALLOWED_TOOLS` (`config.py:171`) is
`_BASE_ALLOWED_TOOLS` (no `WebSearch`, no `WebFetch`, no Perplexity rule)
expanded by `with_path_prefixes`. Bash rules are name-on-PATH anchored
(`PATH_PREFIXES`, `config.py:92`); `smart-mcps-perplexity` is on PATH and
`PERPLEXITY_API_KEY` is set on this machine. Landlock (`confinement.py`)
confines writes only — reads and network are free — so a research worker
needs no confinement change (origin brainstorm, verified again).
`SessionRole` (`model.py:127`) is `base | coder | reviewer | runner`; the
Observatory pins `export type SessionRole` at `ui/src/types.ts:61`;
`ExportSession.role` is a plain `str` (`export.py:72`).

**Reports.** `parse_report(text, model_cls)` and `nudge_until_report(runner, result, model_cls, cwd, verification_ids)`
(`sessions.py:1108`, `:1141`) are generic over the pydantic model.
`CoderReport` (`model.py:299`) carries `status`, `summary`, `question`,
`denied_command`, `denial_error`, `denial_source`, `verification_results`,
`surprises`; the loop reads all of them. `Surprise.kind` (`model.py:234`) is
the Literal `interface_mismatch | missing_dependency | merge_conflict | other | informational`; `SurpriseBoard.mark` (`surprises.py:107`) validates
`affected_groups` against the run's group and task ids and resolves a task
id to its owning group; `pending_for`/`consume` feed
`_handle_pending_surprises` at group start, which calls
`EscalationHandlers._rewrite` (`escalating.py:237`) — that spends one of
`max_rewrites`, consumes the board, calls `deps.rewrite_spec(group, surprises)`
(`cli._rewrite_provider`, `cli.py:2870`: skeleton `{tasks, files, previous_spec, rewrite_context, operator_decisions}` through
`prompts/rewrite_speccer.md`, recorded by `JsonlCallRecorder`) and persists
`spec-gen<N>.json`.

**Prompt assembly.** `render_coder_prompt(run_id, group, decisions)`
(`prompting.py:151`) substitutes `identity_block`, `group_name`,
`verification`, `report_contract` (`prompts/report_contract.md`) and
`decisions` into `prompts/coder.md`; `load_template(name)` reads
`orchestrator/prompts/<name>.md`. `render_artifact_inputs_block`
(`artifacts.py:105`) folds non-`code` upstream entries (summary +
measurements, capped at 8,000 chars) into a downstream prompt;
`_upstream_artifact_entries` (`generation.py:154`) picks direct
`Group.dependencies` whose recipe is not `code`. The base context
(`grouping/base_context.py:27`) opens with `prompts/worker_ground_rules.md`,
which forbids backgrounded work and tells coders about the 10-minute tool
cap and `.coder-scratch/`.

**Pricing.** `estimator.node_work` dispatches on `metadata["recipe"]`
(`estimator.py:55–66`); `price_plan` (`:180–215`) builds non-`code` metadata
as only `{recipe, recipe_args, triage_tokens}` — no `files`/`source_bytes`
— so a recipe that wants the code arithmetic plus something cannot get it
today. `TaskPrice` carries `recipe`, `wall_clock_s`, `priced_by_default`.
`Group` (`model.py:98`) has `recipe`, `recipe_args: dict | None`,
`estimated_wall_clock_s`. `plan_reader._parsed_recipe` validates
`recipe_args` with the recipe's `args_model`; a non-`code` unit with a `slice`
is a hard error; `pipeline._check_run_outputs` (`:439`) checks `run` outputs
against data dirs. Non-`code` units are fixed singletons in the partitioner
(`partition.py:388`); the scheduler skips stranded-work resolve for any
non-`code` recipe (`scheduler.py:766`) — so a FAILED `optimize` or
`research` group is `retry`-only, as `run` is.

**Report and bundle.** `report/facts.py:711–721` marks a non-`code` group's
verification items `recipe` when no coder ran them; `report/markdown.py:60–107`
renders `runner` sessions as "unknown (recipe child)". `export.py:287`
carries `artifact_manifest` as the one optional top-level key;
`docs/run-bundle-contract.md` states new optional keys and enum values do
not bump `schema_version` (2). Recipe children are never costed
(memory `run-recipe-nested-llm-cost-unknown`).

**Tests that pin behaviour.** `tests/test_review_loop.py` (the code loop
against `tests/fake_claude.py`), `tests/test_run_executor.py` (real Landlock,
real git, a scripted `deps.triage`), `tests/test_run_child.py`,
`tests/test_recipe_registry.py`, `tests/test_recipe_partition.py`,
`tests/test_artifact_manifest.py`, `tests/test_surprise_board.py`,
`tests/test_sessions.py`, `tests/test_export.py`, `tests/test_report_facts.py`,
`tests/test_report_markdown.py`, `tests/test_estimator.py`,
`tests/test_plan_reader.py`. The live tier is `-m llm` (`addopts = -m "not llm"`), e.g. `tests/test_run_recipe_live.py`. The full suite reads 2062
passed on the branch this plan starts from.

**Tooling.** `smart-mcps-orchestrate` on PATH is a non-editable uv-tool copy;
every command below is `uv run smart-mcps-orchestrate …`. The plugin is at
`0.20.0` (`.claude-plugin/plugin.json`); the last ADR is 0011.

## Decisions

- **One worker loop, parameterised by the registry.** `GenerationLoop` reads
  the group's recipe entry for its worker prompt template, report contract,
  reviewer and handoff templates, extra allowed tools and merge policy;
  `code` supplies exactly today's values so its prompts and reports are
  byte-identical (R4 of the origin brainstorm still holds). `research` is
  then a registry entry plus prompts plus a contract, and the next document
  recipe costs the same. Rejected: a separate `research_executor.py`
  (a second copy of a 250-line loop to keep in step with liveness, breaker,
  re-entry and escalation); a "profile" object outside the registry (two
  places to enumerate recipes).
- **New tests go in new files.** `tests/test_review_loop.py` (96 KB) and `tests/test_sessions.py` (50 KB) are never edited by this plan — a unit that listed either priced over the coder budget on its own — so U4, U5 and U15 add sibling test files and keep the old ones as unedited regression guards.
- **Two zero-behaviour-change extractions lead the plan.** U1 splits
  `_run_generation_body` into overridable steps; U2 lifts Run Child
  launch/poll/adopt into `CommandRunner`. Acceptance for both is the full
  suite green with no test file edited. Rejected: a separate plan first (one
  more run cycle for two small units); no extraction (optimize copies the
  loop body).
- **A report base model.** `WorkerReport` carries the fields the loop reads
  (`status`, `summary`, `question`, `denied_*`, `verification_results`,
  `surprises`); `CoderReport` and `FindingsReport` extend it. The loop stays
  generic over `recipe.contract` and `nudge_until_report` is untouched.
- **Merge policy is declared commit globs; `None` is the code ladder.**
  `UnitRecipe.merge` becomes a callable `args -> MergePolicy(commit_globs)`;
  `code` returns `None` (unrestricted), `run` returns its `commit_paths`,
  `research` returns its declared output path. `MergeLadder._merge` (U15) enforces
  a non-`None` policy with the same porcelain check the run executor uses
  and fails the merge naming the paths (R7, R25). Rejected: keeping two
  merge code paths (`code_ladder` / `run_commit_paths`) and adding a third.
- **Per-recipe tools are extra rules on every call, never a second runner.**
  `SessionRunner` calls take `extra_allowed_tools`; the loop passes the
  recipe's tuple on `start_worker`, `start_fork` and `resume` alike. The
  `research` entry adds `Bash(smart-mcps-perplexity *)`, `WebSearch`,
  `WebFetch`. NotebookLM is not added. Rejected: widening
  `DEFAULT_ALLOWED_TOOLS` for everyone (the exact drift seed B warned about).
- **A Spec Refinement rides the surprise board and spends no rewrite.** A
  `FindingsReport.spec_refinement {target_task, refinement}` is validated by
  the loop against the groups that depend on the research group and marked
  as a `Surprise(kind="spec_refinement", affected_groups=[target_task])`;
  the consumer's pre-launch `_handle_pending_surprises` rewrites its spec
  through the existing speccer with the refinement as binding context, and
  `_rewrite` does not count a refinement-only rewrite against
  `max_rewrites`. Provenance is the rewrite context line itself, which the
  speccer call records. Rejected: a new rewrite path (duplicates
  `_rewrite`); letting the research unit edit `groups.json` (immutable
  grouper output).
- **`evaluate` subclasses the run executor; `optimize` subclasses the code
  loop.** `EvaluateExecution(_RunExecution)` adds a harness hash check and
  optional smoke command before the first command and a KPI extraction after
  measurements. `OptimizeLoop(GenerationLoop)` overrides the settle step:
  after a `completed` report it checks the mutable region, launches the
  evaluate command through `CommandRunner` in the group's own worktree at the
  candidate commit, decides, appends the ledger, and either advances the
  Champion or resets the worktree. Rejected: an `optimize` group that
  schedules a separate `evaluate` group per round (a unit creating units,
  ADR 0010).
- **The harness baseline hash is captured at loop start.** The evaluate
  child hashes the KPI Contract's `harness_paths` in the worktree at the
  first evaluation (the integration tip the group launched from) and
  records it; every later scoring run compares against it and a mismatch is
  a Work Failure naming the path (R12). Rejected: a cross-group registry of
  "the hash when the harness unit merged" (state between groups that a
  resumed run must also carry).
- **Keep-or-revert is a pure function.** `execution/kpi.py` holds
  `KpiContract`, `noise_floor(deltas)` (MAD over the last five), `decide(...)`
  returning `keep | promising | inconclusive | discard | crash`, and the
  `Ledger` file model; both executors import it, and it is tested with no
  git, no subprocess and no LLM. (→ ADR 0012)
- **The optimize round cap is the evaluation cap.** `max_rounds_per_generation`
  (3) would retire an optimizer after three candidates, so `OptimizeLoop`
  bounds rounds by `recipe_args.evaluations` instead; the context-token
  breaker still retires into a handoff generation that carries the ledger,
  and `max_generations` still applies. Confirmation runs count against the
  evaluation cap; crashes count against rounds only. Patience (4) and the
  consecutive-revert cap (3) live in `[recipes.optimize]` and raise
  `CAPS_EXHAUSTED`-style escalations with the ledger attached rather than
  failing (R19).
- **The cheat reviewer is a reviewer session with a different prompt, only
  on `promising`.** It runs regardless of `ReviewIntensity` (the one
  documented exception, R22) and its `changes_required` is a `discard` with
  the notes in the ledger. Rejected: a reviewer on every candidate (twenty
  reviewer rounds for one expected hit).
- **Ledger location and export.** `<group_dir>/ledger.json` (append-only,
  atomic writes), rendered into every optimize prompt as a capped table, and
  exported on the Run Bundle as an optional `ledger` key on the group;
  `schema_version` stays 2. A zero-hit loop registers the ledger as the
  group's Artifact Manifest entry (`schema="Ledger"`).
- **Session roles.** `SessionRole.RESEARCHER` for research workers; evaluate
  children keep `RUNNER`; optimize sessions are `CODER`/`REVIEWER`. The
  registry entry names the worker role so `_record` and
  `session_display_name` need no recipe branches.
- **Prompts stay in the plugin; docs carry the harness contract.** No
  per-project override. `docs/orchestrator-kpi-harness.md` is the contract
  a harness unit implements (measurements JSON keys, held-out inputs, frozen
  inputs, judge-script patterns lifted from Infinity Skills'
  `summary_prompt_tuning`), so Run 2's harness unit and any judge script have
  a spec to cite.
- **Live proof ships here.** One `-m llm` file drives a research group with a
  real Perplexity call into a code group, and an optimize loop on a scratch
  repo with a deterministic KPI script; both are driver-run items.

## Units

### U1. round-hooks — split the code loop's round body into overridable steps, zero behaviour change

- **Summary**: `GenerationLoop._run_generation_body` is split into `_first_round`, `_collect_report`, `_settle_round` and `_next_round_prompt` steps so a subclass can change what happens after a completed report without copying the loop; every test passes unedited.
- **Goal**: `generation.py:231–475` becomes a short driver calling four methods: `_first_round()` (prompt build + launch or re-entry, returns the first `RoundResult`), `_collect_report(result)` (nudge to a report, `needs_input` handling), `_settle_round(report, report_path, rounds)` (denial, non-completed, verification gate, review, approve→merge; returns `merged: bool | None` where `None` means "continue with the next round"), and `_next_round_prompt(verdict, verdict_path)` (the revision prompt). Behaviour, log lines, heartbeat phases, artifact names and exception flow are identical. `host.py`'s `ExecutionHost` protocol lists the new methods.
- **Recipe**: —
- **Files**: `orchestrator/execution/generation.py`, `orchestrator/execution/host.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: `round-hooks` / —
- **Verification**:
  - Run: `uv run pytest tests/test_review_loop.py tests/test_review_recovery_log.py tests/test_review_scratch.py tests/test_e2e_stub.py -q` Pass: every test passes and `git diff --stat -- tests/` is empty.
  - Run: `uv run pytest -q` Pass: the full suite passes with the same count as the launch baseline (no test skipped or removed).
  - The four step methods exist on `GenerationLoop` and `_run_generation_body` calls each of them exactly once per round path; `grep -n "def _first_round\|def _collect_report\|def _settle_round\|def _next_round_prompt" orchestrator/execution/generation.py` lists all four.
  - Run: `uv run pytest tests/test_e2e_stub.py -q -k "two_groups or e2e"` Pass: the stub end-to-end run's `run.log` carries the same round lines (`round N: started`/`ended (...)`) as before the split, compared by the test's existing assertions.

### U2. command-runner — lift Run Child launch, poll and re-adoption out of the run executor, zero behaviour change

- **Summary**: A reusable `CommandRunner` in `execution/command_runner.py` owns launching a confined detached command into an attempt directory, polling it under a wall-clock cap, re-adopting it after a restart and returning a `CommandResult`; `_RunExecution` delegates to it and every run-recipe test passes unedited.
- **Goal**: `CommandRunner(paths, gid, heartbeat, config)` exposes `run(attempt_dir, n, command, cwd, *, preexec_fn, env) -> CommandResult` (raising `TimedOut` / `CommandDied` as today), `existing_attempts`, `write_result`, `write_settled`, and the state/exit/out/err file layout documented in `run_executor.py`'s module docstring, moved verbatim. `_RunExecution._run_command`, `_await_exit`, `_relabel_heartbeat`, `_discard_cancelled_child` become thin delegations. `_confinement_preexec` and `_child_env` stay on the executor (they know recipe args) and are passed in.
- **Recipe**: —
- **Files**: `orchestrator/execution/run_executor.py`, `orchestrator/execution/command_runner.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: `command-runner` / —
- **Verification**:
  - Run: `uv run pytest tests/test_run_executor.py tests/test_run_child.py tests/test_run_recipe_model.py -q` Pass: all pass under real Landlock with `git diff --stat -- tests/` empty.
  - Run: `uv run pytest tests/test_run_executor.py -q -k "adopt or readopt or resume"` Pass: the re-adoption tests (a real `sleep` child surviving a simulated restart) pass through `CommandRunner`.
  - `python -c "from orchestrator.execution.command_runner import CommandRunner; print(CommandRunner.run.__doc__)"` prints a docstring naming the attempt-dir file layout.
  - Run: `uv run pytest -q` Pass: full suite green, same count as the baseline.

### U3. recipe-entry — the registry entry carries worker prompt, extra tools, session role, contract base and a declared-globs merge policy

- **Summary**: `UnitRecipe` gains `worker_prompt`, `extra_allowed_tools`, `worker_role` and a callable `merge` returning `MergePolicy(commit_globs | None)`; `WorkerReport` is the report base `CoderReport` extends; `SessionRole.RESEARCHER` exists end to end; non-`code` pricing receives the full file metadata.
- **Goal**: In `orchestrator/recipes/registry.py`: `MergePolicy` (frozen dataclass, `commit_globs: tuple[str, ...] | None`), `UnitRecipe.worker_prompt: str` (template name, `code` → `"coder"`), `extra_allowed_tools: tuple[str, ...]` (default `()`), `worker_role: SessionRole` (`code` → `CODER`, `run` → `RUNNER`), `merge: Callable[[BaseModel | None], MergePolicy]` replacing the Literal (`code` → `MergePolicy(None)`, `run` → `MergePolicy(tuple(args.commit_paths))`). In `orchestrator/model.py`: `class WorkerReport(BaseModel)` with the fields the loop reads (`status`, `summary`, `question`, `denied_command`, `denial_error`, `denial_source`, `verification_results`, `surprises`) and the existing validators; `CoderReport(WorkerReport)` keeps its schema byte-identical (`CoderReport.model_json_schema()` unchanged); `SessionRole.RESEARCHER = "researcher"`. In `ui/src/types.ts`: the `SessionRole` union gains `"researcher"`. In `orchestrator/grouping/estimator.py`: `price_plan` and `node_work` pass `files`, `prospective_files`, `source_bytes` and `size_hints` in the metadata handed to *every* recipe's `price`, so a recipe can add the code arithmetic to its own figure; `code` and `run` prices are unchanged to the token.
- **Recipe**: —
- **Files**: `orchestrator/recipes/registry.py`, `orchestrator/recipes/code.py`, `orchestrator/recipes/run.py`, `orchestrator/model.py`, `orchestrator/grouping/estimator.py`, `ui/src/types.ts`, `tests/test_recipe_registry.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: `recipe-entry-v2` / —
- **Verification**:
  - Run: `uv run pytest tests/test_recipe_registry.py -q` Pass: a test asserts every registered entry has a `worker_prompt` template file under `orchestrator/prompts/`, a `worker_role` in `SessionRole`, and `merge(args)` returns a `MergePolicy`; `code` returns `commit_globs is None`, `run` returns its `commit_paths`.
  - Run: `uv run python -c "from orchestrator.model import CoderReport; import json; print(json.dumps(CoderReport.model_json_schema(), sort_keys=True))" > .coder-scratch/schema.json` Pass: identical to the same command's output on the launch commit (`git stash`-free: compare against `git show <launch>:orchestrator/model.py` via a scratch venv import, or assert the field set and required list in a test).
  - Run: `uv run smart-mcps-orchestrate group docs/plans/2026-09-24-001-feat-unit-recipes-run-plan.md --price` Pass: every `code` task's `node_work` and the `run` task's tokens and wall clock equal the figures on the launch commit (recorded in the test as fixture numbers from `tests/test_estimator.py`).
  - Run: `cd ui && npx tsc --noEmit` Pass: exits 0 with `"researcher"` in the `SessionRole` union.
  - Run: `uv run pytest tests/test_estimator.py tests/test_cli_price.py tests/test_export.py -q` Pass: all green.

### U4. recipe-allowlist — every session call can add a recipe's extra allowed tools

- **Summary**: `SessionRunner.start_worker`, `start_fork` and `resume` accept `extra_allowed_tools`, appended to `--allowedTools` for that call only, and the code loop passes the group's recipe tuple on every coder and reviewer call.
- **Goal**: `_call` takes `extra_allowed: Sequence[str] = ()` and appends it (de-duplicated, before `worktree_path_rules`) when `self.allowed_tools` is set — and *also* when it is not, so a recipe's tools are honoured under a config with no allowlist. `GenerationLoop` and `ReviewerRound` pass `get_recipe(self.group.recipe).extra_allowed_tools` on `_launch_call`, `resume` and the reviewer's calls. With `code` (empty tuple) the argv is byte-identical to today.
- **Recipe**: —
- **Files**: `orchestrator/execution/sessions.py`, `orchestrator/execution/generation.py`, `orchestrator/execution/reviewer.py`, `tests/test_recipe_allowlist.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: U1, U3
- **Slice**: —
- **Implements / Consumes**: — / `round-hooks`, `recipe-entry-v2`
- **Verification**:
  - Run: `uv run pytest tests/test_recipe_allowlist.py tests/test_sessions.py -q` Pass: `tests/test_sessions.py` passes unedited, and a new test captures the argv of `start_worker(..., extra_allowed_tools=("WebSearch",))` and asserts `WebSearch` appears in `--allowedTools` exactly once, and that a call with `()` produces the same argv as before.
  - Run: `uv run pytest tests/test_review_loop.py -q` Pass: the loop's fake-claude tests pass; one new test asserts the coder launch argv for a group whose recipe declares extra tools carries them.
  - Run: `uv run smart-mcps-orchestrate --help` Pass: exits 0 (import sanity of the changed modules).

### U5. recipe-loop — the code loop takes prompt, contract, reviewer, handoff, role and merge policy from the registry

- **Summary**: `GenerationLoop` and `ReviewerRound` read the group's registry entry for the worker prompt template, report contract class, reviewer and handoff templates and worker session role; `code` produces byte-identical prompts, reports and session records.
- **Goal**: `render_worker_prompt(template, run_id, group, ...)` generalises `render_coder_prompt` (which stays as a thin alias); `_first_round` builds the prompt from `recipe.worker_prompt`; `_collect_report` nudges toward `recipe.contract`; `_record(recipe.worker_role, sid)`; `_review_round` renders `recipe.reviewer_prompt` (skipped entirely when it is `None`, as `run` declares); `_prepare_handoff` renders `recipe.handoff_prompt`. `ReviewDeps` gains nothing; the registry is imported lazily inside the loop. New tests live in `tests/test_recipe_loop.py`; `tests/test_review_loop.py` is not edited.
- **Recipe**: —
- **Files**: `orchestrator/execution/generation.py`, `orchestrator/execution/reviewer.py`, `orchestrator/execution/prompting.py`, `orchestrator/execution/review.py`, `orchestrator/recipes/registry.py`, `tests/test_recipe_loop.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U1, U3
- **Slice**: —
- **Implements / Consumes**: `recipe-loop` / `round-hooks`, `recipe-entry-v2`
- **Verification**:
  - Run: `uv run pytest tests/test_review_loop.py tests/test_recipe_loop.py -q` Pass: every existing test passes unedited; a new test renders a `code` group's first prompt through the registry path and asserts it equals `render_coder_prompt(...)` byte for byte, and a stub recipe entry with `worker_prompt="reviewer"` and `worker_role=RESEARCHER` produces a first prompt from that template and a session recorded with role `researcher`.
  - Run: `uv run pytest tests/test_e2e_stub.py -q` Pass: the stub end-to-end run merges its `code` groups and the Artifact Manifest entries still read `schema: "CoderReport"`.
  - Run: `uv run pytest -q` Pass: full suite green.

### U6. research-recipe — the `research` Unit Recipe: args, findings contract, prompts, pricing and registration

- **Summary**: `recipe: research` registers a worker session that grounds a declared question in codegraph, queries Perplexity with the web tools as a recorded fallback, and commits a Findings Artifact at its declared `docs/research/` path whose schema requires a source on every finding.
- **Goal**: `orchestrator/recipes/research.py`: `ResearchArgs(question: str, output: str, focus_paths: list[str] = [], size: small|medium|large = medium)` with `output` validated to start with `docs/research/` and end in `.md`; `Finding(claim, sources: list[str] (min 1, each a URL or a repo path), confidence: low|medium|high, freshness: str | None)`; `FindingsReport(WorkerReport)` adding `findings: list[Finding]` (min 1 when `status == "completed"`), `provider_fallback: bool = False`, `spec_refinement: SpecRefinement | None` (`target_task: str`, `refinement: str`, max 2,000 chars); summary ≤ 2,000 chars; `price_research` = size class tokens (`small` 30k / `medium` 60k / `large` 120k coder tokens, `defaulted=True` when `size` was not declared); `RESEARCH_RECIPE` with `worker_prompt="research"`, `reviewer_prompt="research_reviewer"`, `handoff_prompt="research_handoff"`, `worker_role=RESEARCHER`, `extra_allowed_tools=("Bash(smart-mcps-perplexity *)", "WebSearch", "WebFetch")`, `merge=lambda a: MergePolicy((a.output,))`, `executor="orchestrator.execution.review:make_executor"`. Prompts *(new)*: `orchestrator/prompts/research.md` (derived from `agents/perplexity-explorer.md`'s four steps: ground in codegraph, write the brief, `smart-mcps-perplexity ask|reason --file`, verify; write the artifact at `$output`; fall back to `WebSearch`/`WebFetch` only when the CLI fails and then set `provider_fallback`; never spawn `claude`; the report contract block for `FindingsReport`), `research_reviewer.md` (open every cited source or path, judge support, check the refinement follows from the findings), `research_handoff.md` (found / exhausted / open). The loop logs `group <gid>: research provider fallback (WebSearch/WebFetch)` and prefixes the manifest summary with `[fallback]` when `provider_fallback` is true (R3).
- **Recipe**: —
- **Files**: `orchestrator/recipes/research.py` *(new, large)*, `orchestrator/recipes/registry.py`, `orchestrator/prompts/research.md` *(new, medium)*, `orchestrator/prompts/research_reviewer.md` *(new, small)*, `orchestrator/prompts/research_handoff.md` *(new, small)*, `orchestrator/execution/generation.py`, `tests/test_research_recipe.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U4, U5, U15
- **Slice**: —
- **Implements / Consumes**: `findings-report` / `recipe-loop`, `recipe-entry-v2`, `merge-policy`
- **Verification**:
  - Run: `uv run pytest tests/test_research_recipe.py -q` Pass: `FindingsReport` rejects a finding with no source naming `findings.0.sources`; rejects a `completed` report with zero findings; accepts a report with a refinement; `ResearchArgs` rejects `output: notes/x.md`; `price_research` returns 60k with `defaulted=True` for no size.
  - Run: `uv run pytest tests/test_executor_dispatch.py tests/test_recipe_registry.py -q` Pass: `research` resolves to the code executor and its three prompt templates load.
  - Run: `uv run pytest tests/test_review_loop.py -q -k research` Pass: with `fake_claude.py` scripted to emit a `FindingsReport`, a `research` group's first prompt contains the declared question and `smart-mcps-perplexity`, the session is recorded with role `researcher`, the merge registers `schema: "FindingsReport"` with `paths == [output]`, and a scripted `provider_fallback: true` produces the fallback log line and the `[fallback]` summary prefix.
  - Run: `smart-mcps-perplexity ask "what is Landlock LSM" --context-size low` Pass: exits 0 and prints an answer (the real CLI the prompt names is installed and keyed on this machine; report `skipped` with the error if the key is absent).

### U7. spec-refinement — a Findings Artifact refines its declared consumer's spec through the surprise board, without spending a rewrite

- **Summary**: `Surprise.kind` gains `spec_refinement`; after a `research` report is approved the loop validates `spec_refinement.target_task` is a task of a group that depends on this group, marks the surprise for it, and the consumer's pre-launch rewrite folds the refinement in as binding context without counting against `max_rewrites`.
- **Goal**: `model.py`: `Surprise.kind` Literal adds `"spec_refinement"`. `generation.py` (`_settle_round` for a recipe whose contract has `spec_refinement`): a target that is not a task of a direct or transitive downstream group (from `deps.groups_by_id` dependencies) is a contract violation — the coder is nudged with `render_coder_nudge_contract` naming the target and the allowed tasks, and the round does not settle; a valid one becomes `Surprise(kind="spec_refinement", description=f"[from {gid} artifact {gid}] {refinement}", affected_groups=[target_task])` spread through `_spread`. `surprises.py`: `mark` accepts the kind (task id resolution unchanged). `escalating.py`: `_rewrite` receives `counted: bool` — the pre-launch call site passes `counted=False` when every consumed surprise is a `spec_refinement`, so `self.rewrites` is not incremented and the log line says `(spec refinement, not counted)`. `prompts/rewrite_speccer.md`: a `[spec_refinement]` context line is binding — the rewritten spec must incorporate it and say so.
- **Recipe**: —
- **Files**: `orchestrator/model.py`, `orchestrator/execution/surprises.py`, `orchestrator/execution/escalating.py`, `orchestrator/execution/generation.py`, `orchestrator/prompts/rewrite_speccer.md`, `tests/test_surprise_board.py`, `tests/test_spec_refinement.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U6
- **Slice**: —
- **Implements / Consumes**: — / `findings-report`
- **Verification**:
  - Run: `uv run pytest tests/test_spec_refinement.py -q` Pass: a research group `g1` with downstream `g2` (tasks `u2-…`) and unrelated `g3`: a report targeting `u2-…` marks one `spec_refinement` surprise for `g2`; a report targeting `u3-…` is nudged with a contract error naming `u3-…` and the allowed tasks, and marks nothing.
  - Run: `uv run pytest tests/test_spec_refinement.py -q -k rewrite_not_counted` Pass: with `max_rewrites=0`, a group whose only pending surprise is a `spec_refinement` still launches after a rewrite, `rewrites` stays 0, and `spec-gen1.json` exists carrying the refinement text; a pending `other` surprise under the same config exhausts the cap as today.
  - Run: `uv run pytest tests/test_surprise_board.py tests/test_informational_surprise.py tests/test_escalation.py -q` Pass: green.
  - Run (driver): `uv run smart-mcps-orchestrate export r20260924-134934` Pass: exits 0 and `.orchestrator/runs/r20260924-134934/ingest.json` still validates (the kind Literal is additive; an old run's surprises load).

### U8. evaluate-recipe — the `evaluate` Unit Recipe: `run` plus a KPI Contract, a hash-checked harness and a smoke gate

- **Summary**: `recipe: evaluate` runs declared commands exactly as `run` does and additionally checks the harness paths' hash before scoring, runs an optional smoke command first, and reads the KPI, guards and threshold off the measurements JSON into an `EvaluationRecord` that never gates the unit.
- **Goal**: `orchestrator/recipes/evaluate.py`: `EvaluateArgs(RunArgs)` adding `kpi: KpiContract` (from `execution/kpi.py`, U9) with `measurements` required; `EvaluationRecord(RunRecord)` adding `kpi_key`, `kpi_value: float | None`, `guards: dict[str, float]`, `harness_hash: str`, `threshold_cleared: bool | None`; `price_evaluate = price_run`; `EVALUATE_RECIPE` (`worker_role=RUNNER`, `reviewer_prompt=None`, `merge` = run's, `executor="orchestrator.execution.evaluate_executor:make_executor"`). `orchestrator/execution/evaluate_executor.py`: `EvaluateExecution(_RunExecution)` — before command 1, `harness_hash(workspace, kpi.harness_paths)` (sha256 over sorted path+content, symlinks resolved) is computed and compared with the hash recorded in `<group_dir>/eval/harness.sha256` when that file exists (first attempt writes it); a mismatch is `_triage_and_fail`-free: an immediate `GroupFailure` naming the first differing path. If `kpi.smoke` is set it runs through `CommandRunner` with a 5-minute cap; a non-zero exit is a Work Failure `smoke failed` and no measurement is recorded. After `_read_measurements`, a missing `kpi.key` after a zero exit is a Work Failure naming the key (R11); guards are read the same way; `threshold_cleared` compares `kpi_value` to `kpi.good_enough` when declared. The manifest entry has `schema="EvaluationRecord"` and its measurements carry the KPI and guards.
- **Recipe**: —
- **Files**: `orchestrator/recipes/evaluate.py` *(new, medium)*, `orchestrator/execution/evaluate_executor.py` *(new, medium)*, `orchestrator/execution/run_executor.py`, `orchestrator/recipes/registry.py`, `tests/test_evaluate_recipe.py` *(new, large)*
- **Symbols**: —
- **Depends-on**: U2, U3, U9
- **Slice**: kpi
- **Implements / Consumes**: `evaluation-record` / `command-runner`, `recipe-entry-v2`, `kpi-ledger`
- **Verification**:
  - Run: `uv run pytest tests/test_evaluate_recipe.py -q` Pass: on a real git repo with a real shell script harness (`scripts/score.sh` writing `{"score": 3, "guard": 1}`): an evaluate group completes with `kpi_value == 3.0` and `harness_hash` recorded; editing `scripts/score.sh` between attempts fails the second attempt naming `scripts/score.sh`; a harness whose JSON lacks the KPI key fails naming `score`; a failing smoke command fails with `smoke failed` and no `*.result.json` for command 1.
  - Run: `uv run pytest tests/test_run_executor.py -q` Pass: the `run` recipe's behaviour is unchanged (green, no test edited).
  - Run: `uv run pytest tests/test_executor_dispatch.py -q` Pass: `evaluate` resolves.
  - Run: `uv run smart-mcps-orchestrate group docs/orchestrator-task-map.md --no-spec` Pass: exits non-zero *only* because that file is not a plan — the command runs (import sanity); the real map check is U13's example.

### U9. keep-or-revert — the pure KPI contract, noise floor, decision function and Attempt Ledger

- **Summary**: `execution/kpi.py` defines `KpiContract`, `noise_floor`, `decide` (keep / promising / inconclusive / discard / crash) and the append-only `Ledger` file model with its prompt table renderer, tested without git, subprocess or LLM.
- **Goal**: `KpiContract(key: str, direction: "min" | "max", min_effect: float = 0.0, guards: list[Guard(key, direction, max_regression: float)] = [], harness_paths: list[str] (min 1), smoke: str | None = None, good_enough: float | None = None)`. `signed_delta(candidate, champion, direction)` so that positive is better. `noise_floor(deltas: Sequence[float]) -> float` = median absolute deviation over the last five (0.0 with fewer than two). `decide(delta, guard_deltas, floor, contract, *, crashed: bool) -> Outcome` per R17: `crash`; `discard` if `delta < 0` or any guard regresses past `max_regression`; `keep` if `delta >= max(min_effect, 2 × floor)` and `floor == 0`; `promising` if it clears and `floor > 0`; else `inconclusive`. `Attempt(round_no, candidate_commit, kpi_value, guard_values, delta, noise_floor, outcome, harness_hash, why, at)`; `Ledger` loads/appends `<group_dir>/ledger.json` atomically; `render_ledger_table(ledger, max_chars=6000)` renders newest-last rows and a `+N earlier rows — see ledger.json` line when capped; `Ledger.deltas()` for the floor; `Ledger.champion()`.
- **Recipe**: —
- **Files**: `orchestrator/execution/kpi.py` *(new, large)*, `tests/test_kpi_decision.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: kpi
- **Implements / Consumes**: `kpi-ledger` / —
- **Verification**:
  - Run: `uv run pytest tests/test_kpi_decision.py -q` Pass: table-driven cases cover every outcome; a deterministic history (all deltas equal) yields `floor == 0` and a delta of exactly `min_effect` is `keep`; a noisy history yields `promising` for a clearing delta; a guard regression past its bound is `discard` even with a large KPI gain; `direction: min` flips the sign.
  - Run: `uv run pytest tests/test_kpi_decision.py -q -k ledger` Pass: appending 30 attempts and re-loading round-trips byte-equal; `render_ledger_table` under `max_chars=500` ends with the `+N earlier rows` line and always includes the newest row.
  - Run: `uv run python -c "from orchestrator.execution.kpi import KpiContract; KpiContract(key='hat', direction='max', harness_paths=['scripts/eval.py'])"` Pass: constructs; the same with `harness_paths=[]` raises a validation error.
  - Run: `uv run python -c "from statistics import median; from orchestrator.execution.kpi import noise_floor; d=[0.1,0.3,-0.2,0.05,0.4,0.9,-0.3]; w=d[-5:]; m=median(w); ref=median(abs(x-m) for x in w); assert abs(noise_floor(d)-ref) < 1e-9, (noise_floor(d), ref); print(ref)"` Pass: prints the reference MAD computed independently from the stdlib over the last five values, and the assertion holds.

### U10. optimize-recipe — the `optimize` Unit Recipe: one candidate, one evaluation, one decision per round

- **Summary**: `recipe: optimize` runs the code loop with a settle step that checks the mutable region, scores each committed candidate with the evaluate command in the group's worktree, decides keep-or-revert from the ledger, runs the cheat reviewer on `promising` candidates before a confirmation run, and merges the Champion or registers the ledger when the loop ends.
- **Goal**: `orchestrator/recipes/optimize.py`: `OptimizeArgs(commands, measurements, kpi: KpiContract, evaluations: int (1–100), allow_write = [])` (the evaluate command set, reusing `RunCommand`); `OptimizeReport(CoderReport)` adding `candidate: str` (one line describing the candidate); `price_optimize` = the code arithmetic from the full metadata (U3) plus `evaluations × TRIAGE_TOKEN_ALLOWANCE / 4` tokens and `wall_clock_s = evaluations × Σ wall_clock_min × 60`; `OPTIMIZE_RECIPE` (`worker_prompt="optimize"`, `reviewer_prompt="optimize_reviewer"`, `handoff_prompt="optimize_handoff"`, `worker_role=CODER`, `merge=lambda a: MergePolicy(None)`, `executor="orchestrator.execution.optimize_executor:make_executor"`). `[recipes.optimize]` in `config.py`: `patience: int = 4`, `consecutive_reverts: int = 3`. `grouping/pipeline.py`: `_check_optimize_files(mappings)` beside `_check_run_outputs` — for every `optimize` mapping, any entry of `mapping.files` matching a `recipe_args.kpi.harness_paths` glob (or equal to it) is a `GrouperError` naming the task, the file and the glob (R12's parse-time half; `OptimizeArgs` cannot see the map's `files`, so the cross-field check lives here), called from `run_grouping` with the other recipe checks. `orchestrator/execution/optimize_executor.py`: `OptimizeLoop(GenerationLoop)` + the same mixins as `_GroupExecution`; on entry records `champion = HEAD` and the harness hash (first evaluation writes `<group_dir>/eval/harness.sha256`); `_settle_round` after a `completed` report: (1) mutable region — `git diff --name-only <champion>..HEAD` plus porcelain entries outside `group.files` or matching any `harness_paths` glob → outcome `discard` with the paths as `why`, no scoring; (2) otherwise run the evaluate commands through `CommandRunner` under `<group_dir>/eval/attempt-<round>/` with the run-child confinement (worker profile + data dirs + `allow_write` + attempt dir), hash-check first, smoke if declared (failure → `crash`); (3) read measurements → `decide`; (4) `promising` → the reviewer session with `optimize_reviewer` (regardless of intensity): `changes_required` → `discard` with the reviewer notes; approved → one confirmation evaluation, `keep` only if it clears again; (5) `keep` → `champion = HEAD`; every other outcome → `git reset --hard <champion>` and `git clean -fd` outside `.coder-scratch/`; (6) append the `Attempt`, log `round N: <outcome> (Δ=…, floor=…)`, heartbeat phase `evaluating candidate N/M`; (7) stop conditions — evaluations used ≥ `evaluations`, or `good_enough` cleared → end; patience or consecutive reverts reached → `_escalate(CAPS_EXHAUSTED, prompt with the ledger table)`; an answer continues, `None` ends the loop; (8) otherwise the next round resumes the coder with `optimize_round.md` (outcome, numbers, ledger table). Rounds are bounded by `evaluations`, not `max_rounds_per_generation`; the context breaker retires into `optimize_handoff.md` carrying the ledger table. Loop end: champion ≠ launch commit → `_merge()` (code ladder, Preflight) and the Artifact Manifest entry summary lists every keep; champion == launch → register `ArtifactEntry(schema="Ledger", paths=["ledger.json"], summary=<ruled-out list>)`, log `group <gid>: no improvement found — N candidates ruled out`, return `COMPLETED` (R20). Prompts *(new)*: `optimize.md` (the objective, the KPI and guards in words, the mutable files, the rule "one candidate per round, commit it, report `completed` with `candidate`; you never decide keep"), `optimize_round.md`, `optimize_reviewer.md` (not a repeat of any ledger row; no harness, seed or budget-line change; no special-casing of held-out inputs; verdict as today), `optimize_handoff.md`.
- **Recipe**: —
- **Files**: `orchestrator/recipes/optimize.py` *(new, large)*, `orchestrator/execution/optimize_executor.py` *(new, large)*, `orchestrator/prompts/optimize.md` *(new, medium)*, `orchestrator/prompts/optimize_round.md` *(new, small)*, `orchestrator/prompts/optimize_reviewer.md` *(new, small)*, `orchestrator/prompts/optimize_handoff.md` *(new, small)*, `orchestrator/recipes/registry.py`, `orchestrator/config.py`, `orchestrator/grouping/pipeline.py`, `tests/test_recipe_partition.py`, `tests/test_optimize_executor.py` *(new, large)*
- **Symbols**: —
- **Depends-on**: U2, U5, U8, U9
- **Slice**: —
- **Implements / Consumes**: `optimize-loop` / `round-hooks`, `recipe-loop`, `command-runner`, `kpi-ledger`, `evaluation-record`
- **Verification**:
  - Run: `uv run pytest tests/test_optimize_executor.py -q` Pass: on a real git repo with `scripts/score.sh` printing the number in `value.txt` as `{"score": N}` and `fake_claude.py` scripted to commit `value.txt` candidates `5, 9, 9, 2` against a champion of `5` with `direction: max`, `min_effect: 1`: outcomes read `inconclusive, keep, inconclusive, discard`, `ledger.json` has four rows, the worktree's `value.txt` reads `9` at the end, and the group merges a champion whose `value.txt` is `9`.
  - Run: `uv run pytest tests/test_optimize_executor.py -q -k "harness or mutable"` Pass: a candidate that edits `scripts/score.sh` is `discard` with the path in `why` and no `eval/attempt-*` dir is created; a candidate touching a file outside `group.files` is likewise discarded unscored; a harness edited outside the loop between evaluations fails the group naming the path.
  - Run: `uv run pytest tests/test_optimize_executor.py -q -k "zero_hit or patience"` Pass: candidates that never clear end `COMPLETED` with an `ArtifactEntry(schema="Ledger")` and no merge; with `patience=2` the third non-improving evaluation raises an escalation whose prompt contains the ledger table, and a `None` response ends the loop `COMPLETED`.
  - Run: `uv run pytest tests/test_optimize_executor.py -q -k promising` Pass: with a noisy scripted harness (floor > 0), a clearing candidate triggers the reviewer session with the `optimize_reviewer` prompt, then a second evaluation, and is `keep` only when both clear; a reviewer `changes_required` makes it `discard` with the notes in `why`.
  - Run: `uv run pytest tests/test_recipe_partition.py -q -k optimize_files` Pass: a task map whose `optimize` unit lists `scripts/score.sh` in `files` and in `kpi.harness_paths` fails `group` with a `GrouperError` naming the task, `scripts/score.sh` and the glob; the same map with `files: [value.txt]` groups.
  - Run: `uv run pytest tests/test_executor_dispatch.py tests/test_recipe_registry.py tests/test_review_loop.py -q` Pass: green.
  - Run: `bash -c 'echo 5 > /tmp/v && printf "{\"score\": %s}\n" "$(cat /tmp/v)"'` Pass: prints `{"score": 5}` — the shape of the harness the unit's tests script, run for real.

### U11. bundle-additive — the Run Bundle carries the ledger and the new roles as optional, additive fields

- **Summary**: `export` emits an optional `ledger` key per group and passes `researcher` sessions through, `schema_version` stays 2, and `docs/run-bundle-contract.md` documents both plus the `research`/`evaluate`/`optimize` recipe values and the `FindingsReport`/`EvaluationRecord`/`Ledger` schema names.
- **Goal**: `export.py`: `ExportGroup.ledger: list[dict] | None = None` read from `<group_dir>/ledger.json` when present, omitted from the JSON when `None` (same treatment as `artifact_manifest`); the artifact manifest entry passes `schema` through unchanged. The contract doc gains a `ledger` row, the three recipe values, the schema names, and the `researcher` role value in every table that lists roles. An old run exports byte-identically.
- **Recipe**: —
- **Files**: `orchestrator/execution/export.py`, `docs/run-bundle-contract.md`, `tests/test_export.py`
- **Symbols**: —
- **Depends-on**: U3, U9
- **Slice**: —
- **Implements / Consumes**: — / `recipe-entry-v2`, `kpi-ledger`
- **Verification**:
  - Run: `uv run pytest tests/test_export.py -q` Pass: a run dir with `groups/g1/ledger.json` exports `groups[0].ledger` as its rows; one without has no `ledger` key; `schema_version == 2`.
  - Run (driver): `uv run smart-mcps-orchestrate export r20260924-134934 && python -c "import json; d=json.load(open('.orchestrator/runs/r20260924-134934/ingest.json')); print(d['schema_version'], 'ledger' in d['groups'][0])"` Pass: prints `2 False`.
  - Run: `grep -c "researcher" docs/run-bundle-contract.md` Pass: ≥ 2 (the role tables).

### U12. report-recipes — the run report renders research, evaluate and optimize groups honestly

- **Summary**: The report facts and markdown treat a `research` group as a coder-like group with a Findings Artifact, an `evaluate` group like `run`, and an `optimize` group as a loop: a `## Optimization` block per optimize group lists every kept candidate with its delta and links `ledger.json`, and a zero-hit loop reads "no improvement found (N ruled out)", never "not landed".
- **Goal**: `report/facts.py`: `GroupFacts` gains `ledger_rows: int`, `keeps: list[{round, delta, candidate_commit}]`, `champion_moved: bool`; the `recipe` verification status covers `evaluate` and `optimize`; an `optimize` unit is landed when its group completed (merged or zero-hit). `report/markdown.py`: the block above; `researcher` sessions cost like coders; `runner` sessions of evaluate groups keep "unknown (recipe child)". The one-pager's Next steps template gets one line per keep (R21).
- **Recipe**: —
- **Files**: `orchestrator/report/facts.py`, `orchestrator/report/markdown.py`, `tests/test_report_facts.py`, `tests/test_report_markdown.py`
- **Symbols**: —
- **Depends-on**: U3, U9
- **Slice**: —
- **Implements / Consumes**: — / `recipe-entry-v2`, `kpi-ledger`
- **Verification**:
  - Run: `uv run pytest tests/test_report_facts.py tests/test_report_markdown.py -q` Pass: a fixture run with an optimize group holding a 4-row ledger with one keep renders `## Optimization` with that keep's delta and `ledger.json`; a zero-hit ledger renders "no improvement found (3 ruled out)" and the unit counts as landed; a research group's session with role `researcher` appears in the cost line.
  - Run (driver): `uv run smart-mcps-orchestrate report r20260924-134934 --format facts` Pass: exits 0 and the old run's facts are unchanged apart from the new empty fields (`keeps: []`, `ledger_rows: 0`).
  - Run: `uv run pytest tests/test_report_onepager.py tests/test_report_html.py -q` Pass: green.

### U13. docs-skills — the task-map contract, the harness contract, and the planning skills know the three recipes

- **Summary**: `docs/orchestrator-task-map.md` shows `research`, `evaluate` and `optimize` examples; `docs/orchestrator-kpi-harness.md` is the contract a harness unit implements, with the judge-script patterns from Infinity Skills; the plan, deepen and run skills say when to reach for each recipe, what a `Run (driver):` sweep must allow, and how a driver reads a ledger and answers a patience escalation; the plugin becomes 0.21.0.
- **Goal**: Task-map doc: a v2 example per recipe with every `recipe_args` field and the parse-time rules (research `output` under `docs/research/`; evaluate `measurements` required and `harness_paths` non-empty; optimize `files:` may not overlap `harness_paths`, checked at `group` by `pipeline._check_optimize_files` (U10), and the doc says so). Harness doc: the measurements JSON keys, held-out and frozen inputs (content-hashed with the harness, never a live codegraph index), the smoke command, and the judge-script section: pairwise position-swapped comparisons against the live baseline, a rubric with per-field reasons, disk-cached calls, a fixed instance set — each cited to `infinity-skills/src/infinity_skills/eval/summary_prompt_tuning.py` by name only. Skills: `orchestrator-plan/SKILL.md` gains a "research / evaluate / optimize" section beside the `run` one (harness unit first; one KPI per optimize unit; `evaluations` and wall clock plan-declared); `orchestrator-deepen/SKILL.md`'s sandbox sweep lists the research recipe's three extra rules; `orchestrator-run/SKILL.md` gains "driving an optimize group" (read `groups/<gid>/ledger.json`, what a patience escalation asks, that `retry` is the recovery for a FAILED optimize group). `plugin.json` version `0.21.0`.
- **Recipe**: —
- **Files**: `docs/orchestrator-task-map.md`, `docs/orchestrator-kpi-harness.md` *(new, medium)*, `skills/orchestrator-plan/SKILL.md`, `skills/orchestrator-deepen/SKILL.md`, `skills/orchestrator-run/SKILL.md`, `.claude-plugin/plugin.json`
- **Symbols**: —
- **Depends-on**: U6, U8, U10
- **Slice**: —
- **Implements / Consumes**: — / `findings-report`, `evaluation-record`, `optimize-loop`
- **Verification**:
  - Run: `uv run smart-mcps-orchestrate group docs/orchestrator-task-map.md --no-spec` is not applicable (not a plan); instead: `uv run python -c "import yaml,re,sys; t=open('docs/orchestrator-task-map.md').read(); blocks=re.findall(r'\x60\x60\x60yaml\n(# orchestrator-task-map v2\n.*?)\x60\x60\x60', t, re.S); [yaml.safe_load(b) for b in blocks]; print(len(blocks))"` Pass: prints ≥ 4 (the run example plus the three new ones parse as YAML).
  - Run: `uv run pytest tests/test_plan_reader.py -q -k recipe` Pass: green (the reader accepts the documented examples; U10/U8/U6 args models validate them — add one test per documented example that parses the doc's YAML through `parse_task_map` with the recipes enabled).
  - Run: `grep -n "evaluate\|optimize\|research" skills/orchestrator-plan/SKILL.md skills/orchestrator-deepen/SKILL.md skills/orchestrator-run/SKILL.md | wc -l` Pass: ≥ 12.
  - Run: `python -c "import json; print(json.load(open('.claude-plugin/plugin.json'))['version'])"` Pass: `0.21.0`.

### U14. live-tier — a real research group and a real optimize loop, driver-run

- **Summary**: `tests/test_recipes_live.py` (`-m llm`) drives a real `research` group with a real Perplexity call into a real `code` group, and a real `optimize` loop on a scratch repo with a deterministic KPI script for three evaluations, asserting the ledger, the champion merge and a harness-tamper discard through the real `claude` CLI and real Landlock.
- **Goal**: Modelled on `tests/test_run_recipe_live.py`: (a) a two-group grouping (`g1` research: question "what does Linux Landlock ABI 4 add", output `docs/research/live-probe.md`; `g2` code depending on it) run through `main([...])` with `[recipes] enabled = ["research"]`; asserts the artifact file exists with ≥ 1 finding and a source, the manifest entry has `schema: "FindingsReport"`, and `g2`'s transcript's first prompt carries the `## Upstream artifacts` block with `g1`; skipped when `PERPLEXITY_API_KEY` is unset. (b) a one-group optimize grouping on a scratch repo whose `scripts/score.sh` prints `{"score": <number in value.txt>}`, `files: [value.txt]`, `evaluations: 3`, `kpi: {key: score, direction: max, min_effect: 1, harness_paths: [scripts/score.sh]}`; the coder prompt says "raise the number"; asserts three ledger rows, ≥ 1 `keep`, the integration branch's `value.txt` greater than the launch value, and — in a second run seeded with a coder instruction to edit `scripts/score.sh` — a `discard` whose `why` names the script and no `eval/attempt-*` for that round.
- **Recipe**: —
- **Files**: `tests/test_recipes_live.py` *(new, large)*
- **Symbols**: —
- **Depends-on**: U7, U10, U11
- **Slice**: —
- **Implements / Consumes**: — / `findings-report`, `optimize-loop`
- **Verification**:
  - Run (driver): `uv run pytest -m llm tests/test_recipes_live.py -q -k research` Pass: 1 passed (or 1 skipped naming the missing key), and the scratch run's `docs/research/live-probe.md` cites at least one URL.
  - Run (driver): `uv run pytest -m llm tests/test_recipes_live.py -q -k optimize` Pass: 2 passed; the run log of the first shows three `round N:` outcome lines and one `merged into the integration branch`; the second shows `discard` naming `scripts/score.sh`.
  - Run: `uv run pytest tests/test_recipes_live.py -q` Pass: 0 selected under the default `-m "not llm"` (the file is opt-in only).
  - Run: `uv run python -c "import ast,sys; ast.parse(open('tests/test_recipes_live.py').read())"` Pass: exits 0.

### U15. merge-policy — the merge ladder enforces a recipe's declared commit globs and registers the recipe's schema

- **Summary**: `MergeLadder._merge` resolves the group's recipe merge policy; when it declares commit globs, any changed or untracked path outside them fails the merge naming the paths, and the Artifact Manifest entry is registered under the recipe contract's class name — `code` (policy `None`) merges and registers exactly as today.
- **Goal**: `MergeLadder._merge` computes `get_recipe(self.group.recipe).merge(self.group.recipe_args)` before `deps.merge_group`; with `commit_globs` not `None`, the union of `changed_paths(workspace, base_ref)` and `git status --porcelain` entries (rename targets, as `_porcelain_offenders` handles them) not matching any glob raises `GroupFailure("merge refused: paths outside the recipe's declared commit globs: …")` naming every offender — no rewrite is spent, no escalation is raised, the group is FAILED and `retry` is its recovery (R7, R25). `_register_code_artifact` becomes `_register_artifact(commit, paths)` writing `schema=recipe.contract.__name__` and the last report's summary; for `code` the entry is byte-identical to today's (`schema: "CoderReport"`). New tests live in `tests/test_merge_policy.py`; `tests/test_review_loop.py` is not edited.
- **Recipe**: —
- **Files**: `orchestrator/execution/merge_ladder.py`, `tests/test_merge_policy.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U3
- **Slice**: —
- **Implements / Consumes**: `merge-policy` / `recipe-entry-v2`
- **Verification**:
  - Run: `uv run pytest tests/test_merge_policy.py -q` Pass: on a real git worktree, a stub recipe with `commit_globs=("docs/research/*.md",)` whose tree changed `docs/research/x.md` and `README.md` fails the merge with a message naming `README.md` and not `docs/research/x.md`; the same tree with only `docs/research/x.md` changed merges and registers `schema` equal to the stub contract's class name.
  - Run: `uv run pytest tests/test_review_loop.py tests/test_e2e_stub.py -q` Pass: every existing `code` test passes unedited and the stub end-to-end run's Artifact Manifest entries still read `schema: "CoderReport"`.
  - Run: `uv run pytest tests/test_merge_policy.py -q -k retry` Pass: the refused group's `state.json` entry is FAILED with the offending path in `failure`, and `rewrites` for the group is 0.

## Task Map

```yaml
# orchestrator-task-map v2
tasks:
  - task_id: u1-round-hooks
    description: Split GenerationLoop._run_generation_body into _first_round, _collect_report, _settle_round and _next_round_prompt with zero behaviour change
    slice: null
    files:
      - orchestrator/execution/generation.py
      - orchestrator/execution/host.py
    symbols: []
    depends_on: []
    implements: [round-hooks]
    consumes: []
  - task_id: u2-command-runner
    description: Lift Run Child launch, poll and re-adoption out of run_executor into a reusable CommandRunner with zero behaviour change
    slice: null
    files:
      - orchestrator/execution/run_executor.py
      - orchestrator/execution/command_runner.py
    size_hints:
      orchestrator/execution/command_runner.py: medium
    symbols: []
    depends_on: []
    implements: [command-runner]
    consumes: []
  - task_id: u3-recipe-entry
    description: Registry entry carries worker_prompt, extra_allowed_tools, worker_role and a declared-globs MergePolicy; WorkerReport base; SessionRole.RESEARCHER; full metadata to every recipe price
    slice: null
    files:
      - orchestrator/recipes/registry.py
      - orchestrator/recipes/code.py
      - orchestrator/recipes/run.py
      - orchestrator/model.py
      - orchestrator/grouping/estimator.py
      - ui/src/types.ts
      - tests/test_recipe_registry.py
    symbols: []
    depends_on: []
    implements: [recipe-entry-v2]
    consumes: []
  - task_id: u4-recipe-allowlist
    description: SessionRunner calls accept extra_allowed_tools and the code loop passes the recipe's tuple on every coder and reviewer call
    slice: null
    files:
      - orchestrator/execution/sessions.py
      - orchestrator/execution/generation.py
      - orchestrator/execution/reviewer.py
      - tests/test_recipe_allowlist.py
    size_hints:
      tests/test_recipe_allowlist.py: small
    symbols: []
    depends_on: [u1-round-hooks, u3-recipe-entry]
    implements: []
    consumes: [round-hooks, recipe-entry-v2]
  - task_id: u5-recipe-loop
    description: The code loop takes prompt, contract, reviewer, handoff, role and merge policy from the registry; declared commit globs are enforced before merge_group
    slice: null
    files:
      - orchestrator/execution/generation.py
      - orchestrator/execution/reviewer.py
      - orchestrator/execution/prompting.py
      - orchestrator/execution/review.py
      - orchestrator/recipes/registry.py
      - tests/test_recipe_loop.py
    size_hints:
      tests/test_recipe_loop.py: medium
    symbols: []
    depends_on: [u1-round-hooks, u3-recipe-entry]
    implements: [recipe-loop]
    consumes: [round-hooks, recipe-entry-v2]
  - task_id: u6-research-recipe
    description: The research Unit Recipe — ResearchArgs, FindingsReport contract with sources by schema, prompts, size-class pricing, Perplexity-first tools with recorded fallback, registration
    slice: null
    files:
      - orchestrator/recipes/research.py
      - orchestrator/recipes/registry.py
      - orchestrator/prompts/research.md
      - orchestrator/prompts/research_reviewer.md
      - orchestrator/prompts/research_handoff.md
      - orchestrator/execution/generation.py
      - tests/test_research_recipe.py
    size_hints:
      orchestrator/recipes/research.py: large
      orchestrator/prompts/research.md: medium
      orchestrator/prompts/research_reviewer.md: small
      orchestrator/prompts/research_handoff.md: small
      tests/test_research_recipe.py: medium
    symbols: []
    depends_on: [u4-recipe-allowlist, u5-recipe-loop, u15-merge-policy]
    implements: [findings-report]
    consumes: [recipe-loop, recipe-entry-v2, merge-policy]
  - task_id: u7-spec-refinement
    description: A Findings Artifact's spec_refinement is validated against declared consumers, marked as a spec_refinement surprise, and folded into the consumer's pre-launch rewrite without spending a rewrite
    slice: null
    files:
      - orchestrator/model.py
      - orchestrator/execution/surprises.py
      - orchestrator/execution/escalating.py
      - orchestrator/execution/generation.py
      - orchestrator/prompts/rewrite_speccer.md
      - tests/test_surprise_board.py
      - tests/test_spec_refinement.py
    size_hints:
      tests/test_spec_refinement.py: medium
    symbols: []
    depends_on: [u6-research-recipe]
    implements: []
    consumes: [findings-report]
  - task_id: u8-evaluate-recipe
    description: The evaluate Unit Recipe — run plus a KPI Contract, harness hash check, optional smoke gate, EvaluationRecord that never gates
    slice: kpi
    files:
      - orchestrator/recipes/evaluate.py
      - orchestrator/execution/evaluate_executor.py
      - orchestrator/execution/run_executor.py
      - orchestrator/recipes/registry.py
      - tests/test_evaluate_recipe.py
    size_hints:
      orchestrator/recipes/evaluate.py: medium
      orchestrator/execution/evaluate_executor.py: medium
      tests/test_evaluate_recipe.py: large
    symbols: []
    depends_on: [u2-command-runner, u3-recipe-entry, u9-keep-or-revert]
    implements: [evaluation-record]
    consumes: [command-runner, recipe-entry-v2, kpi-ledger]
  - task_id: u9-keep-or-revert
    description: The pure KPI contract, noise floor, five-valued decision function and append-only Attempt Ledger with its prompt table renderer
    slice: kpi
    files:
      - orchestrator/execution/kpi.py
      - tests/test_kpi_decision.py
    size_hints:
      orchestrator/execution/kpi.py: large
      tests/test_kpi_decision.py: medium
    symbols: []
    depends_on: []
    implements: [kpi-ledger]
    consumes: []
  - task_id: u10-optimize-recipe
    description: The optimize Unit Recipe — OptimizeArgs, OptimizeLoop settle step (mutable region, evaluate child, decide, ledger, cheat reviewer, champion advance or reset, bounded dials that escalate), champion merge or ledger artifact, prompts, config defaults
    slice: null
    files:
      - orchestrator/recipes/optimize.py
      - orchestrator/execution/optimize_executor.py
      - orchestrator/prompts/optimize.md
      - orchestrator/prompts/optimize_round.md
      - orchestrator/prompts/optimize_reviewer.md
      - orchestrator/prompts/optimize_handoff.md
      - orchestrator/recipes/registry.py
      - orchestrator/config.py
      - orchestrator/grouping/pipeline.py
      - tests/test_recipe_partition.py
      - tests/test_optimize_executor.py
    size_hints:
      orchestrator/recipes/optimize.py: large
      orchestrator/execution/optimize_executor.py: large
      orchestrator/prompts/optimize.md: medium
      orchestrator/prompts/optimize_round.md: small
      orchestrator/prompts/optimize_reviewer.md: small
      orchestrator/prompts/optimize_handoff.md: small
      tests/test_optimize_executor.py: large
    symbols: []
    depends_on: [u2-command-runner, u5-recipe-loop, u8-evaluate-recipe, u9-keep-or-revert]
    implements: [optimize-loop]
    consumes: [round-hooks, recipe-loop, command-runner, kpi-ledger, evaluation-record]
  - task_id: u11-bundle-additive
    description: The Run Bundle carries an optional per-group ledger key and the researcher role; schema_version stays 2; the contract doc documents the new recipes, schemas and role
    slice: null
    files:
      - orchestrator/execution/export.py
      - docs/run-bundle-contract.md
      - tests/test_export.py
    symbols: []
    depends_on: [u3-recipe-entry, u9-keep-or-revert]
    implements: []
    consumes: [recipe-entry-v2, kpi-ledger]
  - task_id: u12-report-recipes
    description: Report facts and markdown render research, evaluate and optimize groups — an Optimization block per optimize group listing keeps and the ledger, zero-hit loops landed
    slice: null
    files:
      - orchestrator/report/facts.py
      - orchestrator/report/markdown.py
      - tests/test_report_facts.py
      - tests/test_report_markdown.py
    symbols: []
    depends_on: [u3-recipe-entry, u9-keep-or-revert]
    implements: []
    consumes: [recipe-entry-v2, kpi-ledger]
  - task_id: u13-docs-skills
    description: Task-map v2 examples for the three recipes, the KPI harness contract doc with judge-script patterns, plan/deepen/run skill sections, plugin 0.21.0
    slice: null
    files:
      - docs/orchestrator-task-map.md
      - docs/orchestrator-kpi-harness.md
      - skills/orchestrator-plan/SKILL.md
      - skills/orchestrator-deepen/SKILL.md
      - skills/orchestrator-run/SKILL.md
      - .claude-plugin/plugin.json
    size_hints:
      docs/orchestrator-kpi-harness.md: medium
    symbols: []
    depends_on: [u6-research-recipe, u8-evaluate-recipe, u10-optimize-recipe]
    implements: []
    consumes: [findings-report, evaluation-record, optimize-loop]
  - task_id: u14-live-tier
    description: An -m llm live file driving a real research group into a code group and a real optimize loop on a scratch repo with a deterministic KPI script
    slice: null
    files:
      - tests/test_recipes_live.py
    size_hints:
      tests/test_recipes_live.py: large
    symbols: []
    depends_on: [u7-spec-refinement, u10-optimize-recipe, u11-bundle-additive]
    implements: []
    consumes: [findings-report, optimize-loop]
  - task_id: u15-merge-policy
    description: MergeLadder enforces a recipe's declared commit globs before merge_group, fails naming the paths, and registers the recipe contract's schema name
    slice: null
    files:
      - orchestrator/execution/merge_ladder.py
      - tests/test_merge_policy.py
    size_hints:
      tests/test_merge_policy.py: medium
    symbols: []
    depends_on: [u3-recipe-entry]
    implements: [merge-policy]
    consumes: [recipe-entry-v2]
```
