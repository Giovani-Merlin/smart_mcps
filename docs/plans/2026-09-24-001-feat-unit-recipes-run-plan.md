---
title: Unit Recipes v1 — the registry, the `run` recipe, and the Artifact Manifest
type: feat
date: 2026-09-24
origin: docs/brainstorms/2026-09-21-unit-recipes-requirements.md (Plan B of its Next Step)
---

# Unit Recipes v1 — the registry, the `run` recipe, and the Artifact Manifest

## Objective

Let a plan unit declare what kind of work it is, and give each kind its own
machine. After this plan:

- a task map can say `recipe: run` with a `recipe_args` mapping (task-map v2),
  and a map with no `recipe` behaves exactly as today (R1, R3, R4);
- a registry is the single place Unit Recipes are enumerated, and each entry
  owns its args schema, pricing, completion contract, reviewer prompt, merge
  policy, handoff prompt, executor and an uninterpreted `custom` mapping (R2,
  R10, R13, R14);
- the grouper never puts two recipes in one group, gives every non-`code` unit
  its own group, and prices each unit through its recipe (R11, R12);
- `make_executor` dispatches on the group's recipe without the scheduler
  learning recipes exist (R8, R9);
- a `run` unit's commands execute as detached, Landlock-confined,
  crash-re-adoptable Run Children under a wall-clock cap, with measurements,
  optional committed outputs, and a one-shot LLM triage on failure (R5a, R15,
  R18);
- a run-level Artifact Manifest indexes what every group produced, feeds `run`
  entries to downstream prompts, and rides the Run Bundle as an additive key
  (R16, R17).

**Not covered, by design (origin's next increment):** R5. research-recipe,
R6. synthesize-recipe, R7. recipe-base. The registry built here is where they
land; nothing in this plan registers them. The origin's Next Step 3 —
validating on `learning_podcast` — is the run *after* this plan, not a unit of
it.

## What we already know (resolved context)

**Plan A has landed** (merged in `f7c85a5`, run `r20260923-163956`).
`orchestrator/execution/review.py` is now a 204-line composition root:
`ReviewDeps` (dataclass, `review.py:64`), `make_executor(deps) -> Executor`
(`review.py:107`), and `_GroupExecution(GenerationLoop, ReviewerRound, SessionRecords, MergeLadder, EscalationHandlers, SurpriseHandling)` whose
`__init__` is the single place instance state is born. The mixins live in
`execution/generation.py`, `reviewer.py`, `records.py`, `merge_ladder.py`,
`escalating.py`, `surprises.py`; their shared-attribute protocol is
`execution/host.py` (e.g. `_apply_env_notice`, `_apply_briefing`,
`_apply_operator_note` at `host.py:114–118`). This is R9's precondition, done:
a second executor imports `RoundHeartbeat` (`execution/heartbeat.py`),
`EscalationBroker`/`EscalationPolicy` (`execution/escalation.py`), `log_event`
and `ManifestStore` (`execution/manifest.py`) directly rather than prising
methods out of a class.

**The executor seam.** `Executor = Callable[[GroupContext], Awaitable[GroupState]]`
(`execution/scheduler.py:281`); `GroupContext` carries `group`, `generation`,
`set_state`, `set_generation`, `record_cure` (`scheduler.py:266`). There is one
construction site: `cli.py:2251` builds `ReviewDeps`, `cli.py:2288` calls
`make_executor(deps)`; `cli.py:98` imports both from `execution.review`.
Nothing else in `orchestrator/` constructs an executor.

**The task map today** (`docs/orchestrator-task-map.md`, reader
`grouping/plan_reader.py`). `VERSION_MARKER = "# orchestrator-task-map v1"`
(`plan_reader.py:23`); a block with any other `vN` raises
`unsupported task map version` (`plan_reader.py:67–71`, and again in
`parse_task_map_for_pricing` at `:145`). `_KNOWN_KEYS` (`:42`) is
`task_id, description, slice, size_hints, files, symbols, depends_on, implements, consumes`; an unknown key is a hard error. Parsed entries become
`TaskMapping` (frozen dataclass, `grouping/graphing.py:98`) whose fields feed
node metadata. `SLICE_TASK_CAP = 5`.

**Pricing today** (`grouping/estimator.py`). `node_work(metadata, config)`
(`:55`) is pure file arithmetic: `source_bytes / bytes_per_token × slack_multiplier + files × per_file_tool_allowance`, prospective files priced
by `size_hints`, all × `coder_slack_multiplier`. `partition_budget_cap`,
`estimate_group_tokens`, `price_plan` (`group --price`, `PriceReport`) sit
beside it. A `run` unit declares no files, so today it prices at ~0 — P1.
**Import hazard:** `estimator.py` imports `plan_reader`; the registry's `code`
pricing must call `node_work` without making `grouping` import a module that
imports `grouping` back (lazy import or keep the arithmetic in the estimator
and have the registry reference it).

**The partitioner** (`grouping/partition.py`,
`DefaultPartitionStrategy.partition` at `:373`): `detect_hub_roles` →
`slice_atoms`/`_contract_slices` (must-link) → `_hub_isolated_clustering`
(Louvain; utility hubs isolated as their own group) → `lift_independent` →
`split_over_budget` → `merge_small_groups` (`:949`) → `repair_cycles`
(`:1322`, merges any cyclic group-SCC) → `_renumber` → `build_group_dag`.
Slice integrity is asserted after the fact by `_assert_slice_integrity`
(`grouping/pipeline.py:203`). Groups are assembled into `Group` models in
`run_grouping` (`pipeline.py:811`, the `Group(...)` call at `:922`), with
`intensity = intensity_for(difficulty)`. Golden partitions of real plans are
pinned by `tests/test_golden_partitions.py`.

**Models** (`orchestrator/model.py`). `Group` (`:67`, `validate_assignment`)
has no recipe field. `SessionRole` (`:93`) is `base | coder | reviewer`.
`CoderReport` is the `code` completion contract, validated every round today;
`GroupSpec.summary`/`Group.summary` cap at `SUMMARY_MAX_CHARS = 120`.

**Processes and resume.** Worker children are tracked through the
`SubprocessTracker` protocol (`execution/sessions.py:377`) into
`RunState.live_pids: dict[int, LivePid]` (`scheduler.py:230`), where `LivePid`
holds `context`, `cmdline_head`, `starttime` (`scheduler.py:202`).
`Scheduler._reap_orphans` (`scheduler.py:417`) kills every recorded pid that
`_is_same_process` confirms, then clears the map; `_reenter_on_resume` moves
mid-flight groups back to READY.

**Confinement.** `build_policy(worktree, claude_home, project_slug, system_paths, cache_dirs)` (`execution/confinement.py:393`) grants read-write
to the worktree, `~/.claude/projects/<encode_cwd(worktree)>`, the worktree's
git dirs, the probed `~/.claude` runtime dirs (never `projects/` wholesale),
`~/.claude/.credentials.json`, system temp paths, and the orchestrator cache
root; reads are never restricted. `landlock_preexec(policy)` (`:457`) returns
a `preexec_fn` or `None` when Landlock is unavailable (never raises). A nested
`claude` started with `cwd` = the worktree writes its transcript to the
already-granted slug — B6's refusals were Claude Code *Bash permission rules*,
which do not exist for a process the orchestrator launches itself.
`worktrees.data_layer_write_paths(repo_root, workspace)` (`worktrees.py:700`)
returns the `data_dirs` write paths.

**Merge and Preflight.** `IntegrationMerger.merge_group(group, worktree)`
(`execution/merge.py:71`) runs Preflight inside the merge and raises
`PreflightFailure` or `MergeConflict`; the `code` ladder around it is
`MergeLadder._merge` (`merge_ladder.py:83`). Resolve of a FAILED group's
Stranded Work is scheduler-owned through `ResolveDeps`
(`scheduler.py:104`), landing the group in `GroupState.RESOLVED`
(`scheduler.py:754`).

**Run directory and export.** `RunPaths` (`execution/manifest.py:160ff`) names
every run-dir file (`manifest.json`, `state.json`, `groups.json`,
`groups/<gid>/…`). The Run Bundle v2 export is `execution/export.py`;
`ExportSession.role` is a plain `str` (`export.py:68`), and
`docs/run-bundle-contract.md` states a new optional key or enum value does not
bump `schema_version`. The Observatory types pin
`export type SessionRole = "base" | "coder" | "reviewer"` (`ui/src/types.ts:56`).

**LLM one-shots.** The run-time spec rewrite is a one-shot `claude -p` JSON
call: `call_llm_json` + `claude_json_runner` + `with_usage_limit_retry`
(`grouping/llm.py`), recorded by `JsonlCallRecorder` (wired inside the `rewrite_spec=` block,
`cli.py:2275`). Prompt templates live in `orchestrator/prompts/*.md`.

**Tooling note.** `smart-mcps-orchestrate` on PATH is a non-editable uv-tool
copy; every command in this plan is `uv run smart-mcps-orchestrate …` so it
runs the worktree's code.

## Decisions

- **`run` declares its work in a generic `recipe_args` mapping, validated by
  the recipe's own pydantic model.** Task-map v2 adds exactly two keys:
  `recipe` and `recipe_args`. Rationale: the next recipes (`research`,
  `evaluate`) add args without minting v3; one registry entry owns what its
  args mean and how they are priced. Rejected: first-class `run_*` keys (each
  recipe re-bumps the contract); parsing commands out of `Run:` prose (prose
  becomes an execution contract).
- **Non-`code` units are pre-isolated as fixed singleton groups before
  Louvain.** The brainstorm's "contract by recipe as by slice" reads as a
  must-link, but R11 needs a *cannot-link*. Louvain/split/merge/repair operate
  on `code` units only; `merge_small_groups` and `repair_cycles` never touch a
  fixed singleton, and a cycle that could only be repaired by absorbing one
  fails loudly naming the path. A non-`code` unit may not carry a `slice`
  (parse error). Rejected: partitioning per recipe and stitching (N pipeline
  passes; cross-recipe cycles surface only after stitching). Consequence: a
  future multi-unit `research` group needs this lifted.
- **A Run Child is re-adopted, not reaped, on resume** (→ ADR 0011). Exit
  status goes to a file written by a wrapper; the child is recorded per attempt
  under the group's directory, never in `live_pids`; the cap counts from the
  original start. Rejected: kill-and-relaunch (re-pays an hour-long render per
  crash; mixes partial outputs).
- **The Run Child runs under the worker Landlock profile, plus `data_dirs`,
  plus a per-unit `recipe_args.allow_write` list.** Chosen by the human over
  running unconfined: default-safe, flexible per unit. `allow_write` entries
  that equal or contain `~/.claude` or `~/.claude/projects` are a parse-time
  error, so the operator-memory protection cannot be reopened by a plan.
  Rejected: unconfined-and-logged; widening the whole profile with
  `~/.claude` read-write.
- **Failure triage is a one-shot `claude -p` JSON call inside the run.** It
  reads the command, exit status and output tails and returns a validated
  `RunTriage {verdict: work_failure | needs_decision, diagnosis}`, recorded as
  a `SessionRole.RUNNER` call with its cost. It never relaunches. Rejected:
  escalating the raw tail to the run driver (leaves HITL-off runs with an
  undiagnosed FAILED and nothing for the `runner` role to attribute).
- **Both `run` and `code` groups register Artifact Manifest entries; only
  non-`code` entries are injected into downstream prompts.** A `code` entry is
  written by the orchestrator at merge from data it already has (merge commit,
  changed files, the final `CoderReport.summary`, which is under R16's
  2,000-char cap) — the coder's prompt and report are untouched. Downstream
  injection is limited to direct `depends_on` upstreams whose recipe is not
  `code`, so a code-only plan's prompts stay byte-identical (R4). Rejected:
  injecting every upstream entry (changes every existing plan's prompts); an
  explicit `inputs:` key (duplicates `depends_on`).
- **Measurements come from a declared JSON file.** `recipe_args.measurements`
  names a path the command writes; its top-level scalar keys become the
  entry's observations, never a gate (P2). Rejected: regex over stdout
  (silently empty when the log format drifts).
- **A `run` unit may commit files matching `recipe_args.commit_paths` globs,
  through the same Preflight gate and `merge_group`.** Anything else in
  `git status --porcelain` fails the unit naming the paths (R15). A Preflight
  failure or merge conflict goes to the triage call, not a rewrite — there is
  no coder. With no `commit_paths`, the unit never commits.
- **The gate is an allow-list: `[recipes] enabled = ["run"]`.** Checked at
  `group` time and again at `run` time (the config can change in between).
  `code` is always allowed. A plan declaring a non-enabled recipe fails naming
  the units and the config key. Rejected: a boolean (cannot allow `run` while
  refusing a later recipe).
- **`run` groups schedule like any group.** Under `[execution] sequential = true` they serialize with coders. Rejected: running beside a coder (judge
  passes spend tokens concurrently, defeating why `sequential` exists).
- **A `run` group is `self_verify` with difficulty 0 and no reviewer at any
  intensity** (R14); its pricing is its declared wall clock plus a fixed
  triage-token allowance, and a unit without a declared wall clock takes the
  recipe default, recorded in the grouping trace (R12).
- **`review.py` keeps its name** and remains the `code` executor; the
  dispatcher is a new `execution/dispatch.py`. Rejected: renaming to
  `code_executor.py` (34 test imports churn for no behaviour).
- **The registry lives in a new top-level `orchestrator/recipes/` package**
  that imports neither `grouping` nor `execution` at module load, because both
  consume it; each entry names its executor by dotted path, resolved lazily by
  the dispatcher, so the registry stays the single enumeration (R2).

## Units

### U1. recipe-registry — one registry enumerates the Unit Recipes and owns each one's args, pricing, contract, prompts, merge policy and executor

- **Summary**: New `orchestrator/recipes/` package: `get_recipe(name)` / `registered_names()` over two entries, `code` (today's arithmetic, `CoderReport`, existing prompts) and `run` (`RunArgs` args model, `RunRecord` contract, wall-clock pricing, no reviewer, executor by dotted path).
- **Goal**: A `UnitRecipe` record carries `name`, `args_model` (pydantic or `None`), `price(args, metadata, config) -> RecipePrice` (tokens + optional wall-clock seconds + a `defaulted` flag), `contract` (pydantic model), `reviewer_prompt` (template name or `None`), `handoff_prompt` (template name or `None`), `merge` policy (`code_ladder` | `run_commit_paths`), `executor` (dotted path), and `custom: Mapping[str, object]` the core never reads. `code` points at today's pieces unchanged. `run`'s `RunArgs` is `extra="forbid"` with `commands: list[{cmd, wall_clock_min, cwd?}]` (≥ 1; `cmd` is one shell string, so `VAR=1 uv run …`, pipes and globs are declarable; `wall_clock_min` is a float > 0), `outputs: list[str]` (repo-relative, no absolute path, no `..` — whether each sits under a configured `data_dirs` entry is checked at `group` time by U4, because the parser has no config), `measurements: str | None`, `commit_paths: list[str]`, `allow_write: list[str]` (each absolute or `~/`-prefixed; relative paths and `$VAR` references are rejected); `RunRecord` (`extra="forbid"`) holds per-command exit status, duration, output paths, measurements and a ≤ 2,000-char summary. An unknown name raises an error listing `registered_names()`.
- **Files**: `orchestrator/recipes/__init__.py` *(new, small)*, `orchestrator/recipes/registry.py` *(new, medium)*, `orchestrator/recipes/code.py` *(new, small)*, `orchestrator/recipes/run.py` *(new, medium)*, `tests/test_recipe_registry.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `UnitRecipe`
- **Verification**:
  - `uv run python -c "from orchestrator.recipes import registered_names; print(registered_names())"` prints exactly `('code', 'run')`.
  - `get_recipe("research")` raises an error whose message names `research` and lists `code` and `run`.
  - `RunArgs` rejects: zero commands; a non-positive `wall_clock_min`; an unknown key; an `allow_write` entry equal to or containing `~/.claude` or `~/.claude/projects` (after `~` expansion) — each error names the offending field.
  - The `code` entry's price for every task of the real plan `docs/plans/2026-09-23-001-refactor-review-loop-split-plan.md` equals `estimator.node_work` for the same metadata (the real plan is the oracle, not a hand-built fixture).
  - `python -c "import orchestrator.recipes"` in a fresh interpreter does not import `orchestrator.grouping` or `orchestrator.execution` (checked via `sys.modules`).
- **Edge cases**:
  - An `outputs` entry that is absolute or contains `..` is rejected at parse time naming the entry; one that is well-formed but outside every `data_dirs` entry passes here and is rejected by U4 at `group` time.
  - An `allow_write` entry that is relative (`data/x`) or uses a variable (`$HOME/x`) is rejected naming the entry; `~/x` is expanded against the orchestrator's `HOME` before the `~/.claude` containment check.
- **Non-goals / must-not**: —

<!-- deepened from plan sha256:e607a693d938 -->

### U2. recipe-field — task-map v2 carries `recipe` and `recipe_args`, and v1 maps parse exactly as today

- **Summary**: `parse_task_map` accepts `# orchestrator-task-map v1` and `v2`; v2 entries may carry `recipe` (a registered name) and `recipe_args` (validated by that recipe's args model); `TaskMapping` gains `recipe` (default `"code"`) and `recipe_args`; the contract doc describes v2.
- **Goal**: Both markers are accepted through **one** marker regex defined in `plan_reader.py` and used by `parse_task_map`, `parse_task_map_for_pricing`, `strip_task_map` and `task_map_block_span`; `plan_sections.py` drops its own hardcoded v1 copy (`_TASK_MAP_BLOCK`, `plan_sections.py:26`) and imports that regex — the one edit outside U2's declared Files, touched by no other unit; `recipe`/`recipe_args` in a v1 block are a hard error telling the author to mark the block v2. An unknown recipe is a hard error naming the task and listing registered names. `recipe_args` on a `code` unit, or missing required args on a non-`code` unit, is a hard error naming the task and field. A non-`code` unit with a `slice` is a hard error. `TaskMapping.recipe`/`recipe_args` flow into node metadata so the grouper and estimator can read them. `docs/orchestrator-task-map.md` gains a v2 section: the two keys, the `run` args table, an example, and the rules above; the `split`/`plan-check` byte-preservation list includes the new keys.
- **Files**: `orchestrator/grouping/plan_reader.py`, `orchestrator/grouping/graphing.py`, `docs/orchestrator-task-map.md`, `tests/test_plan_reader.py`
- **Symbols**: —
- **Depends-on**: U1
- **Slice**: —
- **Implements / Consumes**: implements `task-map-v2`; consumes `UnitRecipe`
- **Verification**:
  - Every committed plan under `docs/plans/` that carries a v1 task map parses to the same `MapperOutput` (mappings, descriptions, flags) before and after the change.
  - A v2 block whose tasks declare no `recipe` parses to mappings identical to the same block marked v1, with `recipe == "code"` on each.
  - A v2 `run` task with valid `recipe_args` parses; the same task with `recipe_args.commands: []`, with `slice: x`, or marked v1 each raises `TaskMapError` naming the task.
  - `uv run smart-mcps-orchestrate plan-check docs/plans/2026-09-23-001-refactor-review-loop-split-plan.md` exits 0 (the real v1 plan still validates).
  - `strip_task_map` removes a v2 block (and its `## Task Map` heading) exactly as it removes a v1 block, and `plan-check` on a v2 plan reports it internally consistent — a v2 map never reaches an LLM context.
- **Edge cases**:
  - A v2 plan's map is found by every marker consumer — the parser, `strip_task_map`, `task_map_block_span` (hence `plan-check`/`split`) and the plan digest's declared-unit list; a `v3` marker still raises `unsupported task map version`.
- **Non-goals / must-not**: —

<!-- deepened from plan sha256:e607a693d938 -->

### U3. recipe-pricing — each recipe prices its own units, and groups carry their recipe

- **Summary**: The estimator prices a task through its recipe (`code` = today's `node_work`, `run` = triage-token allowance plus declared wall clock); `Group` gains `recipe`, `recipe_args`, `estimated_wall_clock_s`; `SessionRole` gains `runner`; config gains `[recipes] enabled` and `[recipes.run]` defaults; `group --price` shows recipe and wall clock.
- **Goal**: `node_work` dispatches on the metadata's recipe; `code` keeps today's arithmetic bit-for-bit. `run` prices at `[recipes.run] triage_tokens` (default 20,000) — also the run group's `estimated_tokens`, the most it should spend (one triage on failure), so a successful run's zero actual spend reads as expected — and carries the summed `wall_clock_min` as seconds; a unit whose args omit a wall clock (only possible for future recipes) takes the recipe default and the grouping trace records `priced_by_default: true` for it. `Group` gains `recipe: str = "code"`, `recipe_args: dict | None = None`, `estimated_wall_clock_s: int | None = None` (defaults keep old `groups.json` loading). `SessionRole.RUNNER = "runner"`. `OrchestratorConfig.recipes: RecipesConfig` with `enabled: list[str] = []` (validated at config load against `registered_names()` through an import *inside* the validator — `orchestrator/recipes/` imports config types only under `TYPE_CHECKING`, so there is no import cycle; `code` implicit) and a `run` sub-table (`triage_tokens`, `triage_model` — unset means inherit `[session] model`, `output_tail_lines`, `poll_interval_s`). `PriceReport` rows carry recipe and wall clock, and the report carries the plan's summed `run` wall clock; U5 prints both (the printer is `cli.py:1163`).
- **Files**: `orchestrator/grouping/estimator.py`, `orchestrator/grouping/trace.py`, `orchestrator/model.py`, `orchestrator/config.py`, `tests/test_estimator.py`
- **Symbols**: —
- **Depends-on**: U1
- **Slice**: —
- **Implements / Consumes**: implements `Group.recipe`; consumes `UnitRecipe`
- **Verification**:
  - `uv run smart-mcps-orchestrate group --price docs/plans/2026-09-23-001-refactor-review-loop-split-plan.md` prints the same per-task node work and slice totals as on `main` (captured before the change), plus a recipe column reading `code`.
  - A v2 fixture with one `run` task declaring two commands of 30 and 45 minutes prices at exactly 20,000 tokens (the triage allowance, with no coder multiplier applied) and `estimated_wall_clock_s == 4500`.
  - Run (driver): from the main checkout (`.orchestrator/` is gitignored, so a worker's worktree has no past runs), load `.orchestrator/runs/r20260923-163956/groups.json` into `GroupingResult` — Pass: it loads, and every group's `recipe == "code"`.
  - A `groups.json` serialized by today's `GroupingResult` (no `recipe` keys), committed as a test fixture, loads with every group's `recipe == "code"`.
  - An `.orchestrator/config.toml` with `[recipes] enabled = ["nope"]` fails to load with an error naming `nope` and the registered names.
  - With `[session] model = "sonnet"` and no `triage_model` set, the resolved `RecipesConfig.run.triage_model` is `sonnet`.
  - A v2 fixture with two `run` tasks of 60 and 90 declared minutes yields a `PriceReport` whose run wall-clock total is 9,000 seconds.
- **Edge cases**: —
- **Non-goals / must-not**: —

<!-- deepened from plan sha256:e607a693d938 -->

### U4. single-recipe-groups — the partitioner isolates every non-`code` unit as its own group and refuses to mix recipes

- **Summary**: `DefaultPartitionStrategy` removes non-`code` nodes before hub detection and re-adds each as a fixed singleton group that split/merge/repair never touch; a cycle that only merging one could break raises `GrouperError` naming the path; `run_grouping` asserts one recipe per group, sets `Group.recipe`/`recipe_args`, forces `self_verify` for non-`code` groups, and enforces the `[recipes] enabled` gate at `group` time.
- **Goal**: A plan with no non-`code` units produces the same partition, group ids, intensities and estimates as today (golden partitions unchanged). With `run` units: each is its own group whose DAG edges come from its `depends_on`; `merge_small_groups` and `repair_cycles` skip fixed singletons; if a group-level cycle passes through a fixed singleton, grouping fails naming the singleton and the `code` tasks on the path. `_assert_single_recipe` (beside `_assert_slice_integrity`) is the final guard. The gate: non-`code` units whose recipe is not in `config.recipes.enabled` fail `group` naming each unit, its recipe, and the key `[recipes] enabled`. `group --dry-run` prints each `run` group's commands verbatim.
- **Files**: `orchestrator/grouping/partition.py`, `orchestrator/grouping/pipeline.py`, `tests/test_recipe_partition.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U2, U3
- **Slice**: —
- **Implements / Consumes**: consumes `task-map-v2`, `Group.recipe`
- **Verification**:
  - `uv run pytest tests/test_golden_partitions.py` passes with no golden file regenerated (the recorded real-plan partitions are the oracle).
  - A v2 fixture: code units a→b, a→c, run unit r depending on b, code unit d depending on r. Grouping yields r alone in its group, `recipe == "run"`, `intensity == self_verify`, and the group DAG orders b's group → r's group → d's group.
  - The same fixture with d also a dependency of b's group-mates such that b's group → r → d's group → b's group forms a cycle raises `GrouperError` naming r and the `code` tasks on the cycle.
  - With `[recipes] enabled = []`, `uv run smart-mcps-orchestrate group <fixture> --no-spec` exits non-zero and its stderr names the `run` unit and `[recipes] enabled`.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U5. executor-dispatch — `make_executor` dispatches on the group's recipe, and the `code` path is byte-for-byte today's

- **Summary**: New `execution/dispatch.py` exports `make_executor(deps)` that returns an executor reading `ctx.group.recipe` and delegating to the registry-named executor (`code` → `review.make_executor`); `cli.py` builds through it, supplies the triage LLM seam on `ReviewDeps`, and re-checks the `[recipes] enabled` gate before a run or resume starts.
- **Goal**: `dispatch.make_executor(deps)` resolves each recipe's executor factory once (lazy import of the registry's dotted path) and returns one `Executor`; the scheduler, `Executor` and `GroupContext` are unchanged. `ReviewDeps` gains optional fields the `run` executor needs — `triage: Callable | None` (a `call_llm_json`-backed one-shot built in `cli.py` with the usage-limit retry and `JsonlCallRecorder`, model `[recipes.run] triage_model`), `workspace_config`, `recipes_config` — all defaulting to `None` so every existing test constructor keeps working. `cli.py` imports `make_executor` from `execution.dispatch`. `run` and `resume` refuse a `groups.json` holding a recipe not in `[recipes] enabled`, naming groups and the key, before any worktree is created.
- **Files**: `orchestrator/execution/dispatch.py` *(new, small)*, `orchestrator/execution/review.py`, `orchestrator/cli.py`, `tests/test_executor_dispatch.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U1, U3
- **Slice**: —
- **Implements / Consumes**: implements `executor-dispatch`; consumes `UnitRecipe`, `Group.recipe`
- **Verification**:
  - `uv run pytest tests/test_review_loop.py tests/test_suspend_cure.py tests/test_liveness_wiring.py tests/test_merge_gate_triage.py` passes with no test file edited.
  - A dispatch test with a group of recipe `code` observes the same sequence of `SessionRunner` calls (prompts byte-compared) as `review.make_executor` does for the same group.
  - `run` against a `groups.json` holding a `run` group, with `[recipes] enabled = []`, exits non-zero naming the group and `[recipes] enabled`, and no worktree directory is created under `.orchestrator/`.
  - Run (driver): `uv run pytest -m llm tests/test_e2e_live.py` — Pass: the live code-only run terminates, commits and is confined exactly as before (the real `claude` CLI is the oracle).
- **Edge cases**: —
- **Non-goals / must-not**: —

### U6. artifact-manifest — a run-level index records every group's output, and `run` entries reach downstream prompts

- **Summary**: New `execution/artifacts.py` holds `ArtifactEntry`/`ArtifactManifest` and an atomic, lock-guarded store at `RunPaths.artifact_manifest_path` (`<run_dir>/artifacts.json`); `code` groups register an entry on merge and a `partial` one on Resolve; a coder whose direct upstream groups are non-`code` gets their entries (never file bodies) folded into its first prompt.
- **Goal**: `ArtifactEntry` carries `artifact_id`, `group_id`, `tasks`, `recipe`, `paths`, `sha256` (per file, for files that exist), `commit` (merge commit or `None`), `schema` (the contract model's name), `summary` (≤ 2,000 chars, required), `status` (`complete` | `partial`), `measurements` (`dict[str, scalar]`, empty for `code`), `recorded_at`. `register(entry)` is idempotent per `artifact_id` (a re-merge replaces, never duplicates). `MergeLadder._merge` registers a `complete` `code` entry after a successful `merge_group` (summary = the final `CoderReport.summary`, paths = the group's changed files); the scheduler registers a `partial` entry when a group lands `RESOLVED`. A new `_apply_artifact_inputs` (declared in `host.py`, called where the first coder prompt is built in `generation.py`) appends one bounded block listing each non-`code` direct-upstream entry; with none, the prompt is unchanged byte-for-byte.
- **Files**: `orchestrator/execution/artifacts.py` *(new, medium)*, `orchestrator/execution/manifest.py`, `orchestrator/execution/merge_ladder.py`, `orchestrator/execution/generation.py`, `orchestrator/execution/host.py`, `orchestrator/execution/scheduler.py`, `tests/test_artifact_manifest.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U3
- **Slice**: —
- **Implements / Consumes**: implements `ArtifactManifest`; consumes `Group.recipe`
- **Verification**:
  - After a `tests/test_review_loop.py`-harness run of two code groups (real git worktrees), `artifacts.json` holds two `complete` `code` entries whose `commit` values each resolve with `git cat-file -t <sha>` → `commit` in that repo.
  - A group resolved from Stranded Work produces an entry with `status == "partial"`.
  - A code group downstream of a `run` group gets a first prompt containing the run entry's `artifact_id`, summary and measurements and none of the output files' bytes; a code group downstream only of code groups gets a first prompt byte-identical to one built before this change.
  - An entry with a 2,001-char summary is rejected at `register` naming `summary`.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U7. bundle-additive — the Run Bundle exports the Artifact Manifest and the `runner` role without a version bump

- **Summary**: `export` writes the Artifact Manifest as a new optional top-level key and exports `runner` sessions like any other; `schema_version` stays 2; the contract doc and the Observatory's `SessionRole` type learn the new value.
- **Goal**: `ExportBundle` (the `ingest.json` model in `export.py`) gains `artifacts: list[ExportArtifact] | None`, absent when the run has no `artifacts.json`. `runner` triage calls appear as sessions with `role == "runner"`, their cost in `cost_usd`. `docs/run-bundle-contract.md` documents the key, the entry fields, and the new role value under the additive-change rule. `ui/src/types.ts` `SessionRole` adds `"runner"`, and the UI's role-dependent code (`attempts.ts`, `cost.ts`) treats an unknown or `runner` role without throwing.
- **Files**: `orchestrator/execution/export.py`, `docs/run-bundle-contract.md`, `ui/src/types.ts`, `ui/src/attempts.ts`, `tests/test_export.py`
- **Symbols**: —
- **Depends-on**: U6
- **Slice**: —
- **Implements / Consumes**: consumes `ArtifactManifest`
- **Verification**:
  - Run (driver): from the main checkout (`.orchestrator/runs/` is gitignored and absent in a worktree), `uv run smart-mcps-orchestrate export r20260923-163956 --out <scratch dir>` — Pass: it succeeds, and `ingest.json` has `schema_version == 2` and no `artifacts` key (additive: old runs export unchanged).
  - A run directory with an `artifacts.json` and a `runner` session exports both, and the exported bundle validates against the models in `export.py`.
  - `npm --prefix ui run build` (which runs `tsc`) and `npm --prefix ui test` pass, including a test rendering a group whose sessions include a `runner` session.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U8. run-recipe — the `run` executor runs declared commands as confined, re-adoptable Run Children and records what happened

- **Summary**: New `execution/run_child.py` (launch wrapper, per-attempt state, re-adoption, wall-clock cap) and `execution/run_executor.py` (the `run` recipe's executor: worktree, commands in order, measurements, porcelain check against `commit_paths`, commit + `merge_group`, `RunRecord` validation, Artifact Manifest entry, one-shot triage on failure); `confinement.build_policy` gains an `extra_write` parameter.
- **Goal**: For each command in order, the executor launches `sh -c '<cmd>; echo $? > <attempt>/<n>.exit'` with `cwd` = the group worktree (or the declared `cwd` inside it), `start_new_session=True`, stdout/stderr to `<attempt>/<n>.out`/`.err`, and `preexec_fn` from `landlock_preexec(build_policy(worktree, …, extra_write=data_layer_write_paths + allow_write))`. It records `{n, pid, starttime, cmdline_head, started_at}` in `groups/<gid>/run/attempt-<k>/state.json` — never in `live_pids`. It polls; the RoundHeartbeat phase reads `command n/N · elapsed/cap · +bytes`. On exceeding `wall_clock_min` it kills the process group and treats the command as timed out. On start, if the latest attempt's state names a pid that `_is_same_process` confirms, it re-adopts that child (cap from the original `started_at`); if the pid is gone with no exit file, the command counts as "died with the orchestrator". A fresh `retry` starts attempt k+1. After all commands exit 0, it reads the `measurements` file (top-level scalars), runs `git status --porcelain`: paths outside `commit_paths` fail the unit naming them; matches are committed and merged via `deps.merge_group`. It builds a `RunRecord`, validates it (a validation error names the field), registers a `complete` `run` entry (summary generated from exit codes, durations, measurements) and returns COMPLETED. Any non-zero exit, timeout, porcelain violation, `PreflightFailure` or `MergeConflict` goes to `deps.triage` once: `work_failure` → FAILED with the diagnosis in the group failure and `run.log`; `needs_decision` → an escalation via the broker (or FAILED when HITL is off). It never relaunches itself; no reviewer session ever runs.
- **Files**: `orchestrator/execution/run_child.py` *(new, medium)*, `orchestrator/execution/run_executor.py` *(new, large)*, `orchestrator/execution/confinement.py`, `orchestrator/prompts/run_triage.md` *(new, small)*, `tests/test_run_child.py` *(new, medium)*, `tests/test_run_executor.py` *(new, large)*
- **Symbols**: —
- **Depends-on**: U1, U5, U6
- **Slice**: —
- **Implements / Consumes**: consumes `UnitRecipe`, `executor-dispatch`, `ArtifactManifest`
- **Verification**:
  - A real `run` group whose command is `python -c` writing `{"score": 0.9}` to a `data_dirs` path completes: `artifacts.json` has a `run` entry with `measurements == {"score": 0.9}`, the output file's sha256, and exit status 0; no `SessionEntry` of role `coder` or `reviewer` exists for the group.
  - A command `sleep 30` with `wall_clock_min` set to 0.05 is killed (its process group no longer exists per `/proc`) and triage is called once with a timed-out status.
  - Re-adoption: start a `sleep 20` Run Child, drop the executor (simulated crash), construct a new executor for the same group; it re-adopts the same pid (no second `sleep` process appears in `/proc`) and completes when the child exits.
  - Confinement, real kernel: on a host where `landlock_abi_version() > 0`, a command writing to `$HOME/.claude/projects/<other-slug>/probe` fails while a write into the declared `data_dirs` output succeeds; a declared `allow_write` path becomes writable.
  - A command that edits a tracked file outside `commit_paths` fails the unit, and the failure names that path.
  - Run (driver): kill -9 the `run` process mid-command on a real run of a fixture plan with a 2-minute `sleep` command, then `uv run smart-mcps-orchestrate resume <run_id>` — Pass: `run.log` shows the child re-adopted (same pid), the group completes, and exactly one `sleep` process ever existed.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U9. recipe-docs — planners, deepeners and run drivers know how to declare and drive `run` units, proven by one live run

- **Summary**: The plan, deepen and run skills document `recipe`/`recipe_args` and the `[recipes] enabled` gate; `CONTEXT.md` gains the `run` recipe's terms; a live-tier test drives a real two-group run (a `run` unit feeding a `code` unit) end to end.
- **Goal**: `skills/orchestrator-plan/SKILL.md`: the unit template gains a `- **Recipe**:` line (`—` for `code`, or `run` with its args), the task map is written as v2 when any unit declares a recipe, and a unit whose real work is running a command is written as `run`, not as a coder unit with a `Run:` item. `skills/orchestrator-deepen/SKILL.md`: the sandbox sweep treats `run` units' commands against the Run Child profile (`allow_write`), and asks for wall-clock and cost figures (P5). `skills/orchestrator-run/SKILL.md`: preflight checks `[recipes] enabled`, triage of `runner` diagnoses, and what re-adoption looks like in `run.log`. `CONTEXT.md`: the Unit Recipe entry notes v1 ships `code` and `run`. `tests/test_run_recipe_live.py` builds a scratch repo, plans a `run` unit that writes a metrics JSON into a data dir and a downstream `code` unit, runs it with the real CLI, and asserts both groups complete, the code group's first prompt carried the run entry, and the bundle exports both entries.
- **Files**: `skills/orchestrator-plan/SKILL.md`, `skills/orchestrator-deepen/SKILL.md`, `skills/orchestrator-run/SKILL.md`, `CONTEXT.md`, `tests/test_run_recipe_live.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U2, U7, U8
- **Slice**: —
- **Implements / Consumes**: consumes `task-map-v2`
- **Verification**:
  - `uv run smart-mcps-orchestrate group docs/plans/2026-09-24-001-feat-unit-recipes-run-plan.md --no-spec` still succeeds after the skill edits (the skills changed, the v1 reader did not).
  - The plan skill's template, pasted into a scratch plan with one `run` unit and marked v2, passes `plan-check` and `group --no-spec` with `[recipes] enabled = ["run"]`.
  - Run (driver): `uv run pytest -m llm tests/test_run_recipe_live.py` — Pass: both groups COMPLETED, `artifacts.json` holds one `run` and one `code` entry, and the exported `ingest.json` carries both under `artifacts`.
- **Edge cases**: —
- **Non-goals / must-not**: —

## Requirement coverage

| requirement               | units                                                                                                    |
| ------------------------- | -------------------------------------------------------------------------------------------------------- |
| R1. recipe-field          | U2                                                                                                       |
| R2. recipe-registry       | U1                                                                                                       |
| R3. recipe-gate           | U3 (config), U4 (group time), U5 (run time)                                                              |
| R4. code-unchanged        | U4 (golden partitions), U5 (prompt byte-compare, live tier), U6 (prompt unchanged without run upstreams) |
| R5a. run-recipe           | U8                                                                                                       |
| R5, R6, R7                | next increment — not in this plan                                                                        |
| R8. executor-dispatch     | U5                                                                                                       |
| R9. shared-plumbing       | Plan A (landed) + U8 composes heartbeat/escalation/manifest directly                                     |
| R10. recipe-generations   | U1 (`handoff_prompt` slot; `code` unchanged, `run` has no session)                                       |
| R11. single-recipe-groups | U4                                                                                                       |
| R12. recipe-pricing       | U3                                                                                                       |
| R13. completion-contract  | U1 (contract per entry), U8 (`RunRecord` validated)                                                      |
| R14. recipe-review        | U1 (reviewer slot), U4 (`run` = self_verify), U8 (no reviewer)                                           |
| R15. recipe-merge         | U8                                                                                                       |
| R16. artifact-manifest    | U6                                                                                                       |
| R17. bundle-additive      | U3 (`SessionRole.RUNNER`), U7                                                                            |
| R18. repeat-then-human    | U1 (`extra="forbid"` contracts), U8 (no self-relaunch; `retry` = new attempt)                            |

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-recipe-registry
    description: New orchestrator/recipes package whose registry enumerates the code and run Unit Recipes with args, pricing, contract, prompts, merge policy and executor
    slice: null
    files:
      - orchestrator/recipes/__init__.py
      - orchestrator/recipes/registry.py
      - orchestrator/recipes/code.py
      - orchestrator/recipes/run.py
      - tests/test_recipe_registry.py
    size_hints:
      orchestrator/recipes/__init__.py: small
      orchestrator/recipes/registry.py: medium
      orchestrator/recipes/code.py: small
      orchestrator/recipes/run.py: medium
      tests/test_recipe_registry.py: medium
    symbols: []
    depends_on: []
    implements: ["UnitRecipe"]
    consumes: []
  - task_id: u2-recipe-field
    description: Task-map v2 accepts recipe and recipe_args validated by the recipe's args model while v1 maps parse exactly as today
    slice: null
    files:
      - orchestrator/grouping/plan_reader.py
      - orchestrator/grouping/graphing.py
      - docs/orchestrator-task-map.md
      - tests/test_plan_reader.py
    symbols: []
    depends_on: [u1-recipe-registry]
    implements: ["task-map-v2"]
    consumes: ["UnitRecipe"]
  - task_id: u3-recipe-pricing
    description: The estimator prices each task through its recipe and Group, SessionRole and config gain the recipe fields
    slice: null
    files:
      - orchestrator/grouping/estimator.py
      - orchestrator/grouping/trace.py
      - orchestrator/model.py
      - orchestrator/config.py
      - tests/test_estimator.py
    symbols: []
    depends_on: [u1-recipe-registry]
    implements: ["Group.recipe"]
    consumes: ["UnitRecipe"]
  - task_id: u4-single-recipe-groups
    description: The partitioner isolates every non-code unit as a fixed singleton group, refuses to mix recipes, and enforces the recipes gate at group time
    slice: null
    files:
      - orchestrator/grouping/partition.py
      - orchestrator/grouping/pipeline.py
      - tests/test_recipe_partition.py
    size_hints:
      tests/test_recipe_partition.py: medium
    symbols: []
    depends_on: [u2-recipe-field, u3-recipe-pricing]
    implements: []
    consumes: ["task-map-v2", "Group.recipe"]
  - task_id: u5-executor-dispatch
    description: make_executor dispatches on the group's recipe through execution/dispatch.py with the code path unchanged and the recipes gate rechecked at run time
    slice: null
    files:
      - orchestrator/execution/dispatch.py
      - orchestrator/execution/review.py
      - orchestrator/cli.py
      - tests/test_executor_dispatch.py
    size_hints:
      orchestrator/execution/dispatch.py: small
      tests/test_executor_dispatch.py: medium
    symbols: []
    depends_on: [u1-recipe-registry, u3-recipe-pricing]
    implements: ["executor-dispatch"]
    consumes: ["UnitRecipe", "Group.recipe"]
  - task_id: u6-artifact-manifest
    description: A run-level Artifact Manifest records code entries on merge and partial entries on Resolve, and injects non-code upstream entries into downstream first prompts
    slice: null
    files:
      - orchestrator/execution/artifacts.py
      - orchestrator/execution/manifest.py
      - orchestrator/execution/merge_ladder.py
      - orchestrator/execution/generation.py
      - orchestrator/execution/host.py
      - orchestrator/execution/scheduler.py
      - tests/test_artifact_manifest.py
    size_hints:
      orchestrator/execution/artifacts.py: medium
      tests/test_artifact_manifest.py: medium
    symbols: []
    depends_on: [u3-recipe-pricing]
    implements: ["ArtifactManifest"]
    consumes: ["Group.recipe"]
  - task_id: u7-bundle-additive
    description: The Run Bundle exports the Artifact Manifest and runner sessions as additive changes with schema_version still 2
    slice: null
    files:
      - orchestrator/execution/export.py
      - docs/run-bundle-contract.md
      - ui/src/types.ts
      - ui/src/attempts.ts
      - tests/test_export.py
    symbols: []
    depends_on: [u6-artifact-manifest]
    implements: []
    consumes: ["ArtifactManifest"]
  - task_id: u8-run-recipe
    description: The run executor launches declared commands as confined re-adoptable Run Children under a wall-clock cap, records measurements and entries, and triages failures once
    slice: null
    files:
      - orchestrator/execution/run_child.py
      - orchestrator/execution/run_executor.py
      - orchestrator/execution/confinement.py
      - orchestrator/prompts/run_triage.md
      - tests/test_run_child.py
      - tests/test_run_executor.py
    size_hints:
      orchestrator/execution/run_child.py: medium
      orchestrator/execution/run_executor.py: large
      orchestrator/prompts/run_triage.md: small
      tests/test_run_child.py: medium
      tests/test_run_executor.py: large
    symbols: []
    depends_on: [u1-recipe-registry, u5-executor-dispatch, u6-artifact-manifest]
    implements: []
    consumes: ["UnitRecipe", "executor-dispatch", "ArtifactManifest"]
  - task_id: u9-recipe-docs
    description: The plan, deepen and run skills and the glossary document run units, proven by a live two-group run
    slice: null
    files:
      - skills/orchestrator-plan/SKILL.md
      - skills/orchestrator-deepen/SKILL.md
      - skills/orchestrator-run/SKILL.md
      - CONTEXT.md
      - tests/test_run_recipe_live.py
    size_hints:
      tests/test_run_recipe_live.py: medium
    symbols: []
    depends_on: [u2-recipe-field, u7-bundle-additive, u8-run-recipe]
    implements: []
    consumes: ["task-map-v2"]
```
